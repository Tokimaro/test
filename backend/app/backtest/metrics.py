"""Метрики результатов торговли: кривая капитала и владения монетами."""

import math
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

import numpy as np

from app.backtest.engine import BacktestResult, ClosedTrade

YEAR_MS = 365 * 86_400_000


def trade_stats(trades: Iterable[ClosedTrade]) -> dict[str, Any]:
    """Статистика владений монетами (от покупки до полной продажи).
    r_multiple у стратегии тренда — доходность владения (pnl / вложено)."""
    ts = list(trades)
    n = len(ts)
    if n == 0:
        return {"trades": 0}
    pnl = np.array([t.pnl for t in ts])
    ret = np.array([t.r_multiple for t in ts]) * 100
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_win, gross_loss = float(wins.sum()), float(-losses.sum())
    return {
        "trades": n,
        "win_rate": round(len(wins) / n * 100, 2),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
        "avg_return_pct": round(float(ret.mean()), 3),
        "avg_win_return_pct": round(float(ret[pnl > 0].mean()), 3) if len(wins) else 0.0,
        "avg_loss_return_pct": round(float(ret[pnl <= 0].mean()), 3) if len(losses) else 0.0,
        "best_return_pct": round(float(ret.max()), 2),
        "worst_return_pct": round(float(ret.min()), 2),
        "avg_win": round(float(wins.mean()), 2) if len(wins) else 0.0,
        "avg_loss": round(float(losses.mean()), 2) if len(losses) else 0.0,
        "net_pnl": round(float(pnl.sum()), 2),
        "fees": round(sum(t.fees for t in ts), 2),
        "avg_days_held": round(sum(t.bars_held for t in ts) / n, 1),
    }


def equity_stats(curve: list[tuple[int, float]], initial: float, bar_ms: int) -> dict[str, Any]:
    if len(curve) < 2:
        return {}
    eq = np.array([initial] + [e for _, e in curve], dtype=float)
    peak = np.maximum.accumulate(eq)
    dd = (peak - eq) / peak
    rets = np.diff(eq) / eq[:-1]
    bars_per_year = YEAR_MS / bar_ms
    std = rets.std(ddof=1)
    downside = rets[rets < 0]
    down_std = math.sqrt(float((downside**2).mean())) if len(downside) else 0.0
    years = (curve[-1][0] - curve[0][0] + bar_ms) / YEAR_MS
    total = float(eq[-1] / initial - 1)
    cagr = float((eq[-1] / initial) ** (1 / years) - 1) if years > 0 and eq[-1] > 0 else -1.0
    max_dd = float(dd.max())
    return {
        "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "sharpe": round(float(rets.mean() / std * math.sqrt(bars_per_year)), 3) if std > 0 else 0.0,
        "sortino": round(float(rets.mean() / down_std * math.sqrt(bars_per_year)), 3)
        if down_std > 0
        else 0.0,
        "calmar": round(cagr / max_dd, 3) if max_dd > 0 else None,
        "final_equity": round(float(eq[-1]), 2),
        "years": round(years, 3),
    }


def breakdown(trades: list[ClosedTrade], key: str) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[ClosedTrade]] = defaultdict(list)
    for t in trades:
        groups[str(getattr(t, key))].append(t)
    return {k: trade_stats(v) for k, v in sorted(groups.items())}


def summarize(result: BacktestResult) -> dict[str, Any]:
    trades = result.trades
    return {
        "summary": {
            **equity_stats(result.equity_curve, result.initial_equity, result.bar_ms),
            **trade_stats(trades),
        },
        "by_symbol": breakdown(trades, "symbol"),
        "by_close_reason": breakdown(trades, "close_reason"),
        "signals": result.signal_stats,
        "risk_events": len(result.risk_events),
    }
