from __future__ import annotations

import numpy as np
import pandas as pd

from ..models import Candle


def candles_to_frame(candles: list[Candle]) -> pd.DataFrame:
    df = pd.DataFrame(
        [(c.ts, c.open, c.high, c.low, c.close, c.volume) for c in candles],
        columns=["ts", "open", "high", "low", "close", "volume"],
    )
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


# ---- trend --------------------------------------------------------------------------------------


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def ema_stack(close: pd.Series, periods: tuple[int, ...] = (20, 50, 200)) -> pd.Series:
    """+1 when EMAs are stacked bullish (fast > mid > slow), -1 bearish, 0 mixed or warming up."""
    emas = [ema(close, p) for p in periods]
    bull = pd.Series(True, index=close.index)
    bear = pd.Series(True, index=close.index)
    for fast, slow in zip(emas, emas[1:]):
        bull &= fast > slow
        bear &= fast < slow
    return bull.astype(int) - bear.astype(int)


def directional_index(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    """Wilder's +DI and -DI (0-100)."""
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    atr_ = _true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return pd.DataFrame({
        "plus_di": 100 * plus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr_,
        "minus_di": 100 * minus_dm.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr_,
    })


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Average Directional Index (Wilder): trend strength, 0-100."""
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = _true_range(df)
    atr_ = tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr_
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr_
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def supertrend(df: pd.DataFrame, n: int = 10, mult: float = 3.0) -> pd.DataFrame:
    """Supertrend: ``mult`` x ATR(``n``) bands around the bar's mid price (high + low) / 2.
    The lower band only rises and the upper band only falls while price stays inside them; a
    close beyond the active band flips the trend. ``direction`` is +1 (line below price, up),
    -1 (line above price, down) or 0 during warm-up; ``supertrend`` is the active band. Each bar
    uses only itself and earlier bars. The first bar after warm-up starts in +1 (an arbitrary
    seed, as in most charting tools), so treat the first flip after warm-up with care."""
    a = atr(df, n).to_numpy(float)
    hl2 = ((df["high"] + df["low"]) / 2).to_numpy(float)
    close = df["close"].to_numpy(float)
    size = len(df)
    upper, lower = np.full(size, np.nan), np.full(size, np.nan)
    line = np.full(size, np.nan)
    direction = np.zeros(size, dtype=int)
    prev = None  # index of the previous bar with a value
    for i in range(size):
        if np.isnan(a[i]):
            continue
        bu, bl = hl2[i] + mult * a[i], hl2[i] - mult * a[i]
        if prev is None:
            upper[i], lower[i], d = bu, bl, 1
        else:
            upper[i] = bu if bu < upper[prev] or close[prev] > upper[prev] else upper[prev]
            lower[i] = bl if bl > lower[prev] or close[prev] < lower[prev] else lower[prev]
            d = direction[prev]
            if d == 1 and close[i] < lower[i]:
                d = -1
            elif d == -1 and close[i] > upper[i]:
                d = 1
        direction[i] = d
        line[i] = lower[i] if d == 1 else upper[i]
        prev = i
    return pd.DataFrame({"supertrend": line, "direction": direction, "upper": upper, "lower": lower},
                        index=df.index)


# ---- momentum -----------------------------------------------------------------------------------


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss
    out = 100 - 100 / (1 + rs)
    return out.where(loss != 0, 100.0).where(gain.notna())


def stoch_rsi(close: pd.Series, n: int = 14, k: int = 3, d: int = 3) -> pd.DataFrame:
    """Stochastic RSI (0-1): where RSI sits within its own ``n``-bar range. Binance Academy:
    above 0.8 is overbought, below 0.2 oversold. ``k`` and ``d`` are the usual smoothings."""
    r = rsi(close, n)
    lo = r.rolling(n, min_periods=n).min()
    hi = r.rolling(n, min_periods=n).max()
    raw = ((r - lo) / (hi - lo).replace(0, np.nan)).clip(0, 1)
    k_line = raw.rolling(k, min_periods=k).mean()
    return pd.DataFrame({"stoch_rsi": raw, "k": k_line, "d": k_line.rolling(d, min_periods=d).mean()})


def stochastic(df: pd.DataFrame, n: int = 14, smooth: int = 3, d: int = 3) -> pd.DataFrame:
    """Slow stochastic oscillator (0-100): %K smoothed over ``smooth`` bars, %D over ``d``."""
    lo = df["low"].rolling(n, min_periods=n).min()
    hi = df["high"].rolling(n, min_periods=n).max()
    raw = 100 * (df["close"] - lo) / (hi - lo).replace(0, np.nan)
    k = raw.rolling(smooth, min_periods=smooth).mean()
    return pd.DataFrame({"k": k, "d": k.rolling(d, min_periods=d).mean()})


def cci(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """Commodity Channel Index on the typical price with Lambert's 0.015 constant."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    ma = tp.rolling(n, min_periods=n).mean()
    mad = tp.rolling(n, min_periods=n).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (tp - ma) / (0.015 * mad.replace(0, np.nan))


def force_index(df: pd.DataFrame, n: int = 13) -> pd.Series:
    """Elder's Force Index: EMA of (close - previous close) x volume."""
    raw = df["close"].diff() * df["volume"]
    return raw.ewm(span=n, adjust=False, min_periods=n).mean()


def heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    o = np.empty(len(df))
    if len(df):
        o[0] = (df["open"].iloc[0] + df["close"].iloc[0]) / 2
        hc = ha_close.to_numpy(float)
        for i in range(1, len(df)):
            o[i] = (o[i - 1] + hc[i - 1]) / 2
    ha_open = pd.Series(o, index=df.index)
    return pd.DataFrame({"open": ha_open, "close": ha_close})


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def williams_r(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Williams %R (-100 to 0): where the close sits in the last ``n`` bars' high-low range.
    Near -100 is oversold, near 0 overbought. NaN during warm-up or on a zero range."""
    hi = df["high"].rolling(n, min_periods=n).max()
    lo = df["low"].rolling(n, min_periods=n).min()
    return -100 * (hi - df["close"]) / (hi - lo).replace(0, np.nan)


# ---- volatility ---------------------------------------------------------------------------------


def _true_range(df: pd.DataFrame) -> pd.Series:
    prev = df["close"].shift()
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - prev).abs(), (df["low"] - prev).abs()], axis=1
    ).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return _true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def atr_percentile(df: pd.DataFrame, n: int = 14, lookback: int = 500) -> pd.Series:
    """Where today's ATR sits within the last ``lookback`` ATR values (0-1)."""
    a = atr(df, n)
    return a.rolling(lookback, min_periods=max(n, 20)).rank(pct=True)


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    upper, lower = mid + k * sd, mid - k * sd
    return pd.DataFrame({"mid": mid, "upper": upper, "lower": lower, "width": (upper - lower) / mid})


def keltner(df: pd.DataFrame, n: int = 20, mult: float = 2.0, atr_n: int = 10) -> pd.DataFrame:
    """Keltner Channels: EMA(``n``) of close plus and minus ``mult`` x ATR(``atr_n``)."""
    mid = ema(df["close"], n)
    a = atr(df, atr_n)
    return pd.DataFrame({"mid": mid, "upper": mid + mult * a, "lower": mid - mult * a})


def parabolic_sar(df: pd.DataFrame, step: float = 0.02, max_step: float = 0.2) -> pd.DataFrame:
    """Wilder's Parabolic SAR. ``sar`` is the stop level for bar t, built from bars before t and
    flipped when bar t crosses it; ``trend`` is +1 (dots below price) or -1 (dots above)."""
    high, low = df["high"].to_numpy(float), df["low"].to_numpy(float)
    n = len(df)
    sar = np.full(n, np.nan)
    trend = np.zeros(n, dtype=int)
    if n < 2:
        return pd.DataFrame({"sar": sar, "trend": trend}, index=df.index)
    up = high[1] + low[1] >= high[0] + low[0]
    ep = high[0] if up else low[0]
    cur = low[0] if up else high[0]
    af = step
    for i in range(1, n):
        cur = cur + af * (ep - cur)
        if up:
            cur = min(cur, low[i - 1], low[i - 2] if i >= 2 else low[i - 1])
            if low[i] < cur:  # reversal: the stop was hit
                up, cur, ep, af = False, ep, low[i], step
            elif high[i] > ep:
                ep, af = high[i], min(af + step, max_step)
        else:
            cur = max(cur, high[i - 1], high[i - 2] if i >= 2 else high[i - 1])
            if high[i] > cur:
                up, cur, ep, af = True, ep, high[i], step
            elif low[i] < ep:
                ep, af = low[i], min(af + step, max_step)
        sar[i] = cur
        trend[i] = 1 if up else -1
    return pd.DataFrame({"sar": sar, "trend": trend}, index=df.index)


def ichimoku(df: pd.DataFrame, conversion: int = 9, base: int = 26, span: int = 52) -> pd.DataFrame:
    """Ichimoku Cloud. The leading spans are shifted forward ``base`` bars, so the cloud at bar t
    uses data up to t - base (no look-ahead). The lagging span (chikou) is left out because it
    plots today's close in the past and would leak future data into a model."""
    def mid(n):
        return (df["high"].rolling(n, min_periods=n).max() + df["low"].rolling(n, min_periods=n).min()) / 2

    tenkan, kijun = mid(conversion), mid(base)
    span_a = ((tenkan + kijun) / 2).shift(base)
    span_b = mid(span).shift(base)
    top = pd.concat([span_a, span_b], axis=1).max(axis=1, skipna=False)
    bottom = pd.concat([span_a, span_b], axis=1).min(axis=1, skipna=False)
    pos = np.where(df["close"] > top, 1, np.where(df["close"] < bottom, -1, 0))
    return pd.DataFrame({"tenkan": tenkan, "kijun": kijun, "span_a": span_a, "span_b": span_b,
                         "above_cloud": pd.Series(pos, index=df.index).where(top.notna(), 0)})


# ---- volume -------------------------------------------------------------------------------------


def vwap(df: pd.DataFrame, n: int | None = 20, anchor: str | None = None) -> pd.Series:
    """Volume-weighted average price of the typical price. Rolling over ``n`` bars, or reset
    at each ``anchor`` period instead (``anchor="D"`` gives the usual daily VWAP, UTC)."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    pv = typical * df["volume"]
    if anchor:
        key = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.floor(anchor).to_numpy()
        return pv.groupby(key).cumsum() / df["volume"].groupby(key).cumsum().replace(0, np.nan)
    return pv.rolling(n, min_periods=n).sum() / df["volume"].rolling(n, min_periods=n).sum().replace(0, np.nan)


def vwap_bands(df: pd.DataFrame, n: int | None = 20, anchor: str | None = "D") -> pd.DataFrame:
    """VWAP with its volume-weighted standard deviation of the typical price over the same bars
    (anchored at each ``anchor`` period, UTC, or rolling over ``n`` bars). ``bars`` counts the
    bars in the current anchor period (or ``n`` once a rolling window is full)."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"]
    if anchor:
        key = pd.to_datetime(df["ts"], unit="ms", utc=True).dt.floor(anchor).to_numpy()
        v = vol.groupby(key).cumsum().replace(0, np.nan)
        mean = (typical * vol).groupby(key).cumsum() / v
        sq = (typical ** 2 * vol).groupby(key).cumsum() / v
        bars = pd.Series(1, index=df.index).groupby(key).cumsum()
    else:
        v = vol.rolling(n, min_periods=n).sum().replace(0, np.nan)
        mean = (typical * vol).rolling(n, min_periods=n).sum() / v
        sq = (typical ** 2 * vol).rolling(n, min_periods=n).sum() / v
        bars = pd.Series(np.minimum(np.arange(len(df)) + 1, n), index=df.index)
    sd = np.sqrt((sq - mean ** 2).clip(lower=0))
    return pd.DataFrame({"vwap": mean, "sd": sd, "upper1": mean + sd, "lower1": mean - sd,
                         "upper2": mean + 2 * sd, "lower2": mean - 2 * sd, "bars": bars}, index=df.index)


def relative_volume(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """This bar's volume divided by the average of the previous ``n`` bars."""
    return df["volume"] / df["volume"].shift(1).rolling(n, min_periods=n).mean()


def obv(df: pd.DataFrame) -> pd.Series:
    """On-Balance Volume: running total of volume, added on up closes and subtracted on down
    closes. It starts at 0 on the first bar, so only its changes and its own highs, lows and
    averages mean anything, never its level."""
    sign = np.sign(df["close"].diff()).fillna(0.0)
    return (sign * df["volume"]).cumsum()


def mfi(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """Money Flow Index (0-100): RSI-like ratio of up-bar to down-bar money flow (typical price
    x volume) over ``n`` bars. 100 when there was no down flow, 50 when there was no flow at all.
    The first value needs ``n`` price changes, so ``n + 1`` bars."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    flow = tp * df["volume"]
    change = tp.diff()
    pos = flow.where(change > 0, 0.0).where(change.notna()).rolling(n, min_periods=n).sum()
    neg = flow.where(change < 0, 0.0).where(change.notna()).rolling(n, min_periods=n).sum()
    out = 100 - 100 / (1 + pos / neg.replace(0, np.nan))
    out = out.where(neg != 0, 100.0).where(pos + neg != 0, 50.0)
    return out.where(pos.notna())


def cmf(df: pd.DataFrame, n: int = 20) -> pd.Series:
    """Chaikin Money Flow (-1 to +1): volume-weighted average over ``n`` bars of where each bar
    closed in its range (+1 at the high, -1 at the low). Above 0 means buying pressure. A bar
    with no range counts as 0; NaN while there is no volume in the window."""
    rng = df["high"] - df["low"]
    mfm = (((df["close"] - df["low"]) - (df["high"] - df["close"])) / rng.replace(0, np.nan)).fillna(0.0)
    vol = df["volume"].rolling(n, min_periods=n).sum()
    return (mfm * df["volume"]).rolling(n, min_periods=n).sum() / vol.replace(0, np.nan)


# ---- structure and levels -----------------------------------------------------------------------


def swing_points(df: pd.DataFrame, left: int = 2, right: int = 2) -> pd.DataFrame:
    """Fractal swing highs/lows. A swing is only *known* ``right`` bars later, so the flags are
    placed on the confirming bar (no look-ahead); ``swing_high``/``swing_low`` hold the price."""
    win = left + right + 1
    hi_max = df["high"].rolling(win).max()
    lo_min = df["low"].rolling(win).min()
    centre_hi = df["high"].shift(right)
    centre_lo = df["low"].shift(right)
    return pd.DataFrame(
        {
            "swing_high": centre_hi.where(centre_hi == hi_max),
            "swing_low": centre_lo.where(centre_lo == lo_min),
        },
        index=df.index,
    )


def market_structure(df: pd.DataFrame, left: int = 2, right: int = 2) -> pd.Series:
    """+1 uptrend (higher high and higher low), -1 downtrend (lower high and lower low), else 0."""
    sw = swing_points(df, left, right)
    highs = sw["swing_high"].dropna()
    lows = sw["swing_low"].dropna()
    hh = (highs > highs.shift()).reindex(df.index).ffill()
    lh = (highs < highs.shift()).reindex(df.index).ffill()
    hl = (lows > lows.shift()).reindex(df.index).ffill()
    ll = (lows < lows.shift()).reindex(df.index).ffill()
    up = (hh.fillna(False) & hl.fillna(False)).astype(int)
    down = (lh.fillna(False) & ll.fillna(False)).astype(int)
    return up - down


def support_resistance(
    df: pd.DataFrame, tolerance: float = 0.003, min_touches: int = 2, left: int = 3, right: int = 3
) -> list[dict]:
    """Cluster swing highs/lows within ``tolerance`` (fraction of price) into levels."""
    sw = swing_points(df, left, right)
    pts = sorted(pd.concat([sw["swing_high"].dropna(), sw["swing_low"].dropna()]).tolist())
    levels: list[list[float]] = []
    for p in pts:
        if levels and abs(p - np.mean(levels[-1])) / p <= tolerance:
            levels[-1].append(p)
        else:
            levels.append([p])
    return [
        {"price": float(np.mean(g)), "touches": len(g)} for g in levels if len(g) >= min_touches
    ]


# ---- candlestick patterns (1.0 where the pattern completes on this bar, else 0.0) ---------------


def candle_patterns(df: pd.DataFrame) -> pd.DataFrame:
    """Common single- and two-bar patterns, judged on the bar that completes them.

    - bullish_engulfing: a down bar followed by an up bar whose body covers the previous body
    - hammer: lower wick at least 2x the body, upper wick at most 0.5x the body (or a tenth of
      the range), body in the upper third of the range
    - doji: body at most 10% of the bar's range
    - inside_bar: high below the previous high and low above the previous low
    - bullish_bar: close above open
    """
    o, h, lo, c = df["open"], df["high"], df["low"], df["close"]
    body = (c - o).abs()
    rng = (h - lo).replace(0, np.nan)
    upper = h - pd.concat([o, c], axis=1).max(axis=1)
    lower = pd.concat([o, c], axis=1).min(axis=1) - lo
    po, pc = o.shift(1), c.shift(1)
    engulf = (pc < po) & (c > o) & (c >= po) & (o <= pc) & (body > (pc - po).abs())
    hammer = (lower >= 2 * body) & ((upper <= 0.5 * body) | (upper <= 0.1 * rng)) & \
        (pd.concat([o, c], axis=1).min(axis=1) >= lo + 2 / 3 * (h - lo)) & (body > 0)
    doji = body <= 0.1 * rng
    inside = (h < h.shift(1)) & (lo > lo.shift(1))
    return pd.DataFrame({"bullish_engulfing": engulf, "hammer": hammer, "doji": doji,
                         "inside_bar": inside, "bullish_bar": c > o}, index=df.index).astype(float)


def streak(cond: pd.Series) -> pd.Series:
    """How many bars in a row (ending at this bar) ``cond`` has been true."""
    c = cond.fillna(False).astype(bool)
    groups = (~c).cumsum()
    return c.astype(int).groupby(groups).cumsum().astype(float)


def bullish_divergence(df: pd.DataFrame, osc: pd.Series, lookback: int = 30,
                       left: int = 2, right: int = 2) -> pd.Series:
    """1.0 on the bar where a bullish divergence becomes known: the latest confirmed swing low
    in price is lower than the previous swing low (within ``lookback`` bars), while the
    oscillator at those two lows made a higher low. Swings are fractal lows confirmed ``right``
    bars later, so there is no look-ahead."""
    lows = df["low"].to_numpy(float)
    o = osc.to_numpy(float)
    n, win = len(df), left + right + 1
    out = np.zeros(n)
    prev = None  # (index, price, osc) of the previous swing low
    for t in range(win - 1, n):
        j = t - right  # candidate swing low, confirmed at bar t
        seg = lows[t - win + 1:t + 1]
        if lows[j] != seg.min() or np.isnan(o[j]):
            continue
        if prev is not None and j - prev[0] <= lookback and lows[j] < prev[1] and o[j] > prev[2]:
            out[t] = 1.0
        prev = (j, lows[j], o[j])
    return pd.Series(out, index=df.index)


FIB_RATIOS = (0.236, 0.382, 0.5, 0.618, 0.786)


def donchian(df: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    """Donchian channel of the *previous* ``n`` bars (the current bar is left out, so a close
    above ``upper`` is a breakout): highest high, lowest low and their middle."""
    upper = df["high"].shift(1).rolling(n, min_periods=n).max()
    lower = df["low"].shift(1).rolling(n, min_periods=n).min()
    return pd.DataFrame({"upper": upper, "lower": lower, "mid": (upper + lower) / 2}, index=df.index)


def fib_levels(swing_low: float, swing_high: float, ratios=FIB_RATIOS) -> dict[float, float]:
    """Fibonacci retracement prices for a move from ``swing_low`` up to ``swing_high``: how far
    a pullback would go at each ratio (Binance Academy: 23.6, 38.2, 50, 61.8, 78.6%)."""
    span = swing_high - swing_low
    return {r: swing_high - r * span for r in ratios}


def volume_profile(df: pd.DataFrame, bins: int = 24) -> pd.DataFrame:
    """Volume traded per price bucket (using each bar's typical price). Includes the point of
    control (POC) as the bucket with the most volume."""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    edges = np.linspace(df["low"].min(), df["high"].max(), bins + 1)
    idx = np.clip(np.digitize(typical, edges) - 1, 0, bins - 1)
    vol = np.bincount(idx, weights=df["volume"].to_numpy(), minlength=bins)
    out = pd.DataFrame({"low": edges[:-1], "high": edges[1:], "volume": vol})
    out["poc"] = out["volume"] == out["volume"].max()
    return out


def value_area(df: pd.DataFrame, bins: int = 24, share: float = 0.70) -> dict:
    """Point of control and value area of ``df`` (pass a trailing window, e.g. the last 100
    bars, so nothing later leaks in): the busiest price bucket, then neighbouring buckets added
    on the busier side until ``share`` of the volume is inside. Prices are bucket mid-points
    for the POC and bucket edges for VAH/VAL."""
    prof = volume_profile(df, bins)
    vol = prof["volume"].to_numpy(float)
    total = vol.sum()
    if not len(df) or total <= 0:
        return {"poc": float("nan"), "vah": float("nan"), "val": float("nan")}
    poc = int(np.argmax(vol))
    lo = hi = poc
    inside = vol[poc]
    while inside < share * total and (lo > 0 or hi < len(vol) - 1):
        below = vol[lo - 1] if lo > 0 else -1.0
        above = vol[hi + 1] if hi < len(vol) - 1 else -1.0
        if above >= below:
            hi += 1
            inside += above
        else:
            lo -= 1
            inside += below
    row = prof.iloc[poc]
    return {"poc": float((row["low"] + row["high"]) / 2), "vah": float(prof["high"].iloc[hi]),
            "val": float(prof["low"].iloc[lo])}


# ---- validity -----------------------------------------------------------------------------------
# Every indicator above returns NaN (or 0 for the +1/-1 state columns) until it has enough bars.
# They don't see gaps in the candle series, though: a rolling window that spans a missing candle
# silently covers more time than it should. ``indicator_valid`` gives the same yes/no answer for
# any indicator: warm-up done and no missing candle inside its window.


def _step_ms(df: pd.DataFrame, step_ms: int | None) -> int:
    if step_ms:
        return int(step_ms)
    diffs = np.diff(df["ts"].to_numpy(np.int64))
    return int(np.median(diffs)) if len(diffs) else 0


def missing_candles(df: pd.DataFrame, step_ms: int | None = None) -> pd.Series:
    """How many candles are missing just before each bar (0 for a regular series). ``step_ms``
    is the timeframe in ms; by default the median spacing of ``ts``."""
    step = _step_ms(df, step_ms)
    gap = df["ts"].astype("int64").diff()
    if not step:
        return pd.Series(0, index=df.index, dtype=int)
    return ((gap / step).round() - 1).clip(lower=0).fillna(0).astype(int)


def indicator_valid(df: pd.DataFrame, warmup: int, step_ms: int | None = None,
                    value: pd.Series | None = None) -> pd.Series:
    """True on bars where an indicator that needs ``warmup`` bars can be trusted: at least
    ``warmup`` bars of history, no missing candle inside the last ``warmup`` bars, and (when
    ``value`` is given) the value itself is a finite number. Uses only past bars."""
    warmup = int(warmup)
    ok = pd.Series(np.arange(len(df)) + 1 >= warmup, index=df.index)
    if warmup > 1:
        # the window's own gaps sit before its last warmup - 1 bars (not before its first bar)
        gaps = (missing_candles(df, step_ms) > 0).astype(int)
        ok &= gaps.rolling(warmup - 1, min_periods=1).max() == 0
    if value is not None:
        ok &= np.isfinite(value.astype(float))
    return ok
