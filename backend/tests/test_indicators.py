import numpy as np
import pandas as pd
import pytest

from app.analysis import indicators as ind
from app.analysis.features import align_closed, build_features
from app.analysis.regime import Regime, classify_regime, higher_tf_bias
from app.domain import Timeframe
from app.trading_config import StrategySettings
from tests.synthetic import frame_to_candles, make_ohlcv, resample

talib = pytest.importorskip("talib")


@pytest.fixture(scope="module")
def df() -> pd.DataFrame:
    return make_ohlcv(800, vol=0.012, seed=7)


def assert_same(ours: pd.Series, ref: np.ndarray, tol: float = 1e-9) -> None:
    a = ours.to_numpy(dtype=float)
    assert np.array_equal(np.isnan(a), np.isnan(ref)), "разный период прогрева"
    m = ~np.isnan(a)
    np.testing.assert_allclose(a[m], ref[m], rtol=tol, atol=tol)


def test_matches_talib(df: pd.DataFrame) -> None:
    h, lo, c = (df[k].to_numpy() for k in ("high", "low", "close"))
    assert_same(ind.sma(df["close"], 20), talib.SMA(c, 20))
    for n in (9, 21, 200):
        assert_same(ind.ema(df["close"], n), talib.EMA(c, n))
    assert_same(ind.rsi(df["close"], 14), talib.RSI(c, 14))
    assert_same(ind.atr(df, 14), talib.ATR(h, lo, c, 14))
    a = ind.adx(df, 14)
    assert_same(a["adx"], talib.ADX(h, lo, c, 14))
    assert_same(a["plus_di"], talib.PLUS_DI(h, lo, c, 14))
    assert_same(a["minus_di"], talib.MINUS_DI(h, lo, c, 14))
    m = ind.macd(df["close"])
    tm, ts, th = talib.MACD(c)
    assert_same(m["macd"], tm)
    assert_same(m["signal"], ts)
    assert_same(m["hist"], th)
    b = ind.bollinger(df["close"])
    up, _mid, low = talib.BBANDS(c, 20, 2, 2)
    assert_same(b["upper"], up)
    assert_same(b["lower"], low)


def test_ema_handles_leading_nan() -> None:
    s = pd.Series([np.nan, np.nan, 1.0, 2.0, 3.0, 4.0])
    out = ind.ema(s, 3)
    assert np.isnan(out.iloc[3])
    assert out.iloc[4] == pytest.approx(2.0)  # SMA(1,2,3)
    assert out.iloc[5] == pytest.approx(3.0)  # 2 + 0.5*(4-2)


def test_rsi_flat_series_is_neutral() -> None:
    s = pd.Series(np.full(30, 5.0))
    assert ind.rsi(s, 14).iloc[-1] == 50.0


def test_donchian_excludes_current_bar() -> None:
    df = pd.DataFrame({"high": [1.0, 2, 3, 10], "low": [0.5, 1, 2, 9]})
    d = ind.donchian(df, 3)
    assert d["upper"].iloc[3] == 3.0  # новый максимум 10 ещё не в канале
    assert d["lower"].iloc[3] == 0.5


def test_rolling_percentile() -> None:
    s = pd.Series([1.0, 2, 3, 4, 5, 0])
    p = ind.rolling_percentile(s, 5)
    assert p.iloc[4] == 100.0
    assert p.iloc[5] == 20.0  # 0 — минимум из 5 значений


def test_swing_points_confirmed_with_delay() -> None:
    low = [5.0, 4, 3, 1, 3, 4, 5, 6]
    df = pd.DataFrame({"high": [x + 1 for x in low], "low": low})
    sw = ind.swing_points(df, left=2, right=2)
    # минимум 1 в строке 3 подтверждается только в строке 5
    assert np.isnan(sw["swing_low"].iloc[4])
    assert sw["swing_low"].iloc[5] == 1.0
    assert sw["swing_low"].iloc[7] == 1.0


@pytest.mark.parametrize("cut", [300, 517, 799])
def test_features_are_causal(df: pd.DataFrame, cut: int) -> None:
    """Признаки на усечённой истории совпадают с признаками на полной — нет заглядывания."""
    cfg = StrategySettings()
    tf = Timeframe.H1
    h4 = resample(df, tf, Timeframe.H4)
    m15 = make_ohlcv(len(df) * 4, tf=Timeframe.M15, seed=3)
    full = build_features(df, tf, h4, Timeframe.H4, m15, Timeframe.M15, cfg)
    end_ts = df.index[cut - 1]
    part = build_features(
        df.loc[:end_ts],
        tf,
        h4[h4.index + Timeframe.H4.ms <= end_ts + tf.ms],
        Timeframe.H4,
        m15[m15.index + Timeframe.M15.ms <= end_ts + tf.ms],
        Timeframe.M15,
        cfg,
    )
    pd.testing.assert_frame_equal(full.loc[:end_ts], part, check_exact=False, rtol=1e-9)


def test_align_closed_uses_only_closed_candles() -> None:
    h1 = Timeframe.H1
    h4 = Timeframe.H4
    base = pd.DataFrame(index=pd.Index([h4.ms * 10 + i * h1.ms for i in range(8)], name="ts"))
    other = pd.DataFrame(
        {"close": [1.0, 2.0, 3.0]}, index=pd.Index([h4.ms * 9, h4.ms * 10, h4.ms * 11])
    )
    out = align_closed(base, h1, other, h4, ["close"], "h_")
    # H1-свечи 0..2 закрываются до закрытия 4h-свечи №10 → видят только свечу №9
    assert out["h_close"].tolist() == [1.0, 1.0, 1.0, 2.0, 2.0, 2.0, 2.0, 3.0]


def test_regime_detection() -> None:
    cfg = StrategySettings()
    n = 3000
    drifts = np.zeros(n)
    vols = np.full(n, 0.004)
    drifts[1000:1600] = 0.004  # сильный рост
    drifts[1600:2200] = -0.004  # сильное падение
    vols[2600:2700] = 0.05  # всплеск волатильности
    df = make_ohlcv(n, drifts=drifts, vols=vols, seed=11)
    h4 = resample(df, Timeframe.H1, Timeframe.H4)
    feats = build_features(df, Timeframe.H1, h4, Timeframe.H4, None, None, cfg)
    regime = classify_regime(feats, cfg)

    assert regime.iloc[:30].eq(Regime.UNKNOWN.value).all()
    up = regime.iloc[1100:1600].value_counts(normalize=True)
    down = regime.iloc[1700:2200].value_counts(normalize=True)
    flat = regime.iloc[400:1000].value_counts(normalize=True)
    assert up.get(Regime.TREND_UP.value, 0) > 0.7
    assert down.get(Regime.TREND_DOWN.value, 0) > 0.7
    assert flat.get(Regime.TREND_UP.value, 0) + flat.get(Regime.TREND_DOWN.value, 0) < 0.4
    assert (regime.iloc[2600:2700] == Regime.CHAOS.value).mean() > 0.5

    bias = higher_tf_bias(feats)
    assert (bias.iloc[1400:1600] == 1).mean() > 0.9
    assert (bias.iloc[2000:2200] == -1).mean() > 0.9


def test_frame_roundtrip() -> None:
    df = make_ohlcv(10)
    back = ind.candles_to_frame(frame_to_candles(df))
    pd.testing.assert_frame_equal(back, df, check_names=False)
