"""Торговая конфигурация: инструменты, риск, стратегия дневного тренда.

Загружается из YAML (config/default.yaml), может переопределяться из панели.
Все диапазоны валидируются здесь, чтобы ошибка в настройках не дошла до биржи.
"""

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain import MarketType


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskProfile(StrEnum):
    CONSERVATIVE = "conservative"
    MODERATE = "moderate"
    AGGRESSIVE = "aggressive"
    CUSTOM = "custom"


class RiskSettings(_Model):
    """Риск портфеля.

    target_vol_pct — целевая годовая волатильность каждой позиции: чем выше, тем больше доля
    монеты в портфеле (суммарно не больше 100% капитала — спот без плеча). 25% — значение,
    с которым стратегия проверялась.
    max_weight_pct — предел доли одной монеты.
    max_drawdown_stop_pct — при просадке капитала глубже этого значения бот продаёт всё в USDT
    и останавливается до ручного возобновления (0 — выключено; в проверке не использовалось).
    """

    profile: RiskProfile = RiskProfile.MODERATE
    target_vol_pct: float = Field(25.0, ge=5.0, le=100.0)
    max_weight_pct: float = Field(100.0, gt=0, le=100.0)
    max_drawdown_stop_pct: float = Field(0.0, ge=0, le=90.0)


RISK_PROFILE_PRESETS: dict[RiskProfile, dict[str, Any]] = {
    RiskProfile.CONSERVATIVE: {"target_vol_pct": 15.0},
    RiskProfile.MODERATE: {"target_vol_pct": 25.0},
    RiskProfile.AGGRESSIVE: {"target_vol_pct": 40.0},
}


def resolve_risk(raw: dict[str, Any]) -> RiskSettings:
    """Применяет пресет профиля: для не-custom профиля пресет перекрывает ручные значения,
    чтобы «moderate» всегда означал одно и то же."""
    profile = RiskProfile(raw.get("profile", RiskProfile.MODERATE))
    data = dict(raw)
    if profile is not RiskProfile.CUSTOM:
        data.update(RISK_PROFILE_PRESETS[profile])
    return RiskSettings(**data)


class MarketSettings(_Model):
    enabled: bool = True
    market_type: MarketType
    broker: str = "bybit"  # bybit | alpaca
    category: str  # категория Bybit: spot; для акций — "stock"
    symbols: list[str] = Field(default_factory=list)


class StrategySettings(_Model):
    """Дневной тренд «лонг или кэш» (time-series momentum, только лонг).

    Единственная стратегия бота: по итогам проверки (docs/research-strategies.md,
    docs/intraday-strategies.md, docs/volatile-pairs.md) только она дала устойчивый результат
    вне выборки. Значения по умолчанию — проверенная конфигурация.

    Каждый день по закрытию дневной свечи: сигнал монеты — среднее знаков её доходности за
    lookbacks дней (только положительная часть: падающая монета — 0, т.е. кэш); доля в портфеле
    — сигнал / √(число монет) × целевая волатильность / волатильность монеты за vol_lookback
    дней; сумма долей ≤ 100%. Портфель перестраивается раз в неделю (rebalance_weekday).
    """

    name: Literal["trend"] = "trend"
    lookbacks: tuple[int, ...] = (20, 60, 120)
    vol_lookback: int = Field(30, ge=5, le=365)
    rebalance_weekday: int = Field(0, ge=0, le=6)  # 0 — понедельник (UTC)
    # Держать монеты только когда BTC выше своей btc_ma_days-дневной средней. Вне выборки
    # 2022–26 улучшал результат, но в отборе 2018–21 был хуже — по умолчанию выключен.
    btc_filter: bool = False
    btc_ma_days: int = Field(200, ge=20, le=400)
    # Не торговать монету, если её доля меняется меньше чем на min_trade_pct% капитала
    min_trade_pct: float = Field(1.0, ge=0, le=20)

    @model_validator(mode="after")
    def _lookbacks(self) -> Self:
        lbs = self.lookbacks
        if not lbs or any(not 2 <= lb <= 400 for lb in lbs) or list(lbs) != sorted(set(lbs)):
            raise ValueError("lookbacks: возрастающие периоды от 2 до 400 дней")
        return self

    @property
    def history_days(self) -> int:
        """Сколько дней истории нужно для сигнала."""
        need = max(self.lookbacks) + 1
        need = max(need, self.vol_lookback + 1)
        return max(need, self.btc_ma_days) if self.btc_filter else need


# стратегия проверялась на споте: только лонг, без плеча, комиссия Bybit 0.1%
TREND_CATEGORIES = ("spot",)


class TradingConfig(_Model):
    markets: dict[str, MarketSettings]
    risk: RiskSettings
    strategy: StrategySettings = StrategySettings()

    @model_validator(mode="after")
    def _supported_markets(self) -> Self:
        for name, m in self.markets.items():
            if m.enabled and self.strategy.btc_filter and "BTCUSDT" not in m.symbols:
                raise ValueError(f"рынок {name}: для btc_filter в списке монет нужен BTCUSDT")
            if m.enabled and (m.category not in TREND_CATEGORIES or m.market_type != "crypto"):
                raise ValueError(
                    f"рынок {name}: стратегия проверена только на споте криптовалют "
                    f"(market_type: crypto, category: spot); {m.market_type}/{m.category} — "
                    "не проверялся, включать его нельзя"
                )
        return self

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
