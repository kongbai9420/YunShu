"""SSH 采集测试。

- 单元测试：回放录制的命令输出（tests/fixtures/ssh/*.txt）。
- 本机测试服务器：用 paramiko 在 127.0.0.1 上起一个只回放固定输出的 SSH 服务端，走真实的
  密钥交换、主机密钥固定和密码认证。不代表任何真实设备已验证。
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import paramiko
import pytest

from app.collect.base import AdapterError, EndpointContext, Trust
from app.collect.ssh import COMMAND, MARK, ParamikoRunner, SshAdapter, host_key_text, split_sections

FIX = Path(__file__).parent / "fixtures" / "ssh"
FAKE_USER, FAKE_PW = "fakeuser", "fake-ssh-pw-9"


class Replay:
    def __init__(self, name: str) -> None:
        self.text = (FIX / f"{name}.txt").read_text(encoding="utf-8")
        self.commands: list[str] = []

    def run(self, command: str) -> str:
        self.commands.append(command)
        return self.text

    def close(self) -> None:
        pass


def ctx(**kw) -> EndpointContext:  # type: ignore[no-untyped-def]
    return EndpointContext("192.0.2.40", 22, "ssh", username=FAKE_USER, secret=FAKE_PW, **kw)


def pts(result) -> dict:  # type: ignore[no-untyped-def]
    return {p.key: p for p in result.points}


def test_command_is_fixed_and_read_only() -> None:
    # 只读：没有任何写入、删除、重启或包管理命令
    for bad in ("rm ", "reboot", "shutdown", ">", "apt", "opkg", "systemctl", "kill"):
        assert bad not in COMMAND.replace("2>/dev/null", "")
    assert COMMAND.count(MARK) == 10


def test_debian_metrics() -> None:
    replay = Replay("debian")
    t = [0.0]
    a = SshAdapter(runner_factory=lambda c: replay, clock=lambda: t[0])
    r = a.collect(ctx())
    p = pts(r)
    assert r.facts == {"hostname": "fake-debian", "os": "Debian GNU/Linux 12 (bookworm)", "os_id": "debian", "cpu_count": 4}
    assert p["mem.used_pct"].value == 25.0
    assert p["swap.used_pct"].value == 25.0
    assert p["disk.used_pct:/"].value == 25.0 and p["disk.used_pct:/data"].value == 50.0
    assert "disk.used_pct:/dev" not in p and "disk.used_pct:/run" not in p  # 虚拟文件系统不算
    assert p["load.1m"].value == 0.42 and p["system.uptime"].value == 86400
    assert p["cpu.temp"].value == 52.0 and p["temp:acpitz"].value == 27.8
    assert "cpu.load" not in p and "if.in_bps:eth0" not in p  # 首次没有差值
    assert "if.errors:lo" not in p and p["if.errors:eth0"].value == 3

    # 第二次：CPU 计数器增加 1000（其中空闲 750）→ 25%；eth0 60 秒收 750 MB → 100 Mbit/s
    replay.text = replay.text.replace("cpu  1000 0 500 8000 500", "cpu  1150 0 600 8750 500").replace(
        "eth0: 5000000000", "eth0: 5750000000"
    )
    t[0] = 60.0
    p2 = pts(a.collect(ctx()))
    assert p2["cpu.load"].value == 25.0
    assert p2["if.in_bps:eth0"].value == 100_000_000.0


def test_openwrt_busybox() -> None:
    p = pts(SshAdapter(runner_factory=lambda c: Replay("openwrt")).collect(ctx()))
    r = SshAdapter(runner_factory=lambda c: Replay("openwrt")).probe(ctx())
    assert r.facts["os_id"] == "openwrt" and r.facts["hostname"] == "fake-openwrt"
    assert p["mem.used_pct"].value == 40.0  # 没有 MemAvailable 时用 Free + Buffers + Cached
    assert "swap.used_pct" not in p
    assert p["disk.used_pct:/overlay"].value == 25.0
    assert "cpu.temp" not in p


def test_split_sections_tolerates_missing() -> None:
    s = split_sections(f"{MARK}hostname\nh\n{MARK}thermal\n{MARK}nproc\n2\n")
    assert s == {"hostname": "h", "thermal": "", "nproc": "2"}


# ---- 本机 paramiko 测试服务器 ----


class _Server(paramiko.ServerInterface):
    def __init__(self) -> None:
        self.exec_requested = threading.Event()

    def check_auth_password(self, username: str, password: str) -> int:
        ok = username == FAKE_USER and password == FAKE_PW
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username: str) -> str:
        return "password"

    def check_channel_request(self, kind: str, chanid: int) -> int:
        return paramiko.OPEN_SUCCEEDED

    def check_channel_exec_request(self, channel, command) -> bool:  # type: ignore[no-untyped-def]
        # 只记下请求；回复放到 accept 之后的线程里，等 exec 请求的确认先发出去
        self.exec_requested.set()
        return True


@pytest.fixture
def ssh_server():  # type: ignore[no-untyped-def]
    host_key = paramiko.Ed25519Key.from_private_key_file(str(FIX / "test_host_key")) if (FIX / "test_host_key").exists() else None
    if host_key is None:
        host_key = paramiko.RSAKey.generate(2048)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = listener.getsockname()[1]
    output = (FIX / "debian.txt").read_text(encoding="utf-8")
    stop = threading.Event()

    def serve() -> None:
        listener.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    def handle(conn: socket.socket) -> None:
        t = paramiko.Transport(conn)
        t.add_server_key(host_key)
        server = _Server()
        try:
            t.start_server(server=server)
        except (paramiko.SSHException, EOFError):
            return
        chan = t.accept(10)
        if chan is None:
            t.close()
            return
        if server.exec_requested.wait(10):
            chan.sendall(output.encode())
            chan.send_exit_status(0)
        chan.close()

    th = threading.Thread(target=serve, daemon=True)
    th.start()
    yield port, host_key_text(host_key)
    stop.set()
    listener.close()


def local(port: int, **kw) -> EndpointContext:  # type: ignore[no-untyped-def]
    return EndpointContext("127.0.0.1", port, "ssh", username=FAKE_USER, secret=FAKE_PW, timeout=5, **kw)


def test_real_paramiko_first_use_trust_and_collect(ssh_server) -> None:  # type: ignore[no-untyped-def]
    port, key = ssh_server
    c = local(port)
    r = SshAdapter().collect(c)
    assert r.facts["hostname"] == "fake-debian"
    assert c.trust.observed_ssh == key  # 首次：记下主机密钥，由采集流程固定

    pinned = local(port, trust=Trust(ssh_host_key=key))
    assert SshAdapter().collect(pinned).facts["os_id"] == "debian"


def test_host_key_mismatch_refused_before_password(ssh_server) -> None:  # type: ignore[no-untyped-def]
    port, _ = ssh_server
    c = local(port, trust=Trust(ssh_host_key="ssh-ed25519 AAAAfakeotherkey"))
    with pytest.raises(AdapterError) as e:
        ParamikoRunner(c)
    assert e.value.kind == "untrusted"


def test_wrong_password(ssh_server) -> None:  # type: ignore[no-untyped-def]
    port, _ = ssh_server
    with pytest.raises(AdapterError) as e:
        ParamikoRunner(EndpointContext("127.0.0.1", port, "ssh", username=FAKE_USER, secret="wrong", timeout=5))
    assert e.value.kind == "auth"


def test_unreachable_and_unconfigured() -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(AdapterError) as e:
        ParamikoRunner(EndpointContext("127.0.0.1", port, "ssh", username="u", secret="p", timeout=2))
    assert e.value.kind in ("unreachable", "timeout")  # Windows 上连接被拒会表现为超时
    # 没有凭据时不去连接，直接报待配置（不会等到连接超时）
    with pytest.raises(AdapterError) as e:
        ParamikoRunner(EndpointContext("127.0.0.1", port, "ssh", timeout=2))
    assert e.value.kind == "unconfigured"
