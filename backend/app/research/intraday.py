"""Внутридневные стратегии (1m/5m/15m) и симулятор сделок со стопом и тейком.

Правила симуляции (консервативные):
* сигнал считается по закрытию бара i только по данным до i включительно;
* рыночный вход — по открытию бара i+1; лимитный — при касании цены в течение expiry баров
  (если бар открылся за лимитом — по цене открытия);
* если в одном баре задеты и стоп, и тейк — считаем, что сработал стоп;
* гэп через стоп — выход по цене открытия (хуже стопа); через тейк — по тейку, без бонуса;
* на бар входа по лимиту проверяется только стоп;
* одна позиция на инструмент: пока сделка открыта, новые сигналы пропускаются;
* издержки — доля от цены на каждую сторону (комиссия + проскальзывание).

Результат сделки — в R (доля от риска |вход − стоп|): так сравнимы стратегии с разными стопами.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

FloatArray = np.ndarray[Any, np.dtype[np.float64]]
SIGNAL_COLUMNS = ["i", "dir", "limit", "sl", "tp", "expiry", "max_bars"]


# ---------- данные ----------


def dt_index(df: pd.DataFrame | pd.Series) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(df.index)


def to_frame(candles: pd.DataFrame) -> pd.DataFrame:
    """Свечи загрузчика (индекс — мс) → DatetimeIndex UTC."""
    out = candles.copy()
    out.index = pd.to_datetime(out.index, unit="ms", utc=True)
    return out


def freq(rule: str) -> str:
    """ "5m" → "5min" (pandas понимает "m" как месяц)."""
    return rule[:-1] + "min" if rule.endswith("m") else rule


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    if freq(rule) == "1min":
        return df
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = df.resample(freq(rule), label="left", closed="left").agg(agg)
    return out.dropna()


def bar_delta(df: pd.DataFrame) -> pd.Timedelta:
    return pd.Timedelta(df.index.to_series().diff().mode().iloc[0])


# ---------- индикаторы (pandas ewm/rolling — строго по прошлому) ----------


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    down = (-d).clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 * up / (up + down)


def htf_bias(df: pd.DataFrame, rule: str = "1h", n: int = 50) -> pd.Series:
    """+1/-1: закрытие старшего ТФ выше/ниже его EMA. Значение часа доступно только после
    закрытия этого часа — сопоставляем по времени закрытия баров."""
    step = max(pd.Timedelta(freq(rule)), bar_delta(df))  # старший ТФ не младше рабочего
    h = resample(df, rule) if step == pd.Timedelta(freq(rule)) else df
    diff = h["close"] - ema(h["close"], n)
    up = pd.Series(np.sign(diff.to_numpy()), index=h.index).fillna(0.0)
    up.index = up.index + step  # время закрытия бара старшего ТФ
    ltf_close = df.index + bar_delta(df)
    return pd.Series(up.reindex(ltf_close, method="ffill").fillna(0.0).to_numpy(), df.index)


def confirmed_swings(df: pd.DataFrame, n: int = 3) -> pd.DataFrame:
    """Последние подтверждённые фракталы: пивот в j виден начиная с j+n."""
    w = 2 * n + 1
    hi, lo = df["high"], df["low"]
    is_h = hi == hi.rolling(w, center=True).max()
    is_l = lo == lo.rolling(w, center=True).min()
    sh = hi.where(is_h).shift(n).ffill()
    sl = lo.where(is_l).shift(n).ffill()
    return pd.DataFrame({"swing_high": sh, "swing_low": sl}, index=df.index)


# ---------- симулятор ----------


@dataclass
class Costs:
    """Доля цены на сторону сделки. taker — рыночные ордера и стопы (комиссия +
    проскальзывание), maker — исполнившиеся лимитные ордера (лимитный вход, тейк)."""

    taker: float
    maker: float


TAKER_FEE = 0.00055  # Bybit USDT-перпетуалы, не-VIP: taker 0.055%
MAKER_FEE = 0.0002  # maker 0.02%
SLIPPAGE = 0.0002  # проскальзывание рыночного ордера/стопа на ликвидных парах


def cost_models(slippage: float = SLIPPAGE) -> dict[str, "Costs"]:
    """taker — всё рыночными ордерами; mixed — лимитный вход и тейк по maker без
    проскальзывания, рыночный вход, стоп и выход по времени — taker + проскальзывание."""
    taker = TAKER_FEE + slippage
    return {"taker": Costs(taker, taker), "mixed": Costs(taker, MAKER_FEE)}


TAKER = cost_models()["taker"]
MIXED = cost_models()["mixed"]
MAKER = Costs(MAKER_FEE, MAKER_FEE)  # оптимистично: всё по maker
FREE = Costs(0.0, 0.0)


def signals_frame(rows: list[tuple[Any, ...]] | None = None) -> pd.DataFrame:
    return pd.DataFrame(rows or [], columns=SIGNAL_COLUMNS)


def _first_hit(mask_fn: Callable[[int, int], np.ndarray], start: int, end: int) -> int:
    """Первый индекс k в [start, end), где маска истинна; -1 если нет. Поиск растущими окнами."""
    step = 256
    k = start
    while k < end:
        stop = min(end, k + step)
        m = mask_fn(k, stop)
        if m.any():
            return k + int(np.argmax(m))
        k = stop
        step *= 4
    return -1


def simulate(df: pd.DataFrame, signals: pd.DataFrame, fill_through: float = 0.0) -> pd.DataFrame:
    """Прогон сигналов по барам. Возвращает сделки с gross R и ценами для расчёта издержек.

    Несколько неисполненных лимиток могут висеть одновременно: сделку открывает та, что
    исполнилась ПЕРВОЙ, остальные снимаются; сигналы во время открытой сделки пропускаются.
    (Ранняя версия брала самый ранний сигнал, исполнившийся когда-нибудь, — это заглядывание в
    будущее: она «знала», что старая лимитка дождётся глубокого отката, и пропускала сделки по
    более свежим лимиткам, которые исполнились бы раньше. См. docs/intraday-strategies.md.)

    fill_through — насколько (доля цены) рынок должен пройти ЗА лимитную цену, чтобы ордер
    считался исполненным: 0 — исполнение при касании (оптимистично, очередь не учитывается)."""
    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    lo = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)
    n = len(o)
    trades: list[tuple[Any, ...]] = []
    sig = signals.sort_values("i", kind="stable")
    cols = {k: sig[k].to_numpy(dtype=float) for k in SIGNAL_COLUMNS}
    m = len(sig)

    # 1) момент исполнения каждого ордера, как если бы он был единственным
    fill_bar = np.full(m, -1, dtype=np.int64)
    fill_px = np.full(m, np.nan)
    for row in range(m):
        i, d = int(cols["i"][row]), int(cols["dir"][row])
        if i + 1 >= n:
            continue
        lim = cols["limit"][row]
        if np.isnan(lim):
            fill_bar[row], fill_px[row] = i + 1, o[i + 1]
            continue
        end = min(n, i + 1 + int(cols["expiry"][row]))
        trigger = lim * (1 - d * fill_through)

        def touch(a: int, b: int, d: int = d, trigger: float = trigger) -> np.ndarray:
            return lo[a:b] <= trigger if d > 0 else h[a:b] >= trigger

        j = _first_hit(touch, i + 1, end)
        if j >= 0:
            fill_bar[row] = j
            fill_px[row] = min(o[j], lim) if d > 0 else max(o[j], lim)

    # 2) сделки по очереди: из ордеров, выставленных после закрытия прошлой сделки,
    #    срабатывает тот, что исполнился раньше всех; остальные снимаются
    sig_i = cols["i"].astype(np.int64)
    busy_until = -1
    p = 0
    while p < m:
        best = -1
        for row in range(p, m):
            if sig_i[row] <= busy_until:
                continue
            if best >= 0 and sig_i[row] >= fill_bar[best]:
                break  # выставлен после лучшего исполнения — уже не успеет
            if fill_bar[row] >= 0 and (best < 0 or fill_bar[row] < fill_bar[best]):
                best = row
        if best < 0:
            break
        row = best
        i, d = int(sig_i[row]), int(cols["dir"][row])
        j, px = int(fill_bar[row]), float(fill_px[row])
        sl, tp = cols["sl"][row], cols["tp"][row]
        max_bars = int(cols["max_bars"][row])
        limit_entry = not np.isnan(cols["limit"][row])
        # остальные ордера сняты в момент исполнения; сигнал на свече, где позиция уже
        # закрылась, допустим (ордер ставится на её закрытии)
        risk = (px - sl) * d
        if not risk > 0 or (not np.isnan(tp) and (tp - px) * d <= 0):
            # открылись уже за стопом/тейком — позиция закрылась бы на той же свече
            busy_until = j - 1
            p = int(np.searchsorted(sig_i, busy_until, side="right"))
            continue
        # --- выход ---
        end = n if not max_bars else min(n, j + max_bars)

        def hit(
            a: int,
            b: int,
            d: int = d,
            sl: float = sl,
            tp: float = tp,
            j: int = j,
            limit_entry: bool = limit_entry,
        ) -> np.ndarray:
            stop = lo[a:b] <= sl if d > 0 else h[a:b] >= sl
            if np.isnan(tp):
                return stop
            take = h[a:b] >= tp if d > 0 else lo[a:b] <= tp
            if limit_entry and a == j:
                take = take.copy()
                take[0] = False  # на баре входа по лимиту тейк не засчитываем
            return stop | take

        k = _first_hit(hit, j, end)
        if k < 0:
            if max_bars and end < n:
                k, exit_px, reason = end - 1, c[end - 1], "time"
            else:
                break  # данные кончились — сделка не завершена, дальше сигналов нет
        else:
            stop_hit = lo[k] <= sl if d > 0 else h[k] >= sl
            if stop_hit:
                gapped = (o[k] < sl) if d > 0 else (o[k] > sl)
                exit_px = o[k] if gapped and k > j else sl
                reason = "sl"
            else:
                exit_px, reason = tp, "tp"
        trades.append(
            (df.index[i], df.index[k], d, px, exit_px, sl, risk, reason, k - j + 1, limit_entry)
        )
        busy_until = k - 1
        p = int(np.searchsorted(sig_i, busy_until, side="right"))
    out = pd.DataFrame(
        trades,
        columns=[
            "signal_ts",
            "exit_ts",
            "dir",
            "entry",
            "exit",
            "sl",
            "risk",
            "reason",
            "bars",
            "limit_entry",
        ],
    )
    out["gross_r"] = (out["exit"] - out["entry"]) * out["dir"] / out["risk"]
    return out


def net_r(trades: pd.DataFrame, costs: Costs) -> pd.Series:
    entry_rate = np.where(trades["limit_entry"].astype(bool), costs.maker, costs.taker)
    exit_rate = np.where(trades["reason"] == "tp", costs.maker, costs.taker)
    entry, exit_, risk = (trades[k].to_numpy(dtype=float) for k in ("entry", "exit", "risk"))
    cost = (entry_rate * entry + exit_rate * exit_) / risk
    return pd.Series(trades["gross_r"].to_numpy(dtype=float) - cost, index=trades.index)


def summarize(trades: pd.DataFrame, costs: Costs, risk_pct: float = 0.5) -> dict[str, Any]:
    """Метрики по сделкам (по всем инструментам, в порядке закрытия).
    Доходность — при риске risk_pct% капитала на сделку, с реинвестированием."""
    if trades.empty:
        return {"trades": 0}
    t = trades.sort_values("exit_ts")
    r = net_r(t, costs).to_numpy()
    eq = np.cumprod(1 + r * risk_pct / 100)
    dd = float((eq / np.maximum.accumulate(eq) - 1).min())
    days = max((t["exit_ts"].max() - t["signal_ts"].min()).total_seconds() / 86400, 1.0)
    wins, losses = r[r > 0].sum(), -r[r < 0].sum()
    return {
        "trades": len(r),
        "trades_per_day": round(len(r) / days, 2),
        "win_rate_pct": round(float((r > 0).mean() * 100), 1),
        "avg_r": round(float(r.mean()), 3),
        "t_stat": round(float(r.mean() / (r.std(ddof=1) / np.sqrt(len(r)))), 2)
        if len(r) > 2 and r.std() > 0
        else 0.0,
        "profit_factor": round(float(wins / losses), 2) if losses > 0 else None,
        "cost_r": round(float(np.mean(net_r(t, FREE).to_numpy() - r)), 3),
        "return_pct": round(float(eq[-1] - 1) * 100, 1),
        "cagr_pct": round(float(eq[-1] ** (365 / days) - 1) * 100, 1) if eq[-1] > 0 else -100.0,
        "max_dd_pct": round(dd * 100, 1),
        "median_stop_pct": round(float((t["risk"] / t["entry"]).median() * 100), 3),
    }


# ---------- стратегии: каждая возвращает сигналы (см. SIGNAL_COLUMNS) ----------


def _market(
    idx: np.ndarray, d: np.ndarray, sl: FloatArray, tp: FloatArray, max_bars: int = 0
) -> pd.DataFrame:
    ok = ~(np.isnan(sl))
    return pd.DataFrame(
        {
            "i": idx[ok],
            "dir": d[ok],
            "limit": np.nan,
            "sl": sl[ok],
            "tp": tp[ok],
            "expiry": 0,
            "max_bars": max_bars,
        }
    )


def _bias_ok(df: pd.DataFrame, d: pd.Series, use_bias: bool) -> pd.Series:
    if not use_bias:
        return pd.Series(True, index=df.index)
    return htf_bias(df) == d


def _entries(
    df: pd.DataFrame,
    long: pd.Series,
    short: pd.Series,
    stop_dist: pd.Series,
    rr: float,
    max_bars: int = 0,
) -> pd.DataFrame:
    c = df["close"]
    d = pd.Series(0, index=df.index) + long.astype(int) - short.astype(int)
    pos = np.flatnonzero((d != 0).to_numpy() & stop_dist.notna().to_numpy())
    dd = d.to_numpy()[pos]
    ref = c.to_numpy()[pos]
    dist = stop_dist.to_numpy()[pos]
    sl = ref - dd * dist
    tp = ref + dd * dist * rr if rr > 0 else np.full(len(pos), np.nan)
    return _market(pos, dd, sl, tp, max_bars)


def ema_cross(
    df: pd.DataFrame,
    rr: float = 2.0,
    use_bias: bool = True,
    fast: int = 9,
    slow: int = 21,
    atr_mult: float = 1.5,
) -> pd.DataFrame:
    """Скальпинг на пересечении EMA 9/21 по направлению тренда (EMA 200 + часовой фильтр)."""
    c = df["close"]
    f, s, t = ema(c, fast), ema(c, slow), ema(c, 200)
    up = (f > s) & (f.shift(1) <= s.shift(1)) & (c > t)
    dn = (f < s) & (f.shift(1) >= s.shift(1)) & (c < t)
    up &= _bias_ok(df, pd.Series(1.0, index=df.index), use_bias)
    dn &= _bias_ok(df, pd.Series(-1.0, index=df.index), use_bias)
    return _entries(df, up, dn, atr(df) * atr_mult, rr)


def bb_rsi_reversion(
    df: pd.DataFrame, rr: float = 1.0, use_bias: bool = False, atr_mult: float = 1.5
) -> pd.DataFrame:
    """Скальпинг возврата к среднему: закрытие за полосой Боллинджера + RSI в экстремуме."""
    c = df["close"]
    mid = c.rolling(20).mean()
    sd = c.rolling(20).std(ddof=0)
    r = rsi(c, 14)
    up = (c < mid - 2 * sd) & (r < 30)
    dn = (c > mid + 2 * sd) & (r > 70)
    up &= _bias_ok(df, pd.Series(1.0, index=df.index), use_bias)
    dn &= _bias_ok(df, pd.Series(-1.0, index=df.index), use_bias)
    return _entries(df, up, dn, atr(df) * atr_mult, rr)


def donchian_breakout(
    df: pd.DataFrame, rr: float = 2.0, use_bias: bool = True, n: int = 55, atr_mult: float = 2.0
) -> pd.DataFrame:
    """Трендовый пробой канала Дончиана (максимум/минимум прошлых n баров)."""
    c = df["close"]
    hi = df["high"].shift(1).rolling(n).max()
    lo = df["low"].shift(1).rolling(n).min()
    up = (c > hi) & (c.shift(1) <= hi.shift(1))
    dn = (c < lo) & (c.shift(1) >= lo.shift(1))
    up &= _bias_ok(df, pd.Series(1.0, index=df.index), use_bias)
    dn &= _bias_ok(df, pd.Series(-1.0, index=df.index), use_bias)
    return _entries(df, up, dn, atr(df) * atr_mult, rr)


def vwap_pullback(
    df: pd.DataFrame, rr: float = 2.0, use_bias: bool = True, atr_mult: float = 1.5
) -> pd.DataFrame:
    """Откат к дневному VWAP (сброс в 00:00 UTC) и возврат над ним по направлению тренда."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    day = dt_index(df).floor("D")
    pv = (typical * df["volume"]).groupby(day).cumsum()
    vol = df["volume"].groupby(day).cumsum()
    vwap = pv / vol.replace(0, np.nan)
    c = df["close"]
    trend = ema(c, 200)
    up = (c > vwap) & (df["low"] <= vwap) & (c.shift(1) > vwap.shift(1)) & (c > trend)
    dn = (c < vwap) & (df["high"] >= vwap) & (c.shift(1) < vwap.shift(1)) & (c < trend)
    up &= _bias_ok(df, pd.Series(1.0, index=df.index), use_bias)
    dn &= _bias_ok(df, pd.Series(-1.0, index=df.index), use_bias)
    return _entries(df, up, dn, atr(df) * atr_mult, rr)


def opening_range_breakout(
    df: pd.DataFrame,
    rr: float = 2.0,
    use_bias: bool = False,
    start: str = "13:30",
    minutes: int = 30,
) -> pd.DataFrame:
    """Пробой диапазона первых минут сессии (по умолчанию открытие США 13:30 UTC).
    Одна сделка в день, стоп — противоположная граница диапазона."""
    t0 = pd.Timedelta(start + ":00")
    since = pd.Series(dt_index(df) - dt_index(df).floor("D"), index=df.index)
    in_range = (since >= t0) & (since < t0 + pd.Timedelta(minutes=minutes))
    after = (since >= t0 + pd.Timedelta(minutes=minutes)) & (since < t0 + pd.Timedelta(hours=6))
    day = dt_index(df).floor("D")
    rh = df["high"].where(in_range).groupby(day).transform("max")
    rl = df["low"].where(in_range).groupby(day).transform("min")
    c = df["close"]
    long = after & (c > rh)
    short = after & (c < rl)
    long &= _bias_ok(df, pd.Series(1.0, index=df.index), use_bias).to_numpy()
    short &= _bias_ok(df, pd.Series(-1.0, index=df.index), use_bias).to_numpy()
    first = (long | short) & ~((long | short).astype(int).groupby(day).cumsum() > 1)
    rows = []
    for i in np.flatnonzero(first.to_numpy()):
        d = 1 if long.iloc[i] else -1
        sl = rl.iloc[i] if d > 0 else rh.iloc[i]
        dist = (c.iloc[i] - sl) * d
        rows.append((i, d, np.nan, sl, c.iloc[i] + d * dist * rr, 0, 0))
    return signals_frame(rows)


def fvg_retest(
    df: pd.DataFrame,
    rr: float = 2.0,
    use_bias: bool = True,
    min_gap_atr: float = 0.3,
    expiry: int = 20,
) -> pd.DataFrame:
    """SMC: Fair Value Gap. Бычий разрыв, если low[i] > high[i-2]; ждём возврата цены
    к середине разрыва (лимит), стоп — за минимумом первой свечи, тейк rr·R."""
    h, lo = df["high"], df["low"]
    a = atr(df)
    bull = (lo > h.shift(2)) & ((lo - h.shift(2)) >= min_gap_atr * a)
    bear = (h < lo.shift(2)) & ((lo.shift(2) - h) >= min_gap_atr * a)
    bull &= _bias_ok(df, pd.Series(1.0, index=df.index), use_bias)
    bear &= _bias_ok(df, pd.Series(-1.0, index=df.index), use_bias)
    rows = []
    hv, lv, av = h.to_numpy(), lo.to_numpy(), a.to_numpy()
    for i in np.flatnonzero(bull.to_numpy()):
        mid = (lv[i] + hv[i - 2]) / 2
        sl = lv[i - 2] - 0.1 * av[i]
        rows.append((i, 1, mid, sl, mid + (mid - sl) * rr, expiry, 0))
    for i in np.flatnonzero(bear.to_numpy()):
        mid = (hv[i] + lv[i - 2]) / 2
        sl = hv[i - 2] + 0.1 * av[i]
        rows.append((i, -1, mid, sl, mid - (sl - mid) * rr, expiry, 0))
    return signals_frame(rows)


def order_block(
    df: pd.DataFrame,
    rr: float = 2.0,
    use_bias: bool = True,
    swing: int = 5,
    lookback: int = 30,
    expiry: int = 30,
) -> pd.DataFrame:
    """SMC: слом структуры (закрытие за последним подтверждённым свингом) → последний
    противоположный бар перед импульсом = order block; лимит на возврат к его границе."""
    sw = confirmed_swings(df, swing)
    c, o = df["close"], df["open"]
    bos_up = (c > sw["swing_high"]) & (c.shift(1) <= sw["swing_high"].shift(1))
    bos_dn = (c < sw["swing_low"]) & (c.shift(1) >= sw["swing_low"].shift(1))
    bos_up &= _bias_ok(df, pd.Series(1.0, index=df.index), use_bias)
    bos_dn &= _bias_ok(df, pd.Series(-1.0, index=df.index), use_bias)
    pos = np.arange(len(df))
    last_bear = pd.Series(np.where(c < o, pos, np.nan)).ffill().to_numpy()
    last_bull = pd.Series(np.where(c > o, pos, np.nan)).ffill().to_numpy()
    hv, lv, cv, av = df["high"].to_numpy(), df["low"].to_numpy(), c.to_numpy(), atr(df).to_numpy()
    rows = []
    for i in np.flatnonzero(bos_up.to_numpy()):
        ob = last_bear[i]
        if np.isnan(ob) or i - ob > lookback:
            continue
        k = int(ob)
        entry, sl = hv[k], lv[k] - 0.1 * av[i]
        if entry < cv[i]:
            rows.append((i, 1, entry, sl, entry + (entry - sl) * rr, expiry, 0))
    for i in np.flatnonzero(bos_dn.to_numpy()):
        ob = last_bull[i]
        if np.isnan(ob) or i - ob > lookback:
            continue
        k = int(ob)
        entry, sl = lv[k], hv[k] + 0.1 * av[i]
        if entry > cv[i]:
            rows.append((i, -1, entry, sl, entry - (sl - entry) * rr, expiry, 0))
    return signals_frame(rows)


def liquidity_sweep(
    df: pd.DataFrame, rr: float = 2.0, use_bias: bool = True, level: str = "day"
) -> pd.DataFrame:
    """SMC/ICT: снятие ликвидности. Цена прокалывает максимум/минимум прошлого дня
    (level="day") или последний подтверждённый свинг (level="swing"), затем закрывается
    обратно за уровнем — вход против прокола, стоп за экстремумом прокола."""
    h, lo, c = df["high"], df["low"], df["close"]
    if level == "day":
        day = dt_index(df).floor("D")
        daily = df.groupby(day).agg(hi=("high", "max"), lo=("low", "min")).shift(1)
        top = pd.Series(daily["hi"].reindex(day).to_numpy(), df.index)
        bottom = pd.Series(daily["lo"].reindex(day).to_numpy(), df.index)
        ext_hi = h.groupby(day).cummax()  # экстремум дня до текущего бара включительно
        ext_lo = lo.groupby(day).cummin()
        short = (ext_hi > top) & (c < top)
        long = (ext_lo < bottom) & (c > bottom)
        # только первое возвращение за уровень в этот день
        short &= short.astype(int).groupby(day).cumsum() == 1
        long &= long.astype(int).groupby(day).cumsum() == 1
    else:
        sw = confirmed_swings(df, 10)
        top, bottom = sw["swing_high"], sw["swing_low"]
        ext_hi, ext_lo = h, lo
        short = (h > top) & (c < top)
        long = (lo < bottom) & (c > bottom)
    long &= _bias_ok(df, pd.Series(1.0, index=df.index), use_bias)
    short &= _bias_ok(df, pd.Series(-1.0, index=df.index), use_bias)
    a = atr(df)
    rows = []
    for i in np.flatnonzero(long.to_numpy()):
        sl = ext_lo.iloc[i] - 0.1 * a.iloc[i]
        rows.append((i, 1, np.nan, sl, c.iloc[i] + (c.iloc[i] - sl) * rr, 0, 0))
    for i in np.flatnonzero(short.to_numpy()):
        sl = ext_hi.iloc[i] + 0.1 * a.iloc[i]
        rows.append((i, -1, np.nan, sl, c.iloc[i] - (sl - c.iloc[i]) * rr, 0, 0))
    return signals_frame(rows).dropna(subset=["sl"])


def seasonality(
    df: pd.DataFrame, rr: float = 0.0, use_bias: bool = False, hour: int = 21, hold_hours: int = 2
) -> pd.DataFrame:
    """Сезонность (Quantpedia/Padysak & Vojtko): лонг BTC в 21:00–23:00 UTC.
    Выход по времени; аварийный стоп 2 ATR часового графика."""
    h1 = resample(df, "1h")
    sig_at = dt_index(h1).hour == (hour - 1) % 24  # закрытие бара hour-1 = начало окна
    a = atr(h1)
    bars_per_hour = int(pd.Timedelta("1h") / bar_delta(df))
    rows = []
    close_pos = df.index.get_indexer(h1.index + pd.Timedelta("1h") - bar_delta(df))
    bias = htf_bias(h1)
    for k in np.flatnonzero(sig_at):
        i = close_pos[k]
        if i < 0 or np.isnan(a.iloc[k]):
            continue
        if use_bias and bias.iloc[k] <= 0:
            continue
        cl = df["close"].iloc[i]
        rows.append((i, 1, np.nan, cl - 2 * a.iloc[k], np.nan, 0, hold_hours * bars_per_hour))
    return signals_frame(rows)


def intraday_momentum(df: pd.DataFrame, rr: float = 0.0, use_bias: bool = False) -> pd.DataFrame:
    """Внутридневной TSMOM (Shen et al., Financial Review 2022): доходность дня до 23:30 UTC
    задаёт направление на последние 30 минут. Выход по времени, аварийный стоп 2 ATR(30m)."""
    m30 = resample(df, "30min")
    day = dt_index(m30).floor("D")
    first_open = m30["open"].groupby(day).transform("first")
    ret = m30["close"] / first_open - 1
    at = (dt_index(m30).hour == 23) & (dt_index(m30).minute == 0)  # бар 23:00–23:30
    a = atr(m30)
    close_pos = df.index.get_indexer(m30.index + pd.Timedelta("30min") - bar_delta(df))
    bars = int(pd.Timedelta("30min") / bar_delta(df))
    rows = []
    for k in np.flatnonzero(at):
        i = close_pos[k]
        if i < 0 or np.isnan(a.iloc[k]) or ret.iloc[k] == 0:
            continue
        d = 1 if ret.iloc[k] > 0 else -1
        cl = df["close"].iloc[i]
        rows.append((i, d, np.nan, cl - d * 2 * a.iloc[k], np.nan, 0, bars))
    return signals_frame(rows)


STRATEGIES: dict[str, Callable[..., pd.DataFrame]] = {
    "ema_cross": ema_cross,
    "bb_rsi_reversion": bb_rsi_reversion,
    "donchian_breakout": donchian_breakout,
    "vwap_pullback": vwap_pullback,
    "opening_range_breakout": opening_range_breakout,
    "smc_fvg": fvg_retest,
    "smc_order_block": order_block,
    "smc_sweep_day": lambda df, **kw: liquidity_sweep(df, level="day", **kw),
    "smc_sweep_swing": lambda df, **kw: liquidity_sweep(df, level="swing", **kw),
    "seasonality_21_23": seasonality,
    "intraday_momentum": intraday_momentum,
}
