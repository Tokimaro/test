"""Проверка прогнозов бота на исторических данных.

1. Прогноз в случайные моменты: данные ОБРЕЗАЮТСЯ по моменту прогноза (будущего в расчёте
   физически нет), затем по дальнейшим 15-минутным свечам проверяется, что случилось.
2. Систематическая проверка всех сигналов за период + базовая линия «монетка»
   (те же стопы и цели, случайное направление).
3. Портфельный бэктест с комиссиями и walk-forward (app.backtest.engine).

    uv run --extra research python -m app.backtest.evaluate --data ../data/binance \\
        --test-days 30 --moments 8
"""

import argparse
import json
import random
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.analysis.features import build_features
from app.backtest.dataset import available_symbols, load_candles
from app.backtest.engine import Backtester, BacktestSettings, SymbolData
from app.backtest.metrics import summarize
from app.backtest.walk_forward import walk_forward
from app.config import BACKEND_DIR
from app.domain import Direction, Instrument, MarketType, Timeframe
from app.strategy.base import Row, columns_of
from app.strategy.ensemble import Signal, SignalEngine, prepare
from app.strategy.planner import TradePlan, plan_trade
from app.trading_config import TradingConfig

H = Timeframe.H1.ms
M15 = Timeframe.M15.ms
HORIZON_MS = 24 * H
TAKER_FEE = 0.00055
SLIPPAGE = 0.0005


@dataclass
class Outcome:
    symbol: str
    ts: int  # момент прогноза (закрытие сигнальной свечи)
    direction: str
    confidence: float
    strategy: str
    regime: str
    entry: float
    stop: float
    tp1: float | None
    tp2: float
    first: str  # target / stop / timeout — что было задето раньше
    r_gross: float  # результат в R по правилам ведения (TP1 → безубыток → TP2), без издержек
    r_net: float  # с комиссиями и проскальзыванием
    ret_4h: float  # движение цены в сторону прогноза через 4 и 24 часа, %
    ret_24h: float
    hours: float  # время до исхода


def simulate(plan: TradePlan, entry: float, path: pd.DataFrame) -> tuple[str, float, float]:
    """Проходит по 15m-свечам после входа. Консервативно: если в одной свече задеты
    и стоп, и цель — считается стоп. Возвращает (первый исход, R брутто, часы)."""
    sign = plan.direction.sign
    risk = abs(entry - plan.stop)
    stop = plan.stop
    first_target = plan.tp1 if plan.tp1 is not None else plan.tp2
    frac = plan.tp1_fraction if plan.tp1 is not None else 1.0
    banked, remaining, first = 0.0, 1.0, "timeout"
    for ts, o, h, lo in zip(path.index, path["open"], path["high"], path["low"], strict=True):
        hours = (ts - path.index[0] + M15) / H
        stop_hit = (lo <= stop) if sign > 0 else (h >= stop)
        if stop_hit:
            fill = o if (o - stop) * sign <= 0 else stop  # гэп — по open
            r = banked + remaining * (fill - entry) * sign / risk
            return (first if first != "timeout" else "stop"), r, hours

        def tgt_hit(level: float, h: float = h, lo: float = lo) -> bool:
            return (h >= level) if sign > 0 else (lo <= level)

        if remaining == 1.0 and tgt_hit(first_target):
            first = "target"
            if frac >= 1.0:
                return first, (first_target - entry) * sign / risk, hours
            banked += frac * (first_target - entry) * sign / risk
            remaining -= frac
            stop = entry  # безубыток
            if tgt_hit(plan.tp2):
                return first, banked + remaining * (plan.tp2 - entry) * sign / risk, hours
        elif remaining < 1.0 and tgt_hit(plan.tp2):
            return first, banked + remaining * (plan.tp2 - entry) * sign / risk, hours
    last = float(path["close"].iloc[-1]) if len(path) else entry
    return first, banked + remaining * (last - entry) * sign / risk, HORIZON_MS / H


def evaluate_signal(
    symbol: str, signal: Signal, row: Row, m15: pd.DataFrame, cfg: TradingConfig
) -> Outcome | None:
    plan = plan_trade(signal, row, cfg.strategy.stops)
    if not isinstance(plan, TradePlan) or signal.direction is None:
        return None
    t = signal.ts + H  # сигнальная свеча закрылась — вход по открытию следующей 15m
    path = m15[(m15.index >= t) & (m15.index < t + HORIZON_MS)]
    if len(path) < 4:
        return None
    sign = signal.direction.sign
    entry = float(path["open"].iloc[0])
    if (entry - plan.stop) * sign <= 0:
        return None  # цена ушла за стоп до входа
    first, r_gross, hours = simulate(plan, entry, path)
    risk_pct = abs(entry - plan.stop) / entry
    costs_r = (2 * TAKER_FEE + 2 * SLIPPAGE) / risk_pct

    def fwd(hrs: int) -> float:
        p = m15[m15.index < t + hrs * H]
        return float((p["close"].iloc[-1] / entry - 1) * 100 * sign) if len(p) else 0.0

    return Outcome(
        symbol=symbol,
        ts=t,
        direction=signal.direction.value,
        confidence=signal.confidence,
        strategy=signal.strategy,
        regime=signal.regime.value,
        entry=entry,
        stop=plan.stop,
        tp1=plan.tp1,
        tp2=plan.tp2,
        first=first,
        r_gross=round(r_gross, 3),
        r_net=round(r_gross - costs_r, 3),
        ret_4h=round(fwd(4), 3),
        ret_24h=round(fwd(24), 3),
        hours=round(hours, 2),
    )


@dataclass
class Market:
    h1: pd.DataFrame
    h4: pd.DataFrame
    m15: pd.DataFrame


def load_market(root: Path, symbols: list[str]) -> dict[str, Market]:
    return {
        s: Market(
            load_candles(root, s, Timeframe.H1),
            load_candles(root, s, Timeframe.H4),
            load_candles(root, s, Timeframe.M15),
        )
        for s in symbols
    }


def features_until(m: Market, moment: int, cfg: TradingConfig) -> pd.DataFrame:
    """Признаки только по свечам, ЗАКРЫТЫМ к моменту прогноза (будущее отрезано)."""
    h1 = m.h1[m.h1.index + H <= moment]
    h4 = m.h4[m.h4.index + Timeframe.H4.ms <= moment]
    m15 = m.m15[m.m15.index + M15 <= moment]
    feats = build_features(h1, Timeframe.H1, h4, Timeframe.H4, m15, Timeframe.M15, cfg.strategy)
    return prepare(feats, cfg.strategy)


def random_moments(
    markets: dict[str, Market], cfg: TradingConfig, start: int, end: int, n: int, seed: int
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    engine = SignalEngine(cfg.strategy)
    hours = list(range(start // H * H + H, (end - HORIZON_MS) // H * H, H))
    out = []
    for moment in sorted(rng.sample(hours, n)):
        for symbol, m in markets.items():
            feats = features_until(m, moment, cfg)
            assert int(feats.index[-1]) + H == moment  # последняя свеча — только что закрытая
            signal = engine.evaluate_last(feats, symbol)
            row = Row(columns_of(feats), len(feats) - 1)
            item: dict[str, Any] = {
                "moment": moment,
                "symbol": symbol,
                "price": float(feats["close"].iloc[-1]),
                "direction": signal.direction.value if signal.direction else None,
                "confidence": signal.confidence,
                "regime": signal.regime.value,
                "strategy": signal.strategy,
                "tradable": signal.direction is not None
                and signal.confidence >= cfg.risk.confidence_threshold,
            }
            if signal.direction is not None:
                o = evaluate_signal(symbol, signal, row, m.m15, cfg)
                if o is not None:
                    item.update(
                        entry=o.entry,
                        stop=o.stop,
                        tp1=o.tp1,
                        tp2=o.tp2,
                        first=o.first,
                        r_gross=o.r_gross,
                        r_net=o.r_net,
                        ret_24h=o.ret_24h,
                    )
            out.append(item)
    return out


def all_signals(
    markets: dict[str, Market], cfg: TradingConfig, start: int, end: int
) -> tuple[list[Outcome], list[Outcome]]:
    """Все направленные сигналы за период + «монетка» (случайное направление) как базовая линия."""
    engine = SignalEngine(cfg.strategy)
    rng = random.Random(7)
    outcomes: list[Outcome] = []
    coin: list[Outcome] = []
    for symbol, m in markets.items():
        feats = prepare(
            build_features(
                m.h1, Timeframe.H1, m.h4, Timeframe.H4, m.m15, Timeframe.M15, cfg.strategy
            ),
            cfg.strategy,
        )
        cols = columns_of(feats)
        idx = feats.index.to_numpy()
        for i in np.flatnonzero((idx + H > start) & (idx + H <= end - HORIZON_MS)):
            row = Row(cols, int(i))
            signal = engine.evaluate_row(row, int(idx[i]), symbol)
            if signal.direction is not None:
                o = evaluate_signal(symbol, signal, row, m.m15, cfg)
                if o is not None:
                    outcomes.append(o)
            if signal.regime.tradable and i % 4 == 0:
                d = rng.choice([Direction.LONG, Direction.SHORT])
                fake = Signal(int(idx[i]), symbol, d, 0.0, signal.regime, "trend")
                o = evaluate_signal(symbol, fake, row, m.m15, cfg)
                if o is not None:
                    coin.append(o)
    return outcomes, coin


def group_stats(items: list[Outcome]) -> dict[str, Any]:
    if not items:
        return {"n": 0}
    n = len(items)
    return {
        "n": n,
        "target_first_pct": round(sum(o.first == "target" for o in items) / n * 100, 1),
        "stop_first_pct": round(sum(o.first == "stop" for o in items) / n * 100, 1),
        "dir_ok_4h_pct": round(sum(o.ret_4h > 0 for o in items) / n * 100, 1),
        "dir_ok_24h_pct": round(sum(o.ret_24h > 0 for o in items) / n * 100, 1),
        "avg_r_gross": round(float(np.mean([o.r_gross for o in items])), 3),
        "avg_r_net": round(float(np.mean([o.r_net for o in items])), 3),
    }


BUCKETS = [(0, 50), (50, 65), (65, 80), (80, 101)]


def instrument(symbol: str) -> Instrument:
    return Instrument(
        symbol=symbol,
        market_type=MarketType.CRYPTO,
        category="linear",
        tick_size=Decimal("0.0001"),
        qty_step=Decimal("0.001"),
        min_qty=Decimal("0.001"),
        max_qty=Decimal(10**9),
        max_leverage=Decimal(50),
        taker_fee=Decimal(str(TAKER_FEE)),
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.data)
    cfg = TradingConfig.load(BACKEND_DIR / "config" / "default.yaml")
    market_cfg = cfg.markets["crypto"]
    symbols = args.symbols or available_symbols(root)
    markets = load_market(root, symbols)
    end = min(int(m.h1.index[-1]) + H for m in markets.values())
    start = end - args.test_days * 86_400_000
    report: dict[str, Any] = {
        "period": [_d(start), _d(end)],
        "symbols": symbols,
        "threshold": cfg.risk.confidence_threshold,
    }

    report["random_moments"] = random_moments(markets, cfg, start, end, args.moments, args.seed)

    outcomes, coin = all_signals(markets, cfg, start, end)
    thr = cfg.risk.confidence_threshold
    report["signals"] = {
        "all_directional": group_stats(outcomes),
        "tradable": group_stats([o for o in outcomes if o.confidence >= thr]),
        "coin_flip_baseline": group_stats(coin),
        "by_confidence": {
            f"{lo}-{min(hi, 100)}": group_stats([o for o in outcomes if lo <= o.confidence < hi])
            for lo, hi in BUCKETS
        },
        "by_symbol_tradable": {
            s: group_stats([o for o in outcomes if o.symbol == s and o.confidence >= thr])
            for s in symbols
        },
        "by_strategy_tradable": {
            k: group_stats([o for o in outcomes if o.strategy == k and o.confidence >= thr])
            for k in sorted({o.strategy for o in outcomes})
        },
    }

    data = [SymbolData(s, instrument(s), m.h1, m.h4, m.m15) for s, m in markets.items()]
    bt = Backtester(cfg, market_cfg, BacktestSettings(initial_equity=10_000))
    prepared = bt.prepare(data)
    month = summarize(bt.run(prepared, start_ms=start, end_ms=end))
    report["backtest_test_period"] = month
    warm = int(min(m.h1.index[0] for m in markets.values())) + 100 * 86_400_000
    full = bt.run(prepared, start_ms=warm, end_ms=end)
    report["backtest_full"] = summarize(full)
    report["backtest_full_period"] = [_d(warm), _d(end)]
    wf = walk_forward(
        bt,
        prepared,
        in_sample_ms=60 * 86_400_000,
        out_of_sample_ms=30 * 86_400_000,
        min_trades=5,
    )
    report["walk_forward"] = {
        "summary": wf.summary,
        "windows": [
            {"oos_start": _d(w.oos_start), "threshold": w.best_threshold, **w.oos_stats}
            for w in wf.windows
        ],
    }
    if args.dump:
        Path(args.dump).write_text(
            json.dumps(
                {"report": report, "outcomes": [asdict(o) for o in outcomes]},
                ensure_ascii=False,
                default=str,
                indent=1,
            )
        )
    return report


def _d(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", required=True, help="корень датасета (папка с data/)")
    p.add_argument("--symbols", nargs="*")
    p.add_argument("--test-days", type=int, default=30)
    p.add_argument("--moments", type=int, default=8)
    p.add_argument("--seed", type=int, default=2026)
    p.add_argument("--dump", help="сохранить полный отчёт в JSON")
    report = run(p.parse_args())
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
