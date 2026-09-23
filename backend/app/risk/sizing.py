"""Расчёт размера позиции (раздел 6.1 плана).

    risk_amount   = equity × risk_pct
    qty           = risk_amount / (|entry − stop| + издержки на единицу)
    qty           → вниз к qty_step, проверки minQty / minNotional / maxQty
    leverage      = минимальное, при котором хватает свободной маржи (не рычаг прибыли!)

Плечо не увеличивает риск: риск определяется расстоянием до стопа и объёмом.
"""

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from app.domain import Direction, Instrument

MAINTENANCE_MARGIN_RATE = Decimal("0.005")


@dataclass(frozen=True, slots=True)
class SizingResult:
    qty: Decimal
    risk_amount: Decimal  # фактический риск с учётом издержек после округления объёма
    notional: Decimal
    leverage: Decimal
    reject: str | None = None

    @property
    def ok(self) -> bool:
        return self.reject is None


def _reject(reason: str) -> SizingResult:
    z = Decimal(0)
    return SizingResult(z, z, z, Decimal(1), reason)


def size_position(
    *,
    equity: Decimal,
    risk_pct: float,
    direction: Direction,
    entry: Decimal,
    stop: Decimal,
    instrument: Instrument,
    available_margin: Decimal,
    max_leverage: Decimal,
    atr: Decimal | None = None,
    slippage_pct: Decimal = Decimal("0.0005"),
    derivatives: bool = True,
) -> SizingResult:
    if equity <= 0 or entry <= 0 or stop <= 0:
        return _reject("invalid_inputs")
    if (entry - stop) * direction.sign <= 0:
        return _reject("stop_on_wrong_side")
    if risk_pct <= 0:
        return _reject("zero_risk")

    stop_dist = abs(entry - stop)
    fee = instrument.taker_fee
    # Вход и выход по стопу — рыночные: комиссия на обе ноги + проскальзывание на обеих
    cost_per_unit = entry * fee + stop * fee + (entry + stop) * slippage_pct
    loss_per_unit = stop_dist + cost_per_unit
    risk_budget = equity * Decimal(str(risk_pct)) / 100

    qty = instrument.round_qty(risk_budget / loss_per_unit)
    if instrument.max_qty > 0:
        qty = min(qty, instrument.round_qty(instrument.max_qty))

    lev_cap = (
        max(Decimal(1), min(max_leverage, instrument.max_leverage)) if derivatives else Decimal(1)
    )
    max_notional = max(available_margin, Decimal(0)) * lev_cap
    if qty * entry > max_notional:
        # не хватает маржи даже на максимальном плече — уменьшаем объём (риск только падает)
        qty = instrument.round_qty(max_notional / entry)

    if qty <= 0 or qty < instrument.min_qty:
        return _reject("below_min_qty")
    notional = qty * entry
    if notional < instrument.min_notional:
        return _reject("below_min_notional")

    if derivatives and available_margin > 0:
        leverage = (notional / available_margin).to_integral_value(rounding=ROUND_CEILING)
        leverage = max(Decimal(1), min(leverage, lev_cap))
    else:
        leverage = Decimal(1)

    if derivatives and leverage > 1:
        # Ликвидация должна быть дальше стопа хотя бы на 1 ATR (приближённо, изолированная маржа)
        liq_dist = entry * (1 / leverage - MAINTENANCE_MARGIN_RATE)
        buffer = atr if atr is not None else Decimal(0)
        if liq_dist <= stop_dist + buffer:
            return _reject("liquidation_too_close")

    return SizingResult(
        qty=qty,
        risk_amount=qty * loss_per_unit,
        notional=notional,
        leverage=leverage,
    )
