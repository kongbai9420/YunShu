from fastapi.testclient import TestClient

from app import __version__
from app.main import create_app


def test_health_returns_ok_and_version() -> None:
    client = TestClient(create_app())

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "4.4.0-beta.1"}
    assert __version__ == "4.4.0-beta.1"


def test_serves_built_frontend_without_shadowing_api(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from app.settings import Settings

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>云枢</title>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    cfg = Settings(data_dir=tmp_path / "data", database_url="sqlite://", master_key_file=tmp_path / "k", frontend_dir=dist)
    client = TestClient(create_app(cfg))
    assert "云枢" in client.get("/").text
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/api/does-not-exist").status_code == 404
