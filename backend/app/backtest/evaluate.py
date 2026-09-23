"""Проверка прогнозов бота на исторических данных.

Каждый прогноз доводится до конца — до тейк-профита или стопа (без ограничения по
времени). Ведение: стоп → убыток; TP1 → частичная фиксация и стоп в безубыток → TP2 или
безубыток. Если в одной свече задеты и стоп, и цель — считается стоп (консервативно).
Сделки, не завершившиеся к концу данных, исключаются и считаются отдельно.

1. Прогнозы в случайные моменты: данные ОБРЕЗАЮТСЯ по моменту прогноза.
2. Все сигналы периода и «последовательные» сделки (по монете новая — только после
   закрытия предыдущей), базовая линия «монетка», сравнение с фильтром тренда и без.
3. Портфельный бэктест с комиссиями, лимитами риска и walk-forward.

    uv run --extra research python -m app.backtest.evaluate --data ../data/binance --test-days 365
"""

import argparse
import json
import random
from collections import defaultdict
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
from app.strategy.base import MarketContext, Row, columns_of
from app.strategy.ensemble import Signal, SignalEngine, prepare
from app.strategy.planner import TradePlan, plan_trade
from app.trading_config import TradingConfig

H = Timeframe.H1.ms
M15 = Timeframe.M15.ms
DAY = 86_400_000
TAKER_FEE = 0.00055
SLIPPAGE = 0.0005


# ---------------------------------------------------------------------- исход сделки
@dataclass(frozen=True)
class Resolution:
    kind: str  # "tp" — цель, "tp1_be" — TP1 и остаток в безубыток, "sl" — стоп
    r: float  # результат в R без издержек
    exit_idx: int  # индекс 15m-свечи выхода


class Path15:
    """15-минутные свечи в numpy для быстрого поиска первого касания уровня."""

    def __init__(self, m15: pd.DataFrame) -> None:
        self.ts = m15.index.to_numpy(dtype="int64")
        self.open = m15["open"].to_numpy()
        self.high = m15["high"].to_numpy()
        self.low = m15["low"].to_numpy()
        self.close = m15["close"].to_numpy()

    def first(self, start: int, sign: int, adverse: float, favourable: float) -> tuple[int, str]:
        """Первая свеча с индекса start, где задет adverse (стоп) или favourable (цель).
        Возвращает (индекс, "adverse"/"favourable"/"none"); одновременное касание — adverse."""
        n = len(self.ts)
        step = 512
        i = start
        while i < n:
            j = min(n, i + step)
            lo, hi = self.low[i:j], self.high[i:j]
            if sign > 0:
                adv, fav = lo <= adverse, hi >= favourable
            else:
                adv, fav = hi >= adverse, lo <= favourable
            hit = adv | fav
            if hit.any():
                k = int(np.argmax(hit))
                return i + k, "adverse" if adv[k] else "favourable"
            i, step = j, step * 4
        return n, "none"


def resolve(plan: TradePlan, entry: float, start: int, p: Path15) -> Resolution | None:
    sign = plan.direction.sign
    risk = abs(entry - plan.stop)
    single = plan.tp1 is None or plan.tp1_fraction >= 1.0
    target1 = plan.tp2 if single or plan.tp1 is None else plan.tp1
    k, what = p.first(start, sign, plan.stop, target1)
    if what == "none":
        return None
    if what == "adverse":
        o = p.open[k]
        fill = o if (o - plan.stop) * sign <= 0 else plan.stop  # гэп через стоп — по open
        return Resolution("sl", float((fill - entry) * sign / risk), k)
    if single:
        return Resolution("tp", (target1 - entry) * sign / risk, k)
    frac = plan.tp1_fraction
    banked = frac * (target1 - entry) * sign / risk
    rest = 1.0 - frac
    tp2_now = p.high[k] >= plan.tp2 if sign > 0 else p.low[k] <= plan.tp2
    if tp2_now:
        return Resolution("tp", banked + rest * (plan.tp2 - entry) * sign / risk, k)
    k2, what2 = p.first(k + 1, sign, entry, plan.tp2)  # стоп перенесён в безубыток
    if what2 == "none":
        return None
    if what2 == "adverse":
        return Resolution("tp1_be", banked, k2)
    return Resolution("tp", banked + rest * (plan.tp2 - entry) * sign / risk, k2)


@dataclass
class Outcome:
    symbol: str
    ts: int  # вход (открытие 15m-свечи после закрытия сигнальной)
    exit_ts: int
    direction: str
    confidence: float
    strategy: str
    regime: str
    trend: int  # долгосрочный тренд монеты: +1/0/-1
    with_trend: int  # +1 по тренду, -1 против, 0 нейтрально
    entry: float
    stop: float
    kind: str
    r_gross: float
    r_net: float


def evaluate_signal(
    symbol: str, signal: Signal, row: Row, p: Path15, cfg: TradingConfig
) -> Outcome | None:
    plan = plan_trade(signal, row, cfg.strategy.stops)
    if not isinstance(plan, TradePlan) or signal.direction is None:
        return None
    t = signal.ts + H
    start = int(np.searchsorted(p.ts, t))
    if start >= len(p.ts) or p.ts[start] - t > 2 * H:
        return None
    sign = signal.direction.sign
    entry = float(p.open[start])
    if (entry - plan.stop) * sign <= 0:
        return None  # цена ушла за стоп ещё до входа
    res = resolve(plan, entry, start, p)
    if res is None:
        return None
    costs_r = (2 * TAKER_FEE + 2 * SLIPPAGE) / (abs(entry - plan.stop) / entry)
    trend = int(row["long_trend"])
    return Outcome(
        symbol=symbol,
        ts=t,
        exit_ts=int(p.ts[res.exit_idx]),
        direction=signal.direction.value,
        confidence=signal.confidence,
        strategy=signal.strategy,
        regime=signal.regime.value,
        trend=trend,
        with_trend=trend * sign,
        entry=entry,
        stop=plan.stop,
        kind=res.kind,
        r_gross=round(res.r, 4),
        r_net=round(res.r - costs_r, 4),
    )


# ---------------------------------------------------------------------- данные
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


def features(m: Market, cfg: TradingConfig, moment: int | None = None) -> pd.DataFrame:
    """Признаки; с moment — только по свечам, ЗАКРЫТЫМ к этому моменту (будущее отрезано)."""
    h1, h4, m15 = m.h1, m.h4, m.m15
    if moment is not None:
        h1 = h1[h1.index + H <= moment]
        h4 = h4[h4.index + Timeframe.H4.ms <= moment]
        m15 = m15[m15.index + M15 <= moment]
    feats = build_features(h1, Timeframe.H1, h4, Timeframe.H4, m15, Timeframe.M15, cfg.strategy)
    return prepare(feats, cfg.strategy)


def with_penalties(cfg: TradingConfig, own: float, market: float) -> TradingConfig:
    raw = cfg.model_dump(mode="json")
    raw["strategy"]["counter_trend_penalty"] = own
    raw["strategy"]["market_trend_penalty"] = market
    return TradingConfig.from_dict(raw)


def market_trend_series(markets: dict[str, Market], cfg: TradingConfig) -> dict[int, int]:
    if "BTCUSDT" not in markets:
        return {}
    btc = features(markets["BTCUSDT"], cfg)
    return dict(zip(btc.index.tolist(), btc["long_trend"].astype(int).tolist(), strict=True))


# ---------------------------------------------------------------------- 1. случайные моменты
def random_moments(
    markets: dict[str, Market],
    paths: dict[str, Path15],
    cfg: TradingConfig,
    start: int,
    end: int,
    n: int,
    seed: int,
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    engine = SignalEngine(cfg.strategy)
    hours = list(range(start // H * H + H, end // H * H, H))
    out = []
    for moment in sorted(rng.sample(hours, n)):
        btc_trend = None
        if "BTCUSDT" in markets:
            btc_trend = int(features(markets["BTCUSDT"], cfg, moment)["long_trend"].iloc[-1])
        for symbol, m in markets.items():
            feats = features(m, cfg, moment)
            assert int(feats.index[-1]) + H == moment  # последняя свеча — только что закрытая
            ctx = MarketContext(market_trend=btc_trend, is_btc=symbol == "BTCUSDT")
            signal = engine.evaluate_last(feats, symbol, ctx)
            if signal.direction is None or signal.confidence < cfg.risk.confidence_threshold:
                continue
            row = Row(columns_of(feats), len(feats) - 1)
            o = evaluate_signal(symbol, signal, row, paths[symbol], cfg)
            item: dict[str, Any] = {
                "moment": moment,
                "symbol": symbol,
                "direction": signal.direction.value,
                "confidence": signal.confidence,
                "filters": signal.components.get("filters", {}),
            }
            if o is not None:
                item |= {
                    "entry": o.entry,
                    "stop": o.stop,
                    "kind": o.kind,
                    "exit_ts": o.exit_ts,
                    "r_net": o.r_net,
                }
            else:
                item["kind"] = "unresolved"
            out.append(item)
    return out


# ---------------------------------------------------------------------- 2. все сигналы
def all_signals(
    markets: dict[str, Market],
    paths: dict[str, Path15],
    cfg: TradingConfig,
    start: int,
    end: int,
    coin: bool = False,
) -> tuple[list[Outcome], int]:
    """Все направленные сигналы (или случайные направления при coin=True).
    Возвращает исходы и число сделок, не завершившихся к концу данных."""
    engine = SignalEngine(cfg.strategy)
    btc_trend = market_trend_series(markets, cfg)
    rng = random.Random(7)
    outcomes: list[Outcome] = []
    unresolved = 0
    for symbol, m in markets.items():
        feats = features(m, cfg)
        cols = columns_of(feats)
        idx = feats.index.to_numpy()
        for i in np.flatnonzero((idx + H > start) & (idx + H <= end)):
            row = Row(cols, int(i))
            ts = int(idx[i])
            ctx = MarketContext(market_trend=btc_trend.get(ts), is_btc=symbol == "BTCUSDT")
            signal = engine.evaluate_row(row, ts, symbol, ctx)
            if coin:
                if not signal.regime.tradable or i % 4:
                    continue
                d = rng.choice([Direction.LONG, Direction.SHORT])
                signal = Signal(ts, symbol, d, 100.0, signal.regime, "trend")
            if signal.direction is None:
                continue
            o = evaluate_signal(symbol, signal, row, paths[symbol], cfg)
            if o is None:
                if isinstance(plan_trade(signal, row, cfg.strategy.stops), TradePlan):
                    unresolved += 1
                continue
            outcomes.append(o)
    return outcomes, unresolved


def sequential(outcomes: list[Outcome], threshold: float) -> list[Outcome]:
    """Реалистичная последовательность: по монете новая сделка только после закрытия
    предыдущей (без пересекающихся «повторов» одного сигнала)."""
    out: list[Outcome] = []
    busy_until: dict[str, int] = defaultdict(int)
    for o in sorted(outcomes, key=lambda x: x.ts):
        if o.confidence >= threshold and o.ts >= busy_until[o.symbol]:
            out.append(o)
            busy_until[o.symbol] = o.exit_ts + M15
    return out


def stats(items: list[Outcome]) -> dict[str, Any]:
    if not items:
        return {"n": 0}
    r = np.array([o.r_net for o in items])
    wins, losses = r[r > 0], r[r <= 0]
    n = len(items)
    return {
        "n": n,
        "tp_pct": round(sum(o.kind == "tp" for o in items) / n * 100, 1),
        "tp1_be_pct": round(sum(o.kind == "tp1_be" for o in items) / n * 100, 1),
        "sl_pct": round(sum(o.kind == "sl" for o in items) / n * 100, 1),
        "win_rate": round(len(wins) / n * 100, 1),
        "avg_r_gross": round(float(np.mean([o.r_gross for o in items])), 3),
        "avg_r_net": round(float(r.mean()), 3),
        "profit_factor": round(float(wins.sum() / -losses.sum()), 3) if losses.sum() < 0 else None,
        "sum_r_net": round(float(r.sum()), 1),
    }


def bootstrap_ci(items: list[Outcome], n: int = 3000) -> list[float]:
    """95% ДИ среднего R нетто, блочный бутстрэп по (монета, неделя)."""
    groups: dict[tuple[str, int], list[float]] = defaultdict(list)
    for o in items:
        groups[(o.symbol, o.ts // (7 * DAY))].append(o.r_net)
    g = list(groups.values())
    if len(g) < 5:
        return []
    rng = np.random.default_rng(1)
    sums = np.array([sum(x) for x in g])
    counts = np.array([len(x) for x in g])
    means = []
    for _ in range(n):
        pick = rng.integers(0, len(g), len(g))
        means.append(sums[pick].sum() / counts[pick].sum())
    return [round(float(np.percentile(means, 2.5)), 3), round(float(np.percentile(means, 97.5)), 3)]


BUCKETS = [(0, 50), (50, 65), (65, 80), (80, 101)]


def report_signals(outs: list[Outcome], thr: float) -> dict[str, Any]:
    trad = [o for o in outs if o.confidence >= thr]
    seq = sequential(outs, thr)
    months: dict[str, list[Outcome]] = defaultdict(list)
    for o in seq:
        months[datetime.fromtimestamp(o.ts / 1000, UTC).strftime("%Y-%m")].append(o)
    return {
        "all_directional": stats(outs),
        "tradable": {**stats(trad), "ci95_r_net": bootstrap_ci(trad)},
        "sequential": {**stats(seq), "ci95_r_net": bootstrap_ci(seq)},
        "by_confidence": {
            f"{lo}-{min(hi, 100)}": stats([o for o in outs if lo <= o.confidence < hi])
            for lo, hi in BUCKETS
        },
        "sequential_by_trend": {
            name: stats([o for o in seq if o.with_trend == v])
            for name, v in (("по тренду", 1), ("нейтрально", 0), ("против тренда", -1))
        },
        "sequential_by_direction": {
            d: stats([o for o in seq if o.direction == d]) for d in ("long", "short")
        },
        "sequential_by_symbol": {
            s: stats([o for o in seq if o.symbol == s]) for s in sorted({o.symbol for o in outs})
        },
        "sequential_by_month": {k: stats(v) for k, v in sorted(months.items())},
    }


# ---------------------------------------------------------------------- 3. портфель
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


def portfolio(
    markets: dict[str, Market], cfg: TradingConfig, start: int, end: int, wf: bool
) -> dict[str, Any]:
    data = [SymbolData(s, instrument(s), m.h1, m.h4, m.m15) for s, m in markets.items()]
    bt = Backtester(cfg, cfg.markets["crypto"], BacktestSettings(initial_equity=10_000))
    prepared = bt.prepare(data)
    res = bt.run(prepared, start_ms=start, end_ms=end)
    rep = summarize(res)
    out: dict[str, Any] = {
        "summary": rep["summary"],
        "by_close_reason": {k: v["trades"] for k, v in rep["by_close_reason"].items()},
        "calibration": rep["calibration"],
        "halted_at": [_d(e.ts) for e in res.risk_events if e.type == "max_drawdown"],
    }
    if wf:
        w = walk_forward(
            bt, prepared, in_sample_ms=90 * DAY, out_of_sample_ms=30 * DAY, min_trades=10
        )
        out["walk_forward"] = {
            "summary": w.summary,
            "windows": [
                {"oos_start": _d(x.oos_start)[:10], "threshold": x.best_threshold, **x.oos_stats}
                for x in w.windows
            ],
        }
    return out


def run(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.data)
    cfg = TradingConfig.load(BACKEND_DIR / "config" / "default.yaml")
    base = with_penalties(cfg, 1.0, 1.0)  # та же стратегия без учёта долгосрочного тренда
    symbols = args.symbols or available_symbols(root)
    markets = load_market(root, symbols)
    paths = {s: Path15(m.m15) for s, m in markets.items()}
    end = min(int(m.h1.index[-1]) + H for m in markets.values())
    start = end - args.test_days * DAY
    thr = cfg.risk.confidence_threshold
    report: dict[str, Any] = {"period": [_d(start), _d(end)], "symbols": symbols, "threshold": thr}

    report["random_moments"] = random_moments(
        markets, paths, cfg, start, end, args.moments, args.seed
    )
    dump: dict[str, Any] = {}
    for name, c in (("без_фильтра_тренда", base), ("с_фильтром_тренда", cfg)):
        outs, unresolved = all_signals(markets, paths, c, start, end)
        report[name] = {"unresolved_excluded": unresolved, **report_signals(outs, thr)}
        dump[name] = [asdict(o) for o in outs]
    coin, _ = all_signals(markets, paths, cfg, start, end, coin=True)
    report["монетка"] = {"all": stats(coin), "sequential": stats(sequential(coin, 0))}

    report["портфель_без_фильтра"] = portfolio(markets, base, start, end, wf=False)
    report["портфель_с_фильтром"] = portfolio(markets, cfg, start, end, wf=True)
    if args.dump:
        Path(args.dump).write_text(
            json.dumps({"report": report, "outcomes": dump}, ensure_ascii=False, default=str)
        )
    return report


def _d(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", required=True, help="корень датасета (папка с data/)")
    p.add_argument("--symbols", nargs="*")
    p.add_argument("--test-days", type=int, default=365)
    p.add_argument("--moments", type=int, default=12)
    p.add_argument("--seed", type=int, default=2026)
    p.add_argument("--dump", help="сохранить полный отчёт в JSON")
    report = run(p.parse_args())
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
