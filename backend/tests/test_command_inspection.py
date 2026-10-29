"""4.4 P21 命令巡检：判定、危险命令拦截、定时、网络设备命令行解析、整条流程、告警与权限。

只用伪造的 SSH 会话与内存数据库，不连接任何设备。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import command_inspection as ci
from app.collect.base import AdapterError
from app.inspection_templates import BUILTIN, dangerous, validate_items
from app.models import AlertEvent, Asset, InspectionJob, InspectionResult, InspectionRun, InspectionTemplate, ManagementEndpoint, User, UserScopeAsset
from app.services.credentials import create_credential
from tests.api_helpers import add_user, login

LINUX = {i["key"]: i for i in BUILTIN[0]["items"]}
H3C = {i["key"]: i for i in BUILTIN[3]["items"]}


# ---- 判定 ----


def test_judge_rules() -> None:
    disk = LINUX["disk"]
    assert ci.judge(disk, "41% /\n12% /boot\n", 0)[0] == "ok"
    verdict, sev, detail = ci.judge(disk, "88% /\n96% /var\n", 0)
    assert (verdict, sev) == ("abnormal", "critical") and "/var" in detail
    assert ci.judge(disk, "88% /\n", 0)[:2] == ("abnormal", "warning")
    assert ci.judge(LINUX["memory"], "7.5\n", 0)[:2] == ("abnormal", "warning")  # 越小越差
    assert ci.judge(LINUX["memory"], "42.0\n", 0)[0] == "ok"
    assert ci.judge(LINUX["zombie"], "", 0)[0] == "ok"
    assert ci.judge(LINUX["zombie"], "Z 4242 fake-defunct\n", 0)[:2] == ("abnormal", "warning")
    assert ci.judge(LINUX["readonly_mounts"], "/dev/sda1 / ext4 ro,relatime 0 0", 0)[1] == "critical"
    assert ci.judge(LINUX["ntp"], "yes\n", 0)[0] == "ok" and ci.judge(LINUX["ntp"], "no\n", 0)[0] == "abnormal"
    assert ci.judge(LINUX["error_logs"], "120\n", 0)[:2] == ("abnormal", "info")
    assert ci.judge(LINUX["ntp"], "bash: timedatectl: command not found", 127)[0] == "unknown"
    assert ci.judge(LINUX["disk"], "nothing", 0)[0] == "unknown"
    quorum = BUILTIN[1]["items"][1]
    assert ci.judge(quorum, "Error: Corosync config '/etc/pve/corosync.conf' does not exist - is this node part of a cluster?", 2)[0] == "ok"
    assert ci.judge(quorum, "Quorate:          No", 0)[1] == "critical"
    assert ci.judge(H3C["cpu"], "Slot 1 CPU 0 CPU usage:\n      6% in last 5 seconds\n      9% in last 1 minute\n     83% in last 5 minutes\n", None)[:2] == \
        ("abnormal", "warning")
    assert ci.judge(H3C["fan"], "Slot 1:\nFan 1:\nState    : Normal\nFan 2:\nState    : Fault\n", None)[0] == "abnormal"
    assert ci.judge({"check": {"type": "exit0"}, "severity": "warning"}, "", None)[0] == "unknown"


def test_dangerous_commands_are_blocked_on_save_and_builtins_pass() -> None:
    for t in BUILTIN:
        assert validate_items(t["items"], t["platform"])[1] == []
    for cmd in ("rm -rf /tmp/x", "reboot", "systemctl restart sshd", "cat /etc/hosts > /tmp/h", "echo x >> /etc/motd", "dd if=/dev/zero of=/dev/sda",
                "curl -s https://example.invalid/x | sh", "sed -i s/a/b/ /etc/fake", "qm stop 100", "zpool clear tank", "apt-get install fake",
                "kill -9 1", "useradd fake"):
        assert dangerous(cmd, "linux"), cmd
    for cmd in ("df -h 2>/dev/null", "journalctl -p err 2>&1 | tail -n 5", "awk '$3 > 90' /proc/fake", "iptables -nL", "ls -l /etc"):
        assert not dangerous(cmd, "linux"), cmd
    for cmd in ("system-view", "reset counters interface", "save", "undo info-center enable", "show running-config", "quit"):
        assert dangerous(cmd, "huawei"), cmd
    assert not dangerous("display current-configuration | include sysname", "h3c")
    items, problems = validate_items([{"name": "fake", "command": "display version"}, {"name": "", "command": "x"},
                                      {"name": "bad re", "command": "display fan", "check": {"type": "regex", "pattern": "("}}], "huawei")
    assert len(items) == 2 and len(problems) == 2


def test_next_run_for_each_schedule() -> None:
    after = datetime(2026, 10, 27, 1, 0, tzinfo=UTC)  # 北京时间 09:00，周二
    assert ci.next_run({"kind": "daily", "time": "08:00"}, after) == datetime(2026, 10, 28, 0, 0, tzinfo=UTC)
    assert ci.next_run({"kind": "daily", "time": "10:00"}, after) == datetime(2026, 10, 27, 2, 0, tzinfo=UTC)
    assert ci.next_run({"kind": "weekly", "time": "08:00", "weekdays": [0]}, after) == datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    assert ci.next_run({"kind": "monthly", "time": "08:00", "day": 31}, after) == datetime(2026, 10, 31, 0, 0, tzinfo=UTC)
    assert ci.next_run({"kind": "monthly", "time": "08:00", "day": 31}, datetime(2026, 11, 1, tzinfo=UTC)) == datetime(2026, 11, 30, 0, 0, tzinfo=UTC)
    assert ci.next_run({"kind": "once", "at": "2026-10-27T12:00:00+08:00"}, after) == datetime(2026, 10, 27, 4, 0, tzinfo=UTC)
    assert ci.next_run({"kind": "once", "at": "2026-10-27T08:00:00+08:00"}, after) is None
    with pytest.raises(ValueError):
        ci.validate_schedule({"kind": "weekly", "time": "08:00", "weekdays": []})
    with pytest.raises(ValueError):
        ci.validate_schedule({"kind": "daily", "time": "25:00"})


# ---- 网络设备命令行 ----


class FakeChannel:
    def __init__(self, replies: dict[str, list[str]], banner: str = "\r\nInfo: fake banner\r\n<fake-sw>") -> None:
        self.pending = [banner]
        self.replies, self.sent, self.closed = replies, [], False

    def recv_ready(self) -> bool:
        return bool(self.pending)

    def recv(self, _n: int) -> bytes:
        return self.pending.pop(0).encode()

    def send(self, data: str) -> None:
        self.sent.append(data)
        if data == " ":
            return
        self.pending.extend(self.replies.get(data.strip(), [f"{data}\r\n<fake-sw>"]))

    def exit_status_ready(self) -> bool:
        return False


def shell(chan: FakeChannel, platform: str = "h3c") -> ci.ShellSession:
    s = object.__new__(ci.ShellSession)
    s.chan = chan
    s._read(5)
    s.run(ci.PAGING_OFF[platform], 5)
    return s


def test_shell_session_strips_echo_prompt_and_pages() -> None:
    chan = FakeChannel({"display fan": ["display fan\r\n Fan 1: Normal\r\n", "  ---- More ----\x1b[42D", " Fan 2: Normal\r\n<fake-sw>"]})
    s = shell(chan)
    out, code = s.run("display fan", 5)
    assert out == " Fan 1: Normal\n Fan 2: Normal" and code is None
    assert chan.sent[0] == "screen-length disable\n" and " " in chan.sent
    chan2 = FakeChannel({})
    s2 = shell(chan2)
    chan2.replies["display slow"] = ["display slow\r\nstill running"]
    with pytest.raises(AdapterError):
        s2.run("display slow", 0.2)


# ---- 整条流程 ----


class FakeSession:
    def __init__(self, outputs: dict[str, tuple[str, int | None]], calls: list[str]) -> None:
        self.outputs, self.calls = outputs, calls

    def run(self, command: str, timeout: float) -> tuple[str, int | None]:
        self.calls.append(command)
        if command not in self.outputs:
            return "", 0
        text, code = self.outputs[command]
        if code == -1:
            raise AdapterError("timeout", "命令超过 30 秒没有结束")
        return text, code

    def close(self) -> None:
        pass


class Fleet:
    def __init__(self) -> None:
        self.outputs: dict[str, tuple[str, int | None]] = {}
        self.calls: list[str] = []
        self.fail = False

    def factory(self, ctx, platform):  # type: ignore[no-untyped-def]
        assert ctx.protocol == "ssh" and ctx.username == "fake-ops"
        if self.fail:
            raise AdapterError("auth", "SSH 用户名或密码 / 私钥不正确")
        return FakeSession(self.outputs, self.calls)


def make_host(db: Session, cipher, name: str, address: str, *, purpose: str = "monitoring") -> uuid.UUID:  # type: ignore[no-untyped-def]
    cred = create_credential(db, cipher, alias=f"ssh-{uuid.uuid4()}", kind="password", username="fake-ops", secret="fake-ssh-password")
    a = Asset(name=name, form="physical", role="server", lifecycle="active")
    a.endpoints.append(ManagementEndpoint(address=address, port=22, protocol="ssh", purpose=purpose, credential_id=cred.id))
    db.add(a)
    db.commit()
    return a.id


def worker(app_factory, factory, fleet: Fleet):  # type: ignore[no-untyped-def]
    from app.worker import Worker

    return Worker(factory, {ci.TASK: ci.make_handler(lambda: app_factory.cipher, fleet.factory)})


def drain(w) -> int:  # type: ignore[no-untyped-def]
    n = 0
    while w.run_once() == "succeeded":
        n += 1
    return n


def builtin(db: Session, key: str) -> InspectionTemplate:
    return db.scalar(select(InspectionTemplate).where(InspectionTemplate.builtin_key == key))


def test_custom_templates_admin_only_with_confirmation_and_guard(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "ada", "admin")
    add_user(factory, "otto", "operator")
    admin, op = login(app_factory(), "ada"), login(app_factory(), "otto")
    listed = op.get("/api/command-inspection/templates").json()
    assert sorted(t["platform"] for t in listed["items"] if t["builtin"]) == ["cisco", "h3c", "huawei", "linux", "pve"]
    body = {"name": "fake-nginx", "platform": "linux", "items": [{"name": "nginx", "command": "systemctl is-active nginx",
                                                                    "check": {"type": "regex", "pattern": "^active"}}], "confirm": "fake-nginx"}
    assert op.post("/api/command-inspection/templates", json=body).status_code == 403
    assert admin.post("/api/command-inspection/templates", json=body | {"confirm": ""}).status_code == 422
    bad = admin.post("/api/command-inspection/templates", json=body | {"items": [{"name": "x", "command": "systemctl restart nginx"}]})
    assert bad.status_code == 422 and "危险" in bad.json()["detail"]["message"]
    r = admin.post("/api/command-inspection/templates", json=body)
    assert r.status_code == 201 and r.json()["items"][0]["key"] == "item1"
    tid = r.json()["id"]
    lid = next(t["id"] for t in listed["items"] if t["name"] == "Linux 通用巡检")
    assert admin.put(f"/api/command-inspection/templates/{lid}", json=body).status_code == 403  # 内置只读
    assert admin.put(f"/api/command-inspection/templates/{tid}", json=body | {"revision": 9}).status_code == 409
    assert admin.delete(f"/api/command-inspection/templates/{lid}").status_code == 403
    assert admin.delete(f"/api/command-inspection/templates/{tid}").status_code == 204


def test_job_runs_through_worker_raises_and_recovers_alert(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    from app.alerting import evaluate

    add_user(factory, "otto", "operator")
    op = login(app_factory(), "otto")
    with factory() as db:
        h1 = make_host(db, app_factory.cipher, "fake-web-1", "192.0.2.61")
        h2 = make_host(db, app_factory.cipher, "fake-web-2", "192.0.2.62")
        tid = str(builtin(db, "builtin-linux").id)
    fleet = Fleet()
    fleet.outputs = {LINUX["disk"]["command"]: ("40% /\n97% /var\n", 0), LINUX["ntp"]["command"]: ("yes\n", 0),
                     LINUX["memory"]["command"]: ("55.0\n", 0), LINUX["load"]["command"]: ("0.30\n", 0)}
    r = op.post("/api/command-inspection/jobs", json={"name": "fake-daily", "template_id": tid, "asset_ids": [str(h1), str(h2)],
                                                      "schedule": {"kind": "once"}})
    assert r.status_code == 201 and r.json()["run_id"] and r.json()["enabled"] is False
    run_id = r.json()["run_id"]
    assert drain(worker(app_factory, factory, fleet)) == 2
    detail = op.get(f"/api/command-inspection/runs/{run_id}").json()
    assert detail["status"] == "done" and detail["assets_done"] == 2 and detail["counts"]["critical"] == 2
    assert len(detail["results"]) == 2 * len(LINUX)
    var = next(x for x in detail["results"] if x["item_key"] == "disk" and x["asset_name"] == "fake-web-1")
    assert var["verdict"] == "abnormal" and var["severity"] == "critical" and "/var" in var["detail"]
    with factory() as db:
        evaluate(db)
        db.commit()
        events = db.scalars(select(AlertEvent).where(AlertEvent.metric_key == "inspection.critical", AlertEvent.kind == "trigger")).all()
        assert len(events) == 2 and all("磁盘使用率" in e.message and e.notify for e in events)
        ep = db.scalar(select(ManagementEndpoint).where(ManagementEndpoint.address == "192.0.2.61"))
    # 第二次巡检正常 → 告警恢复；结果详情能看到上一次的输出
    fleet.outputs[LINUX["disk"]["command"]] = ("40% /\n60% /var\n", 0)
    run2 = op.post(f"/api/command-inspection/jobs/{r.json()['id']}/run").json()["run_id"]
    drain(worker(app_factory, factory, fleet))
    res = next(x for x in op.get(f"/api/command-inspection/runs/{run2}").json()["results"] if x["item_key"] == "disk" and x["asset_name"] == "fake-web-1")
    full = op.get(f"/api/command-inspection/results/{res['id']}").json()
    assert full["verdict"] == "ok" and full["previous"]["output"].endswith("97% /var\n")
    with factory() as db:
        evaluate(db)
        db.commit()
        assert len(db.scalars(select(AlertEvent).where(AlertEvent.metric_key == "inspection.critical", AlertEvent.kind == "recover")).all()) == 2
    assert ep is not None


def test_connection_failure_dangerous_item_and_session_break(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    with factory() as db:
        h = make_host(db, app_factory.cipher, "fake-db-1", "192.0.2.63")
        none = Asset(name="fake-no-ssh", form="physical", role="server", lifecycle="active")
        db.add(none)
        db.flush()
        t = InspectionTemplate(name="fake-mixed", platform="linux", items=[
            {"key": "a", "name": "ok", "command": "uptime", "check": {"type": "record"}, "severity": "warning", "timeout": 30},
            {"key": "b", "name": "bad", "command": "rm -rf /tmp/fake", "check": {"type": "record"}, "severity": "warning", "timeout": 30},
            {"key": "c", "name": "slow", "command": "fake-slow", "check": {"type": "record"}, "severity": "warning", "timeout": 30},
            {"key": "d", "name": "after", "command": "hostname", "check": {"type": "record"}, "severity": "warning", "timeout": 30}])
        db.add(t)
        db.flush()
        run = ci.start_run(db, t, [db.get(Asset, h), none], by="fake")
        db.commit()
        run_id = run.id
    fleet = Fleet()
    fleet.outputs = {"fake-slow": ("", -1)}
    drain(worker(app_factory, factory, fleet))
    assert "rm -rf /tmp/fake" not in fleet.calls and fleet.calls == ["uptime", "fake-slow", "hostname"]  # 被改过的模板：危险命令执行前再拦一次
    with factory() as db:
        rows = {(r.asset_name, r.item_key): r for r in db.scalars(select(InspectionResult).where(InspectionResult.run_id == run_id))}
        assert rows[("fake-db-1", "b")].verdict == "error" and "危险" in rows[("fake-db-1", "b")].detail
        assert rows[("fake-db-1", "c")].verdict == "error" and rows[("fake-db-1", "d")].verdict == "ok"
        assert rows[("fake-no-ssh", "")].verdict == "error" and "SSH 端点" in rows[("fake-no-ssh", "")].detail
        assert db.get(InspectionRun, run_id).status == "done"
    fleet.fail = True
    with factory() as db:
        run = ci.start_run(db, db.get(InspectionTemplate, t.id), [db.get(Asset, h)], by="fake")
        db.commit()
        rid = run.id
    drain(worker(app_factory, factory, fleet))
    with factory() as db:
        r = db.scalar(select(InspectionResult).where(InspectionResult.run_id == rid))
        assert r.verdict == "error" and "连接失败" in r.detail


def test_schedule_scope_and_retention(app_factory, factory) -> None:  # type: ignore[no-untyped-def]
    add_user(factory, "otto", "operator")
    add_user(factory, "vic", "viewer")
    op, viewer = login(app_factory(), "otto"), login(app_factory(), "vic")
    with factory() as db:
        mine = make_host(db, app_factory.cipher, "fake-mine", "192.0.2.64")
        other = make_host(db, app_factory.cipher, "fake-other", "192.0.2.65")
        u = db.scalar(select(User).where(User.username == "otto"))
        u.scope_all = False
        db.add(UserScopeAsset(user_id=u.id, asset_id=mine))
        db.commit()
        tid = str(builtin(db, "builtin-linux").id)
    job = {"name": "fake-weekly", "template_id": tid, "schedule": {"kind": "weekly", "time": "07:30", "weekdays": [0, 3]}}
    assert op.post("/api/command-inspection/jobs", json=job | {"asset_ids": [str(other)]}).status_code == 404  # 范围外当不存在
    assert viewer.post("/api/command-inspection/jobs", json=job | {"asset_ids": [str(mine)]}).status_code == 403
    r = op.post("/api/command-inspection/jobs", json=job | {"asset_ids": [str(mine)]})
    assert r.status_code == 201 and r.json()["next_run_at"]
    jid = uuid.UUID(r.json()["id"])
    assert viewer.get("/api/command-inspection/jobs").json()[0]["name"] == "fake-weekly"
    with factory() as db:
        j = db.get(InspectionJob, jid)
        due = j.next_run_at.replace(tzinfo=UTC) if j.next_run_at.tzinfo is None else j.next_run_at
        assert ci.enqueue_due(db, now=due - timedelta(seconds=1)) == 0
        assert ci.enqueue_due(db, now=due) == 1
        assert db.get(InspectionJob, jid).next_run_at.replace(tzinfo=UTC) > due
        old = InspectionRun(name="fake-old", items=[], created_at=datetime.now(UTC) - timedelta(days=100), counts={})
        db.add(old)
        db.commit()
        assert ci.prune(db) == 1
        db.commit()


# ---- P21 第二批：Cisco 与 cron ----


def test_cisco_template_guard_and_prompt() -> None:
    cisco = {i["key"]: i for i in BUILTIN[4]["items"]}
    assert BUILTIN[4]["platform"] == "cisco"
    assert ci.judge(cisco["cpu"], "CPU utilization for five seconds: 9%/0%; one minute: 12%; five minutes: 86%", None)[:2] == ("abnormal", "warning")
    assert ci.judge(cisco["errdisabled"], "Port    Name   Status       Reason\nGi1/0/3        err-disabled bpduguard", None)[0] == "abnormal"
    for cmd in ("configure terminal", "write memory", "reload", "clear counters", "copy run start", "display version"):
        assert dangerous(cmd, "cisco"), cmd
    assert not dangerous("show interfaces status | include connected", "cisco")
    chan = FakeChannel({"terminal length 0": ["terminal length 0\r\nfake-sw>"],
                        "show version": ["show version\r\nCisco IOS Software, fake\r\n", " --More-- ", "uptime is 3 weeks\r\nfake-sw#"]},
                       banner="\r\nfake-sw>")
    s = object.__new__(ci.ShellSession)
    s.chan, s.prompt = chan, ci._CISCO_PROMPT
    s._read(5)
    s.run("terminal length 0", 5)
    out, _ = s.run("show version", 5)
    assert out.startswith("Cisco IOS Software, fake") and "uptime is 3 weeks" in out and "fake-sw" not in out


def test_cron_schedule() -> None:
    sat = datetime(2026, 10, 31, 1, 0, tzinfo=UTC)  # 北京时间周六 09:00
    assert ci.next_run({"kind": "cron", "expr": "30 8 * * 1-5"}, sat) == datetime(2026, 11, 2, 0, 30, tzinfo=UTC)
    assert ci.next_run({"kind": "cron", "expr": "*/15 * * * *"}, sat) == datetime(2026, 10, 31, 1, 15, tzinfo=UTC)
    assert ci.next_run({"kind": "cron", "expr": "0 2 1,15 * *"}, sat) == datetime(2026, 10, 31, 18, 0, tzinfo=UTC)  # 11 月 1 日 02:00
    assert ci.next_run({"kind": "cron", "expr": "0 9 13 * 5"}, sat) == datetime(2026, 11, 6, 1, 0, tzinfo=UTC)  # 13 日或周五
    assert ci.next_run({"kind": "cron", "expr": "0 0 * * 7"}, sat) == datetime(2026, 10, 31, 16, 0, tzinfo=UTC)  # 7 也是周日
    assert ci.validate_schedule({"kind": "cron", "expr": " 0  8 * * * "}) == {"kind": "cron", "expr": "0 8 * * *"}
    for bad in ("0 8 * *", "61 8 * * *", "0 8 * * mon", "0 8-3 * * *"):
        with pytest.raises(ValueError):
            ci.validate_schedule({"kind": "cron", "expr": bad})
