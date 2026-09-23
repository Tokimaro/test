from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_health(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["mode"] == "paper"
