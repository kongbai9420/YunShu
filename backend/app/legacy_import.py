"""旧数据导入器：config.ini + users.json → 新库（落地方案 P1）。

    python -m app.legacy_import --config <config.ini> --users <users.json>          # 预检，不写库
    python -m app.legacy_import --config <config.ini> --users <users.json> --apply  # 确认导入

规则：
- 只读旧文件：一次读入字节后在内存解析，不用旧版 ConfigManager（它加载时会回写默认值），
  导入前后旧文件内容不变。
- 映射：servers_json → 物理服务器 + IPMI 端点 + 凭据；system_servers_json → Linux 服务器
  + SSH 端点 + 凭据，node_id → 与宿主资产的“承载”关系；users.json → 用户（哈希原样带过来）；
  curve_nodes_json、每台服务器的温控模式、告警设置 → 原样存进映射表，标为“待 P4/P5 迁移”。
- 幂等：以 (来源, 类型, 旧 ID) 为键。重复执行时旧记录没变就跳过；变了且新平台没人改过就更新；
  新平台已改过就列为冲突、不覆盖；新平台已删除的不重建；旧配置里删掉的不在新库删除。
- 密码只以加密形式进新库，报告、日志、映射表里都没有密码。
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import USER_ROLES, is_valid_password_hash
from app.crypto import CredentialCipher, DecryptionError
from app.models import (
    Asset,
    AssetRelation,
    AuditEvent,
    Credential,
    ImportBatch,
    LegacyMapping,
    ManagementEndpoint,
    User,
)
from app.services.credentials import create_credential, reveal_secret, set_secret

SOURCE = "yunshu-3.x"
SECRET_FIELDS = {"password", "webhook_secret", "webhook_url"}
ACCOUNT_FIELDS = {"user", "username"}

BRAND_PLATFORM = {
    "dell": ("Dell", "dell-idrac"),
    "inspur": ("Inspur", "inspur-bmc"),
    "huawei": ("Huawei", "huawei-ibmc"),
    "supermicro": ("Supermicro", "supermicro-bmc"),
    "lenovo": ("Lenovo", "lenovo-xcc"),
    "hpe": ("HPE", "hpe-ilo"),
    "hp": ("HPE", "hpe-ilo"),
}
OS_PLATFORMS = ("proxmox", "ubuntu", "debian", "centos", "rocky", "alpine", "openwrt", "esxi", "windows")


# ---- 读取 ----


@dataclass
class LegacyData:
    hardware_nodes: list[dict[str, Any]] = field(default_factory=list)
    system_servers: list[dict[str, Any]] = field(default_factory=list)
    curve_nodes: list[dict[str, Any]] = field(default_factory=list)
    alert_settings: dict[str, str] = field(default_factory=dict)
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def _json_list(raw: str, label: str, problems: list[str]) -> list[dict[str, Any]]:
    if not raw.strip():
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        problems.append(f"{label} 不是合法 JSON，已跳过")
        return []
    if not isinstance(value, list):
        problems.append(f"{label} 不是列表，已跳过")
        return []
    return [v for v in value if isinstance(v, dict)]


def read_legacy(config_path: Path | None, users_path: Path | None) -> LegacyData:
    data = LegacyData()
    if config_path is not None:
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(config_path.read_bytes().decode("utf-8-sig"))
        cluster = parser["cluster"] if parser.has_section("cluster") else {}
        data.hardware_nodes = _json_list(cluster.get("servers_json", ""), "servers_json", data.problems)
        data.system_servers = _json_list(cluster.get("system_servers_json", ""), "system_servers_json", data.problems)
        if parser.has_section("ipmi"):
            data.curve_nodes = _json_list(parser["ipmi"].get("curve_nodes_json", ""), "curve_nodes_json", data.problems)
        if parser.has_section("alert"):
            data.alert_settings = dict(parser["alert"])
    if users_path is not None:
        raw = json.loads(users_path.read_bytes().decode("utf-8-sig"))
        if isinstance(raw, dict):
            data.users = {k: v for k, v in raw.items() if isinstance(v, dict)}
        else:
            data.problems.append("users.json 不是对象，已跳过")
    return data


# ---- 报告 ----


@dataclass
class ReportItem:
    kind: str
    legacy_id: str
    name: str
    action: str  # created / updated / unchanged / conflict / skipped / pending
    message: str = ""


@dataclass
class ImportReport:
    applied: bool
    items: list[ReportItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    legacy_counts: dict[str, int] = field(default_factory=dict)
    batch_id: str | None = None

    def add(self, kind: str, legacy_id: str, name: str, action: str, message: str = "") -> None:
        self.items.append(ReportItem(kind, legacy_id, name, action, message))

    def counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for item in self.items:
            out.setdefault(item.kind, {}).setdefault(item.action, 0)
            out[item.kind][item.action] += 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "batch_id": self.batch_id,
            "legacy_counts": self.legacy_counts,
            "counts": self.counts(),
            "warnings": self.warnings,
            "items": [item.__dict__ for item in self.items],
        }

    def brief(self) -> str:
        c = self.counts()
        parts = [f"物理服务器 {sum(c.get('hardware_node', {}).values())} 台", f"Linux 服务器 {sum(c.get('system_server', {}).values())} 台"]
        if self.legacy_counts.get("user"):
            parts.append(f"用户 {sum(c.get('user', {}).values())} 个")
        flagged = sum(1 for i in self.items if i.action in ("conflict", "skipped"))
        head = "已从旧配置导入" if self.applied else "旧配置预检"
        return f"{head}：" + "，".join(parts) + (f"；{flagged} 条需人工确认" if flagged else "") + (f"；{len(self.warnings)} 条提示" if self.warnings else "")

    def to_text(self) -> str:
        labels = {"created": "新增", "updated": "更新", "unchanged": "无变化", "conflict": "冲突", "skipped": "跳过", "pending": "待后续阶段"}
        kinds = {"hardware_node": "物理服务器", "system_server": "Linux 服务器", "user": "用户", "curve_nodes": "温控曲线", "alert_settings": "告警设置"}
        lines = ["旧数据导入" + ("（已提交）" if self.applied else "（预检，未写入）")]
        for kind, n in self.legacy_counts.items():
            got = self.counts().get(kind, {})
            detail = "，".join(f"{labels[a]} {c}" for a, c in got.items())
            lines.append(f"- {kinds.get(kind, kind)}：旧配置 {n} 条；{detail}")
        flagged = [i for i in self.items if i.action in ("conflict", "skipped", "pending")]
        if flagged:
            lines.append("需要人工确认：")
            lines += [f"  [{labels[i.action]}] {kinds.get(i.kind, i.kind)} {i.name}（旧 ID {i.legacy_id}）：{i.message}" for i in flagged]
        if self.warnings:
            lines.append("提示：")
            lines += [f"  {w}" for w in self.warnings]
        if self.batch_id:
            lines.append(f"导入批次：{self.batch_id}")
        return "\n".join(lines)


# ---- 导入 ----


def _redacted(record: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k not in SECRET_FIELDS and k not in ACCOUNT_FIELDS}


def _hash(record: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(_redacted(record), sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _platform_from_os(os_name: str) -> str:
    low = os_name.lower()
    for name in OS_PLATFORMS:
        if name in low:
            return name
    return low.split()[0] if low.split() else ""


class _Importer:
    def __init__(self, db: Session, cipher: CredentialCipher, report: ImportReport) -> None:
        self.db = db
        self.cipher = cipher
        self.report = report
        self.batch: ImportBatch | None = None
        self.asset_by_legacy: dict[str, Asset] = {}

    def mapping(self, kind: str, legacy_id: str) -> LegacyMapping | None:
        return self.db.scalar(
            select(LegacyMapping).where(
                LegacyMapping.source == SOURCE, LegacyMapping.legacy_kind == kind, LegacyMapping.legacy_id == legacy_id
            )
        )

    def new_mapping(self, kind: str, legacy_id: str, record: dict[str, Any], target_type: str) -> LegacyMapping:
        m = LegacyMapping(source=SOURCE, legacy_kind=kind, legacy_id=legacy_id, target_type=target_type)
        self.db.add(m)
        self.touch(m, record)
        return m

    def touch(self, m: LegacyMapping, record: dict[str, Any] | list[Any]) -> None:
        # 先把本条记录的改动写下去，再记导入时间；之后的修改时间晚于它才算“导入后被改过”
        self.db.flush()
        if isinstance(record, dict):
            m.payload = _redacted(record)
            m.payload_hash = _hash(record)
        else:
            m.payload = {"items": record}
            m.payload_hash = hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        m.imported_at = datetime.now(UTC)
        m.batch_id = self.batch.id if self.batch else None

    # -- 凭据 --

    def sync_credential(
        self, m: LegacyMapping, *, alias: str, username: str, password: str, name: str, kind: str
    ) -> tuple[Credential | None, str]:
        """返回 (凭据, 状态)。状态：created / updated / unchanged / conflict / none。"""
        cred = self.db.get(Credential, m.credential_id) if m.credential_id else None
        if not password:
            return cred, "none"
        if cred is None:
            unique = alias
            n = 2
            while self.db.scalar(select(Credential).where(Credential.alias == unique)):
                unique = f"{alias}-{n}"
                n += 1
            cred = create_credential(
                self.db, self.cipher, alias=unique, kind="password", secret=password, username=username,
                notes=f"从旧版导入：{name}", legacy_source=SOURCE, legacy_id=f"{kind}:{m.legacy_id}",
            )
            self.db.flush()
            m.credential_id = cred.id
            return cred, "created"
        try:
            same = reveal_secret(cred, self.cipher) == password and cred.username == username
        except DecryptionError:
            same = False
        if same:
            return cred, "unchanged"
        if _aware(cred.updated_at) > _aware(m.imported_at):
            return cred, "conflict"
        set_secret(cred, self.cipher, password)
        cred.username = username
        return cred, "updated"

    # -- 资产 --

    def sync_asset(
        self,
        kind: str,
        record: dict[str, Any],
        fields: dict[str, Any],
        endpoint: dict[str, Any],
        cred_alias: str,
        username: str,
    ) -> Asset | None:
        legacy_id = str(record.get("id") or "").strip()
        name = fields["name"]
        if not legacy_id:
            self.report.add(kind, "-", name, "skipped", "旧记录没有 id")
            return None
        m = self.mapping(kind, legacy_id)
        password = str(record.get("password") or "")

        if m is None:
            asset = Asset(**fields, legacy_source=f"{SOURCE}:{kind}", legacy_id=legacy_id)
            ep = ManagementEndpoint(**endpoint)
            asset.endpoints.append(ep)
            self.db.add(asset)
            self.db.flush()
            m = self.new_mapping(kind, legacy_id, record, "asset")
            m.target_id, m.endpoint_id = asset.id, ep.id
            cred, _ = self.sync_credential(m, alias=cred_alias, username=username, password=password, name=name, kind=kind)
            ep.credential_id = cred.id if cred else None
            self.touch(m, record)
            m.target_revision = asset.revision
            msg = "" if password else "旧配置未填写密码，未创建凭据"
            self.report.add(kind, legacy_id, name, "created", msg)
            self.asset_by_legacy[legacy_id] = asset
            return asset

        asset = self.db.get(Asset, m.target_id) if m.target_id else None
        if asset is None:
            self.report.add(kind, legacy_id, name, "skipped", "该资产已在新平台删除，不再重建")
            return None
        if asset.deleted_at is not None:
            self.report.add(kind, legacy_id, name, "skipped", "该资产在新平台的回收站里，未更新")
            return None
        self.asset_by_legacy[legacy_id] = asset

        cred, cred_state = self.sync_credential(m, alias=cred_alias, username=username, password=password, name=name, kind=kind)
        changed = m.payload_hash != _hash(record)
        notes: list[str] = []
        action = "unchanged"
        if changed:
            if asset.revision != m.target_revision:
                action = "conflict"
                notes.append("旧配置有变化，但该资产导入后已在新平台修改，未覆盖")
            else:
                for key, value in fields.items():
                    setattr(asset, key, value)
                ep = self.db.get(ManagementEndpoint, m.endpoint_id) if m.endpoint_id else None
                if ep is not None:
                    for key, value in endpoint.items():
                        setattr(ep, key, value)
                asset.revision += 1
                action = "updated"
        if cred_state == "conflict":
            action = "conflict"
            notes.append("旧配置的密码有变化，但该凭据已在新平台修改，未覆盖")
        elif cred_state in ("created", "updated") and action == "unchanged":
            action = "updated"
        if cred is not None and m.endpoint_id:
            ep = self.db.get(ManagementEndpoint, m.endpoint_id)
            if ep is not None and ep.credential_id is None:
                ep.credential_id = cred.id
        if action != "conflict":
            self.touch(m, record)
            m.target_revision = asset.revision
        self.report.add(kind, legacy_id, name, action, "；".join(notes))
        return asset

    def hardware_node(self, rec: dict[str, Any]) -> None:
        # 旧版把空品牌当作 dell（config_manager.add_server 的默认值），这里保持一致
        brand = str(rec.get("brand") or "").strip().lower() or "dell"
        vendor, platform = BRAND_PLATFORM.get(brand, (brand.capitalize(), f"{brand}-bmc" if brand else ""))
        ip = str(rec.get("ip") or "").strip()
        lid = str(rec.get("id") or "")
        fields = {
            "name": str(rec.get("name") or f"硬件节点 {lid}").strip()[:200],
            "form": "physical",
            "role": "server",
            "platform": platform,
            "vendor": vendor[:100],
            "model": str(rec.get("model") or "")[:100],
            "serial_no": str(rec.get("serial") or "")[:100],
            "lifecycle": "active" if rec.get("enabled", True) else "maintenance",
        }
        endpoint = {"address": ip or "未填写", "port": 623, "protocol": "ipmi", "purpose": "management", "tls_verify": True}
        self.sync_asset("hardware_node", rec, fields, endpoint, f"旧版-{lid}-ipmi", str(rec.get("user") or ""))

    def system_server(self, rec: dict[str, Any]) -> None:
        lid = str(rec.get("id") or "")
        os_name = str(rec.get("os_name") or "")
        bound = str(rec.get("node_id") or "").strip()
        try:
            port = int(rec.get("port") or 22)
        except (TypeError, ValueError):
            port = 22
        fields = {
            "name": str(rec.get("name") or f"系统服务器 {lid}").strip()[:200],
            # 绑定硬件节点的是该物理机上的系统；未绑定的无法判断，按虚拟机导入，导入后可改
            "form": "physical" if bound else "vm",
            "role": "server",
            "platform": _platform_from_os(os_name)[:64],
            "notes": f"系统：{os_name}" if os_name else "",
            "lifecycle": "active" if rec.get("enabled", True) else "maintenance",
        }
        endpoint = {
            "address": str(rec.get("host") or "未填写").strip(),
            "port": port if 1 <= port <= 65535 else 22,
            "protocol": "ssh",
            "purpose": "monitoring",
            "tls_verify": True,
        }
        asset = self.sync_asset("system_server", rec, fields, endpoint, f"旧版-{lid}-ssh", str(rec.get("username") or ""))
        if asset is None:
            return
        if not bound:
            self.report.warnings.append(f"Linux 服务器“{fields['name']}”未绑定硬件节点，按虚拟机导入，请确认形态")
            return
        host = self.asset_by_legacy.get(bound)
        if host is None:
            self.report.warnings.append(f"Linux 服务器“{fields['name']}”绑定的硬件节点 {bound} 不存在，未建立关系")
            return
        exists = self.db.scalar(
            select(AssetRelation).where(
                AssetRelation.source_asset_id == host.id, AssetRelation.target_asset_id == asset.id, AssetRelation.kind == "hosts"
            )
        )
        if exists is None:
            self.db.add(AssetRelation(source_asset_id=host.id, target_asset_id=asset.id, kind="hosts", origin="legacy"))

    def user(self, username: str, rec: dict[str, Any]) -> None:
        name = str(rec.get("username") or username)
        stored_hash = str(rec.get("password_hash") or "")
        if not is_valid_password_hash(stored_hash):
            self.report.add("user", username, name, "skipped", "密码哈希格式无法识别，未导入，请在新平台重新创建")
            return
        role = str(rec.get("role") or "admin")
        if role not in USER_ROLES:
            self.report.warnings.append(f"用户 {name} 的角色“{role}”不在三个角色内，按只读导入")
            role = "viewer"
        values = {
            "password_hash": stored_hash,
            "role": role,
            "display_name": str(rec.get("display_name") or "")[:200],
            "require_password_change": bool(rec.get("require_password_change", False)),
        }
        m = self.mapping("user", username)
        hashed = {**rec, "_role": role}
        if m is None:
            existing = self.db.scalar(select(User).where(User.username == name))
            if existing is not None and not self._is_untouched_bootstrap_admin(existing):
                self.report.add("user", username, name, "conflict", "新平台已有同名用户，未覆盖")
                return
            message = ""
            if existing is not None:
                message = "替换首次启动生成的初始管理员"
                user = existing
                for key, value in values.items():
                    setattr(user, key, value)
            else:
                user = User(username=name, **values)
                self.db.add(user)
            user.legacy_source, user.legacy_id = SOURCE, username
            self.db.flush()
            m = self.new_mapping("user", username, hashed, "user")
            m.target_id = user.id
            self.touch(m, hashed)
            self.report.add("user", username, name, "created", message)
            return
        user = self.db.get(User, m.target_id) if m.target_id else None
        if user is None:
            self.report.add("user", username, name, "skipped", "该用户已在新平台删除，不再重建")
            return
        if m.payload_hash == _hash(hashed) and user.password_hash == stored_hash:
            self.report.add("user", username, name, "unchanged")
            return
        if _aware(user.updated_at) > _aware(m.imported_at):
            self.report.add("user", username, name, "conflict", "旧账号有变化，但该用户导入后已在新平台修改，未覆盖")
            return
        for key, value in values.items():
            setattr(user, key, value)
        self.touch(m, hashed)
        self.report.add("user", username, name, "updated")

    def _is_untouched_bootstrap_admin(self, user: User) -> bool:
        if not user.require_password_change:
            return False
        return self.db.scalar(
            select(AuditEvent).where(AuditEvent.action == "user.bootstrap", AuditEvent.target_id == str(user.id))
        ) is not None

    def pending(self, kind: str, payload: dict[str, Any] | list[Any], name: str, message: str) -> None:
        if not payload:
            return
        m = self.mapping(kind, "global")
        if m is None:
            m = LegacyMapping(source=SOURCE, legacy_kind=kind, legacy_id="global", target_type="pending")
            self.db.add(m)
        self.touch(m, payload)
        self.report.add(kind, "global", name, "pending", message)


def run_import(
    db: Session, cipher: CredentialCipher, data: LegacyData, *, apply: bool, actor_name: str = "", filename: str = ""
) -> ImportReport:
    """执行导入。apply=False 时在同一事务里完整走一遍后回滚，报告与真实导入一致。"""
    report = ImportReport(applied=apply, warnings=list(data.problems))
    report.legacy_counts = {
        "hardware_node": len(data.hardware_nodes),
        "system_server": len(data.system_servers),
        "user": len(data.users),
    }
    imp = _Importer(db, cipher, report)
    try:
        if apply:
            imp.batch = ImportBatch(source="legacy", filename=filename, created_by=actor_name)
            db.add(imp.batch)
            db.flush()
        for rec in data.hardware_nodes:
            imp.hardware_node(rec)
        for rec in data.system_servers:
            imp.system_server(rec)
        for username, rec in data.users.items():
            imp.user(username, rec)
        imp.pending("curve_nodes", data.curve_nodes, "全局温控曲线", "温控曲线与每台服务器的温控模式在 P5 迁入，已原样保存")
        alert = {k: v for k, v in data.alert_settings.items() if k not in SECRET_FIELDS}
        imp.pending("alert_settings", alert, "告警设置", "告警规则与 webhook 在 P4 迁入；webhook 地址与密钥未保存，届时重新填写")
        db.flush()
        if apply:
            imp.batch.summary = report.counts()  # type: ignore[union-attr]
            report.batch_id = str(imp.batch.id)  # type: ignore[union-attr]
            db.add(AuditEvent(actor_name=actor_name, action="import.legacy", target_type="import_batch", target_id=report.batch_id, summary="导入旧版配置", detail={"counts": report.counts()}))
            db.commit()
        else:
            db.rollback()
    except Exception:
        db.rollback()
        raise
    return report


def main(argv: list[str]) -> int:
    from app import migrate
    from app.crypto import load_or_create_keyring
    from app.db import make_engine, make_session_factory
    from app.settings import Settings

    parser = argparse.ArgumentParser(description="把旧版 config.ini 与 users.json 导入 4.0 新库（只读旧文件）")
    parser.add_argument("--config", type=Path, help="旧版 config.ini 路径")
    parser.add_argument("--users", type=Path, help="旧版 users.json 路径")
    parser.add_argument("--apply", action="store_true", help="确认写入；不加时只预检")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    parser.add_argument("--if-empty", action="store_true", help="库里已有资产时什么都不做（本地预览自动填充用）")
    parser.add_argument("--brief", action="store_true", help="只输出一行汇总")
    args = parser.parse_args(argv)
    if args.config is None and args.users is None:
        parser.error("至少指定 --config 或 --users")
    for p in (args.config, args.users):
        if p is not None and not p.is_file():
            parser.error(f"找不到文件：{p}")

    settings = Settings.from_env()
    engine = make_engine(settings.database_url)
    try:
        migrate.upgrade(engine)
        cipher = CredentialCipher(load_or_create_keyring(settings.master_key_file, data_dir=settings.data_dir))
        data = read_legacy(args.config, args.users)
        with make_session_factory(engine)() as db:
            if args.if_empty and db.scalar(select(func.count()).select_from(Asset)):
                print("库里已有资产，跳过旧数据导入")
                return 0
            names = ", ".join(p.name for p in (args.config, args.users) if p is not None)
            report = run_import(db, cipher, data, apply=args.apply, actor_name="legacy_import", filename=names)
    finally:
        engine.dispose()
    if args.json:
        # JSON 给脚本读，固定 UTF-8，不跟随 Windows 控制台代码页
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    elif args.brief:
        print(report.brief())
    else:
        print(report.to_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
