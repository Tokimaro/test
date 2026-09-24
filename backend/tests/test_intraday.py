import numpy as np
import pandas as pd
import pytest

from app.research import intraday as it


def bars(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(rows), freq="1min", tz="UTC")
    o, h, lo, c = zip(*rows, strict=True)
    return pd.DataFrame({"open": o, "high": h, "low": lo, "close": c, "volume": 1.0}, index=idx)


def random_bars(n: int = 6000, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.002, n)))
    o = np.r_[c[0], c[:-1]]
    spread = np.abs(rng.normal(0, 0.001, n)) * c
    idx = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {
            "open": o,
            "high": np.maximum(o, c) + spread,
            "low": np.minimum(o, c) - spread,
            "close": c,
            "volume": rng.uniform(1, 2, n),
        },
        index=idx,
    )


def sig(**kw: float) -> pd.DataFrame:
    row = {"i": 0, "dir": 1, "limit": np.nan, "sl": np.nan, "tp": np.nan, "expiry": 0}
    row.update({"max_bars": 0, **kw})
    return pd.DataFrame([row], columns=it.SIGNAL_COLUMNS)


def test_market_entry_next_open_and_take_profit() -> None:
    df = bars([(100, 100, 100, 100), (101, 101, 101, 101), (101, 104, 101, 103)])
    t = it.simulate(df, sig(i=0, sl=99.0, tp=103.0))
    assert t.loc[0, "entry"] == 101  # открытие следующего бара, а не закрытие сигнального
    assert t.loc[0, "reason"] == "tp"
    assert t.loc[0, "gross_r"] == pytest.approx(1.0)


def test_both_levels_in_one_bar_counts_as_stop() -> None:
    df = bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 105, 95, 100)])
    t = it.simulate(df, sig(i=0, sl=98.0, tp=102.0))
    assert t.loc[0, "reason"] == "sl"
    assert t.loc[0, "gross_r"] == pytest.approx(-1.0)


def test_gap_through_stop_exits_at_open() -> None:
    df = bars([(100, 100, 100, 100), (100, 100, 100, 100), (95, 96, 94, 95)])
    t = it.simulate(df, sig(i=0, sl=98.0, tp=110.0))
    assert t.loc[0, "exit"] == 95
    assert t.loc[0, "gross_r"] == pytest.approx(-2.5)


def test_limit_fill_and_no_take_on_fill_bar() -> None:
    df = bars(
        [
            (100, 100, 100, 100),
            (100, 101, 99.5, 100),  # до лимита 99 не дошли
            (100, 104, 98.9, 100),  # лимит исполнен, тейк 103 на этом же баре не засчитан
            (100, 100, 99.5, 100),
            (100, 103.5, 100, 103),
        ]
    )
    t = it.simulate(df, sig(i=0, limit=99.0, sl=98.0, tp=103.0, expiry=5))
    assert t.loc[0, "entry"] == 99.0
    assert t.loc[0, "exit_ts"] == df.index[4]
    assert t.loc[0, "gross_r"] == pytest.approx(4.0)


def test_limit_fill_through_requires_trading_past_limit() -> None:
    flat, touch, deep = (100, 100, 100, 100), (100, 100, 98.95, 100), (100, 100, 98.8, 100)
    take = (100, 111, 100, 110)
    s = sig(i=0, limit=99.0, sl=90.0, tp=110.0, expiry=5)
    # при касании исполняется на баре 1; с fill_through=0.1% нужна цена ≤ 98.901 — бар 2
    assert it.simulate(bars([flat, touch, deep, take]), s).loc[0, "bars"] == 3
    strict = it.simulate(bars([flat, touch, deep, take]), s, fill_through=0.001)
    assert strict.loc[0, "bars"] == 2 and strict.loc[0, "entry"] == 99.0
    assert it.simulate(bars([flat, touch, take]), s, fill_through=0.001).empty


def test_limit_expires() -> None:
    df = bars([(100, 100, 100, 100)] * 5)
    assert it.simulate(df, sig(i=0, limit=99.0, sl=98.0, tp=103.0, expiry=3)).empty


def test_short_and_costs_in_r() -> None:
    df = bars([(100, 100, 100, 100), (100, 100, 100, 100), (100, 100, 96, 97)])
    t = it.simulate(df, sig(i=0, dir=-1, sl=102.0, tp=96.0))
    assert t.loc[0, "gross_r"] == pytest.approx(2.0)
    # 0.1% на сторону при риске 2 пункта: (100 + 96) × 0.001 / 2 = 0.098 R
    assert it.net_r(t, it.Costs(0.001, 0.001)).iloc[0] == pytest.approx(2.0 - 0.098)


def test_mixed_costs_maker_on_limit_entry_and_take() -> None:
    t = pd.DataFrame(
        {
            "entry": [100.0, 100.0],
            "exit": [102.0, 99.0],
            "risk": [1.0, 1.0],
            "gross_r": [2.0, -1.0],
            "reason": ["tp", "sl"],
            "limit_entry": [True, False],
        }
    )
    r = it.net_r(t, it.Costs(taker=0.001, maker=0.0001))
    assert r.iloc[0] == pytest.approx(2.0 - (0.01 + 0.0102))  # оба конца — maker
    assert r.iloc[1] == pytest.approx(-1.0 - (0.1 + 0.099))  # рыночный вход и стоп — taker


def test_one_position_at_a_time_and_time_exit() -> None:
    df = bars([(100, 100, 100, 100)] * 10)
    s = pd.concat([sig(i=0, sl=90.0, max_bars=3), sig(i=2, sl=90.0), sig(i=5, sl=90.0, max_bars=2)])
    t = it.simulate(df, s)
    assert list(t["reason"]) == ["time", "time"]
    assert t.loc[0, "bars"] == 3


@pytest.mark.parametrize("name", sorted(it.STRATEGIES))
@pytest.mark.parametrize("tf", ["1m", "5m"])
def test_strategies_are_causal(name: str, tf: str) -> None:
    """Сигналы до момента t не меняются, если отрезать данные после t."""
    df = it.resample(random_bars(), tf)
    fn = it.STRATEGIES[name]
    full = fn(df, rr=2.0, use_bias=True)
    cut = len(df) * 2 // 3
    part = fn(df.iloc[:cut], rr=2.0, use_bias=True)
    # последний час среза может отличаться: неполный часовой бар для фильтра тренда
    limit = cut - int(pd.Timedelta("2h") / it.bar_delta(df))
    a = full[full["i"] < limit].sort_values(["i", "dir"]).reset_index(drop=True)
    b = part[part["i"] < limit].sort_values(["i", "dir"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(a, b, check_dtype=False)


def test_htf_bias_uses_only_closed_hours() -> None:
    df = random_bars(600)
    bias = it.htf_bias(df, n=5)
    # значение внутри часа H должно совпасть при отрезании данных сразу после текущего бара
    for i in (300, 359, 360, 421):
        assert it.htf_bias(df.iloc[: i + 1], n=5).iloc[-1] == bias.iloc[i]


@pytest.mark.parametrize("tf", ["1h", "4h"])
def test_htf_bias_on_higher_working_timeframe_is_causal(tf: str) -> None:
    df = it.resample(random_bars(20000), tf)
    bias = it.htf_bias(df, n=5)
    for i in range(10, len(df), 7):
        assert it.htf_bias(df.iloc[: i + 1], n=5).iloc[-1] == bias.iloc[i]
    # значение на баре i известно после его закрытия — это знак close[i] − EMA
    diff = df["close"] - it.ema(df["close"], 5)
    assert (np.sign(diff.to_numpy())[20:] == bias.to_numpy()[20:]).all()
