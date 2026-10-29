from datetime import timedelta

from app import queue
from app.stability_bench import run


def test_real_worker_restart_and_crash_recovery(tmp_path, monkeypatch):
    # CI 缩短退避等待；正式 10 分钟演练保留真实的 30 秒退避。
    monkeypatch.setattr(queue, "retry_delay", lambda attempt: timedelta(milliseconds=50))
    report = run(tmp_path / "soak.db", seconds=4, devices=2, n_metrics=3)
    assert report["status"] == "ok"
    assert report["samples_written"] >= 4
    assert report["read_attempts"] == 2 and report["control_attempts"] == 1
    assert report["control_calls"] == 0 and report["lock_released"]
    assert {sample["phase"] for sample in report["growth_samples"]} == {1, 2}
