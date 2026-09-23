from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pyotp
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.context import AppContext
from app.api.users import create_user
from app.config import RunMode, Settings
from app.db.candles import upsert_instrument
from app.db.models import SecretRow, SettingRow, TradeRow
from app.db.repo import TradeRepo
from app.domain import Instrument, MarketType
from app.main import create_app

pytestmark = pytest.mark.db

PASSWORD = "correct horse battery"


def app_settings(url: str, **kw: Any) -> Settings:
    return Settings(
        _env_file=None,
        database_url=url,
        mode=RunMode.PAPER,
        run_bot=False,
        log_json=False,
        jwt_secret=SecretStr("t" * 40),
        **kw,
    )


@pytest.fixture
async def seeded(db_sessionmaker: async_sessionmaker[AsyncSession]) -> str:
    """Пользователь с 2FA, инструмент и две закрытые сделки. Возвращает TOTP-секрет."""
    uri = await create_user(db_sessionmaker, "admin", PASSWORD, with_2fa=True)
    assert uri is not None
    secret = pyotp.parse_uri(uri).secret
    inst = Instrument(
        symbol="BTCUSDT",
        market_type=MarketType.CRYPTO,
        category="linear",
        tick_size=Decimal("0.1"),
        qty_step=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        max_qty=Decimal(100),
    )
    async with db_sessionmaker() as s, s.begin():
        iid = await upsert_instrument(s, "bybit", inst)
    repo = TradeRepo(db_sessionmaker, "paper")
    for pnl, r in ((150.0, 1.5), (-100.0, -1.0)):
        tid = await repo.create_trade(
            instrument_id=iid,
            strategy="trend",
            direction="long",
            status="closed",
            qty=Decimal(1),
            remaining_qty=Decimal(0),
            initial_stop=Decimal(90),
            stop_loss=Decimal(90),
            tp2=Decimal(130),
            risk_amount=Decimal(100),
            confidence=72.0,
            entry_price=Decimal(100),
            exit_price=Decimal(100 + pnl / 1),
            realized_pnl=Decimal(str(pnl)),
            r_multiple=r,
            close_reason="tp2" if pnl > 0 else "sl",
            closed_at=datetime.now(UTC),
            extra={"regime": "trend_up"},
        )
        assert tid > 0
    return secret


@pytest.fixture
def client(migrated_db: str, seeded: str) -> Iterator[TestClient]:
    with TestClient(create_app(app_settings(migrated_db))) as c:
        yield c


def login(client: TestClient, secret: str) -> dict[str, str]:
    code = pyotp.TOTP(secret).now()
    resp = client.post(
        "/api/auth/login", json={"login": "admin", "password": PASSWORD, "totp": code}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def test_login_requires_password_and_totp(client: TestClient, seeded: str) -> None:
    assert (
        client.post("/api/auth/login", json={"login": "admin", "password": "nope"}).status_code
        == 401
    )
    bad_code = client.post(
        "/api/auth/login", json={"login": "admin", "password": PASSWORD, "totp": "000000"}
    )
    assert bad_code.status_code == 401
    headers = login(client, seeded)
    assert client.get("/api/auth/me", headers=headers).json() == {"login": "admin"}


def test_login_rate_limited(client: TestClient, seeded: str) -> None:
    for _ in range(5):
        client.post("/api/auth/login", json={"login": "admin", "password": "wrong"})
    resp = client.post(
        "/api/auth/login",
        json={"login": "admin", "password": PASSWORD, "totp": pyotp.TOTP(seeded).now()},
    )
    assert resp.status_code == 429


@pytest.mark.parametrize(
    "path",
    ["/api/status", "/api/trades", "/api/positions", "/api/stats", "/api/settings", "/api/signals"],
)
def test_endpoints_require_auth(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer forged"}).status_code == 401


def test_trades_stats_and_csv(client: TestClient, seeded: str) -> None:
    h = login(client, seeded)
    body = client.get("/api/trades", headers=h).json()
    assert body["total"] == 2
    assert {t["close_reason"] for t in body["items"]} == {"tp2", "sl"}
    wins = client.get("/api/trades", params={"result": "win"}, headers=h).json()
    assert wins["total"] == 1
    detail = client.get(f"/api/trades/{body['items'][0]['id']}", headers=h).json()
    assert detail["symbol"] == "BTCUSDT" and detail["orders"] == []
    assert client.get("/api/trades/99999", headers=h).status_code == 404

    stats = client.get("/api/stats", headers=h).json()
    s = stats["summary"]
    assert s["trades"] == 2 and s["win_rate"] == 50.0
    assert s["profit_factor"] == 1.5
    assert s["expectancy_r"] == 0.25
    assert stats["by_regime"]["trend_up"]["trades"] == 2

    csv_resp = client.get("/api/trades.csv", headers=h)
    assert csv_resp.status_code == 200
    assert csv_resp.text.count("\n") == 3  # заголовок + 2 сделки


def test_settings_validation_and_history(client: TestClient, seeded: str, migrated_db: str) -> None:
    h = login(client, seeded)
    cfg = client.get("/api/settings", headers=h).json()
    bad = {**cfg, "risk": {**cfg["risk"], "profile": "custom", "risk_per_trade_pct": 50}}
    assert client.put("/api/settings", json=bad, headers=h).status_code == 422

    good = {**cfg, "risk": {**cfg["risk"], "profile": "custom", "risk_per_trade_pct": 0.7}}
    resp = client.put("/api/settings", json=good, headers=h)
    assert resp.status_code == 200
    assert resp.json()["config"]["risk"]["risk_per_trade_pct"] == 0.7
    assert client.get("/api/settings", headers=h).json()["risk"]["risk_per_trade_pct"] == 0.7
    history = client.get("/api/settings/history", headers=h).json()
    assert history[0]["updated_by"] == "admin"


def test_control_without_engine(client: TestClient, seeded: str) -> None:
    h = login(client, seeded)
    assert client.post("/api/control/pause", headers=h).status_code == 409
    assert client.post("/api/control/kill", json={"confirm": "no"}, headers=h).status_code == 400
    assert client.get("/api/positions", headers=h).json() == []
    status = client.get("/api/status", headers=h).json()
    assert status["running"] is False and status["mode"] == "paper"


def test_secrets_need_master_key(client: TestClient, seeded: str) -> None:
    h = login(client, seeded)
    resp = client.put(
        "/api/secrets/bybit", json={"api_key": "KEY12345678", "api_secret": "SECRET123"}, headers=h
    )
    assert resp.status_code == 409


async def test_secrets_stored_encrypted(
    migrated_db: str, seeded: str, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    key = Fernet.generate_key().decode()
    with TestClient(create_app(app_settings(migrated_db, master_key=SecretStr(key)))) as c:
        h = login(c, seeded)
        resp = c.put(
            "/api/secrets/bybit",
            json={"api_key": "KEY12345678", "api_secret": "SECRET123"},
            headers=h,
        )
        assert resp.status_code == 200
        assert c.get("/api/secrets", headers=h).json()["bybit_api_key"] == "KEY1…78"
    async with db_sessionmaker() as s:
        rows = (await s.scalars(select(SecretRow))).all()
    assert len(rows) == 2
    assert all("KEY12345678" not in r.ciphertext and "SECRET123" not in r.ciphertext for r in rows)
    assert Fernet(key.encode()).decrypt(rows[0].ciphertext.encode()).decode() in (
        "KEY12345678",
        "SECRET123",
    )


def test_websocket_auth_and_events(client: TestClient, seeded: str) -> None:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/api/ws?token=bad") as ws:
        ws.receive_json()
    token = login(client, seeded)["Authorization"].split()[1]
    ctx: AppContext = client.app.state.ctx  # type: ignore[attr-defined]
    with client.websocket_connect(f"/api/ws?token={token}") as ws:
        client.portal.call(_publish, ctx)  # type: ignore[union-attr]
        msg = ws.receive_json()
    assert msg["type"] == "alert" and msg["data"]["kind"] == "test"


async def _publish(ctx: AppContext) -> None:
    ctx.bus.publish("alert", level="warning", kind="test")


async def test_settings_row_persisted(
    client: TestClient, seeded: str, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    h = login(client, seeded)
    cfg = client.get("/api/settings", headers=h).json()
    client.put("/api/settings", json=cfg, headers=h)
    async with db_sessionmaker() as s:
        rows = (await s.scalars(select(SettingRow))).all()
        trades = (await s.scalars(select(TradeRow))).all()
    assert len(rows) == 1 and rows[0].key == "trading_config"
    assert len(trades) == 2
