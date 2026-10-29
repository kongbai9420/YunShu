"""API 请求与响应模型。凭据响应只含元数据，没有任何密文或明文字段。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.auth import MIN_PASSWORD_LENGTH

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)]
Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
Password = Annotated[str, StringConstraints(min_length=MIN_PASSWORD_LENGTH, max_length=256)]

Form = Literal["physical", "vm", "container"]
AssetRole = Literal["server", "switch", "router", "ap", "firewall", "nas", "storage", "ups", "other"]
Lifecycle = Literal["planned", "active", "maintenance", "retired"]
Protocol = Literal["ipmi", "redfish", "snmp", "ssh", "https", "http", "pve-api", "esxi-api", "winrm", "other"]
Purpose = Literal["management", "monitoring", "control"]
RelationKind = Literal["hosts", "installed_in", "belongs_to", "depends_on"]
SiteKind = Literal["site", "home", "datacenter", "room"]
UserRole = Literal["admin", "operator", "viewer"]
CredentialKind = Literal["password", "snmp_community", "snmpv3", "ssh_key", "token"]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---- 认证与用户 ----


class LoginIn(BaseModel):
    username: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    password: Annotated[str, StringConstraints(min_length=1, max_length=256)]


class UserOut(ORM):
    id: uuid.UUID
    username: str
    display_name: str
    role: str
    is_active: bool
    require_password_change: bool
    last_login_at: datetime | None
    # 资产范围（4.1 P11）：admin 始终全部可见，与此字段无关
    scope_all: bool = True
    # 4.2 P16：local 本地账号 / oidc 单点登录账号（不能改密码）
    auth_source: str = "local"


class LoginOut(BaseModel):
    user: UserOut
    csrf_token: str


class PasswordChangeIn(BaseModel):
    old_password: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    new_password: Password


class UserCreate(BaseModel):
    username: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.@-]+$")]
    password: Password
    role: UserRole
    display_name: Short = ""


class UserUpdate(BaseModel):
    display_name: Short | None = None
    role: UserRole | None = None
    is_active: bool | None = None
    password: Password | None = None


# ---- 场所与机柜 ----


class SiteIn(BaseModel):
    name: Name
    kind: SiteKind = "site"
    parent_id: uuid.UUID | None = None
    notes: str = ""


class SiteUpdate(BaseModel):
    name: Name | None = None
    kind: SiteKind | None = None
    parent_id: uuid.UUID | None = None
    notes: str | None = None


class SiteOut(ORM):
    id: uuid.UUID
    name: str
    kind: str
    parent_id: uuid.UUID | None
    notes: str


class RackIn(BaseModel):
    site_id: uuid.UUID
    name: Name
    height_u: int = Field(42, ge=1, le=60)


class RackUpdate(BaseModel):
    name: Name | None = None
    height_u: int | None = Field(None, ge=1, le=60)


class RackOut(ORM):
    id: uuid.UUID
    site_id: uuid.UUID
    name: str
    height_u: int


# ---- 凭据 ----


class CredentialIn(BaseModel):
    alias: Name
    kind: CredentialKind
    username: Short = ""
    secret: Annotated[str, StringConstraints(min_length=1, max_length=8192)]
    notes: str = ""


class CredentialUpdate(BaseModel):
    alias: Name | None = None
    username: Short | None = None
    secret: Annotated[str, StringConstraints(min_length=1, max_length=8192)] | None = None
    notes: str | None = None


class CredentialOut(ORM):
    id: uuid.UUID
    alias: str
    kind: str
    username: str
    notes: str
    key_id: str
    last_used_at: datetime | None
    updated_at: datetime


# ---- 资产 ----


class EndpointIn(BaseModel):
    address: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
    port: int | None = Field(None, ge=1, le=65535)
    protocol: Protocol
    purpose: Purpose = "management"
    credential_id: uuid.UUID | None = None
    tls_verify: bool = True
    notes: str = ""
    collect_packages: bool = False


class EndpointUpdate(BaseModel):
    address: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)] | None = None
    port: int | None = Field(None, ge=1, le=65535)
    protocol: Protocol | None = None
    purpose: Purpose | None = None
    credential_id: uuid.UUID | None = None
    tls_verify: bool | None = None
    notes: str | None = None
    collect_packages: bool | None = None


class EndpointOut(ORM):
    id: uuid.UUID
    asset_id: uuid.UUID
    address: str
    port: int | None
    protocol: str
    purpose: str
    credential_id: uuid.UUID | None
    tls_verify: bool
    notes: str
    collect_packages: bool = False
    tls_fingerprint: str = ""
    ssh_host_key: str = ""
    trust_pinned_at: datetime | None = None


class AssetBase(BaseModel):
    asset_no: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None = None
    name: Name
    form: Form
    role: AssetRole
    platform: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)] = ""
    vendor: Short = ""
    model: Short = ""
    serial_no: Short = ""
    collection_profile: Short = ""
    lifecycle: Lifecycle = "active"
    # 维护中的预计结束时间（带时区）；不填时改为维护中默认 7 天后
    maintenance_until: datetime | None = None
    site_id: uuid.UUID | None = None
    rack_id: uuid.UUID | None = None
    rack_unit: int | None = Field(None, ge=1, le=60)
    device_type_id: uuid.UUID | None = None
    rack_face: Literal["front", "rear"] = "front"
    tags: list[Tag] = Field(default_factory=list, max_length=50)
    notes: str = ""


class AssetEndpointIn(EndpointIn):
    new_credential: CredentialIn | None = None
    tls_fingerprint: Annotated[str, StringConstraints(max_length=95)] = ""
    ssh_host_key: Annotated[str, StringConstraints(max_length=1500)] = ""

    @model_validator(mode="after")
    def credential_choice(self) -> AssetEndpointIn:
        if self.new_credential is not None and self.credential_id is not None:
            raise ValueError("已有凭据和新建凭据只能选择一种")
        cred = self.new_credential
        if cred is not None:
            allowed = {"ssh": {"password", "ssh_key"}, "snmp": {"snmp_community", "snmpv3"}, "pve-api": {"password", "token"}}
            if cred.kind not in allowed.get(self.protocol, {"password"}):
                raise ValueError("新凭据类型与所选协议不匹配")
            if cred.kind != "snmp_community" and not cred.username:
                raise ValueError("请填写设备用户名")
            if cred.kind == "snmpv3":
                try:
                    cfg = json.loads(cred.secret)
                except ValueError:
                    raise ValueError("SNMPv3 凭据格式不正确") from None
                if not isinstance(cfg, dict):
                    raise ValueError("SNMPv3 凭据应为配置对象")
                if any(not isinstance(cfg[key], str) for key in ("auth_key", "priv_key", "auth_protocol", "priv_protocol") if key in cfg):
                    raise ValueError("SNMPv3 密码与算法应为文本")
                if cfg.get("priv_key") and not cfg.get("auth_key"):
                    raise ValueError("SNMPv3 加密需要同时配置认证")
                if cfg.get("auth_protocol", "SHA") not in {"SHA", "SHA256", "SHA512", "MD5"} or cfg.get("priv_protocol", "AES") not in {"AES", "AES256", "DES"}:
                    raise ValueError("SNMPv3 算法不支持")
        return self


class AssetCreate(AssetBase):
    endpoints: list[AssetEndpointIn] = Field(default_factory=list, max_length=20)


class AssetUpdate(BaseModel):
    """部分更新；必须带上读取时的 revision，不一致返回 409，不静默覆盖他人修改。"""

    revision: int
    asset_no: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None = None
    name: Name | None = None
    form: Form | None = None
    role: AssetRole | None = None
    platform: Annotated[str, StringConstraints(strip_whitespace=True, max_length=64)] | None = None
    vendor: Short | None = None
    model: Short | None = None
    serial_no: Short | None = None
    collection_profile: Short | None = None
    lifecycle: Lifecycle | None = None
    maintenance_until: datetime | None = None
    site_id: uuid.UUID | None = None
    rack_id: uuid.UUID | None = None
    rack_unit: int | None = Field(None, ge=1, le=60)
    device_type_id: uuid.UUID | None = None
    rack_face: Literal["front", "rear"] | None = None
    tags: list[Tag] | None = Field(None, max_length=50)
    notes: str | None = None


class AssetOut(ORM):
    id: uuid.UUID
    asset_no: str | None
    name: str
    form: str
    role: str
    platform: str
    vendor: str
    model: str
    serial_no: str
    collection_profile: str
    lifecycle: str
    maintenance_until: datetime | None = None
    site_id: uuid.UUID | None
    rack_id: uuid.UUID | None
    rack_unit: int | None
    device_type_id: uuid.UUID | None = None
    rack_face: str = "front"
    tags: list[str]
    notes: str
    revision: int
    legacy_source: str | None
    legacy_id: str | None
    created_at: datetime
    updated_at: datetime
    endpoints: list[EndpointOut]


class RelationIn(BaseModel):
    source_asset_id: uuid.UUID
    target_asset_id: uuid.UUID
    kind: RelationKind


class RelationOut(ORM):
    id: uuid.UUID
    source_asset_id: uuid.UUID
    target_asset_id: uuid.UUID
    kind: str
    origin: str


class AssetDetail(AssetOut):
    relations: list[RelationOut]


T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int


class AuditOut(ORM):
    id: uuid.UUID
    occurred_at: datetime
    actor_name: str
    action: str
    target_type: str
    target_id: str
    summary: str
    detail: dict
