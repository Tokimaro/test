"""Торговая конфигурация: инструменты, таймфреймы, риск, стратегия.

Загружается из YAML (config/default.yaml), может переопределяться из панели.
Все диапазоны валидируются здесь, чтобы ошибка в настройках не дошла до биржи.
"""

from enum import StrEnum
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain import MarketType, Timeframe


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskProfile(StrEnum):
    CONSERVATIVE = "conservative"
    MODERATE = "moderate"
    AGGRESSIVE = "aggressive"
    CUSTOM = "custom"


class RiskSettings(_Model):
    profile: RiskProfile = RiskProfile.MODERATE
    risk_per_trade_pct: float = Field(1.0, ge=0.1, le=3.0)
    max_open_positions: int = Field(5, ge=1, le=20)
    max_total_open_risk_pct: float = Field(4.0, gt=0, le=20.0)
    daily_loss_limit_pct: float = Field(3.0, gt=0, le=20.0)
    weekly_loss_limit_pct: float = Field(6.0, gt=0, le=40.0)
    max_drawdown_stop_pct: float = Field(15.0, gt=0, le=50.0)
    max_leverage: float = Field(5.0, ge=1.0, le=20.0)
    confidence_threshold: float = Field(65.0, ge=50.0, le=95.0)
    # Динамическая коррекция риска
    losing_streak_cut: int = Field(3, ge=1, le=10)
    risk_reduction_factor: float = Field(0.5, gt=0, le=1.0)
    scale_by_confidence: bool = False
    max_correlated_positions: int = Field(2, ge=1, le=20)
    correlation_threshold: float = Field(0.8, gt=0, le=1.0)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.risk_per_trade_pct > self.max_total_open_risk_pct:
            raise ValueError("risk_per_trade_pct не может превышать max_total_open_risk_pct")
        if self.daily_loss_limit_pct > self.weekly_loss_limit_pct:
            raise ValueError("daily_loss_limit_pct не может превышать weekly_loss_limit_pct")
        return self


# Значения профилей из раздела 6.2 плана. CUSTOM — берётся как есть из конфигурации.
RISK_PROFILE_PRESETS: dict[RiskProfile, dict[str, Any]] = {
    RiskProfile.CONSERVATIVE: {
        "risk_per_trade_pct": 0.5,
        "max_open_positions": 3,
        "max_total_open_risk_pct": 2.0,
        "daily_loss_limit_pct": 2.0,
        "weekly_loss_limit_pct": 4.0,
        "max_drawdown_stop_pct": 10.0,
        "confidence_threshold": 72.0,
        "max_leverage": 3.0,
    },
    RiskProfile.MODERATE: {
        "risk_per_trade_pct": 1.0,
        "max_open_positions": 5,
        "max_total_open_risk_pct": 4.0,
        "daily_loss_limit_pct": 3.0,
        "weekly_loss_limit_pct": 6.0,
        "max_drawdown_stop_pct": 15.0,
        "confidence_threshold": 65.0,
        "max_leverage": 5.0,
    },
    RiskProfile.AGGRESSIVE: {
        "risk_per_trade_pct": 2.0,
        "max_open_positions": 8,
        "max_total_open_risk_pct": 8.0,
        "daily_loss_limit_pct": 5.0,
        "weekly_loss_limit_pct": 10.0,
        "max_drawdown_stop_pct": 25.0,
        "confidence_threshold": 60.0,
        "max_leverage": 10.0,
    },
}


def resolve_risk(raw: dict[str, Any]) -> RiskSettings:
    """Применяет пресет профиля. Для не-custom профиля пресет перекрывает ручные значения
    ключевых полей, чтобы «moderate» всегда означал одно и то же."""
    profile = RiskProfile(raw.get("profile", RiskProfile.MODERATE))
    data = dict(raw)
    if profile is not RiskProfile.CUSTOM:
        data.update(RISK_PROFILE_PRESETS[profile])
    return RiskSettings(**data)


class TimeframeSet(_Model):
    higher: Timeframe
    working: Timeframe
    entry: Timeframe

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if not (self.higher.seconds > self.working.seconds > self.entry.seconds):
            raise ValueError("таймфреймы должны убывать: higher > working > entry")
        return self


class MarketSettings(_Model):
    enabled: bool = True
    market_type: MarketType
    category: str  # категория Bybit: linear / spot
    symbols: list[str] = Field(default_factory=list)
    timeframes: TimeframeSet


class RegimeWeights(_Model):
    trend: float = Field(ge=0, le=1)
    mean_reversion: float = Field(ge=0, le=1)
    breakout: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _sum_to_one(self) -> Self:
        total = self.trend + self.mean_reversion + self.breakout
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"сумма весов должна быть 1.0, получено {total}")
        return self


class StrategyWeights(_Model):
    trend: RegimeWeights = RegimeWeights(trend=0.6, mean_reversion=0.1, breakout=0.3)
    range: RegimeWeights = RegimeWeights(trend=0.1, mean_reversion=0.6, breakout=0.3)


class StopSettings(_Model):
    sl_atr_min: float = Field(1.5, gt=0)
    sl_atr_max: float = Field(3.0, gt=0)
    tp1_r: float = Field(1.5, gt=0)
    tp1_close_pct: float = Field(50.0, gt=0, le=100)
    tp2_r: float = Field(3.0, gt=0)
    trailing_atr: float = Field(3.0, gt=0)
    min_rr: float = Field(2.0, gt=0)
    min_rr_mean_reversion: float = Field(1.5, gt=0)
    time_stop_bars: int = Field(24, ge=1)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.sl_atr_min > self.sl_atr_max:
            raise ValueError("sl_atr_min > sl_atr_max")
        if self.tp1_r >= self.tp2_r:
            raise ValueError("tp1_r должен быть меньше tp2_r")
        return self


class StrategySettings(_Model):
    weights: StrategyWeights = StrategyWeights()
    stops: StopSettings = StopSettings()
    adx_trend: float = Field(25.0, gt=0)
    adx_range: float = Field(20.0, gt=0)
    chaos_volatility_percentile: float = Field(95.0, gt=50, le=100)
    volatility_lookback_days: int = Field(90, ge=10)
    max_spread_pct: float = Field(0.1, gt=0)

    @model_validator(mode="after")
    def _adx(self) -> Self:
        if self.adx_range > self.adx_trend:
            raise ValueError("adx_range должен быть <= adx_trend")
        return self


class TradingConfig(_Model):
    markets: dict[str, MarketSettings]
    risk: RiskSettings
    strategy: StrategySettings = StrategySettings()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TradingConfig":
        data = dict(raw)
        data["risk"] = resolve_risk(data.get("risk", {}))
        return cls(**data)

    @classmethod
    def load(cls, path: Path) -> "TradingConfig":
        with path.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return cls.from_dict(raw)
