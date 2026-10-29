import pytest

from app.collect import build_adapters
from app.collect.base import PROFILES, infer_profile


def test_all_templates_only_use_available_read_adapters():
    adapters = build_adapters()
    assert {p.batch for p in PROFILES.values()} == {"A", "B", "C"}  # C：4.2 P15 新增的 UPS、RouterOS、群晖
    for profile in PROFILES.values():
        # 4.2 P13：通用 BMC 每次采集都新建 IPMI 会话，默认 5 分钟一次；其余 60 秒
        assert not profile.verified and profile.interval == (300 if profile.name == "generic-bmc" else 60)
        assert profile.platforms and profile.metrics and profile.prerequisites and profile.limitations and profile.evidence
        assert all(s.protocol in adapters for s in profile.steps)


@pytest.mark.parametrize("platform,expected", [("ImmortalWrt", "openwrt"), ("iStoreOS", "openwrt"), ("AlmaLinux 9", "linux-b"), ("Rocky Linux 9", "linux-b"), ("Alpine Linux", "linux-b")])
def test_platform_derivatives_infer_the_correct_template(platform, expected):
    assert infer_profile(platform, "server", {"ssh"}) == expected


@pytest.mark.parametrize("platform,protocols,expected", [("Ubuntu 22.04", {"snmp"}, "linux"), ("PVE 8.1", {"pve-api"}, "pve"), ("", {"esxi-api"}, "esxi")])
def test_snmp_only_linux_and_explicit_api_endpoints(platform, protocols, expected):
    assert infer_profile(platform, "server", protocols) == expected
