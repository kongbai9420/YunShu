"""P1 实体：资产、管理端点、场所与机柜、资产关系、凭据、用户、审计。

其余实体（采集、指标、拓扑、告警、任务）在用到它们的阶段再加（落地方案 P1）。
时间统一按 UTC 写入；SQLite 读回时不带时区信息。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Float,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, Timestamps, UUIDPrimaryKey, utcnow


class Site(UUIDPrimaryKey, Timestamps, Base):
    """场所：家庭、机房、房间等，可逐级嵌套。"""

    __tablename__ = "sites"

    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(32), default="site")
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("sites.id", ondelete="RESTRICT"), index=True
    )
    notes: Mapped[str] = mapped_column(Text, default="")


class Rack(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "racks"
    __table_args__ = (UniqueConstraint("site_id", "name"),)

    site_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("sites.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    height_u: Mapped[int] = mapped_column(Integer, default=42)


class DeviceType(UUIDPrimaryKey, Timestamps, Base):
    """设备型号（4.2 P14）：U 高、是否全深、端口与电源口数量。可从 devicetype-library（CC0）导入或手工录入。"""

    __tablename__ = "device_types"
    __table_args__ = (UniqueConstraint("manufacturer", "model"),)

    manufacturer: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(200), default="")
    part_number: Mapped[str] = mapped_column(String(100), default="")
    u_height: Mapped[float] = mapped_column(Float, default=1)
    is_full_depth: Mapped[bool] = mapped_column(Boolean, default=True)
    interfaces: Mapped[int] = mapped_column(Integer, default=0)
    power_ports: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(32), default="manual")


class Credential(UUIDPrimaryKey, Timestamps, Base):
    """加密凭据。密文、随机数与密钥编号分开存，普通查询接口不返回这三列。"""

    __tablename__ = "credentials"
    __table_args__ = (UniqueConstraint("legacy_source", "legacy_id"),)

    alias: Mapped[str] = mapped_column(String(200), unique=True)
    kind: Mapped[str] = mapped_column(String(32))
    username: Mapped[str] = mapped_column(String(200), default="")
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    secret_nonce: Mapped[bytes] = mapped_column(LargeBinary)
    key_id: Mapped[str] = mapped_column(String(64))
    notes: Mapped[str] = mapped_column(Text, default="")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    legacy_source: Mapped[str | None] = mapped_column(String(64))
    legacy_id: Mapped[str | None] = mapped_column(String(200))


class Asset(UUIDPrimaryKey, Timestamps, Base):
    """资产。形态、角色、平台三个字段相互独立。

    去重只认资产编号或旧系统编号，不按 IP 合并。
    revision 每次修改加一，用于并发修改检测和导入撤销时判断是否被他人改过。
    """

    __tablename__ = "assets"
    __table_args__ = (UniqueConstraint("legacy_source", "legacy_id"),)

    asset_no: Mapped[str | None] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    form: Mapped[str] = mapped_column(String(32))
    role: Mapped[str] = mapped_column(String(32))
    platform: Mapped[str] = mapped_column(String(64), default="")
    vendor: Mapped[str] = mapped_column(String(100), default="")
    model: Mapped[str] = mapped_column(String(100), default="")
    serial_no: Mapped[str] = mapped_column(String(100), default="")
    # 采集模板名称；P2 建 CollectionProfile 后据此关联
    collection_profile: Mapped[str] = mapped_column(String(100), default="")
    lifecycle: Mapped[str] = mapped_column(String(32), default="active")
    # 维护中的预计结束时间；过了还没改回在用会提醒。导入的维护中资产没有，不提醒
    maintenance_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 回收站：删除后保留一段时间可恢复；删除时生命周期改为已退役，后台一律不再处理，恢复时还原
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    deleted_by: Mapped[str] = mapped_column(String(100), default="")
    deleted_lifecycle: Mapped[str] = mapped_column(String(32), default="")
    site_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("sites.id", ondelete="SET NULL"), index=True
    )
    rack_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("racks.id", ondelete="SET NULL"), index=True
    )
    rack_unit: Mapped[int | None] = mapped_column(Integer)
    # 4.2 P14：型号决定占用的 U 数；安装面 front / rear（全深设备两面都占）
    device_type_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("device_types.id", ondelete="SET NULL"), index=True
    )
    rack_face: Mapped[str] = mapped_column(String(8), default="front")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    legacy_source: Mapped[str | None] = mapped_column(String(64))
    legacy_id: Mapped[str | None] = mapped_column(String(200))

    endpoints: Mapped[list[ManagementEndpoint]] = relationship(
        back_populates="asset", cascade="all, delete-orphan", passive_deletes=True
    )


class ManagementEndpoint(UUIDPrimaryKey, Timestamps, Base):
    """管理端点：地址、端口、协议、用途与凭据引用分开记录，一台资产可有多个。"""

    __tablename__ = "management_endpoints"

    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    address: Mapped[str] = mapped_column(String(255), index=True)
    port: Mapped[int | None] = mapped_column(Integer)
    protocol: Mapped[str] = mapped_column(String(32))
    purpose: Mapped[str] = mapped_column(String(32), default="management")
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("credentials.id", ondelete="SET NULL"), index=True
    )
    tls_verify: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    # 首次信任并固定：首次连接时记下，之后不一致就拒绝；用户确认后可清空重新信任
    tls_fingerprint: Mapped[str] = mapped_column(String(128), default="")
    ssh_host_key: Mapped[str] = mapped_column(String(512), default="")
    trust_pinned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 4.3 P18b：是否每天读取一次已安装软件包清单（只对 SSH 端点有效，默认关闭）
    collect_packages: Mapped[bool] = mapped_column(Boolean, default=False)

    asset: Mapped[Asset] = relationship(back_populates="endpoints")


class AssetRelation(UUIDPrimaryKey, Timestamps, Base):
    """已确认的资产关系（承载、安装、属于等），与发现结果、拓扑布局分开存。"""

    __tablename__ = "asset_relations"
    __table_args__ = (UniqueConstraint("source_asset_id", "target_asset_id", "kind"),)

    source_asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("assets.id", ondelete="CASCADE"), index=True
    )
    target_asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("assets.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32))
    origin: Mapped[str] = mapped_column(String(32), default="manual")


class User(UUIDPrimaryKey, Timestamps, Base):
    """用户。首发只有 admin / operator / viewer 三个角色。"""

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("legacy_source", "legacy_id"),)

    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    role: Mapped[str] = mapped_column(String(16))
    display_name: Mapped[str] = mapped_column(String(200), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    require_password_change: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    legacy_source: Mapped[str | None] = mapped_column(String(64))
    legacy_id: Mapped[str | None] = mapped_column(String(200))
    # 资产范围（4.1 P11）：True 为全部可见（现有账号与新建账号默认）；admin 不受限制
    scope_all: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    # 4.2 P16 单点登录：local 为本地密码账号；oidc 账号用身份提供方的 subject 识别，不能用密码登录
    auth_source: Mapped[str] = mapped_column(String(16), default="local", server_default="local")
    external_subject: Mapped[str | None] = mapped_column(String(255), unique=True)
    # P23 外观：{"style": ..., "mode": ...}，为空或缺键时用平台默认
    appearance: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class LoginSession(UUIDPrimaryKey, Base):
    """登录会话。库里只存令牌的 SHA-256，不存令牌本身。"""

    __tablename__ = "login_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    client_ip: Mapped[str] = mapped_column(String(64), default="")


class AuditEvent(UUIDPrimaryKey, Base):
    """操作审计。只追加，普通业务接口不能修改或删除；detail 写入前必须脱敏。"""

    __tablename__ = "audit_events"

    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    actor_name: Mapped[str] = mapped_column(String(100), default="")
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(32), default="")
    target_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ImportBatch(UUIDPrimaryKey, Base):
    """一次导入（旧配置迁移或模板导入）。预检不落库，只有确认提交才生成批次。"""

    __tablename__ = "import_batches"

    source: Mapped[str] = mapped_column(String(32))
    template_kind: Mapped[str] = mapped_column(String(32), default="")
    filename: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    created_by: Mapped[str] = mapped_column(String(100), default="")
    # preview（预览，未提交）/ applied / undone
    status: Mapped[str] = mapped_column(String(16), default="applied", index=True)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list[ImportItem]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", passive_deletes=True, order_by="ImportItem.row_no"
    )


class ImportItem(UUIDPrimaryKey, Base):
    """模板导入的逐行记录：预览结果、提交结果，以及撤销所需的修改前快照。"""

    __tablename__ = "import_items"

    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("import_batches.id", ondelete="CASCADE"), index=True)
    row_no: Mapped[int] = mapped_column(Integer)
    asset_no: Mapped[str] = mapped_column(String(100), default="")
    name: Mapped[str] = mapped_column(String(200), default="")
    # 预览：create / update / unchanged / invalid / conflict；提交后：created / updated / unchanged / skipped / conflict
    action: Mapped[str] = mapped_column(String(16))
    messages: Mapped[list[str]] = mapped_column(JSON, default=list)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    base_revision: Mapped[int | None] = mapped_column(Integer)
    after_revision: Mapped[int | None] = mapped_column(Integer)
    before: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_refs: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    undo_result: Mapped[str] = mapped_column(String(16), default="")

    batch: Mapped[ImportBatch] = relationship(back_populates="items")


class LegacyMapping(UUIDPrimaryKey, Base):
    """旧 ID → 新 UUID 映射，重复导入以它为幂等依据。

    payload 是旧记录脱敏后的原样副本（不含密码），暂时无法转换的字段（温控模式、曲线等）
    留在这里供后续阶段迁移。target_revision 记录导入时资产的修订号，用来判断导入后
    是否被用户改过：改过就不再覆盖。
    """

    __tablename__ = "legacy_mappings"
    __table_args__ = (UniqueConstraint("source", "legacy_kind", "legacy_id"),)

    source: Mapped[str] = mapped_column(String(64))
    legacy_kind: Mapped[str] = mapped_column(String(32))
    legacy_id: Mapped[str] = mapped_column(String(200))
    target_type: Mapped[str] = mapped_column(String(32), default="")
    target_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    endpoint_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    target_revision: Mapped[int | None] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64), default="")
    batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("import_batches.id", ondelete="SET NULL"))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)



class Task(UUIDPrimaryKey, Base):
    """持久任务队列（持久任务约束）。

    - 认领在一条 UPDATE … RETURNING 里完成，并发的 Worker 不会认领到同一个任务。
    - 认领时发放 lease_token；完成、失败、续租都必须带上同一个 token，过期 Worker 不能覆盖新结果。
    - risk=read 的采集任务失败或租约过期可以重试；risk=control 的任务超时或租约过期一律标为
      indeterminate（结果不确定），不自动重试，先人工查设备状态。
    - idempotency_key 相同的任务只入队一次。result 写入前脱敏。
    """

    __tablename__ = "tasks"

    kind: Mapped[str] = mapped_column(String(64), index=True)
    risk: Mapped[str] = mapped_column(String(16), default="read")
    asset_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("assets.id", ondelete="SET NULL"), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # queued / running / succeeded / failed / indeterminate / cancelled
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    lease_owner: Mapped[str] = mapped_column(String(100), default="")
    lease_token: Mapped[str] = mapped_column(String(64), default="")
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SchedulerLock(Base):
    """单实例锁：SQLite 轻量版只允许一个调度 / Worker 实例。持有者定期续期，过期可被接管。"""

    __tablename__ = "scheduler_locks"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner: Mapped[str] = mapped_column(String(100))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UserScopeSite(Base):
    """账号可见的场所（含全部下级场所），4.1 P11。"""

    __tablename__ = "user_scope_sites"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    site_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("sites.id", ondelete="CASCADE"), primary_key=True)


class UserScopeAsset(Base):
    """账号单独可见的资产，4.1 P11。"""

    __tablename__ = "user_scope_assets"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)


class WorkerHeartbeat(Base):
    """Worker 在线状态（4.1 P9）：每轮循环与认领任务时更新，用于判断扩容是否有效；超过若干周期无心跳即视为离线。"""

    __tablename__ = "worker_heartbeats"

    owner: Mapped[str] = mapped_column(String(100), primary_key=True)
    host: Mapped[str] = mapped_column(String(200), default="")
    pid: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String(16), default="all")
    version: Mapped[str] = mapped_column(String(32), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    scheduler: Mapped[bool] = mapped_column(Boolean, default=False)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    task_kind: Mapped[str] = mapped_column(String(64), default="")


class LatestMetric(Base):
    """每台资产每个指标的最新状态（值或空、单位、时间、来源、质量、是否估算）。

    quality 六种：valid / stale / unsupported / unconfigured / failed / estimated。
    stale 不落库，由读取时按 observed_at 与采集间隔计算。失败、不支持、未配置时 value 为空，
    界面不能显示成 0；last_valid_* 保留最近一次有效值供参考。
    """

    __tablename__ = "latest_metrics"

    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    metric_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value: Mapped[float | None] = mapped_column()
    unit: Mapped[str] = mapped_column(String(32), default="")
    quality: Mapped[str] = mapped_column(String(16))
    estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(64), default="")
    message: Mapped[str] = mapped_column(String(500), default="")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    interval_seconds: Mapped[int] = mapped_column(Integer, default=60)
    last_valid_value: Mapped[float | None] = mapped_column()
    last_valid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MetricRollup(Base):
    """压缩存放的汇总，避免每个指标每分钟一行（100 台 × 30 指标时一天就是 432 万行）。

    - resolution=60（分钟）：每台资产每小时一行，bucket 为该小时起点（Unix 秒）；
    - resolution=3600（小时）：每台资产每天一行，bucket 为该天 UTC 零点。
    data 形如 {"cpu.temp": {"a": [平均…], "mn": [最小…], "mx": [最大…], "n": [样本数…]}}，
    数组长度 60 或 24，没有数据的位置为 null；每个槽只有 1 个样本时省略 mn / mx（等于平均）。
    """

    __tablename__ = "metric_rollups"

    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    resolution: Mapped[int] = mapped_column(Integer, primary_key=True)
    bucket: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[str] = mapped_column(Text)


class MetricState(Base):
    """汇总与清理的进度水位（Unix 秒），保证重复执行不重复汇总。"""

    __tablename__ = "metric_state"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(Integer)


class CollectionStatus(Base):
    """每个管理端点最近一次采集的结果，供监控中心显示“采集任务与状态”。"""

    __tablename__ = "collection_status"

    endpoint_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("management_endpoints.id", ondelete="CASCADE"), primary_key=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    protocol: Mapped[str] = mapped_column(String(32))
    # ok / failed / skipped（因降级未执行）/ untrusted
    state: Mapped[str] = mapped_column(String(16))
    error_kind: Mapped[str] = mapped_column(String(32), default="")
    message: Mapped[str] = mapped_column(String(1000), default="")
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list)
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)


class CollectionPlan(Base):
    """每台服务器 BMC 的采集计划（4.4 P20a）：探测到的各协议能力，以及每项数据的主来源与兜底顺序。"""

    __tablename__ = "collection_plans"

    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    # 数据项 → 按优先顺序排列的协议，第一个是主来源
    items: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 协议 → 探测时实际读到的数据项；失败或缺凭据的协议记为 null（每小时重新探测）
    probed: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # 参与探测的端点（协议、地址、端口、凭据）摘要；端点变了就重新探测
    signature: Mapped[str] = mapped_column(String(64), default="")
    probed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 下次采集时重新探测（页面“重新探测”）
    reprobe: Mapped[bool] = mapped_column(Boolean, default=False)
    # 4.4 P20e 双来源核对：{"items": {数据项: 结果}, "pinned": {数据项: "redfish"}}；读数不一致时该项固定改由 Redfish 提供
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    check: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class ServerEvent(UUIDPrimaryKey, Base):
    """服务器告警（4.4 P20c）：BMC 系统事件日志（SEL）等来源的逐条记录，按来源与记录号去重。"""

    __tablename__ = "server_events"
    __table_args__ = (UniqueConstraint("asset_id", "source", "record_key"),)

    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    # redfish / ipmi（BMC 日志）；之后的 SNMP Trap、Redfish 事件订阅也写这里
    source: Mapped[str] = mapped_column(String(16))
    # 记录号 + 发生时间：BMC 清空日志后记录号会重用，加上时间避免误判为同一条
    record_key: Mapped[str] = mapped_column(String(160))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # critical / warning / info
    severity: Mapped[str] = mapped_column(String(16), index=True)
    # temperature / fan / power / memory / storage / cpu / chassis / other
    category: Mapped[str] = mapped_column(String(16), default="other")
    message: Mapped[str] = mapped_column(String(1000), default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # open（待处理）/ handled（已处理）/ archived（首次读到时已是旧记录或只是提示，不需要处理）
    state: Mapped[str] = mapped_column(String(16), default="open", index=True)
    handled_by: Mapped[str] = mapped_column(String(200), default="")
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str] = mapped_column(String(500), default="")


class BmcEventSubscription(UUIDPrimaryKey, Base):
    """平台在 BMC 上配置的告警推送（4.4 P20c）：Redfish 事件订阅或 SNMP Trap 目标，一台资产每种最多一条。

    Redfish 订阅的接收地址带随机令牌，库里只存令牌哈希；接收端凭令牌认出资产。
    """

    __tablename__ = "bmc_event_subscriptions"
    __table_args__ = (UniqueConstraint("asset_id", "kind"),)

    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    # redfish_event / snmp_trap
    kind: Mapped[str] = mapped_column(String(16))
    # Redfish：接收地址（不含令牌）；Trap：BMC 发往的平台地址
    destination: Mapped[str] = mapped_column(String(500), default="")
    # BMC 上的订阅资源路径（Redfish）或 iDRAC 告警目标序号（Trap）
    remote_id: Mapped[str] = mapped_column(String(300), default="")
    token_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(200), default="")
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    events_received: Mapped[int] = mapped_column(Integer, default=0)


class DiscoveryRun(UUIDPrimaryKey, Base):
    """明确授权范围内的只读发现；候选观察记录独立于资产和拓扑。"""

    __tablename__ = "discovery_runs"

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(100), default="")
    scope: Mapped[list[str]] = mapped_column(JSON)
    excludes: Mapped[list[str]] = mapped_column(JSON)
    addresses: Mapped[list[str]] = mapped_column(JSON)
    protocol: Mapped[str] = mapped_column(String(16))
    ports: Mapped[list[int]] = mapped_column(JSON)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("credentials.id", ondelete="SET NULL"))
    site_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("sites.id", ondelete="SET NULL"))
    concurrency: Mapped[int] = mapped_column(Integer, default=8)
    timeout: Mapped[float] = mapped_column(Float, default=1.0)
    state: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    responded: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(String(500), default="")


class DiscoveryObservation(UUIDPrimaryKey, Base):
    __tablename__ = "discovery_observations"
    __table_args__ = (UniqueConstraint("run_id", "address"),)

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("discovery_runs.id", ondelete="CASCADE"), index=True)
    address: Mapped[str] = mapped_column(String(255))
    ports: Mapped[list[int]] = mapped_column(JSON)
    facts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(16), default="candidate")
    asset_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("assets.id", ondelete="SET NULL"))



class Dashboard(UUIDPrimaryKey, Base):
    """总览仪表盘布局（布局存数据库并带 revision，冲突返回明确的冲突响应，不静默覆盖）。

    key="default" 是所有人共用的总览；data 为 {"layouts": {lg/md/sm: [...]}, "watch": [资产 id, ...]}。
    浏览器只做缓存与未保存编辑的临时恢复。
    """

    __tablename__ = "dashboards"

    key: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200), default="总览")
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[str] = mapped_column(String(100), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class InspectionReport(UUIDPrimaryKey, Base):
    """巡检报告（4.2 P16）：生成时把所有发现写成 items，查看时按账号的资产范围过滤。"""

    __tablename__ = "inspection_reports"

    kind: Mapped[str] = mapped_column(String(16))  # daily / manual
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    created_by: Mapped[str] = mapped_column(String(100), default="")
    asset_count: Mapped[int] = mapped_column(Integer, default=0)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)


class CollectionBoost(Base):
    """临时提高采集频率（4.4 P20d）：告警、接近阈值或收到推送事件时登记，until 之前每 15 秒采集一次。"""

    __tablename__ = "collection_boosts"

    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    until: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reason: Mapped[str] = mapped_column(String(200), default="")


class BmcLease(Base):
    """每台 BMC 的排队租约（4.4 P20b，PostgreSQL 多 Worker 时使用）：holder 为空或过期表示空闲。"""

    __tablename__ = "bmc_leases"

    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    holder: Mapped[str] = mapped_column(String(64), default="")
    # collect / control / probe
    kind: Mapped[str] = mapped_column(String(16), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BmcAccess(UUIDPrimaryKey, Base):
    """每次访问 BMC（Redfish / IPMI）的记录，用于每分钟次数上限；保留 1 小时。"""

    __tablename__ = "bmc_accesses"

    key: Mapped[str] = mapped_column(String(300), index=True)
    protocol: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(16))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class InspectionTemplate(UUIDPrimaryKey, Base):
    """命令巡检模板（4.4 P21）：适用平台 + 若干检查项。内置模板只读；自定义模板仅管理员可建改。"""

    __tablename__ = "inspection_templates"

    # 内置模板的固定键（如 builtin-linux）；自定义模板为空
    builtin_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    # linux / pve / huawei / h3c
    platform: Mapped[str] = mapped_column(String(16))
    description: Mapped[str] = mapped_column(String(1000), default="")
    # [{key, name, command, check: {...}, severity, timeout}]
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    updated_by: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class InspectionJob(UUIDPrimaryKey, Base):
    """命令巡检任务：选设备 + 选模板 + 定时（单次 / 每天 / 每周 / 每月）。"""

    __tablename__ = "inspection_jobs"

    name: Mapped[str] = mapped_column(String(200))
    template_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("inspection_templates.id", ondelete="RESTRICT"), index=True)
    asset_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    # {"kind": "once" | "daily" | "weekly" | "monthly", "at": ISO 时间（单次）, "time": "HH:MM", "weekdays": [0-6], "day": 1-28}
    schedule: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class InspectionRun(UUIDPrimaryKey, Base):
    """一次巡检执行：每台设备一个 Worker 任务，全部设备完成后整体完成。"""

    __tablename__ = "inspection_runs"

    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("inspection_jobs.id", ondelete="SET NULL"), index=True)
    template_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("inspection_templates.id", ondelete="SET NULL"), index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    template_name: Mapped[str] = mapped_column(String(200), default="")
    # 执行时的检查项快照：执行中修改模板不影响本次
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    platform: Mapped[str] = mapped_column(String(16), default="linux")
    triggered_by: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # running / done
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    asset_total: Mapped[int] = mapped_column(Integer, default=0)
    assets_done: Mapped[int] = mapped_column(Integer, default=0)
    # {"ok": n, "abnormal": n, "unknown": n, "error": n, "critical": n, "warning": n, "info": n}
    counts: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)


class InspectionResult(UUIDPrimaryKey, Base):
    """一台设备一条检查项的结果；连接失败时记一条 item_key 为空的错误。保留 90 天。"""

    __tablename__ = "inspection_results"

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("inspection_runs.id", ondelete="CASCADE"), index=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    asset_name: Mapped[str] = mapped_column(String(200), default="")
    item_key: Mapped[str] = mapped_column(String(64), default="")
    item_name: Mapped[str] = mapped_column(String(200), default="")
    command: Mapped[str] = mapped_column(String(1000), default="")
    output: Mapped[str] = mapped_column(Text, default="")
    exit_code: Mapped[int | None] = mapped_column(Integer)
    # ok / abnormal / unknown / error
    verdict: Mapped[str] = mapped_column(String(16), index=True)
    # 异常时的严重程度：critical / warning / info
    severity: Mapped[str] = mapped_column(String(16), default="")
    detail: Mapped[str] = mapped_column(String(1000), default="")
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ApiToken(UUIDPrimaryKey, Base):
    """只读 API 令牌（4.2 P16）：目前只用于 /metrics 导出；权限与资产范围跟随所属账号。只存哈希。"""

    __tablename__ = "api_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    purpose: Mapped[str] = mapped_column(String(16), default="metrics")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlatformSetting(Base):
    """平台级的小设置（键值），例如回收站保留天数。没有对应行时用代码里的默认值。"""

    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_by: Mapped[str] = mapped_column(String(100), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UiTheme(Base):
    """管理员导入的主题包（P23）：只含变量值，由 app.appearance 校验后保存。"""

    __tablename__ = "ui_themes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(40))
    base: Mapped[str] = mapped_column(String(16))
    package: Mapped[dict[str, Any]] = mapped_column(JSON)
    updated_by: Mapped[str] = mapped_column(String(100), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UiWallpaper(Base):
    """玻璃主题的自定义壁纸（P23）。存在库里，备份与恢复时一并带上，不依赖数据目录。"""

    __tablename__ = "ui_wallpapers"

    key: Mapped[str] = mapped_column(String(16), primary_key=True)
    media_type: Mapped[str] = mapped_column(String(32))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    sha256: Mapped[str] = mapped_column(String(64))
    updated_by: Mapped[str] = mapped_column(String(100), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
