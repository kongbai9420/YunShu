"""告警接口的有界输入；通知目标、授权码和签名密钥只通过 secret 加密保存。"""
from __future__ import annotations

import string
import uuid
from datetime import datetime
from email.utils import parseaddr
from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, create_model, field_validator, model_validator
from typing import Annotated

Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Tag = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]
Severity = Literal["info", "warning", "critical"]
ChannelKind = Literal["email", "custom_webhook", "serverchan", "dingtalk", "feishu", "wechat_work", "bark", "pushplus"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class RuleIn(Strict):
    name: Name
    enabled: bool = True
    kind: Literal["threshold", "availability"] = "threshold"
    metric_key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] = "cpu.load"
    comparison: Literal["gte", "lte"] = "gte"
    threshold: float = Field(90, ge=-100000, le=1000000000)
    recovery_threshold: float = Field(85, ge=-100000, le=1000000000)
    consecutive_samples: int = Field(1, ge=1, le=100)
    duration_seconds: int = Field(0, ge=0, le=86400)
    cooldown_seconds: int = Field(60, ge=0, le=86400)
    repeat_seconds: int = Field(0, ge=0, le=86400)
    severity: Severity = "warning"
    asset_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    tags: list[Tag] = Field(default_factory=list, max_length=50)
    channel_ids: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    suppress_by_dependency: bool = True

    @model_validator(mode="after")
    def recovery_direction(self):
        if self.kind == "availability":
            self.metric_key = "availability"
            self.threshold, self.recovery_threshold = 1, 0
            self.comparison = "gte"
        if self.kind == "threshold":
            if self.comparison == "gte" and self.recovery_threshold > self.threshold:
                raise ValueError("恢复阈值应小于或等于触发阈值")
            if self.comparison == "lte" and self.recovery_threshold < self.threshold:
                raise ValueError("恢复阈值应大于或等于触发阈值")
        if self.repeat_seconds and self.repeat_seconds < max(30, self.cooldown_seconds):
            raise ValueError("重复提醒间隔至少 30 秒，且不能小于冷却时间；0 表示不重复")
        return self


RulePatch = create_model("RulePatch", __base__=Strict, revision=(int, Field(ge=1)), **{
    name: (Union[field.annotation, None], None) for name, field in RuleIn.model_fields.items()
})


class ChannelConfig(Strict):
    host: Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)] = ""
    port: int = Field(465, ge=1, le=65535)
    tls: Literal["ssl", "starttls"] = "ssl"
    from_address: str = Field("", max_length=254)
    recipients: list[Annotated[str, StringConstraints(strip_whitespace=True, max_length=254)]] = Field(default_factory=list, max_length=50)
    severities: list[Severity] = Field(default_factory=lambda: ["info", "warning", "critical"], max_length=3)
    tags: list[Tag] = Field(default_factory=list, max_length=50)
    group_seconds: int = Field(30, ge=10, le=3600)
    subject_template: str = Field("{title}", max_length=400)
    text_template: str = Field("{message}", max_length=8000)
    html_template: str = Field("", max_length=10000)

    @field_validator("subject_template", "text_template", "html_template")
    @classmethod
    def safe_template(cls, value: str):
        allowed = {"title", "message", "severity", "asset_name", "metric_key", "value", "threshold", "unit", "time", "alert_count", "event_kind"}
        try:
            for _, key, spec, conversion in string.Formatter().parse(value):
                if key is not None and (key not in allowed or spec or conversion):
                    raise ValueError("模板只能使用指定的简单变量，不支持属性、下标或格式表达式")
        except ValueError:
            raise ValueError("模板格式不正确，支持 title、message、severity、asset_name、metric_key、value、threshold、unit、time、alert_count、event_kind") from None
        return value


class ChannelSecret(Strict):
    url: str = Field("", max_length=4096)
    secret: str = Field("", max_length=2000)
    username: str = Field("", max_length=200)
    password: str = Field("", max_length=2000)


class ChannelIn(Strict):
    name: Name
    kind: ChannelKind
    enabled: bool = False
    config: ChannelConfig = Field(default_factory=ChannelConfig)
    secret: ChannelSecret | None = None

    @model_validator(mode="after")
    def email_fields(self):
        if self.kind == "email":
            if not self.config.host or not self.config.recipients:
                raise ValueError("邮件渠道需要 SMTP 主机和收件人")
            for addr in [self.config.from_address, *self.config.recipients]:
                if not addr or parseaddr(addr)[1] != addr or "@" not in addr or any(c in addr for c in "\r\n"):
                    raise ValueError("发件人和收件人须为完整邮箱地址，不含显示名或换行")
            if any(c in self.config.host for c in "/:@\r\n "):
                raise ValueError("SMTP 主机只填域名或 IP")
        return self


class ChannelPatch(Strict):
    revision: int = Field(ge=1)
    name: Name | None = None
    enabled: bool | None = None
    config: ChannelConfig | None = None
    secret: ChannelSecret | None = None


class MaintenanceIn(Strict):
    name: Name
    starts_at: datetime
    ends_at: datetime
    asset_ids: list[uuid.UUID] = Field(default_factory=list, max_length=500)
    tags: list[Tag] = Field(default_factory=list, max_length=50)
    # 不选资产也不填标签时对全部资产生效：必须明确勾选，防止只填时间就静默了全平台
    all_assets: bool = False

    @model_validator(mode="after")
    def ordered(self):
        if not self.asset_ids and not self.tags and not self.all_assets:
            raise ValueError("没有选择资产或标签；要对全部资产生效，请勾选“对全部资产生效”")
        if (self.asset_ids or self.tags) and self.all_assets:
            raise ValueError("已选择资产或标签时不能同时勾选“对全部资产生效”")
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError("维护时间须包含时区")
        if self.ends_at <= self.starts_at:
            raise ValueError("维护结束时间必须晚于开始时间")
        if (self.ends_at - self.starts_at).total_seconds() > 366 * 86400:
            raise ValueError("维护窗口不能超过一年")
        return self


class AlertAction(Strict):
    action: Literal["acknowledge", "silence", "unsilence"]
    minutes: int = Field(60, ge=1, le=43200)


class RetryIn(Strict):
    confirm_uncertain: bool = False
