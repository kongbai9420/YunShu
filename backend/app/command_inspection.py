"""命令巡检（4.4 P21）：经 SSH 登录设备执行模板里的只读命令，按规则判定结果。

- 巡检任务按单次（立即或指定时间）/ 每天 / 每周 / 每月定时；时间按平台时区（TZ，默认北京时间）。
- 每次执行（InspectionRun）为每台设备入队一个 Worker 任务；同一设备的命令在一个 SSH 会话里逐条执行，单条超时，
  一台设备整体最长 15 分钟。Linux / PVE 用 exec 通道（拿得到退出码）；华为 / H3C 交换机走交互式命令行，先关闭分页。
- 只用资产上用途为“监控”或“管理”的 SSH 端点与凭据；首次连接信任并固定主机密钥，与 SSH 采集一致。
- 执行前再按危险命令清单检查一次（模板可能被绕过页面改过），命中的不执行。
- 每条输出最多保存 64 KB；结果保留 90 天。
- 只有判定为“严重”的异常进告警：默认规则“巡检发现严重异常”（合成指标 inspection.critical = 每台设备
  各模板最近一次巡检里的严重异常条数），下一次巡检正常后恢复。
"""

from __future__ import annotations

import calendar
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app import audit, queue
from app.collect.base import AdapterError, EndpointContext
from app.crypto import CredentialCipher
from app.inspection import _local_tz
from app.inspection_templates import BUILTIN, NETWORK_PLATFORMS, PAGING_OFF, dangerous
from app.models import Asset, InspectionJob, InspectionResult, InspectionRun, InspectionTemplate, ManagementEndpoint, User

TASK = "inspection.asset"
INSPECTION_METRIC = "inspection.critical"
OUTPUT_LIMIT = 64 * 1024
ASSET_DEADLINE = 15 * 60
RETENTION_DAYS = 90
SCHEDULE_KINDS = ("once", "daily", "weekly", "monthly", "cron")
VERDICTS = ("ok", "abnormal", "unknown", "error")
_UNKNOWN = re.compile(r"(?i)command not found|no such file or directory|unrecognized command|% ?invalid input|error: (unrecognized|wrong parameter|too many)")


# ---- 判定 ----


def judge(item: dict[str, Any], output: str, exit_code: int | None) -> tuple[str, str, str]:
    """返回 (verdict, severity, detail)。severity 只在异常时有值。"""
    check = item.get("check") or {"type": "record"}
    ctype, sev = check.get("type", "record"), item.get("severity") or "warning"
    text = output.strip()
    if exit_code in (126, 127) or (_UNKNOWN.search(text[:2000]) and ctype != "record"):
        return "unknown", "", "命令不存在、无权执行或设备不认识这条命令"
    if check.get("ok_if") and re.search(check["ok_if"], output):
        return "ok", "", "符合“直接算正常”的条件"
    if ctype == "record":
        return "ok", "", "只记录输出"
    if ctype == "exit0":
        if exit_code is None:
            return "unknown", "", "拿不到退出码（网络设备不支持这种判定）"
        return ("ok", "", "退出码 0") if exit_code == 0 else ("abnormal", sev, f"退出码 {exit_code}")
    if ctype == "empty":
        if not text:
            return "ok", "", "输出为空"
        lines = text.splitlines()
        return "abnormal", sev, f"有 {len(lines)} 行输出：{lines[0][:200]}"
    pattern = check.get("pattern") or ""
    if ctype == "regex":
        return ("ok", "", "输出符合预期") if re.search(pattern, output, re.M) else ("abnormal", sev, "输出不符合预期：" + (text.splitlines() or [""])[0][:200])
    if ctype == "not_regex":
        m = re.search(pattern, output, re.M)
        if not m:
            return "ok", "", "没有发现异常关键字"
        line = next((ln for ln in output.splitlines() if re.search(pattern, ln)), m.group(0))
        return "abnormal", sev, f"发现“{m.group(0)}”：{line.strip()[:200]}"
    # threshold
    values: list[tuple[float, str]] = []
    for m in re.finditer(pattern, output, re.M):
        raw = next((g for g in m.groups() if g is not None), m.group(0)) if m.groups() else m.group(0)
        try:
            line = output[output.rfind("\n", 0, m.start()) + 1: (output.find("\n", m.end()) if output.find("\n", m.end()) >= 0 else len(output))]
            values.append((float(raw), line.strip()))
        except ValueError:
            continue
    if not values:
        return "unknown", "", "输出里没有找到要比较的数值"
    lte = check.get("op") == "lte"
    worse = (lambda v, t: v <= t) if lte else (lambda v, t: v >= t)
    crit, warn = check.get("critical"), check.get("warning")
    bad_c = [x for x in values if crit is not None and worse(x[0], crit)]
    bad_w = [x for x in values if warn is not None and worse(x[0], warn)]
    sign = "≤" if lte else "≥"
    if bad_c:
        return "abnormal", "critical", f"{len(bad_c)} 项{sign}{crit:g}：" + "；".join(x[1][:80] for x in bad_c[:5])
    if bad_w:
        return "abnormal", sev if sev == "info" else "warning", f"{len(bad_w)} 项{sign}{warn:g}：" + "；".join(x[1][:80] for x in bad_w[:5])
    edge = min(values)[0] if lte else max(values)[0]
    return "ok", "", f"{'最低' if lte else '最高'} {edge:g}，未达到阈值"


# ---- SSH 会话 ----


class Session_(Protocol):
    def run(self, command: str, timeout: float) -> tuple[str, int | None]: ...

    def close(self) -> None: ...


SessionFactory = Callable[[EndpointContext, str], Session_]


class ExecSession:
    """Linux / PVE：每条命令一个 exec 通道，拿退出码。"""

    def __init__(self, ctx: EndpointContext) -> None:
        from app.collect.ssh import ParamikoRunner

        self.runner = ParamikoRunner(ctx)

    def run(self, command: str, timeout: float) -> tuple[str, int | None]:
        chan = self.runner.transport.open_session(timeout=timeout)
        chan.settimeout(timeout)
        try:
            chan.exec_command(command)
            out = bytearray()
            while True:
                try:
                    data = chan.recv(65536)
                except TimeoutError as exc:
                    raise AdapterError("timeout", f"命令超过 {int(timeout)} 秒没有结束") from exc
                if not data:
                    break
                if len(out) < OUTPUT_LIMIT * 4:
                    out += data
            return out.decode("utf-8", "replace"), chan.recv_exit_status()
        finally:
            chan.close()

    def close(self) -> None:
        self.runner.close()


_PROMPT = re.compile(r"(?:^|\n)\s*[<\[][^\r\n<>\[\]]{1,80}[>\]]\s*$")
# Cisco IOS：主机名后跟 > 或 #（P21 第二批）
_CISCO_PROMPT = re.compile(r"(?:^|\n)[\w.\-/:()]{1,63}[>#]\s*$")
_MORE = re.compile(r"[ \t]*-{2,}[ \t]*More[ \t]*-{2,}[ \t]*")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class ShellSession:
    """华为 / H3C / Cisco：交互式命令行。读到提示符（<设备名>、[设备名]，Cisco 为 设备名> / 设备名#）算一条命令结束；
    出现分页提示时发空格继续。"""

    prompt = _PROMPT

    def __init__(self, ctx: EndpointContext, platform: str) -> None:
        from app.collect.ssh import ParamikoRunner

        self.prompt = _CISCO_PROMPT if platform == "cisco" else _PROMPT
        self.runner = ParamikoRunner(ctx)
        self.chan = self.runner.transport.open_session(timeout=ctx.timeout)
        self.chan.get_pty(width=511, height=200)
        self.chan.invoke_shell()
        self._read(ctx.timeout)  # 登录横幅与第一个提示符
        if platform in PAGING_OFF:
            self.run(PAGING_OFF[platform], ctx.timeout)

    def _read(self, timeout: float) -> str:
        buf, deadline = "", time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.chan.recv_ready():
                buf += self.chan.recv(65536).decode("utf-8", "replace")
                buf = _ANSI.sub("", buf)
                if _MORE.search(buf[-80:]):
                    buf = _MORE.sub("", buf)
                    self.chan.send(" ")
                if self.prompt.search(buf):
                    return buf
                if len(buf) > OUTPUT_LIMIT * 4:
                    raise AdapterError("protocol", "命令输出过大")
            elif self.chan.closed or self.chan.exit_status_ready():
                raise AdapterError("protocol", "设备关闭了命令行会话")
            else:
                time.sleep(0.05)
        raise AdapterError("timeout", f"命令超过 {int(timeout)} 秒没有结束")

    def run(self, command: str, timeout: float) -> tuple[str, int | None]:
        self.chan.send(command + "\n")
        text = self._read(timeout).replace("\r", "")
        lines = text.split("\n")
        if lines and command.strip() in lines[0]:
            lines = lines[1:]  # 设备回显的命令
        if lines and self.prompt.search("\n" + lines[-1]):
            lines = lines[:-1]
        return "\n".join(lines).strip("\n"), None

    def close(self) -> None:
        try:
            self.chan.close()
        finally:
            self.runner.close()


def default_session(ctx: EndpointContext, platform: str) -> Session_:
    return ShellSession(ctx, platform) if platform in NETWORK_PLATFORMS else ExecSession(ctx)


# ---- 执行一台设备 ----


def ssh_endpoint(asset: Asset) -> ManagementEndpoint | None:
    eps = [e for e in asset.endpoints if e.protocol == "ssh" and e.purpose in ("monitoring", "management") and e.credential_id]
    eps.sort(key=lambda e: 0 if e.purpose == "monitoring" else 1)
    return eps[0] if eps else None


_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(address: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(address, threading.Lock())


def _result(run: InspectionRun, asset: Asset, item: dict[str, Any] | None, verdict: str, *, output: str = "", exit_code: int | None = None,
            severity: str = "", detail: str = "", now: datetime) -> InspectionResult:
    return InspectionResult(run_id=run.id, asset_id=asset.id, asset_name=asset.name, item_key=(item or {}).get("key", ""),
                            item_name=(item or {}).get("name", "连接设备"), command=(item or {}).get("command", "")[:1000],
                            output=output[:OUTPUT_LIMIT], exit_code=exit_code, verdict=verdict, severity=severity, detail=detail[:1000], observed_at=now)


def run_asset(db: Session, cipher: CredentialCipher, run: InspectionRun, asset: Asset, *, factory: SessionFactory = default_session,
              clock: Callable[[], float] = time.monotonic) -> list[InspectionResult]:
    from app.collect.runner import _context

    now = datetime.now(UTC)
    ep = ssh_endpoint(asset)
    if ep is None:
        return [_result(run, asset, None, "error", detail="资产没有用途为“监控”或“管理”、并关联了凭据的 SSH 端点", now=now)]
    try:
        ctx = _context(db, cipher, ep, "ssh")
    except AdapterError as exc:
        return [_result(run, asset, None, "error", detail=f"读取凭据失败：{exc}", now=now)]
    out: list[InspectionResult] = []
    with _lock(ep.address):
        try:
            session = factory(ctx, run.platform)
        except AdapterError as exc:
            return [_result(run, asset, None, "error", detail=f"连接失败：{exc}", now=now)]
        if ctx.trust.observed_ssh and not ep.ssh_host_key:
            ep.ssh_host_key, ep.trust_pinned_at = ctx.trust.observed_ssh, now
        start = clock()
        try:
            for item in run.items:
                now = datetime.now(UTC)
                if why := dangerous(item.get("command", ""), run.platform):
                    out.append(_result(run, asset, item, "error", detail=f"命令含危险操作（{why}），没有执行", now=now))
                    continue
                left = ASSET_DEADLINE - (clock() - start)
                if left <= 0:
                    out.append(_result(run, asset, item, "error", detail="这台设备的巡检超过 15 分钟，剩下的检查项没有执行", now=now))
                    continue
                try:
                    text, code = session.run(item["command"], min(float(item.get("timeout") or 30), left))
                except AdapterError as exc:
                    out.append(_result(run, asset, item, "error", detail=str(exc), now=now))
                    if exc.kind != "timeout":
                        break  # 会话断了：后面的也执行不了
                    continue
                verdict, severity, detail = judge(item, text, code)
                out.append(_result(run, asset, item, verdict, output=text, exit_code=code, severity=severity, detail=detail, now=now))
        finally:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass
    done = {r.item_key for r in out}
    for item in run.items:
        if item["key"] not in done:
            out.append(_result(run, asset, item, "error", detail="会话中断，没有执行", now=datetime.now(UTC)))
    return out


def _finish_asset(db: Session, run_id: uuid.UUID, results: list[InspectionResult], now: datetime) -> None:
    for r in results:
        db.add(r)
    db.flush()
    # 原子地加一，多 Worker（PostgreSQL）并发完成时不丢计数
    db.execute(update(InspectionRun).where(InspectionRun.id == run_id).values(assets_done=InspectionRun.assets_done + 1))
    run = db.get(InspectionRun, run_id)
    db.refresh(run)
    if run.assets_done >= run.asset_total and run.status != "done":
        rows = db.execute(select(InspectionResult.verdict, InspectionResult.severity).where(InspectionResult.run_id == run_id)).all()
        counts = {v: 0 for v in VERDICTS} | {s: 0 for s in ("critical", "warning", "info")}
        for verdict, severity in rows:
            counts[verdict] = counts.get(verdict, 0) + 1
            if verdict == "abnormal" and severity:
                counts[severity] = counts.get(severity, 0) + 1
        run.status, run.finished_at, run.counts = "done", now, counts


def make_handler(cipher_factory: Callable[[], CredentialCipher], factory: SessionFactory | None = None):  # type: ignore[no-untyped-def]
    def handle(ctx) -> dict[str, Any]:  # type: ignore[no-untyped-def]
        from sqlalchemy.orm import selectinload

        run_id, asset_id = uuid.UUID(ctx.lease.payload["run_id"]), uuid.UUID(ctx.lease.payload["asset_id"])
        with ctx.session_factory() as db:
            run = db.get(InspectionRun, run_id)
            if run is None:
                return {"skipped": True}
            if db.scalar(select(InspectionResult.id).where(InspectionResult.run_id == run_id, InspectionResult.asset_id == asset_id).limit(1)):
                return {"skipped": True}  # 任务重试：这台已经有结果
            asset = db.get(Asset, asset_id, options=[selectinload(Asset.endpoints)])
            if asset is None:
                _finish_asset(db, run_id, [], datetime.now(UTC))
                db.commit()
                return {"skipped": True}
            results = run_asset(db, cipher_factory(), run, asset, factory=factory or default_session)
            _finish_asset(db, run_id, results, datetime.now(UTC))
            db.commit()
            return {"results": len(results), "abnormal": sum(r.verdict == "abnormal" for r in results)}

    return handle


# ---- 发起执行与定时 ----


def start_run(db: Session, template: InspectionTemplate, assets: list[Asset], *, by: str, job: InspectionJob | None = None,
              actor: User | None = None, now: datetime | None = None) -> InspectionRun:
    now = now or datetime.now(UTC)
    run = InspectionRun(job_id=job.id if job else None, template_id=template.id, name=job.name if job else f"立即巡检：{template.name}",
                        template_name=template.name, items=list(template.items), platform=template.platform, triggered_by=by,
                        created_at=now, status="running", asset_total=len(assets), assets_done=0, counts={})
    db.add(run)
    db.flush()
    for a in assets:
        queue.enqueue(db, TASK, asset_id=a.id, payload={"run_id": str(run.id), "asset_id": str(a.id)},
                      idempotency_key=f"inspect:{run.id}:{a.id}", max_attempts=1,
                      priority=queue.BACKGROUND_PRIORITY)
    if not assets:
        run.status, run.finished_at = "done", now
    audit.record(db, actor, "inspection.run", target_type="inspection_run", target_id=run.id,
                 summary=f"{'定时' if actor is None else '手动'}执行巡检“{run.name}”（模板“{template.name}”，{len(assets)} 台）")
    return run


def _at(day: date, hhmm: str, tz) -> datetime:  # type: ignore[no-untyped-def]
    h, m = (int(x) for x in (hhmm or "08:00").split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=tz).astimezone(UTC)


_CRON_FIELDS = (("分钟", 0, 59), ("小时", 0, 23), ("日", 1, 31), ("月", 1, 12), ("星期", 0, 7))


def parse_cron(expr: str) -> list[set[int]]:
    """标准 5 段 cron（分 时 日 月 星期），支持 *、列表、范围与步长；星期 0 和 7 都是周日。格式不对抛 ValueError。"""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError("cron 表达式要有 5 段：分 时 日 月 星期，例如 30 8 * * 1-5")
    out: list[set[int]] = []
    for text, (label, lo, hi) in zip(parts, _CRON_FIELDS):
        values: set[int] = set()
        for piece in text.split(","):
            m = re.fullmatch(r"(\*|\d+(?:-\d+)?)(?:/(\d+))?", piece)
            if not m:
                raise ValueError(f"cron 的“{label}”段写法不对：{piece}")
            rng, step = m.group(1), int(m.group(2) or 1)
            start, end = (lo, hi) if rng == "*" else (int(rng.split("-")[0]), int(rng.split("-")[-1]))
            if not (lo <= start <= end <= hi) or step < 1:
                raise ValueError(f"cron 的“{label}”段超出范围（{lo}～{hi}）：{piece}")
            values.update(range(start, end + 1, step))
        out.append(values)
    if 7 in out[4]:
        out[4] = (out[4] - {7}) | {0}
    return out


def _cron_next(expr: str, after: datetime, tz) -> datetime | None:  # type: ignore[no-untyped-def]
    minutes, hours, days, months, weekdays = parse_cron(expr)
    parts = expr.split()
    dom_any, dow_any = parts[2] == "*", parts[4] == "*"
    local = after.astimezone(tz)
    for offset in range(0, 400):
        day = (local + timedelta(days=offset)).date()
        if day.month not in months:
            continue
        dow = (day.weekday() + 1) % 7  # cron：0 = 周日
        dom_ok, dow_ok = day.day in days, dow in weekdays
        # 与标准 cron 一致：日和星期都限定时满足其一即可
        if not ((dom_ok or dow_ok) if not dom_any and not dow_any else (dom_ok and dow_ok)):
            continue
        for h in sorted(hours):
            for mi in sorted(minutes):
                when = datetime(day.year, day.month, day.day, h, mi, tzinfo=tz).astimezone(UTC)
                if when > after:
                    return when
    return None


def next_run(schedule: dict[str, Any], after: datetime) -> datetime | None:
    """after 之后的下一次执行时间；单次执行过了返回 None。"""
    kind, tz = schedule.get("kind"), _local_tz()
    if kind == "cron":
        return _cron_next(str(schedule.get("expr") or ""), after, tz)
    if kind == "once":
        at = schedule.get("at")
        if not at:
            return None
        when = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        when = when if when.tzinfo else when.replace(tzinfo=tz)
        return when.astimezone(UTC) if when > after else None
    local = after.astimezone(tz).date()
    for offset in range(0, 400):
        day = local + timedelta(days=offset)
        if kind == "weekly" and day.weekday() not in (schedule.get("weekdays") or [0]):
            continue
        if kind == "monthly":
            want = int(schedule.get("day") or 1)
            if day.day != min(want, calendar.monthrange(day.year, day.month)[1]):
                continue
        when = _at(day, schedule.get("time") or "08:00", tz)
        if when > after:
            return when
    return None


def validate_schedule(raw: dict[str, Any]) -> dict[str, Any]:
    kind = raw.get("kind")
    if kind not in SCHEDULE_KINDS:
        raise ValueError("执行方式只能是单次、每天、每周、每月或 cron 表达式")
    out: dict[str, Any] = {"kind": kind}
    if kind == "cron":
        expr = " ".join(str(raw.get("expr") or "").split())
        parse_cron(expr)
        out["expr"] = expr
        return out
    if kind == "once":
        if raw.get("at"):
            try:
                datetime.fromisoformat(str(raw["at"]).replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("指定的执行时间格式不正确") from exc
            out["at"] = str(raw["at"])
        return out
    time_text = str(raw.get("time") or "08:00")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time_text):
        raise ValueError("执行时间要写成 HH:MM，例如 08:30")
    out["time"] = time_text
    if kind == "weekly":
        days = sorted({int(d) for d in raw.get("weekdays") or [] if str(d).isdigit() and 0 <= int(d) <= 6})
        if not days:
            raise ValueError("每周执行至少选一天")
        out["weekdays"] = days
    if kind == "monthly":
        day = int(raw.get("day") or 1)
        if not 1 <= day <= 31:
            raise ValueError("每月执行的日期要在 1 到 31 之间（当月没有这一天时在最后一天执行）")
        out["day"] = day
    return out


def job_assets(db: Session, job: InspectionJob) -> list[Asset]:
    ids = [uuid.UUID(x) for x in job.asset_ids]
    return list(db.scalars(select(Asset).where(Asset.id.in_(ids), Asset.lifecycle == "active").order_by(Asset.name))) if ids else []


def enqueue_due(db: Session, *, now: datetime | None = None) -> int:
    """到时间的巡检任务各执行一次；单次任务执行后停用。"""
    now = now or datetime.now(UTC)
    n = 0
    for job in db.scalars(select(InspectionJob).where(InspectionJob.enabled.is_(True), InspectionJob.deleted.is_(False),
                                                      InspectionJob.next_run_at.is_not(None), InspectionJob.next_run_at <= now)):
        template = db.get(InspectionTemplate, job.template_id)
        if template is not None and not template.deleted:
            start_run(db, template, job_assets(db, job), by="定时巡检", job=job, now=now)
            n += 1
        job.last_run_at = now
        job.next_run_at = next_run(job.schedule, now)
        if job.next_run_at is None:
            job.enabled = False
    return n


def prune(db: Session, *, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    res = db.execute(delete(InspectionRun).where(InspectionRun.created_at < now - timedelta(days=RETENTION_DAYS)),
                     execution_options={"synchronize_session": False})
    db.execute(delete(InspectionResult).where(InspectionResult.observed_at < now - timedelta(days=RETENTION_DAYS)),
               execution_options={"synchronize_session": False})
    return res.rowcount or 0


def ensure_builtin(db: Session) -> None:
    """写入或更新内置模板（平台维护，按键覆盖）。"""
    for t in BUILTIN:
        row = db.scalar(select(InspectionTemplate).where(InspectionTemplate.builtin_key == t["key"]))
        if row is None:
            db.add(InspectionTemplate(builtin_key=t["key"], name=t["name"], platform=t["platform"], description=t["description"],
                                      items=t["items"], updated_by="平台内置"))
        elif row.items != t["items"] or row.description != t["description"] or row.name != t["name"]:
            row.items, row.description, row.name, row.revision = t["items"], t["description"], t["name"], row.revision + 1
    db.flush()


# ---- 告警 ----


@dataclass
class Finding:
    count: int
    at: datetime
    names: list[str]


def critical_findings(db: Session, *, days: int = 35) -> dict[uuid.UUID, Finding]:
    """每台设备、每个模板最近一次完成的巡检里，判定为严重的异常。"""
    since = datetime.now(UTC) - timedelta(days=days)
    rows = db.execute(select(InspectionRun.id, InspectionRun.template_id, InspectionRun.created_at, InspectionResult.asset_id)
                      .join(InspectionResult, InspectionResult.run_id == InspectionRun.id)
                      .where(InspectionRun.status == "done", InspectionRun.created_at >= since)
                      .order_by(InspectionRun.created_at.desc()).distinct()).all()
    latest: dict[tuple[uuid.UUID, Any], uuid.UUID] = {}
    for run_id, template_id, _, asset_id in rows:
        latest.setdefault((asset_id, template_id), run_id)
    if not latest:
        return {}
    out: dict[uuid.UUID, Finding] = {}
    crit = db.execute(select(InspectionResult.asset_id, InspectionResult.run_id, InspectionResult.item_name, InspectionResult.observed_at)
                      .where(InspectionResult.run_id.in_(set(latest.values())), InspectionResult.verdict == "abnormal",
                             InspectionResult.severity == "critical")).all()
    wanted = {(a, r) for (a, _), r in latest.items()}
    for asset_id, run_id, name, at in crit:
        if (asset_id, run_id) not in wanted:
            continue
        at = at if at.tzinfo else at.replace(tzinfo=UTC)
        f = out.setdefault(asset_id, Finding(0, at, []))
        f.count += 1
        f.at = max(f.at, at)
        f.names.append(name)
    return out
