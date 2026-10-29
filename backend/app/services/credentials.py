"""凭据的保存与读取。明文只在 reveal_secret 里出现，供采集与控制适配器内部使用，
不经过任何普通查询接口返回。"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.crypto import CredentialCipher, SealedSecret
from app.models import Credential

CREDENTIAL_KINDS = frozenset({"password", "snmp_community", "snmpv3", "ssh_key", "token"})


def _aad(credential_id: uuid.UUID) -> bytes:
    return b"yunshu.credential:" + credential_id.bytes


def create_credential(
    session: Session,
    cipher: CredentialCipher,
    *,
    alias: str,
    kind: str,
    secret: str,
    username: str = "",
    notes: str = "",
    legacy_source: str | None = None,
    legacy_id: str | None = None,
) -> Credential:
    if kind not in CREDENTIAL_KINDS:
        raise ValueError(f"不支持的凭据类型：{kind}")
    cred_id = uuid.uuid4()
    sealed = cipher.seal(secret, aad=_aad(cred_id))
    cred = Credential(
        id=cred_id,
        alias=alias,
        kind=kind,
        username=username,
        secret_ciphertext=sealed.ciphertext,
        secret_nonce=sealed.nonce,
        key_id=sealed.key_id,
        notes=notes,
        legacy_source=legacy_source,
        legacy_id=legacy_id,
    )
    session.add(cred)
    return cred


def set_secret(cred: Credential, cipher: CredentialCipher, secret: str) -> None:
    sealed = cipher.seal(secret, aad=_aad(cred.id))
    cred.secret_ciphertext = sealed.ciphertext
    cred.secret_nonce = sealed.nonce
    cred.key_id = sealed.key_id


def reveal_secret(cred: Credential, cipher: CredentialCipher) -> str:
    sealed = SealedSecret(ciphertext=cred.secret_ciphertext, nonce=cred.secret_nonce, key_id=cred.key_id)
    return cipher.open(sealed, aad=_aad(cred.id))
