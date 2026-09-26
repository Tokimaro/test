import numpy as np
import pandas as pd
import pytest

from app.research import strategies as st
from app.research.portfolio import CostModel, metrics, run_weights

DAY = 86_400_000


def prices(n: int = 400, seed: int = 0, cols: int = 8) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    data = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.03, (n, cols)), axis=0))
    return pd.DataFrame(
        data,
        index=pd.Index(np.arange(n, dtype="int64") * DAY),
        columns=["BTCUSDT", *[f"C{i}USDT" for i in range(cols - 1)]],
    )


def test_weights_apply_to_next_bar_and_costs() -> None:
    closes = pd.DataFrame({"A": [100.0, 110.0, 121.0]}, index=[0, 1, 2])
    w = pd.DataFrame({"A": [1.0, 1.0, 1.0]}, index=closes.index)
    res = run_weights("t", w, closes, 365, CostModel(per_side=0.001, perpetual=False))
    # вес на закрытии бара 0 работает на баре 1: доходность бара 0 не засчитывается
    assert res.gross.tolist() == pytest.approx([0.0, 0.1, 0.1])
    # вход в позицию на баре 0 — оборот 1, дальше вес не меняется (дрейф учтён)
    assert res.turnover.tolist() == pytest.approx([1.0, 0.0, 0.0])
    assert res.returns.iloc[0] == pytest.approx(-0.001)


def test_funding_long_pays_short_receives() -> None:
    closes = pd.DataFrame({"A": [100.0] * 3}, index=[0, 1, 2])
    cm = CostModel(per_side=0.0, funding_8h=0.0001)
    long = run_weights("l", pd.DataFrame({"A": [1.0] * 3}, index=closes.index), closes, 365, cm)
    short = run_weights("s", pd.DataFrame({"A": [-1.0] * 3}, index=closes.index), closes, 365, cm)
    assert long.returns.iloc[1] == pytest.approx(-0.0003)
    assert short.returns.iloc[1] == pytest.approx(0.0003)


def test_spot_rejects_shorts() -> None:
    closes = prices(50)
    with pytest.raises(ValueError):
        run_weights("x", -st.buy_and_hold(closes), closes, 365, CostModel(perpetual=False))


@pytest.mark.parametrize(
    "make",
    [
        lambda c: st.tsmom(c, 365),
        lambda c: st.tsmom(c, 365, long_only=True),
        lambda c: st.xs_momentum(c, 365),
        lambda c: st.st_reversal(c),
        lambda c: st.btc_trend_filter(c),
    ],
)
def test_strategies_are_causal(make) -> None:  # type: ignore[no-untyped-def]
    """Веса на дату t не меняются, если отрезать данные после t."""
    closes = prices()
    full = make(closes)
    for cut in (250, 333):
        part = make(closes.iloc[:cut])
        pd.testing.assert_frame_equal(full.iloc[:cut], part, check_exact=False, atol=1e-12)


def test_market_neutral_long_short() -> None:
    w = st.xs_momentum(prices(), 365, vol_managed=False)
    active = w[w.abs().sum(axis=1) > 0]
    assert (active.sum(axis=1).abs() < 1e-12).all()
    assert np.allclose(active.abs().sum(axis=1), 1.0)


def test_no_edge_on_random_walk() -> None:
    """На случайном блуждании у трендовой стратегии не должно быть устойчивого преимущества."""
    sharpes = []
    for seed in range(6):
        c = prices(1500, seed=seed)
        c = c / c.iloc[0] * 100 * np.exp(-0.0005 * np.arange(len(c)))[:, None]  # без дрейфа
        res = run_weights("t", st.tsmom(c, 365), c, 365, CostModel())
        sharpes.append(metrics(res.returns, 365)["sharpe"])
    assert abs(float(np.mean(sharpes))) < 0.6


def test_ml_walk_forward_never_trains_on_future() -> None:
    pytest.importorskip("sklearn")
    from app.research import ml

    rng = np.random.default_rng(1)
    n = 900
    daily = {}
    for s in ("BTCUSDT", "AUSDT", "BUSDT", "CUSDT", "DUSDT", "EUSDT", "FUSDT"):
        c = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
        daily[s] = pd.DataFrame(
            {
                "open": c,
                "high": c * 1.01,
                "low": c * 0.99,
                "close": c,
                "volume": rng.uniform(1, 2, n),
            },
            index=pd.Index(np.arange(n, dtype="int64") * DAY),
        )
    panel = ml.build_panel(daily)
    seen: list[tuple[int, int]] = []

    class Spy:
        def fit(self, x: pd.DataFrame, y: pd.Series) -> None:
            seen.append((int(panel.loc[x.index, "ts"].max()), 0))

        def predict(self, x: pd.DataFrame) -> np.ndarray:
            seen[-1] = (seen[-1][0], int(panel.loc[x.index, "ts"].min()))
            return np.zeros(len(x))

    pred = ml.walk_forward_predict(panel, Spy, "spy", 400 * DAY, retrain_days=90)
    assert len(seen) >= 4
    # последняя обучающая строка (день t, цель t+1) строго раньше первого прогноза
    assert all(train_max + DAY < test_min for train_max, test_min in seen)
    assert int(pred.frame["ts"].min()) >= 400 * DAY
