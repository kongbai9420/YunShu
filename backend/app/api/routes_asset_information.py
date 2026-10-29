"""新增前或编辑时读取设备资料，仅返回待用户选择的候选值，不修改资产。"""
from __future__ import annotations

import ipaddress
import re
import uuid
from contextlib import suppress
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy.orm import Session

from app import audit
from app.api.deps import editor, get_db
from app.collect import build_adapters
from app.collect.base import AdapterError, EndpointContext, Trust
from app.crypto import DecryptionError
from app.models import Credential, ManagementEndpoint, User
from app.schemas import AssetEndpointIn
from app.services.credentials import reveal_secret

router = APIRouter(prefix="/api/asset-information", tags=["asset-information"])
FIELDS = {"name": ("hostname", "sys_name"), "platform": ("os_id", "platform"),
          "vendor": ("vendor",), "model": ("model",), "serial_no": ("serial", "serial_no")}
ERRORS = {"auth": "设备认证失败，请检查用户名、密码及账户权限。", "timeout": "连接或读取超时，请检查地址、端口和网络。",
          "unreachable": "无法连接设备，请检查地址、端口和网络。", "unconfigured": "请填写或关联适用于所选协议的登录凭据。",
          "unsupported": "此设备未提供该协议所需的只读信息。", "untrusted": "设备指纹与已固定值不一致，请先在管理端点核对并重新信任。"}


class InformationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: AssetEndpointIn | None = None
    endpoint_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def choice(self):
        if (self.endpoint is None) == (self.endpoint_id is None):
            raise ValueError("请选择已有管理端点，或填写待添加的连接信息")
        if self.endpoint:
            address = self.endpoint.address
            try:
                ipaddress.ip_address(address)
            except ValueError:
                if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252}[A-Za-z0-9])?", address):
                    raise ValueError("设备地址只能填写 IP 或主机名，不含协议、路径或参数") from None
        return self


class InformationOut(BaseModel):
    protocol: str
    fields: dict[Literal["name", "platform", "vendor", "model", "serial_no"], str]
    tls_fingerprint: str = ""
    ssh_host_key: str = ""
    message: str


def candidate_fields(facts: dict, secret: str) -> dict[str, str]:
    fields = {}
    for field, keys in FIELDS.items():
        for key in keys:
            value = facts.get(key)
            if not isinstance(value, str):
                continue
            value = value.strip().replace("\x00", "")
            if not value or value.lower() in {"unknown", "none", "n/a", "not specified", "to be filled by o.e.m.", "default string"}:
                continue
            # 设备或适配器意外回显凭据时，也不能进入候选结果。
            if secret and secret in value:
                continue
            maximum = 200 if field == "name" else 64 if field == "platform" else 100
            if len(value) <= maximum:
                fields[field] = value
                break
    return fields


@router.post("/preview", response_model=InformationOut)
def preview(body: InformationIn, request: Request, actor: User = Depends(editor), db: Session = Depends(get_db)) -> dict:
    from app.scope import can_see, credential_visible, scope_for

    scope = scope_for(db, actor)
    ep = None
    if body.endpoint_id:
        ep = db.get(ManagementEndpoint, body.endpoint_id)
        if ep is None or not can_see(db, scope, ep.asset_id):  # 范围外当作不存在（4.1 P11）
            raise HTTPException(404, "管理端点不存在")
        source = ep
    else:
        source = body.endpoint
    assert source is not None
    if source.protocol not in {"ssh", "snmp", "ipmi", "redfish", "pve-api", "esxi-api"}:
        raise HTTPException(422, "所选协议暂不支持获取资产信息，请选择 SSH、SNMP、IPMI、Redfish、PVE API 或 ESXi API")
    username = secret = kind = ""
    if body.endpoint and body.endpoint.new_credential:
        cred = body.endpoint.new_credential
        username, secret, kind = cred.username, cred.secret, cred.kind
    elif source.credential_id:
        cred = db.get(Credential, source.credential_id)
        # 不能借用范围外资产的凭据去连接任意地址
        if cred is None or not credential_visible(db, scope, cred.id):
            raise HTTPException(422, "凭据不存在，请重新选择")
        try:
            secret = reveal_secret(cred, request.app.state.cipher)
        except DecryptionError:
            raise HTTPException(422, "凭据无法解密，请重新填写或更换凭据") from None
        username, kind = cred.username, cred.kind
    allowed = {"ssh": {"password", "ssh_key"}, "snmp": {"snmp_community", "snmpv3"}, "pve-api": {"password", "token"}}
    if not secret or kind not in allowed.get(source.protocol, {"password"}) or (kind != "snmp_community" and not username):
        raise HTTPException(422, ERRORS["unconfigured"])
    ctx = EndpointContext(source.address, source.port, source.protocol, username=username, secret=secret, credential_kind=kind,
                          tls_verify=source.tls_verify, trust=Trust(tls_fingerprint=source.tls_fingerprint, ssh_host_key=source.ssh_host_key))
    adapters = getattr(request.app.state, "information_adapters", None) or build_adapters()
    adapter = adapters[source.protocol]
    from app import bmc_gate

    try:
        # 4.4 P20b：与采集、控制共用每台 BMC 的队列，最多等 30 秒
        with bmc_gate.hold(request.app.state.engine, [source.address] if source.protocol in bmc_gate.BMC_PROTOCOLS else [], "probe",
                           wait=bmc_gate.PROBE_WAIT):
            bmc_gate.allow(request.app.state.engine, source.address, source.protocol, "probe")
            result = getattr(adapter, "inventory", adapter.probe)(ctx)
        if not result.ok:
            raise AdapterError("protocol", "读取失败")
    except bmc_gate.BmcBusy as exc:
        raise HTTPException(409, f"{exc}，请稍后再试") from None
    except AdapterError as exc:
        raise HTTPException(422, ERRORS.get(exc.kind, "设备信息读取失败，请检查协议和只读权限后重试。")) from None
    except Exception:
        raise HTTPException(422, "设备信息读取失败，请检查协议和只读权限后重试。") from None
    finally:
        if source.protocol == "redfish":
            with suppress(Exception):
                adapter.logout(ctx)
    fields = candidate_fields(result.facts, secret)
    audit.record(db, actor, "asset.information_preview", target_type="asset" if ep else "asset_draft",
                 target_id=ep.asset_id if ep else None, summary="只读获取设备信息供手动选择",
                 detail={"protocol": source.protocol, "fields": sorted(fields)})
    return {"protocol": source.protocol, "fields": fields, "tls_fingerprint": ctx.trust.observed_tls,
            "ssh_host_key": ctx.trust.observed_ssh,
            "message": "已获取设备信息，请选择要带入表单的字段。" if fields else "连接成功，但设备没有返回可填入资产的资料。可手动填写或换用其他已开放协议。"}
