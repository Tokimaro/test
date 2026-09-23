"""Метрики результатов торговли (разделы 9.1 и 10 плана)."""

import math
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

import numpy as np

from app.backtest.engine import BacktestResult, ClosedTrade

YEAR_MS = 365 * 86_400_000
CONFIDENCE_BUCKETS = [(50, 60), (60, 65), (65, 70), (70, 75), (75, 80), (80, 90), (90, 101)]


def trade_stats(trades: Iterable[ClosedTrade]) -> dict[str, Any]:
    ts = list(trades)
    n = len(ts)
    if n == 0:
        return {"trades": 0}
    pnl = np.array([t.pnl for t in ts])
    r = np.array([t.r_multiple for t in ts])
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_win, gross_loss = float(wins.sum()), float(-losses.sum())
    return {
        "trades": n,
        "win_rate": round(len(wins) / n * 100, 2),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
        "expectancy_r": round(float(r.mean()), 4),
        "avg_r_win": round(float(r[pnl > 0].mean()), 4) if len(wins) else 0.0,
        "avg_r_loss": round(float(r[pnl <= 0].mean()), 4) if len(losses) else 0.0,
        "avg_win": round(float(wins.mean()), 2) if len(wins) else 0.0,
        "avg_loss": round(float(losses.mean()), 2) if len(losses) else 0.0,
        "best_r": round(float(r.max()), 3),
        "worst_r": round(float(r.min()), 3),
        "net_pnl": round(float(pnl.sum()), 2),
        "fees": round(sum(t.fees for t in ts), 2),
        "funding": round(sum(t.funding for t in ts), 2),
        "avg_bars_held": round(sum(t.bars_held for t in ts) / n, 1),
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


def confidence_calibration(trades: list[ClosedTrade]) -> list[dict[str, Any]]:
    """Фактический винрейт в корзинах уверенности — можно ли ей доверять."""
    out = []
    for lo, hi in CONFIDENCE_BUCKETS:
        bucket = [t for t in trades if lo <= t.confidence < hi]
        if not bucket:
            continue
        wins = sum(1 for t in bucket if t.pnl > 0)
        out.append(
            {
                "bucket": f"{lo}-{min(hi, 100)}",
                "trades": len(bucket),
                "win_rate": round(wins / len(bucket) * 100, 2),
                "expectancy_r": round(sum(t.r_multiple for t in bucket) / len(bucket), 4),
            }
        )
    return out


def summarize(result: BacktestResult) -> dict[str, Any]:
    trades = result.trades
    return {
        "summary": {
            **equity_stats(result.equity_curve, result.initial_equity, result.bar_ms),
            **trade_stats(trades),
        },
        "by_strategy": breakdown(trades, "strategy"),
        "by_symbol": breakdown(trades, "symbol"),
        "by_regime": breakdown(trades, "regime"),
        "by_close_reason": breakdown(trades, "close_reason"),
        "by_direction": breakdown(trades, "direction"),
        "calibration": confidence_calibration(trades),
        "signals": result.signal_stats,
        "risk_events": len(result.risk_events),
    }


def monte_carlo_drawdown(
    r_multiples: list[float], risk_pct: float, runs: int = 1000, seed: int = 0
) -> dict[str, float]:
    """Перемешивание порядка сделок: распределение максимальной просадки (раздел 10.4)."""
    if not r_multiples:
        return {}
    rng = np.random.default_rng(seed)
    r = np.array(r_multiples)
    dds = np.empty(runs)
    for k in range(runs):
        eq = np.cumprod(1 + rng.permutation(r) * risk_pct / 100)
        eq = np.concatenate([[1.0], eq])
        peak = np.maximum.accumulate(eq)
        dds[k] = ((peak - eq) / peak).max()
    return {
        "dd_median_pct": round(float(np.median(dds)) * 100, 2),
        "dd_p95_pct": round(float(np.percentile(dds, 95)) * 100, 2),
        "dd_max_pct": round(float(dds.max()) * 100, 2),
    }
