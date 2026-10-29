"""P5 控制：控制归属开关、控制目标状态、控制操作记录。

- ControlSetting：整个平台只有一行（key="default"）。owner="legacy" 表示旧控制器仍在运行，
  新平台拒绝一切控制写入（零写调用）；切换到 "new" 必须走切换流程（停旧控制器后），默认 legacy。
- ControlTarget：一个管理目标（规范化后的 BMC 地址）一行。同一地址的多台资产共用一行，
  保证同一台 BMC 上的控制事务互斥。state="indeterminate" 时拒绝新的控制写入，直到有人
  只读核对设备后确认解除。thermal_mode / applied_pwm 只在设备接受温控命令后推进（已下发，不等于已验证）；
  失败或不确定时记为 unknown。
- ControlOperation：每次控制请求一行，记录谁、对什么、做了什么、结果如何。“指令已接受”
  （accepted）和“查询确认”（verified）分开；超时为 indeterminate，不自动重试。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKey, utcnow


class ControlSetting(Base):
    __tablename__ = "control_settings"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    # legacy：旧控制器负责控制，新平台零写；new：新平台负责控制
    owner: Mapped[str] = mapped_column(String(16), default="legacy")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    changed_by: Mapped[str] = mapped_column(String(100), default="")
    changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str] = mapped_column(Text, default="")


class ControlTarget(Base):
    __tablename__ = "control_targets"

    # bmc:<规范化地址>，同一地址不同端口也算同一目标
    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    # ok / indeterminate
    state: Mapped[str] = mapped_column(String(16), default="ok")
    # 正在执行的控制操作；为空表示空闲
    busy_operation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    last_operation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    message: Mapped[str] = mapped_column(Text, default="")
    # unknown / auto / manual
    thermal_mode: Mapped[str] = mapped_column(String(16), default="unknown")
    applied_pwm: Mapped[int | None] = mapped_column(Integer)
    # 按风扇设定（per_fan）时设备接受的每个风扇转速；其他模式为空
    applied_speeds: Mapped[list[int] | None] = mapped_column(JSON)
    thermal_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ControlOperation(UUIDPrimaryKey, Base):
    __tablename__ = "control_operations"

    asset_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("assets.id", ondelete="SET NULL"), index=True)
    asset_name: Mapped[str] = mapped_column(String(200), default="")
    endpoint_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("management_endpoints.id", ondelete="SET NULL"))
    target_key: Mapped[str] = mapped_column(String(300), default="", index=True)
    # power / thermal
    kind: Mapped[str] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(32))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # queued / running / accepted / verified / unconfirmed / failed / indeterminate / rejected / cancelled
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    observed: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"))
    requested_by: Mapped[str] = mapped_column(String(100), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("tasks.id", ondelete="SET NULL"))
    request_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ThermalCurve(UUIDPrimaryKey, Base):
    """温控曲线：节点 [{temp, speed}] 按温度升序；最高节点的温度就是安全熔断温度（旧版默认 82℃）。"""

    __tablename__ = "thermal_curves"

    key: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    nodes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String(16), default="user")  # legacy / user
    updated_by: Mapped[str] = mapped_column(String(100), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ThermalPolicy(UUIDPrimaryKey, Base):
    """每台资产的温控策略（期望状态）。只有控制归属为新平台、策略启用且门禁通过时才会下发。

    mode：auto（交给 BMC 原厂自动，不下发）/ fixed（固定转速）/ curve（按曲线）/ preset（情景方案，每个风扇各自的转速）。
    fuse_state：tripped 表示温度达到熔断温度后已请求恢复原厂自动，降到熔断温度减回差以下才恢复策略。
    """

    __tablename__ = "thermal_policies"

    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("assets.id", ondelete="CASCADE"), unique=True)
    mode: Mapped[str] = mapped_column(String(16), default="auto")
    fixed_pwm: Mapped[int | None] = mapped_column(Integer)
    # mode=preset 时使用的情景方案（app.control.thermal.PRESETS 的键）
    preset_key: Mapped[str] = mapped_column(String(32), default="")
    curve_key: Mapped[str] = mapped_column(String(32), default="default")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    source: Mapped[str] = mapped_column(String(16), default="user")
    # 旧版的模式：auto / manual / dynamic / preset；迁入时保留，便于核对
    legacy_mode: Mapped[str] = mapped_column(String(16), default="")
    pending_note: Mapped[str] = mapped_column(Text, default="")
    fuse_state: Mapped[str] = mapped_column(String(16), default="normal")
    last_decision: Mapped[str] = mapped_column(Text, default="")
    last_decision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_request_key: Mapped[str] = mapped_column(String(64), default="")
    last_request_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[str] = mapped_column(String(100), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ThermalCommandProfile(UUIDPrimaryKey, Base):
    """管理员自定义的风扇命令（IPMI raw）。按设备读出的厂商 / 型号 / BMC 固件匹配，优先于内置命令。

    restore_commands / set_commands 每行一条，写法同 ipmitool raw：netfn command data...，
    {pwm} 处填入目标转速。只允许 OEM 段的 netfn，不能借此发送电源、用户管理等命令。
    """

    __tablename__ = "thermal_command_profiles"

    name: Mapped[str] = mapped_column(String(100))
    vendor: Mapped[str] = mapped_column(String(64))
    model_pattern: Mapped[str] = mapped_column(String(200), default="")
    firmware_min: Mapped[str] = mapped_column(String(32), default="")
    firmware_max: Mapped[str] = mapped_column(String(32), default="")
    restore_commands: Mapped[str] = mapped_column(Text)
    set_commands: Mapped[str] = mapped_column(Text)
    # 可选：按风扇设定转速的命令，含 {fan} 的行按风扇逐个发送
    fan_commands: Mapped[str] = mapped_column(Text, default="")
    pwm_scale: Mapped[str] = mapped_column(String(16), default="percent")
    safe_min_pwm: Mapped[int] = mapped_column(Integer, default=20)
    safe_max_pwm: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String(100), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
