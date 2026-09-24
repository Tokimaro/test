from datetime import UTC, datetime
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from app.backtest.engine import Backtester, BacktestResult, BacktestSettings, SymbolData
from app.backtest.metrics import equity_stats, summarize, trade_stats
from app.domain import Instrument, MarketType
from app.research import strategies as research
from app.trading_config import TradingConfig

DAY = 86_400_000
T0 = 1_577_836_800_000  # 2020-01-01 (среда)
SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]


def inst(symbol: str, fee: str = "0.001") -> Instrument:
    return Instrument(
        symbol,
        MarketType.CRYPTO,
        "spot",
        tick_size=Decimal("1e-8"),
        qty_step=Decimal("1e-8"),
        min_qty=Decimal("1e-8"),
        max_qty=Decimal("1e15"),
        taker_fee=Decimal(fee),
        maker_fee=Decimal(fee),
    )


def closes(n: int = 700, seed: int = 0, drift: float = 0.001) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    data = 100 * np.exp(np.cumsum(rng.normal(drift, 0.03, (n, len(SYMS))), axis=0))
    return pd.DataFrame(data, index=pd.Index(T0 + np.arange(n) * DAY), columns=SYMS)


def run(
    c: pd.DataFrame, fee: str = "0.001", slippage: float = 0.0005, **risk: float
) -> tuple[Backtester, BacktestResult]:
    cfg = TradingConfig.from_dict(
        {
            "markets": {"crypto": {"market_type": "crypto", "category": "spot", "symbols": SYMS}},
            "risk": {"profile": "custom", **risk} if risk else {},
        }
    )
    data = [SymbolData(s, inst(s, fee), pd.DataFrame({"close": c[s]})) for s in SYMS]
    bt = Backtester(cfg, cfg.markets["crypto"], BacktestSettings(slippage_pct=slippage))
    return bt, bt.run(bt.prepare(data))


def test_rebalances_only_on_monday_after_first_day() -> None:
    c = closes()
    _, res = run(c)
    assert res.signal_stats["rebalances"] == 1 + sum(
        datetime.fromtimestamp((ts + DAY) / 1000, tz=UTC).weekday() == 0 for ts in c.index[1:]
    )
    days = {datetime.fromtimestamp(t.entry_ts / 1000, tz=UTC).weekday() for t in res.trades}
    days.discard(datetime.fromtimestamp((T0 + DAY) / 1000, tz=UTC).weekday())  # первый день
    assert days <= {0}


def test_trades_are_holding_episodes_and_equity_consistent() -> None:
    _, res = run(closes())
    assert res.trades and all(t.direction == "long" for t in res.trades)
    assert all(t.risk_amount > 0 and t.fees > 0 for t in res.trades)
    net = sum(t.pnl for t in res.trades)
    # после закрытия всех владений весь результат — это сумма сделок
    assert res.final_equity == pytest.approx(res.initial_equity + net, rel=1e-9)
    rep = summarize(res)
    assert rep["summary"]["trades"] == len(res.trades)
    assert set(rep["by_symbol"]) <= set(SYMS)


def test_costs_reduce_result() -> None:
    c = closes()
    _, free = run(c, fee="0", slippage=0)
    _, paid = run(c)
    assert paid.final_equity < free.final_equity


def test_matches_independent_hold_between_rebalances() -> None:
    """Без издержек и порога капитал бота совпадает с независимой формулой: в день
    ребалансировки r — доли w_r из исследовательского кода, до следующей ребалансировки
    монеты просто держатся: V_t = V_r × (1 − Σw + Σ w_i · P_i,t / P_i,r)."""
    c = closes(n=900, seed=5)
    cfg = TradingConfig.from_dict(
        {
            "markets": {"crypto": {"market_type": "crypto", "category": "spot", "symbols": SYMS}},
            "risk": {},
            "strategy": {"min_trade_pct": 0},
        }
    )
    data = [SymbolData(s, inst(s, "0"), pd.DataFrame({"close": c[s]})) for s in SYMS]
    bt = Backtester(cfg, cfg.markets["crypto"], BacktestSettings(slippage_pct=0))
    res = bt.run(bt.prepare(data))
    w = research.tsmom(c, 365, long_only=True, rebalance=1)
    p = c.to_numpy()
    v, ref = 10_000.0, []
    wr, pr = np.zeros(len(SYMS)), p[0]
    for i, ts in enumerate(c.index):
        v_now = v * (1 - wr.sum() + (wr * p[i] / pr).sum())
        if i == 0 or datetime.fromtimestamp((ts + DAY) / 1000, tz=UTC).weekday() == 0:
            v, wr, pr = v_now, w.iloc[i].to_numpy(), p[i]
        ref.append(v_now)
    ours = [e for _, e in res.equity_curve]
    np.testing.assert_allclose(ours[:-1], ref[:-1], rtol=1e-6)


def test_drawdown_stop_goes_to_cash() -> None:
    c = closes(drift=0.002)
    crash = np.r_[np.linspace(1, 0.4, 5), np.full(len(c) - 405, 0.4)]
    c.iloc[400:] = c.iloc[400:] * crash[:, None]  # обвал на 60% за 5 дней
    _, res = run(c, max_drawdown_stop_pct=25)
    assert res.signal_stats.get("drawdown_stop") == 1
    assert any(t.close_reason == "drawdown_stop" for t in res.trades)
    stop_ts = max(t.exit_ts for t in res.trades if t.close_reason == "drawdown_stop")
    after = [e for ts, e in res.equity_curve if ts > stop_ts]
    assert max(after) == pytest.approx(min(after))  # дальше — только кэш


def test_equity_and_trade_stats() -> None:
    _, res = run(closes())
    st = equity_stats(res.equity_curve, res.initial_equity, DAY)
    assert {"sharpe", "cagr_pct", "max_drawdown_pct"} <= set(st)
    ts = trade_stats(res.trades)
    assert 0 <= ts["win_rate"] <= 100 and "avg_return_pct" in ts
