"""Walk-forward проверка (раздел 10.3 плана).

Окно: оптимизация на in-sample (по умолчанию 6 мес.) → проверка на следующих out-of-sample
(2 мес.) → сдвиг. Итог считается ТОЛЬКО по out-of-sample. Оптимизируется небольшой набор
параметров (порог уверенности), чтобы не подгонять стратегию под историю.
"""

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.backtest.engine import Backtester, BacktestResult, ClosedTrade, PreparedSymbol
from app.backtest.metrics import equity_stats, trade_stats
from app.trading_config import RiskProfile, RiskSettings

MONTH_MS = 30 * 86_400_000


@dataclass
class WindowResult:
    is_start: int
    oos_start: int
    oos_end: int
    best_threshold: float
    is_score: float
    oos_trades: list[ClosedTrade]
    oos_stats: dict[str, Any]


@dataclass
class WalkForwardResult:
    windows: list[WindowResult]
    oos_trades: list[ClosedTrade] = field(default_factory=list)
    oos_equity: list[tuple[int, float]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


def objective(result: BacktestResult, min_trades: int = 10) -> float:
    """Expectancy (в R) × √сделок: ценит и качество, и статистическую значимость."""
    n = len(result.trades)
    if n < min_trades:
        return float("-inf")
    exp_r = float(np.mean([t.r_multiple for t in result.trades]))
    return exp_r * float(np.sqrt(n))


def walk_forward(
    backtester: Backtester,
    symbols: list[PreparedSymbol],
    *,
    thresholds: tuple[float, ...] = (60, 65, 70, 75, 80),
    in_sample_ms: int = 6 * MONTH_MS,
    out_of_sample_ms: int = 2 * MONTH_MS,
    min_trades: int = 10,
) -> WalkForwardResult:
    start = min(int(s.index[0]) for s in symbols)
    end = max(int(s.index[-1]) for s in symbols)
    base = backtester.config.risk
    windows: list[WindowResult] = []
    oos_trades: list[ClosedTrade] = []
    oos_curve: list[tuple[int, float]] = []
    equity = backtester.bt.initial_equity

    cursor = start
    while cursor + in_sample_ms + out_of_sample_ms <= end + backtester.tf.ms:
        is_end = cursor + in_sample_ms - 1
        oos_start = cursor + in_sample_ms
        oos_end = oos_start + out_of_sample_ms - 1

        best_thr, best_score = thresholds[0], float("-inf")
        for thr in thresholds:
            res = backtester.run(
                symbols, risk=_with_threshold(base, thr), start_ms=cursor, end_ms=is_end
            )
            score = objective(res, min_trades)
            if score > best_score:
                best_thr, best_score = thr, score

        oos = backtester.run(
            symbols, risk=_with_threshold(base, best_thr), start_ms=oos_start, end_ms=oos_end
        )
        # склеиваем OOS-кривые капитала в одну (в относительных приростах)
        scale = equity / oos.initial_equity
        oos_curve.extend((ts, e * scale) for ts, e in oos.equity_curve)
        equity = oos.final_equity * scale
        oos_trades.extend(oos.trades)
        windows.append(
            WindowResult(
                is_start=cursor,
                oos_start=oos_start,
                oos_end=oos_end,
                best_threshold=best_thr,
                is_score=best_score,
                oos_trades=oos.trades,
                oos_stats=trade_stats(oos.trades),
            )
        )
        cursor += out_of_sample_ms

    summary = {
        **equity_stats(oos_curve, backtester.bt.initial_equity, backtester.tf.ms),
        **trade_stats(oos_trades),
        "windows": len(windows),
        "profitable_windows": sum(1 for w in windows if sum(t.pnl for t in w.oos_trades) > 0),
    }
    return WalkForwardResult(windows, oos_trades, oos_curve, summary)


def _with_threshold(risk: RiskSettings, threshold: float) -> RiskSettings:
    data = risk.model_dump()
    data["confidence_threshold"] = threshold
    data["profile"] = RiskProfile.CUSTOM
    return RiskSettings(**data)
