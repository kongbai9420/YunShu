"""通知传输离线回放；不连接 SMTP 或任何外部 HTTP 服务。"""

import base64
import hashlib
import hmac
import json
import smtplib
import ssl

import httpx
import pytest

from app.notifications import MAX_RESPONSE_BYTES, NotificationError, NotificationMessage, send_notification

MESSAGE = NotificationMessage("测试告警", "设备不可达\n等待恢复", "<p>设备不可达</p>")
SECRETS = {"url": "https://192.0.2.10/notify?token=fake-url-token", "secret": "fake-sign-secret"}
SMTP_CONFIG = {"host": "192.0.2.20", "tls": "ssl", "from_address": "fake-sender@example.test", "recipients": ["fake-receiver@example.test"]}


def transport_with(status=200, payload=None, *, content=None, callback=None):
    def handler(request):
        if callback:
            callback(request)
        if content is not None:
            return httpx.Response(status, content=content)
        return httpx.Response(status, json=payload if payload is not None else {"code": 0, "errcode": 0})
    return httpx.MockTransport(handler)


@pytest.mark.parametrize("kind,reply", [
    ("custom_webhook", {}), ("serverchan", {"code": 0}), ("dingtalk", {"errcode": 0}),
    ("feishu", {"code": 0}), ("wechat_work", {"errcode": 0}), ("bark", {"code": 200}), ("pushplus", {"code": 200}),
])
def test_seven_channels_require_server_acceptance(kind, reply):
    requests = []
    result = send_notification(kind, {}, SECRETS, MESSAGE, http_transport=transport_with(payload=reply, callback=requests.append))
    assert result == {"accepted": True, "kind": kind}
    assert len(requests) == 1 and requests[0].method == "POST"
    assert requests[0].url.scheme == "https"
    assert requests[0].extensions["timeout"] == {k: 10 for k in ("connect", "read", "write", "pool")}
    assert "delivered" not in result


@pytest.mark.parametrize("kind,reply", [
    ("custom_webhook", {"success": False}), ("serverchan", {"code": 1}), ("dingtalk", {"errcode": 310000}),
    ("feishu", {"code": 19021}), ("wechat_work", {"errcode": 93000}), ("bark", {"code": 400}), ("pushplus", {"code": 500}),
])
def test_http_200_with_business_failure_is_not_success(kind, reply):
    reply["message"] = "https://fake-secret.test/never-log fake-password"
    with pytest.raises(NotificationError) as caught:
        send_notification(kind, {}, SECRETS, MESSAGE, http_transport=transport_with(payload=reply))
    assert not caught.value.retryable and not caught.value.uncertain
    assert "fake" not in str(caught.value) and "http" not in str(caught.value)


def test_payloads_and_signatures_match_channel_contract(monkeypatch):
    monkeypatch.setattr("app.notifications.time.time", lambda: 1700000000.25)
    requests = []
    for kind in ("custom_webhook", "dingtalk", "feishu", "wechat_work", "pushplus", "bark"):
        reply = {"code": 200 if kind in {"pushplus", "bark"} else 0, "errcode": 0}
        send_notification(kind, {}, SECRETS, MESSAGE, http_transport=transport_with(payload=reply, callback=requests.append))
    payloads = [json.loads(r.content) for r in requests]
    assert payloads[0] == {"title": MESSAGE.title, "content": MESSAGE.text, "timestamp": 1700000000}
    assert payloads[1]["markdown"]["title"] == MESSAGE.title
    query = requests[1].url.params
    assert query["token"] == "fake-url-token" and query["timestamp"] == "1700000000250"
    expected = base64.b64encode(hmac.new(b"fake-sign-secret", b"1700000000250\nfake-sign-secret", hashlib.sha256).digest()).decode()
    assert query["sign"] == expected
    assert payloads[2]["timestamp"] == "1700000000"
    assert payloads[2]["sign"] == base64.b64encode(hmac.new(b"1700000000\nfake-sign-secret", b"", hashlib.sha256).digest()).decode()
    assert payloads[2]["content"]["post"]["zh_cn"]["content"][0][0]["text"] == MESSAGE.text
    assert payloads[3]["msgtype"] == "markdown"
    assert payloads[4]["token"] == "fake-sign-secret" and payloads[4]["template"] == "txt"
    assert requests[4].url == "https://www.pushplus.plus/send"
    assert payloads[5]["body"] == MESSAGE.text and payloads[5]["device_key"] == "fake-sign-secret"


def test_serverchan_form_key_and_bark_key():
    requests = []
    send_notification("serverchan", {}, {"secret": "fake-SCT-key"}, MESSAGE, http_transport=transport_with(callback=requests.append))
    assert requests[-1].url == "https://sctapi.ftqq.com/fake-SCT-key.send"
    assert httpx.QueryParams(requests[-1].content.decode())["desp"] == MESSAGE.text
    send_notification("bark", {}, {"url": "fake-device-key"}, MESSAGE, http_transport=transport_with(payload={"code": 200}, callback=requests.append))
    assert requests[-1].url == "https://api.day.app/fake-device-key"
    assert "device_key" not in json.loads(requests[-1].content)


@pytest.mark.parametrize("kind", ["serverchan", "pushplus", "bark", "dingtalk"])
def test_missing_secrets_fail_without_network(kind):
    def unexpected(request):
        pytest.fail("未配置不能联网")
    with pytest.raises(NotificationError):
        send_notification(kind, {}, {}, MESSAGE, http_transport=httpx.MockTransport(unexpected))


@pytest.mark.parametrize("url", ["http://192.0.2.10/hook", "https://fakeuser:fakepw@192.0.2.10/hook", "https://192.0.2.10/hook#token", "https://192.0.2.10:bad/hook"])
def test_no_plaintext_or_url_basic_auth(url):
    with pytest.raises(NotificationError):
        send_notification("custom_webhook", {}, {"url": url}, MESSAGE)


def test_no_redirect_and_generic_webhook_accepts_non_json():
    calls = []
    def redirect(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://192.0.2.11/other?token=private"})
    with pytest.raises(NotificationError):
        send_notification("custom_webhook", {}, SECRETS, MESSAGE, http_transport=httpx.MockTransport(redirect))
    assert len(calls) == 1
    assert send_notification("custom_webhook", {}, SECRETS, MESSAGE, http_transport=transport_with(content=b"accepted"))["accepted"]


@pytest.mark.parametrize("payload", [{}, {"code": False}, {"code": "0"}, [], None])
def test_known_channel_requires_explicit_success_code(payload):
    body = json.dumps(payload).encode()
    with pytest.raises(NotificationError) as caught:
        send_notification("serverchan", {}, SECRETS, MESSAGE, http_transport=transport_with(content=body))
    assert caught.value.uncertain and not caught.value.retryable


@pytest.mark.parametrize("content", [b"x" * (MAX_RESPONSE_BYTES + 1), b"not-json"], ids=["oversized", "invalid-json"])
def test_bounded_response_and_invalid_json(content):
    with pytest.raises(NotificationError) as caught:
        send_notification("feishu", {}, SECRETS, MESSAGE, http_transport=transport_with(content=content))
    assert caught.value.uncertain and not caught.value.retryable


def test_unrequested_compression_is_rejected_without_decompression():
    def compressed(request):
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=httpx.ByteStream(b"invalid-gzip"))
    with pytest.raises(NotificationError) as caught:
        send_notification("custom_webhook", {}, SECRETS, MESSAGE, http_transport=httpx.MockTransport(compressed))
    assert caught.value.uncertain


def test_no_httpx_info_log_contains_secret_url(caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    send_notification("custom_webhook", {}, SECRETS, MESSAGE, http_transport=transport_with())
    assert "fake-url-token" not in caplog.text and "fake-sign-secret" not in caplog.text


@pytest.mark.parametrize("status,retryable", [(400, False), (401, False), (429, True), (503, True)])
def test_http_status_retry_policy(status, retryable):
    with pytest.raises(NotificationError) as caught:
        send_notification("dingtalk", {}, SECRETS, MESSAGE, http_transport=transport_with(status, content=b"secret raw response"))
    assert caught.value.retryable is retryable and "secret" not in str(caught.value)


@pytest.mark.parametrize("error,retryable,uncertain", [
    (httpx.ConnectError, True, False), (httpx.ConnectTimeout, True, False),
    (httpx.ReadTimeout, False, True), (httpx.WriteTimeout, False, True), (httpx.RemoteProtocolError, False, True),
])
def test_transport_errors_never_expose_url_or_secrets(error, retryable, uncertain):
    def fail(request):
        raise error("fake-password https://192.0.2.10?token=fake-token", request=request)
    with pytest.raises(NotificationError) as caught:
        send_notification("dingtalk", {}, SECRETS, MESSAGE, http_transport=httpx.MockTransport(fail))
    assert caught.value.retryable is retryable and caught.value.uncertain is uncertain
    assert "fake" not in str(caught.value) and "https" not in str(caught.value)


class FakeSmtp:
    def __init__(self, *, refusal=None, error=None, phase="send_message"):
        self.calls = []
        self.refusal, self.error, self.phase = refusal or {}, error, phase

    def call(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if name == self.phase and self.error:
            raise self.error

    def ehlo(self):
        self.call("ehlo")

    def starttls(self, **kwargs):
        self.call("starttls", **kwargs)

    def login(self, *args):
        self.call("login", *args)

    def send_message(self, *args, **kwargs):
        self.call("send_message", *args, **kwargs)
        return self.refusal

    def close(self):
        self.call("close")


@pytest.mark.parametrize("tls,expected", [("ssl", ["ehlo", "login", "send_message", "close"]), ("starttls", ["ehlo", "starttls", "ehlo", "login", "send_message", "close"])])
def test_smtp_tls_order_mime_and_secret_handling(tls, expected):
    fake, construction = FakeSmtp(), []
    def factory(**kwargs):
        construction.append(kwargs)
        return fake
    result = send_notification("email", {**SMTP_CONFIG, "tls": tls}, {"username": "fake-user", "password": "fake-password"}, MESSAGE, smtp_factory=factory)
    assert result == {"accepted": True, "kind": "email", "accepted_count": 1, "refused_count": 0}
    assert [c[0] for c in fake.calls] == expected
    kwargs = construction[0]
    assert kwargs["timeout"] == 10 and kwargs["port"] == (465 if tls == "ssl" else 587)
    context = kwargs["context"] if tls == "ssl" else fake.calls[1][2]["context"]
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    call = next(c for c in fake.calls if c[0] == "send_message")
    mail = call[1][0]
    assert mail["Subject"] == MESSAGE.title and mail.get_body(preferencelist=("plain",)).get_content().strip() == MESSAGE.text
    assert mail.get_body(preferencelist=("html",)).get_content().strip() == MESSAGE.html
    assert "fake-password" not in mail.as_string()


def test_smtp_partial_refusal_preserves_accepted_count():
    fake = FakeSmtp(refusal={"fake-refused@example.test": (550, b"fake-private-server-response")})
    config = {**SMTP_CONFIG, "recipients": ["fake-receiver@example.test", "fake-refused@example.test"]}
    result = send_notification("email", config, {}, MESSAGE, smtp_factory=lambda **kw: fake)
    assert result["accepted_count"] == 1 and result["refused_count"] == 1
    assert "fake" not in str(result)


@pytest.mark.parametrize("error,phase,retryable,uncertain", [
    (TimeoutError("fake-secret"), "ehlo", True, False),
    (TimeoutError("fake-secret"), "send_message", False, True),
    (smtplib.SMTPServerDisconnected("fake-secret"), "send_message", False, True),
    (smtplib.SMTPAuthenticationError(535, b"fake-secret"), "login", False, False),
    (smtplib.SMTPDataError(451, b"fake-secret"), "send_message", True, False),
    (smtplib.SMTPDataError(554, b"fake-secret"), "send_message", False, False),
    (smtplib.SMTPRecipientsRefused({"fake@example.test": (550, "fake-secret")}), "send_message", False, False),
    (ssl.SSLCertVerificationError("fake-secret"), "starttls", False, False),
    (smtplib.SMTPNotSupportedError("fake-secret"), "starttls", False, False),
])
def test_smtp_failures_are_classified_and_sanitized(error, phase, retryable, uncertain):
    fake = FakeSmtp(error=error, phase=phase)
    with pytest.raises(NotificationError) as caught:
        send_notification("email", {**SMTP_CONFIG, "tls": "starttls"}, {"username": "fake-user"}, MESSAGE, smtp_factory=lambda **kw: fake)
    assert caught.value.retryable is retryable and caught.value.uncertain is uncertain
    assert "fake" not in str(caught.value) and fake.calls[-1][0] == "close"


@pytest.mark.parametrize("changes", [{"tls": "none"}, {"port": True}, {"host": "bad\nhost"}, {"from_address": "bad\nBcc: injected@example.test"}, {"recipients": []}, {"recipients": ["bad-address"]}])
def test_smtp_configuration_validated_before_connection(changes):
    def unexpected(**kwargs):
        pytest.fail("无效配置不能连接")
    with pytest.raises(NotificationError):
        send_notification("email", {**SMTP_CONFIG, **changes}, {}, MESSAGE, smtp_factory=unexpected)


def test_subject_injection_and_unknown_kind_rejected():
    with pytest.raises(NotificationError):
        send_notification("email", SMTP_CONFIG, {}, NotificationMessage("title\nBcc: fake@example.test", "text"))
    with pytest.raises(NotificationError):
        send_notification("unsupported", {}, {}, MESSAGE)
