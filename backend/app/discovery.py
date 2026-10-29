"""受限的 IPv4 TCP / SNMP 只读发现。网络调用在事务外，写入前校验租约与任务代数。

一批最多 16 个地址，避免发现任务占满 Worker；暂停、取消后最多等待当前有界探测结束。
TCP 端口开放仅作为候选证据，不推断设备类型，不自动创建或合并资产。
"""
from __future__ import annotations

import ipaddress
import socket
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Callable

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from app import queue
from app.collect.base import AdapterError, EndpointContext
from app.collect.snmp import SnmpAdapter
from app.crypto import CredentialCipher, DecryptionError
from app.models import Credential, DiscoveryObservation, DiscoveryRun, Task
from app.services.credentials import reveal_secret
from app.worker import TaskContext, TaskError

MAX_ADDRESSES = 256
BATCH_SIZE = 16


def expand_scope(scope: list[str], excludes: list[str]) -> list[str]:
    def network(value: str) -> ipaddress.IPv4Network:
        try:
            net = ipaddress.ip_network(value.strip(), strict=False)
        except ValueError as exc:
            raise ValueError("范围只支持 IPv4 地址或 CIDR 网段") from exc
        if not isinstance(net, ipaddress.IPv4Network) or net.is_multicast or net.is_unspecified:
            raise ValueError("范围只支持单播 IPv4 地址或 CIDR 网段")
        return net

    nets = [network(v) for v in scope]
    omitted = [network(v) for v in excludes]
    # 先限制授权范围大小，再展开；不遍历巨大的网段，即使有排除规则也不能绕过。
    if any(n.num_addresses > MAX_ADDRESSES for n in nets):
        raise ValueError("每次发现最多 256 个地址，请拆分网段")
    result = {a for n in nets for a in n.hosts() if not any(a in e for e in omitted)}
    if not result or len(result) > MAX_ADDRESSES:
        raise ValueError("排除后范围为空或超过 256 个地址")
    return [str(a) for a in sorted(result)]


def enqueue_batch(db: Session, run: DiscoveryRun) -> Task:
    return queue.enqueue(
        db, "discovery.batch", payload={"run_id": str(run.id), "generation": run.generation},
        idempotency_key=f"discovery:{run.id}:{run.generation}:{run.processed}", max_attempts=3,
    )


def probe_address(
    address: str, protocol: str, ports: list[int], timeout: float,
    credential: tuple[str, str, str] | None, *, connect: Callable = socket.create_connection,
    snmp_factory: Callable = SnmpAdapter,
) -> dict | None:
    if protocol == "snmp":
        if credential is None:
            raise AdapterError("unconfigured", "发现凭据不存在或无法解密，请重新选择凭据后重试")
        username, secret, kind = credential
        result = snmp_factory().probe(EndpointContext(address, ports[0], "snmp", username, secret, kind, timeout=timeout))
        if not result.ok:
            return None
        # 仅保存定义过的身份字段，不把适配器的任意响应或凭据带入观察记录。
        facts = {k: str(v)[:500] for k, v in result.facts.items() if k in ("sys_name", "sys_descr") and v is not None}
        return {"address": address, "ports": ports, "facts": facts}
    opened = []
    for port in ports:
        try:
            conn = connect((address, port), timeout=timeout)
            conn.close()
            opened.append(port)
        except OSError:
            pass
    return {"address": address, "ports": opened, "facts": {}} if opened else None


def make_handler(cipher_factory: Callable[[], CredentialCipher], probe: Callable = probe_address):
    def handle(ctx: TaskContext) -> dict:
        run_id = uuid.UUID(ctx.lease.payload["run_id"])
        generation = ctx.lease.payload["generation"]
        with ctx.session_factory() as db:
            run = db.get(DiscoveryRun, run_id)
            if run is None or run.generation != generation or run.state not in ("queued", "running"):
                return {"skipped": True}
            cursor = run.processed
            addresses = run.addresses[cursor:cursor + min(BATCH_SIZE, run.concurrency * 4)]
            protocol, ports, timeout, concurrency = run.protocol, run.ports, run.timeout, run.concurrency
            credential = None
            if protocol == "snmp":
                cred = db.get(Credential, run.credential_id) if run.credential_id else None
                if cred is None:
                    raise TaskError("SNMP 发现凭据已删除，请重新创建发现任务", retryable=False)
                try:
                    credential = (cred.username, reveal_secret(cred, cipher_factory()), cred.kind)
                except DecryptionError as exc:
                    raise TaskError("SNMP 发现凭据无法解密，请重新录入凭据后继续", retryable=False) from exc
            owned = exists(select(Task.id).where(
                Task.id == ctx.lease.task_id, Task.lease_token == ctx.lease.token,
                Task.status == "running", Task.lease_expires_at > datetime.now(UTC),
            ))
            changed = db.execute(update(DiscoveryRun).where(
                DiscoveryRun.id == run_id, DiscoveryRun.generation == generation,
                DiscoveryRun.state.in_(("queued", "running")), owned,
            ).values(state="running"))
            if changed.rowcount != 1:
                return {"skipped": True}
            db.commit()

        def one(address: str) -> tuple[dict | None, str]:
            # 在线程真正开始时再检查，暂停后的批内等待任务不会开始网络探测。
            with ctx.session_factory() as db:
                current = db.get(DiscoveryRun, run_id)
                task = db.get(Task, ctx.lease.task_id)
                expires = task.lease_expires_at if task else None
                if expires and not expires.tzinfo:
                    expires = expires.replace(tzinfo=UTC)
                if (current is None or current.generation != generation or current.state not in ("queued", "running")
                        or task is None or task.lease_token != ctx.lease.token or task.status != "running"
                        or expires is None or expires <= datetime.now(UTC)):
                    return None, ""
            try:
                return probe(address, protocol, ports, timeout, credential), ""
            except AdapterError as exc:
                return None, f"SNMP 探测未成功（{exc.kind}）；未响应不代表设备离线"
            except Exception:
                return None, "探测未成功；请检查协议、凭据与网络范围"

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            results = list(pool.map(one, addresses))
        now = datetime.now(UTC)
        with ctx.session_factory() as db:
            # UPDATE 先取得写锁，并在同一事务内守住暂停/取消、租约和恢复后新代数的边界。
            owned = exists(select(Task.id).where(
                Task.id == ctx.lease.task_id, Task.lease_token == ctx.lease.token,
                Task.status == "running", Task.lease_expires_at > now,
            ))
            changed = db.execute(update(DiscoveryRun).where(
                DiscoveryRun.id == run_id, DiscoveryRun.generation == generation,
                DiscoveryRun.processed == cursor, DiscoveryRun.state.in_(("queued", "running")), owned,
            ).values(state="running", processed=cursor + len(addresses)))
            if changed.rowcount != 1:
                db.rollback()
                return {"skipped": True}
            run = db.get(DiscoveryRun, run_id)
            found = [r for r, _ in results if r is not None]
            for result in found:
                db.add(DiscoveryObservation(run_id=run_id, **result))
            run.responded += len(found)
            errors = [e for _, e in results if e]
            if errors:
                run.error = errors[-1]
            if run.processed >= len(run.addresses):
                run.state = "completed"
            else:
                enqueue_batch(db, run)
            db.commit()
        return {"processed": len(addresses), "responded": len(found)}

    return handle
