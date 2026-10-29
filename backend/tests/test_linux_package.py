"""Linux 压缩包的静态检查（deploy/linux/、scripts/yunshu-linux.spec）：真正的构建与安装测试在 CI（linux-package.yml）。"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.cli import parse_env_file

ROOT = Path(__file__).resolve().parents[2]
LINUX = ROOT / "deploy" / "linux"
UNITS = ("yunshu-api.service", "yunshu-worker.service", "yunshu-migrate.service")


def _lock_subset():
    spec = importlib.util.spec_from_file_location("lock_subset", LINUX / "lock_subset.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _unit(name: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for raw in (LINUX / "systemd" / name).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        key, _, value = line.partition("=")
        values.setdefault(key.strip(), []).append(value.strip())
    return values


SAMPLE_LOCK = """alembic==1.0 \\
    --hash=sha256:aaa \\
    --hash=sha256:bbb
    # via -r x
pyinstaller==6.0 \\
    --hash=sha256:ccc
    # via
    #   -r y
Setuptools==80.0 ; python_version >= "3.11" \\
    --hash=sha256:ddd
"""


def test_lock_subset_keeps_versions_and_all_hashes():
    mod = _lock_subset()
    out = mod.subset(SAMPLE_LOCK, ["pyinstaller", "setuptools"])
    assert out == (
        "pyinstaller==6.0 \\\n    --hash=sha256:ccc\n"
        'Setuptools==80.0 ; python_version >= "3.11" \\\n    --hash=sha256:ddd\n'
    )
    with pytest.raises(KeyError):
        mod.subset(SAMPLE_LOCK, ["pyinstaller", "missing-package"])


def test_lock_subset_finds_pyinstaller_toolchain_in_package_lock():
    mod = _lock_subset()
    text = (ROOT / "backend" / "requirements-package.lock").read_text(encoding="utf-8")
    out = mod.subset(text, mod.PYINSTALLER_PACKAGES)
    entries = mod.parse_lock(out)
    assert set(entries) == {mod.normalize(n) for n in mod.PYINSTALLER_PACKAGES}
    for lines in entries.values():
        assert len(lines) >= 2, lines  # 至少一个哈希
        assert all("--hash=sha256:" in line for line in lines[1:])
    assert not out.rstrip().endswith("\\")
    # 筛出的哈希与原锁一致
    original = mod.parse_lock(text)
    for name, lines in entries.items():
        assert [line.rstrip(" \\") for line in lines] == [line.rstrip(" \\") for line in original[name]]


def test_env_example_defaults():
    env = parse_env_file((LINUX / "yunshu.env.example").read_text(encoding="utf-8"))
    assert env["YUNSHU_DATA_DIR"] == "/var/lib/yunshu"
    assert env["YUNSHU_MASTER_KEY_FILE"] == "/etc/yunshu/keys/master.key"
    assert env["YUNSHU_LOG_DIR"] == "/var/log/yunshu"
    assert env["YUNSHU_EMBEDDED_WORKER"] == "1"
    assert env["YUNSHU_SETUP_WIZARD"] == "0"
    assert env["YUNSHU_PORT"] == "8080"
    # 默认是 SQLite：不设连接串；时区跟随主机：不设 TZ
    assert "YUNSHU_DATABASE_URL" not in env
    assert "TZ" not in env
    # 主密钥不在数据目录里
    assert not env["YUNSHU_MASTER_KEY_FILE"].startswith(env["YUNSHU_DATA_DIR"] + "/")
    text = (LINUX / "yunshu.env.example").read_text(encoding="utf-8")
    for commented in ("#YUNSHU_DATABASE_URL=", "#TZ=", "#FORWARDED_ALLOW_IPS=", "#YUNSHU_SNMP_TRAP=1"):
        assert commented in text
    # 示例值只能是伪造的
    for ip in re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text):
        assert ip.startswith("192.0.2.") or ip in ("0.0.0.0", "127.0.0.1"), ip


@pytest.mark.parametrize("name", UNITS)
def test_units_are_hardened(name: str):
    unit = _unit(name)
    assert unit["User"] == ["yunshu"]
    assert unit["EnvironmentFile"] == ["/etc/yunshu/yunshu.env"]
    assert unit["NoNewPrivileges"] == ["yes"]
    assert unit["ProtectSystem"] == ["strict"]
    assert unit["ReadWritePaths"] == ["/var/lib/yunshu /etc/yunshu /var/log/yunshu"]
    assert unit["PrivateTmp"] == ["yes"]
    assert unit["CapabilityBoundingSet"] == [""]
    assert unit["ExecStart"][0].startswith("/opt/yunshu/current/yunshu/yunshu ")


def test_unit_roles():
    api, worker, migrate = (_unit(n) for n in UNITS)
    assert api["ExecStart"] == ["/opt/yunshu/current/yunshu/yunshu api"]
    assert worker["ExecStart"] == ["/opt/yunshu/current/yunshu/yunshu worker"]
    assert migrate["ExecStart"] == ["/opt/yunshu/current/yunshu/yunshu migrate"]
    assert api["Restart"] == worker["Restart"] == ["on-failure"]
    assert migrate["Type"] == ["oneshot"]
    assert "Restart" not in migrate  # 迁移失败不自动重试
    assert migrate["RequiredBy"] == ["yunshu-api.service yunshu-worker.service"]
    assert "yunshu-migrate.service" in api["After"][0] and "yunshu-migrate.service" in worker["After"][0]


def test_scripts_agree_on_paths():
    install = (LINUX / "install.sh").read_text(encoding="utf-8")
    uninstall = (LINUX / "uninstall.sh").read_text(encoding="utf-8")
    for script in (install, uninstall):
        assert script.startswith("#!/bin/bash\n")
        assert "set -euo pipefail" in script
        for const in ("PREFIX=/opt/yunshu", "DATA_DIR=/var/lib/yunshu", "CONF_DIR=/etc/yunshu",
                      "KEY_DIR=/etc/yunshu/keys", "LOG_DIR=/var/log/yunshu", "BIN_LINK=/usr/local/bin/yunshu",
                      'WRAPPER_MARK="# yunshu-cli-wrapper"'):
            assert const in script, const
        for unit in UNITS:
            assert unit in script
    assert "read -r answer" in uninstall and '[ "$answer" = PURGE ]' in uninstall
    # 不改防火墙：firewall-cmd / ufw 只出现在提示文字里
    for line in install.splitlines():
        if re.search(r"firewall-cmd --|ufw allow", line):
            assert line.strip().startswith("say "), line


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="需要 Linux 上的 bash")
@pytest.mark.parametrize("script", ["deploy/linux/install.sh", "deploy/linux/uninstall.sh",
                                    "deploy/linux/ci/install-test.sh", "scripts/build-linux.sh"])
def test_shell_syntax(script: str):
    subprocess.run(["bash", "-n", str(ROOT / script)], check=True)


def test_spec_bundles_required_files():
    spec = (ROOT / "scripts" / "yunshu-linux.spec").read_text(encoding="utf-8")
    for needle in ('name="yunshu"', "console=True", "from app.cli import main", 'collect_submodules("app",', "sys.path.insert(0, str(backend))", '"app.main" not in app_modules',
                   'collect_submodules("uvicorn")', 'collect_submodules("pysnmp.smi.mibs")',
                   'collect_data_files("tzdata")', '"alembic.ini"', '"migrations"', "profiles.json", "presets.json",
                   '"LICENSE"', '"frontend"'):
        assert needle in spec, needle
    for path in ("backend/alembic.ini", "backend/migrations", "backend/app/collect/profiles.json",
                 "backend/app/ai/presets.json", "LICENSE"):
        assert (ROOT / path).exists(), path


def test_build_targets_glibc_228_baseline():
    script = (ROOT / "scripts" / "build-linux.sh").read_text(encoding="utf-8")
    assert 'GLIBC_MAX="${YUNSHU_GLIBC_MAX:-2.28}"' in script
    assert re.search(r'^BUILD_IMAGE="almalinux:8@sha256:[0-9a-f]{64}"\r?$', script, re.M)
    # Python 运行时按固定版本与 SHA-256 下载（SQLite 编译在 libpython 里；EL8 自带的 3.26 不支持 RETURNING），
    # 打进包里、运行时加载的 libpython 必须是它
    for arch in ("amd64", "arm64"):
        assert re.search(rf"^PYTHON_SHA256_{arch}=[0-9a-f]{{64}}\r?$", script, re.M), arch
    assert re.search(r"^PYTHON_VERSION=3\.11\.\d+\r?$", script, re.M)
    assert "sha256sum -c" in script and 'sha256sum <"$PYLIB_BUNDLED"' in script and "/proc/$API_PID/maps" in script


def test_workflow_publishes_expected_artifacts():
    wf = (ROOT / ".github" / "workflows" / "linux-package.yml").read_text(encoding="utf-8")
    assert "workflow_call:" in wf
    assert "name: linux-${{ matrix.arch }}" in wf
    assert "--scope linux-package" in wf
    for image in ("openeuler/openeuler:20.03-lts-sp4@", "openeuler/openeuler:22.03-lts-sp4@", "almalinux:8@",
                  "rockylinux/rockylinux:8@"):
        assert image in wf, image
    # 每个 uses 都按提交固定
    for ref in re.findall(r"uses:\s*(\S+)", wf):
        assert re.search(r"@[0-9a-f]{40}$", ref), ref
