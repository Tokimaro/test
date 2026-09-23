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
    assert crypto.timeframes.working is Timeframe.H1
    assert cfg.risk.profile is RiskProfile.MODERATE
    assert cfg.risk.risk_per_trade_pct == 1.0
    assert cfg.risk.confidence_threshold == 65.0


def test_preset_overrides_manual_values() -> None:
    risk = resolve_risk({"profile": "conservative", "risk_per_trade_pct": 2.5})
    assert risk.risk_per_trade_pct == 0.5
    assert risk.max_leverage == 3.0


def test_custom_profile_keeps_values() -> None:
    risk = resolve_risk({"profile": "custom", "risk_per_trade_pct": 1.7})
    assert risk.risk_per_trade_pct == 1.7


@pytest.mark.parametrize("value", [0.0, 0.05, 3.5, -1])
def test_risk_per_trade_bounds(value: float) -> None:
    with pytest.raises(ValidationError):
        RiskSettings(profile=RiskProfile.CUSTOM, risk_per_trade_pct=value)


def test_risk_consistency() -> None:
    with pytest.raises(ValidationError):
        RiskSettings(risk_per_trade_pct=3.0, max_total_open_risk_pct=2.0)
    with pytest.raises(ValidationError):
        RiskSettings(daily_loss_limit_pct=8.0, weekly_loss_limit_pct=6.0)


def test_timeframes_must_descend() -> None:
    raw = {
        "markets": {
            "x": {
                "market_type": "crypto",
                "category": "linear",
                "timeframes": {"higher": "15", "working": "60", "entry": "240"},
            }
        },
        "risk": {},
    }
    with pytest.raises(ValidationError):
        TradingConfig.from_dict(raw)


def test_weights_sum_to_one() -> None:
    raw = {
        "markets": {},
        "risk": {},
        "strategy": {"weights": {"trend": {"trend": 0.5, "mean_reversion": 0.1, "breakout": 0.1}}},
    }
    with pytest.raises(ValidationError):
        TradingConfig.from_dict(raw)


def test_live_mode_guards() -> None:
    with pytest.raises(ValidationError, match="testnet"):
        Settings(_env_file=None, mode=RunMode.LIVE, bybit_testnet=True)
    with pytest.raises(ValidationError, match="API_KEY"):
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
