"""OIDC 单点登录（4.2 P16，可选）。只用 httpx 与 cryptography，不新增依赖。

部署用环境变量开启（客户端密钥不进数据库）：
    YUNSHU_OIDC_ISSUER            身份提供方地址，如 https://sso.example.internal/realms/ops
    YUNSHU_OIDC_CLIENT_ID / YUNSHU_OIDC_CLIENT_SECRET
    YUNSHU_OIDC_REDIRECT_URL      本平台回调地址：https://<平台地址>/api/auth/oidc/callback
    YUNSHU_OIDC_LABEL             登录页按钮文字（默认“单点登录”）
    YUNSHU_OIDC_ADMIN_GROUPS / YUNSHU_OIDC_OPERATOR_GROUPS   逗号分隔；groups 声明里有其一即为该角色，否则只读
    YUNSHU_OIDC_SCOPE_ALL         新建账号是否可见全部资产，默认 0（什么都看不到，等管理员设置范围）

规则：
- 授权码流程 + PKCE + state + nonce；ID 令牌校验签名（RS256 / ES256，JWKS）、iss、aud、exp、nonce。
- 账号按身份提供方的 subject 识别（auth_source=oidc），不按用户名关联已有本地账号：同名本地账号存在时拒绝，
  避免冒用。角色每次登录按 groups 同步；资产范围由管理员在平台里设置（P11）。
- 单点登录账号不能用密码登录；本地 admin 始终可用，作为应急账号。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

CLOCK_SKEW = 60


class OidcError(ValueError):
    pass


@dataclass(frozen=True)
class OidcConfig:
    issuer: str
    client_id: str
    client_secret: str
    redirect_url: str
    label: str = "单点登录"
    admin_groups: frozenset[str] = frozenset()
    operator_groups: frozenset[str] = frozenset()
    scope_all: bool = False

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> OidcConfig | None:
        e = os.environ if env is None else env
        issuer, client_id, redirect = (e.get(k, "").strip() for k in ("YUNSHU_OIDC_ISSUER", "YUNSHU_OIDC_CLIENT_ID", "YUNSHU_OIDC_REDIRECT_URL"))
        if not (issuer and client_id and redirect):
            return None
        groups = lambda k: frozenset(g.strip() for g in e.get(k, "").split(",") if g.strip())  # noqa: E731
        return cls(issuer=issuer.rstrip("/"), client_id=client_id, client_secret=e.get("YUNSHU_OIDC_CLIENT_SECRET", ""),
                   redirect_url=redirect, label=e.get("YUNSHU_OIDC_LABEL", "").strip() or "单点登录",
                   admin_groups=groups("YUNSHU_OIDC_ADMIN_GROUPS"), operator_groups=groups("YUNSHU_OIDC_OPERATOR_GROUPS"),
                   scope_all=e.get("YUNSHU_OIDC_SCOPE_ALL", "0") == "1")

    def role_for(self, groups: list[str]) -> str:
        names = set(groups)
        if names & self.admin_groups:
            return "admin"
        if names & self.operator_groups:
            return "operator"
        return "viewer"


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass
class Provider:
    """发现文档与 JWKS 缓存（1 小时）；transport 供测试注入伪造的身份提供方。"""

    config: OidcConfig
    transport: httpx.BaseTransport | None = None
    _discovery: dict[str, Any] = field(default_factory=dict)
    _jwks: dict[str, Any] = field(default_factory=dict)
    _fetched: float = 0

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=10, transport=self.transport)

    def discovery(self) -> dict[str, Any]:
        if not self._discovery or time.monotonic() - self._fetched > 3600:
            with self._client() as c:
                doc = c.get(f"{self.config.issuer}/.well-known/openid-configuration").raise_for_status().json()
                if doc.get("issuer", "").rstrip("/") != self.config.issuer:
                    raise OidcError("发现文档的 issuer 与配置不一致")
                self._discovery = doc
                self._jwks = c.get(doc["jwks_uri"]).raise_for_status().json()
                self._fetched = time.monotonic()
        return self._discovery

    def start(self) -> tuple[str, dict[str, str]]:
        """返回跳转地址，以及要放进 HttpOnly Cookie 的 state / nonce / PKCE 校验串。"""
        doc = self.discovery()
        state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        challenge = _b64e(hashlib.sha256(verifier.encode()).digest())
        params = {"response_type": "code", "client_id": self.config.client_id, "redirect_uri": self.config.redirect_url,
                  "scope": "openid profile email groups", "state": state, "nonce": nonce,
                  "code_challenge": challenge, "code_challenge_method": "S256"}
        return f"{doc['authorization_endpoint']}?{httpx.QueryParams(params)}", {"state": state, "nonce": nonce, "verifier": verifier}

    def finish(self, code: str, flow: dict[str, str]) -> dict[str, Any]:
        doc = self.discovery()
        with self._client() as c:
            r = c.post(doc["token_endpoint"], data={
                "grant_type": "authorization_code", "code": code, "redirect_uri": self.config.redirect_url,
                "client_id": self.config.client_id, "client_secret": self.config.client_secret, "code_verifier": flow["verifier"]})
        if r.status_code >= 400:
            raise OidcError(f"身份提供方拒绝了授权码（HTTP {r.status_code}）")
        id_token = r.json().get("id_token")
        if not id_token:
            raise OidcError("身份提供方没有返回 ID 令牌")
        return self.verify(id_token, flow["nonce"])

    def verify(self, token: str, nonce: str, now: float | None = None) -> dict[str, Any]:
        try:
            head_b64, body_b64, sig_b64 = token.split(".")
            header, claims, sig = json.loads(_b64d(head_b64)), json.loads(_b64d(body_b64)), _b64d(sig_b64)
        except (ValueError, json.JSONDecodeError) as exc:
            raise OidcError("ID 令牌格式不正确") from exc
        key = next((k for k in self._jwks.get("keys", []) if k.get("kid") == header.get("kid")), None)
        if key is None:
            self._fetched = 0  # 身份提供方可能轮换了密钥：下次重新取
            raise OidcError("找不到 ID 令牌的签名密钥")
        _verify_signature(header.get("alg", ""), key, f"{head_b64}.{body_b64}".encode(), sig)
        now = time.time() if now is None else now
        aud = claims.get("aud")
        if str(claims.get("iss", "")).rstrip("/") != self.config.issuer:
            raise OidcError("ID 令牌的签发方不正确")
        if self.config.client_id not in (aud if isinstance(aud, list) else [aud]):
            raise OidcError("ID 令牌不是签发给本平台的")
        if float(claims.get("exp", 0)) < now - CLOCK_SKEW:
            raise OidcError("ID 令牌已过期")
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise OidcError("ID 令牌的 nonce 不匹配")
        if not claims.get("sub"):
            raise OidcError("ID 令牌缺少 sub")
        return claims


def _verify_signature(alg: str, jwk: dict[str, Any], data: bytes, sig: bytes) -> None:
    from cryptography.exceptions import InvalidSignature

    try:
        if alg == "RS256" and jwk.get("kty") == "RSA":
            pub = rsa.RSAPublicNumbers(int.from_bytes(_b64d(jwk["e"]), "big"), int.from_bytes(_b64d(jwk["n"]), "big")).public_key()
            pub.verify(sig, data, padding.PKCS1v15(), hashes.SHA256())
        elif alg == "ES256" and jwk.get("kty") == "EC" and jwk.get("crv") == "P-256":
            pub = ec.EllipticCurvePublicNumbers(int.from_bytes(_b64d(jwk["x"]), "big"), int.from_bytes(_b64d(jwk["y"]), "big"), ec.SECP256R1()).public_key()
            if len(sig) != 64:
                raise OidcError("ID 令牌签名长度不正确")
            pub.verify(encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")), data, ec.ECDSA(hashes.SHA256()))
        else:
            raise OidcError(f"不支持的签名算法：{alg or '无'}")
    except InvalidSignature as exc:
        raise OidcError("ID 令牌签名无效") from exc
