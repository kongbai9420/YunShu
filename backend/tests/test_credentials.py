from __future__ import annotations

import base64
import stat
import sys
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app import keytool
from app.crypto import (
    CredentialCipher,
    DecryptionError,
    KeyFileError,
    KeyRing,
    default_key_path,
    export_keyring,
    load_or_create_keyring,
)
from app.models import Credential
from app.services.credentials import create_credential, reveal_secret, set_secret

FAKE_SECRET = "fake-secret-for-tests-7f3a"


@pytest.fixture
def cipher() -> CredentialCipher:
    return CredentialCipher(KeyRing.generate())


def test_roundtrip(session: Session, cipher: CredentialCipher) -> None:
    cred = create_credential(session, cipher, alias="bmc-default", kind="password", secret=FAKE_SECRET, username="u")
    session.commit()
    session.expire_all()

    loaded = session.get(Credential, cred.id)
    assert loaded is not None
    assert reveal_secret(loaded, cipher) == FAKE_SECRET
    assert loaded.key_id == cipher.current_key_id


def test_database_file_has_no_plaintext(session: Session, engine: Engine, cipher: CredentialCipher) -> None:
    create_credential(session, cipher, alias="a", kind="password", secret=FAKE_SECRET)
    session.commit()
    engine.dispose()  # 关闭连接让 WAL 合并回主文件

    db_file = Path(engine.url.database)
    for f in (db_file, db_file.with_name(db_file.name + "-wal")):
        if f.exists():
            assert FAKE_SECRET.encode() not in f.read_bytes()


def test_nonce_differs_each_time(session: Session, cipher: CredentialCipher) -> None:
    a = create_credential(session, cipher, alias="a", kind="password", secret=FAKE_SECRET)
    b = create_credential(session, cipher, alias="b", kind="password", secret=FAKE_SECRET)
    assert a.secret_nonce != b.secret_nonce
    assert a.secret_ciphertext != b.secret_ciphertext


def test_ciphertext_bound_to_row(session: Session, cipher: CredentialCipher) -> None:
    a = create_credential(session, cipher, alias="a", kind="password", secret=FAKE_SECRET)
    b = create_credential(session, cipher, alias="b", kind="password", secret="other-fake")
    b.secret_ciphertext, b.secret_nonce = a.secret_ciphertext, a.secret_nonce
    with pytest.raises(DecryptionError):
        reveal_secret(b, cipher)


def test_tampered_ciphertext_rejected(session: Session, cipher: CredentialCipher) -> None:
    cred = create_credential(session, cipher, alias="a", kind="password", secret=FAKE_SECRET)
    cred.secret_ciphertext = bytes([cred.secret_ciphertext[0] ^ 1]) + cred.secret_ciphertext[1:]
    with pytest.raises(DecryptionError):
        reveal_secret(cred, cipher)


def test_wrong_key_reports_missing_key_id(session: Session, cipher: CredentialCipher) -> None:
    cred = create_credential(session, cipher, alias="a", kind="password", secret=FAKE_SECRET)
    other = CredentialCipher(KeyRing.generate())
    with pytest.raises(DecryptionError, match=cred.key_id):
        reveal_secret(cred, other)


def test_set_secret_replaces(session: Session, cipher: CredentialCipher) -> None:
    cred = create_credential(session, cipher, alias="a", kind="password", secret=FAKE_SECRET)
    set_secret(cred, cipher, "new-fake-secret")
    assert reveal_secret(cred, cipher) == "new-fake-secret"


def test_unknown_kind_rejected(session: Session, cipher: CredentialCipher) -> None:
    with pytest.raises(ValueError):
        create_credential(session, cipher, alias="a", kind="plaintext", secret=FAKE_SECRET)


def test_key_file_created_once_and_reused(tmp_path: Path) -> None:
    path = tmp_path / "keys" / "master.key"
    first = load_or_create_keyring(path, data_dir=tmp_path / "data")
    second = load_or_create_keyring(path, data_dir=tmp_path / "data")
    assert first.current_id == second.current_id
    assert first.keys == second.keys
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_key_file_inside_data_dir_refused(tmp_path: Path) -> None:
    with pytest.raises(KeyFileError):
        load_or_create_keyring(tmp_path / "data" / "master.key", data_dir=tmp_path / "data")
    assert not (tmp_path / "data" / "master.key").exists()


def test_missing_key_file_without_create(tmp_path: Path) -> None:
    with pytest.raises(KeyFileError):
        load_or_create_keyring(tmp_path / "master.key", create=False)


def test_corrupt_key_file_does_not_leak_content(tmp_path: Path) -> None:
    path = tmp_path / "master.key"
    path.write_text('{"version": 1, "current": "x", "keys": {"x": "AAAA"}}', encoding="utf-8")
    with pytest.raises(KeyFileError) as exc:
        load_or_create_keyring(path)
    assert "AAAA" not in str(exc.value)


def test_export_does_not_overwrite(tmp_path: Path) -> None:
    src = tmp_path / "master.key"
    ring = load_or_create_keyring(src)
    dest = tmp_path / "backup.key"
    assert export_keyring(src, dest) == ring.current_id
    assert load_or_create_keyring(dest, create=False).keys == ring.keys
    with pytest.raises(FileExistsError):
        export_keyring(src, dest)


def test_default_key_path_env_override(tmp_path: Path) -> None:
    assert default_key_path({"YUNSHU_MASTER_KEY_FILE": str(tmp_path / "k")}) == tmp_path / "k"


def test_keytool_never_prints_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    key_path = tmp_path / "keys" / "master.key"
    monkeypatch.setenv("YUNSHU_MASTER_KEY_FILE", str(key_path))
    monkeypatch.setenv("YUNSHU_DATA_DIR", str(tmp_path / "data"))

    assert keytool.main(["init"]) == 0
    assert keytool.main(["fingerprint"]) == 0
    assert keytool.main(["export", str(tmp_path / "out.key")]) == 0
    out = capsys.readouterr().out

    ring = load_or_create_keyring(key_path, create=False)
    for key in ring.keys.values():
        assert base64.b64encode(key).decode() not in out
        assert key.hex() not in out
    assert ring.current_id in out
