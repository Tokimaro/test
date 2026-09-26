from decimal import Decimal

import pytest
from pydantic import SecretStr, ValidationError

from app.config import BACKEND_DIR, RunMode, Settings
from app.domain import Instrument, MarketType, Timeframe
from app.trading_config import RiskProfile, RiskSettings, TradingConfig, resolve_risk


def test_default_config_loads() -> None:
    cfg = TradingConfig.load(BACKEND_DIR / "config" / "default.yaml")
    crypto = cfg.markets["crypto"]
    assert crypto.market_type is MarketType.CRYPTO
    assert crypto.category == "spot"
    assert "BTCUSDT" in crypto.symbols
    assert cfg.risk.profile is RiskProfile.MODERATE
    assert cfg.risk.target_vol_pct == 25.0  # проверенная конфигурация
    assert cfg.strategy.name == "trend"
    assert cfg.strategy.lookbacks == (20, 60, 120)
    assert not cfg.markets["stocks"].enabled


def test_preset_overrides_manual_values() -> None:
    risk = resolve_risk({"profile": "conservative", "target_vol_pct": 80})
    assert risk.target_vol_pct == 15.0
    assert resolve_risk({"profile": "aggressive"}).target_vol_pct == 40.0


def test_custom_profile_keeps_values() -> None:
    risk = resolve_risk({"profile": "custom", "target_vol_pct": 33, "max_drawdown_stop_pct": 30})
    assert risk.target_vol_pct == 33
    assert risk.max_drawdown_stop_pct == 30


@pytest.mark.parametrize(
    "field,value",
    [
        ("target_vol_pct", 2),
        ("target_vol_pct", 150),
        ("max_weight_pct", 0),
        ("max_drawdown_stop_pct", -1),
    ],
)
def test_risk_bounds(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        RiskSettings(profile=RiskProfile.CUSTOM, **{field: value})


def _raw(**market: object) -> dict[str, object]:
    base = {"market_type": "crypto", "category": "spot", "symbols": ["ETHUSDT"]}
    return {"markets": {"x": {**base, **market}}, "risk": {}}


def test_only_validated_markets_can_be_enabled() -> None:
    with pytest.raises(ValidationError, match="проверена только на споте"):
        TradingConfig.from_dict(_raw(category="linear"))
    with pytest.raises(ValidationError, match="проверена только на споте"):
        TradingConfig.from_dict(_raw(market_type="stock", category="stock"))
    # выключенный непроверенный рынок допустим
    TradingConfig.from_dict(_raw(category="linear", enabled=False))


def test_btc_filter_requires_btc() -> None:
    raw = _raw()
    raw["strategy"] = {"btc_filter": True}
    with pytest.raises(ValidationError, match="BTCUSDT"):
        TradingConfig.from_dict(raw)


@pytest.mark.parametrize("lookbacks", [[], [60, 20], [1, 20], [20, 20], [20, 500]])
def test_lookbacks_validated(lookbacks: list[int]) -> None:
    raw = _raw()
    raw["strategy"] = {"lookbacks": lookbacks}
    with pytest.raises(ValidationError):
        TradingConfig.from_dict(raw)


def test_history_days() -> None:
    cfg = TradingConfig.from_dict(_raw()).strategy
    assert cfg.history_days == 121
    raw = _raw(symbols=["BTCUSDT"])
    raw["strategy"] = {"btc_filter": True, "btc_ma_days": 200}
    assert TradingConfig.from_dict(raw).strategy.history_days == 200


def test_live_mode_guards() -> None:
    with pytest.raises(ValidationError, match="testnet"):
        Settings(_env_file=None, mode=RunMode.LIVE, bybit_testnet=True)
    with pytest.raises(ValidationError, match="JWT"):
        Settings(_env_file=None, mode=RunMode.LIVE, bybit_testnet=False)
    ok = Settings(
        _env_file=None,
        mode=RunMode.LIVE,
        bybit_testnet=False,
        bybit_api_key=SecretStr("k"),
        bybit_api_secret=SecretStr("s"),
        jwt_secret=SecretStr("j" * 32),
    )
    assert ok.mode is RunMode.LIVE


def test_timeframe_seconds() -> None:
    assert Timeframe.M15.seconds == 900
    assert Timeframe.H4.seconds == 14_400
    assert Timeframe.D1.seconds == 86_400


def test_instrument_rounding() -> None:
    inst = Instrument(
        symbol="BTCUSDT",
        market_type=MarketType.CRYPTO,
        category="linear",
        tick_size=Decimal("0.10"),
        qty_step=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        max_qty=Decimal("100"),
    )
    assert inst.round_qty(0.12389) == Decimal("0.123")
    assert inst.round_qty(0.0009) == Decimal("0.000")
    assert inst.round_qty(-1) == Decimal(0)
    assert inst.round_price(65000.06) == Decimal("65000.10")
    assert inst.round_price(65000.04) == Decimal("65000.00")
