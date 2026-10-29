"""凭据加密。

- 算法：AES-256-GCM，每条记录随机 12 字节 nonce；附加数据（AAD）绑定凭据 UUID，
  密文换到别的行无法解密。
- 主密钥：独立的密钥文件，不与数据库放在同一目录；首次启动自动生成，权限仅属主可读写。
- 密钥文件可容纳多把密钥，每条密文记录 key_id，为以后轮换留出余地。
- 本模块任何地方都不打印或记录密钥与明文。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12
KEY_FILE_VERSION = 1


class KeyFileError(RuntimeError):
    pass


def key_fingerprint(key: bytes) -> str:
    """密钥编号：密钥 SHA-256 的前 16 位十六进制，可公开显示，不能反推密钥。"""
    return hashlib.sha256(key).hexdigest()[:16]


def default_key_path(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    explicit = env.get("YUNSHU_MASTER_KEY_FILE")
    if explicit:
        return Path(explicit)
    if Path("/.dockerenv").exists():
        return Path("/etc/yunshu/master.key")
    if sys.platform == "win32":
        base = env.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "yunshu" / "master.key"
    base = env.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "yunshu" / "master.key"


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True


@dataclass
class KeyRing:
    current_id: str
    keys: dict[str, bytes] = field(default_factory=dict)

    @classmethod
    def generate(cls) -> KeyRing:
        key = secrets.token_bytes(KEY_BYTES)
        kid = key_fingerprint(key)
        return cls(current_id=kid, keys={kid: key})

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": KEY_FILE_VERSION,
                "current": self.current_id,
                "keys": {kid: base64.b64encode(k).decode("ascii") for kid, k in self.keys.items()},
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> KeyRing:
        try:
            data = json.loads(text)
            if data.get("version") != KEY_FILE_VERSION:
                raise KeyFileError("密钥文件版本不受支持")
            keys = {kid: base64.b64decode(v, validate=True) for kid, v in data["keys"].items()}
            current = data["current"]
        except KeyFileError:
            raise
        except Exception as exc:  # noqa: BLE001 - 不把文件内容带进异常信息
            raise KeyFileError("密钥文件格式错误") from exc
        for kid, key in keys.items():
            if len(key) != KEY_BYTES or key_fingerprint(key) != kid:
                raise KeyFileError("密钥文件内容与密钥编号不一致")
        if current not in keys:
            raise KeyFileError("密钥文件缺少当前密钥")
        return cls(current_id=current, keys=keys)


log = logging.getLogger(__name__)

# Windows 上主密钥文件只留给：当前账号（服务运行时就是服务账号）、SYSTEM、Administrators
_WIN_SYSTEM, _WIN_ADMINS = "S-1-5-18", "S-1-5-32-544"


def _current_sid() -> str:
    out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, text=True, timeout=10, check=True)
    return out.stdout.strip().split(",")[-1].strip('"')


def _restrict_windows(path: Path) -> None:
    """Windows 没有 0600：去掉继承来的权限，只授权给上面三者。失败只警告，不阻止启动（桌面版的 %APPDATA% 本来就只有本人可读）。"""
    try:
        sid = _current_sid()
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                        f"*{sid}:(F)", f"*{_WIN_SYSTEM}:(F)", f"*{_WIN_ADMINS}:(F)"],
                       capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError, IndexError) as exc:
        log.warning("没能收紧主密钥文件 %s 的访问权限（%s），请手动只允许运行账号读取", path, type(exc).__name__)


def _write_owner_only(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    if sys.platform == "win32":
        _restrict_windows(path)
    else:
        os.chmod(path, 0o600)


def load_or_create_keyring(path: Path, *, data_dir: Path | None = None, create: bool = True) -> KeyRing:
    """读取密钥文件；不存在且 create=True 时生成新密钥。

    密钥文件位于数据库数据目录内时拒绝启动：主密钥不得与数据库同存。
    """
    if data_dir is not None and _is_within(path, data_dir):
        # 例如 Linux 服务账号的家目录就是数据目录时，默认的 ~/.config/yunshu 会落在数据目录里
        raise KeyFileError(f"主密钥文件 {path} 在数据目录 {data_dir} 里（主密钥不能与数据库同存），"
                           "请把 YUNSHU_MASTER_KEY_FILE 设到数据目录以外，例如 /etc/yunshu/master.key")
    if path.exists():
        return KeyRing.from_json(path.read_text(encoding="utf-8"))
    if not create:
        raise KeyFileError("找不到主密钥文件")
    ring = KeyRing.generate()
    _write_owner_only(path, ring.to_json())
    return ring


def export_keyring(src: Path, dest: Path) -> str:
    """把密钥文件复制到 dest（不得已存在），返回当前密钥编号。供用户单独备份。"""
    ring = KeyRing.from_json(src.read_text(encoding="utf-8"))
    _write_owner_only(dest, ring.to_json())
    return ring.current_id


class DecryptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class SealedSecret:
    ciphertext: bytes
    nonce: bytes
    key_id: str


class CredentialCipher:
    def __init__(self, ring: KeyRing) -> None:
        self._ring = ring

    @property
    def current_key_id(self) -> str:
        return self._ring.current_id

    def seal(self, plaintext: str, *, aad: bytes) -> SealedSecret:
        nonce = secrets.token_bytes(NONCE_BYTES)
        key = self._ring.keys[self._ring.current_id]
        ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), aad)
        return SealedSecret(ciphertext=ct, nonce=nonce, key_id=self._ring.current_id)

    def open(self, sealed: SealedSecret, *, aad: bytes) -> str:
        key = self._ring.keys.get(sealed.key_id)
        if key is None:
            raise DecryptionError(f"缺少编号为 {sealed.key_id} 的主密钥，无法解密；请恢复对应的密钥文件")
        try:
            return AESGCM(key).decrypt(sealed.nonce, sealed.ciphertext, aad).decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            raise DecryptionError("凭据解密失败：密文损坏或与记录不匹配") from exc
