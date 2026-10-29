"""独立通知传输；调用方负责解密、模板渲染、持久化与重试策略。

accepted 仅表示 SMTP / HTTP 服务端接受，不保证最终送达。所有异常均脱敏。
http_transport 使用 httpx.BaseTransport；smtp_factory 与 smtplib 的构造参数一致，
SSL 构造附带 context，STARTTLS 则通过 starttls(context=...) 设置验证上下文。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from email.message import EmailMessage
import hashlib
import hmac
import json
import re
import smtplib
import ssl
import time
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx

TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 32 * 1024
WEBHOOK_KINDS = frozenset({"custom_webhook", "serverchan", "dingtalk", "feishu", "wechat_work", "bark", "pushplus"})


@dataclass(frozen=True)
class NotificationMessage:
    title: str
    text: str
    html: str = ""


class NotificationError(Exception):
    def __init__(self, message: str, retryable: bool = False, uncertain: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.uncertain = uncertain


def _https_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        if (
            parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.fragment or any(c.isspace() for c in value)
        ):
            raise ValueError
        _ = parts.port
    except (ValueError, TypeError):
        raise NotificationError("通知地址必须是有效的 HTTPS URL，不支持 URL 用户名或密码") from None
    return value


def _webhook_request(kind: str, secrets: dict, message: NotificationMessage) -> tuple[str, dict]:
    url = str(secrets.get("url") or "").strip()
    secret = str(secrets.get("secret") or "").strip()
    title, text = message.title, message.text
    kwargs: dict[str, Any] = {}
    if kind == "custom_webhook":
        payload = {"title": title, "content": text, "timestamp": int(time.time())}
    elif kind == "serverchan":
        # 旧版允许在地址或 secret 中填写 SendKey，也允许填写完整推送 URL。
        key = secret or url
        if not key:
            raise NotificationError("未配置 Server酱 SendKey 或完整地址")
        url = key if key.startswith("https://") else f"https://sctapi.ftqq.com/{quote(key, safe='')}.send"
        kwargs["data"] = {"title": title, "desp": text}
        return _https_url(url), kwargs
    elif kind == "dingtalk":
        payload = {"msgtype": "markdown", "markdown": {"title": title, "text": f"### {title}\n\n{text}"}}
        if secret:
            timestamp = str(int(time.time() * 1000))
            signature = base64.b64encode(hmac.new(secret.encode(), f"{timestamp}\n{secret}".encode(), hashlib.sha256).digest()).decode()
            parts = urlsplit(_https_url(url))
            query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in {"timestamp", "sign"}]
            url = urlunsplit(parts._replace(query=urlencode(query + [("timestamp", timestamp), ("sign", signature)])))
    elif kind == "feishu":
        payload = {"msg_type": "post", "content": {"post": {"zh_cn": {"title": title, "content": [[{"tag": "text", "text": text}]]}}}}
        if secret:
            timestamp = str(int(time.time()))
            # 飞书的 HMAC key 是 timestamp + 换行 + secret，消息为空字节。
            signature = base64.b64encode(hmac.new(f"{timestamp}\n{secret}".encode(), b"", hashlib.sha256).digest()).decode()
            payload.update(timestamp=timestamp, sign=signature)
    elif kind == "wechat_work":
        payload = {"msgtype": "markdown", "markdown": {"content": f"### {title}\n{text}"}}
    elif kind == "bark":
        key = url or secret
        if not key:
            raise NotificationError("未配置 Bark 设备 Key 或完整地址")
        url = key.rstrip("/") if key.startswith("https://") else f"https://api.day.app/{quote(key, safe='')}"
        payload = {"title": title, "body": text, "group": "云枢"}
        if secret and key.startswith("https://"):
            # 自建 Bark 使用完整 /push 地址时可将设备 Key 放在 body。
            payload["device_key"] = secret
    elif kind == "pushplus":
        token = secret or url
        if not token:
            raise NotificationError("未配置 PushPlus Token")
        url = "https://www.pushplus.plus/send"
        # 纯文本交由服务端按纯文本呈现，不把资产名称等内容拼成 HTML。
        payload = {"token": token, "title": title, "content": text, "template": "txt"}
    else:
        raise NotificationError("不支持的通知渠道")
    kwargs["json"] = payload
    return _https_url(url), kwargs


def _response_accepted(kind: str, body: bytes) -> None:
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        if kind == "custom_webhook":
            return  # 通用 Webhook 以 HTTP 2xx 作为服务端接受。
        raise NotificationError("通知服务返回格式无效，无法确认是否接受", uncertain=True) from None
    if not isinstance(data, dict):
        if kind == "custom_webhook":
            return
        raise NotificationError("通知服务返回格式无效，无法确认是否接受", uncertain=True)
    if kind == "custom_webhook":
        rejected = any(data.get(key) is False for key in ("success", "accepted", "ok")) or bool(data.get("error"))
        if rejected:
            raise NotificationError("通知服务拒绝了请求")
        return
    field, expected = {
        "dingtalk": ("errcode", 0), "wechat_work": ("errcode", 0),
        "feishu": ("code", 0), "serverchan": ("code", 0),
        "bark": ("code", 200), "pushplus": ("code", 200),
    }[kind]
    # 飞书历史响应采用 StatusCode，不能将缺字段默认为成功。
    if kind == "feishu" and field not in data and "StatusCode" in data:
        field = "StatusCode"
    code = data.get(field)
    if type(code) is not int:
        raise NotificationError("通知服务未返回明确接受状态", uncertain=True)
    if code != expected:
        raise NotificationError("通知服务拒绝了请求，请核对渠道配置或额度")


def _send_http(kind: str, secrets: dict, message: NotificationMessage, transport: Any) -> dict:
    url, request = _webhook_request(kind, secrets, message)
    try:
        # 直接使用公开的 transport 接口：不跟随重定向，也避免 Client 在 INFO
        # 日志里记录含有 Token / SendKey 的完整请求 URL。TLS 始终执行 CA 校验。
        with (transport or httpx.HTTPTransport(verify=True, retries=0, trust_env=False)) as sender:
            outgoing = httpx.Request(
                "POST", url, **request,
                headers={"User-Agent": "YunShu-Notifications/4.0", "Accept-Encoding": "identity"},
                extensions={"timeout": {key: TIMEOUT_SECONDS for key in ("connect", "read", "write", "pool")}},
            )
            response = sender.handle_request(outgoing)
            try:
                if 300 <= response.status_code < 400:
                    raise NotificationError("通知服务返回重定向，已停止发送")
                if not 200 <= response.status_code < 300:
                    raise NotificationError(
                        "通知服务拒绝了 HTTP 请求", retryable=response.status_code == 429 or response.status_code >= 500,
                    )
                if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
                    # 请求已明确 identity。拒绝意外压缩，避免解压后体积绕过 32 KB 限制。
                    raise NotificationError("通知服务返回了不支持的压缩响应，无法确认接受状态", uncertain=True)
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=8192):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise NotificationError("通知响应超过 32 KB，无法确认接受状态", uncertain=True)
                _response_accepted(kind, bytes(body))
            finally:
                response.close()
    except NotificationError:
        raise
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        raise NotificationError("连接通知服务失败", retryable=True) from None
    except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError):
        raise NotificationError("通知提交结果不确定，请先核对服务端记录", uncertain=True) from None
    except httpx.HTTPError:
        raise NotificationError("通知请求失败，无法确认提交结果", uncertain=True) from None
    return {"accepted": True, "kind": kind}


_ADDRESS = re.compile(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", re.ASCII)


def _smtp_config(config: dict, message: NotificationMessage) -> tuple[str, int, str, str, list[str]]:
    host = config.get("host")
    tls = config.get("tls", "ssl")
    sender = config.get("from_address", config.get("from", ""))
    recipients = config.get("recipients", config.get("to", []))
    if isinstance(recipients, str):
        recipients = [v.strip() for v in re.split(r"[,;]", recipients) if v.strip()]
    port = config.get("port", 465 if tls == "ssl" else 587)
    if not isinstance(host, str) or not host or any(c.isspace() for c in host) or any(c in host for c in "/@"):
        raise NotificationError("SMTP 主机配置无效")
    if tls not in {"ssl", "starttls"}:
        raise NotificationError("SMTP 必须使用 SSL 或 STARTTLS")
    if type(port) is not int or not 1 <= port <= 65535:
        raise NotificationError("SMTP 端口配置无效")
    if not isinstance(sender, str) or not _ADDRESS.fullmatch(sender):
        raise NotificationError("SMTP 发件地址配置无效")
    if not isinstance(recipients, list) or not 1 <= len(recipients) <= 100 or any(not isinstance(v, str) or not _ADDRESS.fullmatch(v) for v in recipients):
        raise NotificationError("SMTP 收件地址配置无效，需要 1 至 100 个邮箱地址")
    if any(c in message.title for c in "\r\n"):
        raise NotificationError("通知标题不能包含换行")
    return host, port, tls, sender, list(dict.fromkeys(recipients))


def _send_smtp(config: dict, secrets: dict, message: NotificationMessage, factory: Any) -> dict:
    host, port, tls, sender, recipients = _smtp_config(config, message)
    mail = EmailMessage()
    mail["Subject"] = message.title
    mail["From"] = sender
    mail["To"] = ", ".join(recipients)
    mail.set_content(message.text)
    if message.html:
        mail.add_alternative(message.html, subtype="html")
    context = ssl.create_default_context()
    smtp = None
    sending = False
    try:
        kwargs: dict[str, Any] = {"host": host, "port": port, "timeout": TIMEOUT_SECONDS}
        if tls == "ssl":
            kwargs["context"] = context
        smtp = (factory or (smtplib.SMTP_SSL if tls == "ssl" else smtplib.SMTP))(**kwargs)
        smtp.ehlo()
        if tls == "starttls":
            smtp.starttls(context=context)
            smtp.ehlo()
        username, password = secrets.get("username") or "", secrets.get("password") or ""
        if username:
            smtp.login(username, password)
        sending = True
        refused = smtp.send_message(mail, from_addr=sender, to_addrs=recipients)
        # smtplib 返回值只包含被拒绝的收件人；发送过的部分不得自动重发。
        refused_count = len(refused or {})
        if refused_count >= len(recipients):
            raise NotificationError("SMTP 拒绝了所有收件地址")
        return {"accepted": True, "kind": "email", "accepted_count": len(recipients) - refused_count, "refused_count": refused_count}
    except NotificationError:
        raise
    except smtplib.SMTPAuthenticationError:
        raise NotificationError("SMTP 身份验证失败，请核对账号和授权码") from None
    except smtplib.SMTPRecipientsRefused:
        raise NotificationError("SMTP 拒绝了所有收件地址") from None
    except smtplib.SMTPResponseException as exc:
        raise NotificationError("SMTP 服务拒绝了请求", retryable=400 <= exc.smtp_code < 500) from None
    except ssl.SSLError:
        raise NotificationError("SMTP TLS 证书或安全连接验证失败") from None
    except smtplib.SMTPNotSupportedError:
        raise NotificationError("SMTP 服务不支持所需的安全连接或验证方式") from None
    except smtplib.SMTPServerDisconnected:
        if sending:
            raise NotificationError("SMTP 提交结果不确定，请先核对服务端记录", uncertain=True) from None
        raise NotificationError("连接 SMTP 服务失败", retryable=True) from None
    except smtplib.SMTPException:
        raise NotificationError("SMTP 发送失败", uncertain=sending) from None
    except OSError:
        if sending:
            raise NotificationError("SMTP 提交结果不确定，请先核对服务端记录", uncertain=True) from None
        raise NotificationError("连接 SMTP 服务失败", retryable=True) from None
    finally:
        if smtp is not None:
            try:
                smtp.close()
            except Exception:
                pass


def send_notification(
    kind: str, config: dict, secrets: dict, message: NotificationMessage,
    *, http_transport: Any = None, smtp_factory: Any = None,
) -> dict:
    """发一次消息；不存库、不自动重试，也不把服务器接受称为最终送达。

    SMTP config 使用 host/port/tls/from_address/recipients，兼容 from/to。
    所有 HTTP 地址、SendKey、签名密钥和 Token 只从 secrets 读取。
    """
    try:
        if not isinstance(message, NotificationMessage) or any(not isinstance(v, str) for v in (message.title, message.text, message.html)):
            raise NotificationError("通知内容格式无效")
        if kind in {"email", "smtp"}:
            return _send_smtp(config, secrets, message, smtp_factory)
        if kind in WEBHOOK_KINDS:
            return _send_http(kind, secrets, message, http_transport)
        raise NotificationError("不支持的通知渠道")
    except NotificationError:
        raise
    except Exception:
        # 禁止底层异常把 URL 查询参数、认证信息或服务端原文带入持久化记录。
        raise NotificationError("通知配置或发送过程异常，详细内容已隐藏") from None
