from __future__ import annotations

import configparser
import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import legacy_import
from app.auth import hash_password, verify_password
from app.bootstrap import ensure_initial_admin
from app.crypto import CredentialCipher, KeyRing
from app.legacy_import import read_legacy, run_import
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
from app.services.credentials import reveal_secret

FAKE_BMC_PW = "fake-bmc-pw-0001"
FAKE_SSH_PW = "fake-ssh-pw-0002"
FAKE_WEBHOOK = "https://hooks.example.invalid/fake-token-0003"

HARDWARE = [
    {"id": "srv_a", "name": "Dell 节点", "ip": "192.0.2.101", "user": "fakeuser", "password": FAKE_BMC_PW, "brand": "dell", "model": "PowerEdge R730xd", "serial": "SN-FAKE-1", "mode": "dynamic", "manual_speed": 25, "preset_key": "silent", "enabled": True},
    {"id": "srv_b", "name": "浪潮 节点", "ip": "192.0.2.102", "user": "fakeuser", "password": "", "brand": "inspur", "model": "NF5280M5", "serial": "", "mode": "auto", "enabled": False},
]
SYSTEM = [
    {"id": "sys_pve", "name": "PVE 母机", "host": "192.0.2.201", "port": 22, "username": "fakeroot", "password": FAKE_SSH_PW, "node_id": "srv_a", "os_name": "Proxmox VE 8.1", "enabled": True},
    {"id": "sys_ubuntu", "name": "独立应用服务器", "host": "192.0.2.202", "port": 2222, "username": "fakeroot", "password": FAKE_SSH_PW, "node_id": "", "os_name": "Ubuntu 22.04 LTS", "enabled": True},
]
CURVE = [{"temp": 45, "speed": 15, "name": "静音基准"}, {"temp": 82, "speed": 100, "is_safety": True, "name": "熔断"}]


def write_config(path: Path, hardware=HARDWARE, system=SYSTEM) -> Path:  # type: ignore[no-untyped-def]
    cp = configparser.ConfigParser(interpolation=None)
    cp["ipmi"] = {"curve_nodes_json": json.dumps(CURVE, ensure_ascii=False), "password": FAKE_BMC_PW}
    cp["cluster"] = {"servers_json": json.dumps(hardware, ensure_ascii=False), "system_servers_json": json.dumps(system, ensure_ascii=False)}
    cp["alert"] = {"enabled": "1", "webhook_url": FAKE_WEBHOOK, "webhook_secret": "fake-secret-0004", "node_cpu_temp_threshold": "80"}
    with path.open("w", encoding="utf-8") as fh:
        cp.write(fh)
    return path


def write_users(path: Path, users: dict | None = None) -> Path:  # type: ignore[type-arg]
    users = users or {
        "admin": {"username": "admin", "display_name": "管理员", "role": "admin", "password_hash": hash_password("fake-admin-pw"), "require_password_change": False},
        "ops": {"username": "ops", "display_name": "值班", "role": "operator", "password_hash": hash_password("fake-ops-pw")},
    }
    path.write_text(json.dumps(users, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(KeyRing.generate())


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    return write_config(tmp_path / "config.ini"), write_users(tmp_path / "users.json")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def do(session: Session, cipher: CredentialCipher, files: tuple[Path, Path], apply: bool = True):  # type: ignore[no-untyped-def]
    return run_import(session, cipher, read_legacy(*files), apply=apply, actor_name="test")


def test_dry_run_writes_nothing(session: Session, cipher: CredentialCipher, files: tuple[Path, Path]) -> None:
    before = [digest(p) for p in files]
    report = do(session, cipher, files, apply=False)
    assert report.counts()["hardware_node"] == {"created": 2}
    assert report.counts()["system_server"] == {"created": 2}
    assert report.counts()["user"] == {"created": 2}
    assert session.scalar(select(Asset)) is None
    assert session.scalar(select(ImportBatch)) is None
    assert session.scalar(select(LegacyMapping)) is None
    assert [digest(p) for p in files] == before


def test_apply_imports_everything(session: Session, cipher: CredentialCipher, files: tuple[Path, Path]) -> None:
    before = [digest(p) for p in files]
    report = do(session, cipher, files)
    assert report.applied and report.batch_id
    assert [digest(p) for p in files] == before  # 旧文件不改

    assets = {a.legacy_id: a for a in session.scalars(select(Asset))}
    assert set(assets) == {"srv_a", "srv_b", "sys_pve", "sys_ubuntu"}
    a = assets["srv_a"]
    assert (a.form, a.role, a.vendor, a.platform, a.model, a.serial_no) == ("physical", "server", "Dell", "dell-idrac", "PowerEdge R730xd", "SN-FAKE-1")
    assert assets["srv_b"].lifecycle == "maintenance"
    assert assets["sys_pve"].form == "physical" and assets["sys_pve"].platform == "proxmox"
    assert assets["sys_ubuntu"].form == "vm" and assets["sys_ubuntu"].platform == "ubuntu"

    ep = session.scalar(select(ManagementEndpoint).where(ManagementEndpoint.asset_id == a.id))
    assert (ep.protocol, ep.address, ep.port) == ("ipmi", "192.0.2.101", 623)
    cred = session.get(Credential, ep.credential_id)
    assert reveal_secret(cred, cipher) == FAKE_BMC_PW
    assert cred.username == "fakeuser"
    # 没填密码的节点不建凭据
    ep_b = session.scalar(select(ManagementEndpoint).where(ManagementEndpoint.asset_id == assets["srv_b"].id))
    assert ep_b.credential_id is None
    ssh = session.scalar(select(ManagementEndpoint).where(ManagementEndpoint.asset_id == assets["sys_ubuntu"].id))
    assert (ssh.protocol, ssh.port) == ("ssh", 2222)

    rel = session.scalars(select(AssetRelation)).all()
    assert [(r.source_asset_id, r.target_asset_id, r.kind) for r in rel] == [(a.id, assets["sys_pve"].id, "hosts")]

    users = {u.username: u for u in session.scalars(select(User))}
    assert users["ops"].role == "operator"
    assert verify_password("fake-ops-pw", users["ops"].password_hash)

    pending = {m.legacy_kind: m for m in session.scalars(select(LegacyMapping).where(LegacyMapping.target_type == "pending"))}
    assert pending["curve_nodes"].payload["items"] == CURVE
    assert "webhook_url" not in pending["alert_settings"].payload
    assert "webhook_secret" not in pending["alert_settings"].payload
    assert session.scalar(select(AuditEvent).where(AuditEvent.action == "import.legacy")) is not None


def test_no_secret_anywhere_outside_ciphertext(session: Session, cipher: CredentialCipher, files: tuple[Path, Path]) -> None:
    report = do(session, cipher, files)
    blobs = [report.to_text(), json.dumps(report.to_dict(), ensure_ascii=False)]
    blobs += [json.dumps(m.payload, ensure_ascii=False) for m in session.scalars(select(LegacyMapping))]
    blobs += [json.dumps(e.detail, ensure_ascii=False) for e in session.scalars(select(AuditEvent))]
    for text in blobs:
        for secret in (FAKE_BMC_PW, FAKE_SSH_PW, FAKE_WEBHOOK, "fake-secret-0004", "fakeuser", "fakeroot"):
            assert secret not in text


def test_rerun_is_idempotent(session: Session, cipher: CredentialCipher, files: tuple[Path, Path]) -> None:
    do(session, cipher, files)
    report = do(session, cipher, files)
    counts = report.counts()
    assert counts["hardware_node"] == {"unchanged": 2}
    assert counts["system_server"] == {"unchanged": 2}
    assert counts["user"] == {"unchanged": 2}
    assert len(session.scalars(select(Asset)).all()) == 4
    assert len(session.scalars(select(Credential)).all()) == 3
    assert len(session.scalars(select(AssetRelation)).all()) == 1


def test_changed_legacy_record_updates(session: Session, cipher: CredentialCipher, files: tuple[Path, Path], tmp_path: Path) -> None:
    do(session, cipher, files)
    changed = [dict(HARDWARE[0], model="PowerEdge R740", password="fake-new-bmc-pw"), HARDWARE[1]]
    report = do(session, cipher, (write_config(tmp_path / "config2.ini", hardware=changed), files[1]))
    assert report.counts()["hardware_node"] == {"updated": 1, "unchanged": 1}
    asset = session.scalar(select(Asset).where(Asset.legacy_id == "srv_a"))
    assert asset.model == "PowerEdge R740"
    assert asset.revision == 2
    ep = session.scalar(select(ManagementEndpoint).where(ManagementEndpoint.asset_id == asset.id))
    assert reveal_secret(session.get(Credential, ep.credential_id), cipher) == "fake-new-bmc-pw"


def test_user_edits_are_not_overwritten(session: Session, cipher: CredentialCipher, files: tuple[Path, Path], tmp_path: Path) -> None:
    do(session, cipher, files)
    asset = session.scalar(select(Asset).where(Asset.legacy_id == "srv_a"))
    asset.name = "用户改过的名字"
    asset.revision += 1
    session.commit()

    changed = [dict(HARDWARE[0], model="PowerEdge R740"), HARDWARE[1]]
    report = do(session, cipher, (write_config(tmp_path / "config2.ini", hardware=changed), files[1]))
    assert report.counts()["hardware_node"]["conflict"] == 1
    session.refresh(asset)
    assert asset.name == "用户改过的名字"
    assert asset.model == "PowerEdge R730xd"


def test_deleted_asset_not_recreated(session: Session, cipher: CredentialCipher, files: tuple[Path, Path]) -> None:
    do(session, cipher, files)
    session.delete(session.scalar(select(Asset).where(Asset.legacy_id == "srv_b")))
    session.commit()
    report = do(session, cipher, files)
    assert report.counts()["hardware_node"] == {"unchanged": 1, "skipped": 1}
    assert session.scalar(select(Asset).where(Asset.legacy_id == "srv_b")) is None


def test_bootstrap_admin_replaced_but_real_user_kept(session: Session, cipher: CredentialCipher, files: tuple[Path, Path]) -> None:
    ensure_initial_admin(session)
    report = do(session, cipher, files)
    admin = session.scalar(select(User).where(User.username == "admin"))
    assert verify_password("fake-admin-pw", admin.password_hash)
    assert admin.require_password_change is False
    assert any(i.legacy_id == "admin" and i.action == "created" for i in report.items)


def test_existing_same_name_user_is_conflict(session: Session, cipher: CredentialCipher, files: tuple[Path, Path]) -> None:
    session.add(User(username="ops", role="viewer", password_hash=hash_password("fake-local-pw")))
    session.commit()
    report = do(session, cipher, files)
    assert any(i.legacy_id == "ops" and i.action == "conflict" for i in report.items)
    ops = session.scalar(select(User).where(User.username == "ops"))
    assert ops.role == "viewer"


def test_unknown_role_and_bad_hash(session: Session, cipher: CredentialCipher, files: tuple[Path, Path], tmp_path: Path) -> None:
    users = write_users(tmp_path / "u2.json", {
        "guest": {"username": "guest", "role": "superuser", "password_hash": hash_password("fake")},
        "broken": {"username": "broken", "role": "admin", "password_hash": "md5$abc"},
    })
    report = do(session, cipher, (files[0], users))
    assert session.scalar(select(User).where(User.username == "guest")).role == "viewer"
    assert session.scalar(select(User).where(User.username == "broken")) is None
    assert any("superuser" in w for w in report.warnings)
    assert any(i.legacy_id == "broken" and i.action == "skipped" for i in report.items)


def test_bad_json_reported(tmp_path: Path) -> None:
    path = tmp_path / "config.ini"
    path.write_text("[cluster]\nservers_json = {not json\n", encoding="utf-8")
    data = read_legacy(path, None)
    assert data.hardware_nodes == []
    assert data.problems


def test_cli_dry_run(tmp_path: Path, files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("YUNSHU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("YUNSHU_MASTER_KEY_FILE", str(tmp_path / "keys" / "master.key"))
    before = [digest(p) for p in files]
    assert legacy_import.main(["--config", str(files[0]), "--users", str(files[1])]) == 0
    out = capsys.readouterr().out
    assert "预检" in out and "物理服务器：旧配置 2 条" in out
    assert FAKE_BMC_PW not in out and "192.0.2.101" not in out
    assert [digest(p) for p in files] == before

    assert legacy_import.main(["--config", str(files[0]), "--users", str(files[1]), "--apply", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["applied"] is True
    assert report["counts"]["hardware_node"] == {"created": 2}


def test_cli_if_empty_and_brief(tmp_path: Path, files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("YUNSHU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("YUNSHU_MASTER_KEY_FILE", str(tmp_path / "keys" / "master.key"))
    monkeypatch.delenv("YUNSHU_DATABASE_URL", raising=False)
    assert legacy_import.main(["--config", str(files[0]), "--apply", "--if-empty", "--brief"]) == 0
    first = capsys.readouterr().out.strip()
    assert first.startswith("已从旧配置导入：物理服务器 2 台，Linux 服务器 2 台")
    assert "用户" not in first and "192.0.2" not in first
    assert legacy_import.main(["--config", str(files[0]), "--apply", "--if-empty", "--brief"]) == 0
    assert "已有资产，跳过" in capsys.readouterr().out



def test_blank_brand_imports_as_dell_like_legacy(session: Session, cipher: CredentialCipher, tmp_path: Path) -> None:
    hw = [dict(HARDWARE[0], brand=""), HARDWARE[1]]
    run_import(session, cipher, read_legacy(write_config(tmp_path / "c.ini", hardware=hw), None), apply=True)
    a = session.scalar(select(Asset).where(Asset.legacy_id == "srv_a"))
    assert (a.vendor, a.platform) == ("Dell", "dell-idrac")
