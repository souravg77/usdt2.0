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


# ---- momentum -----------------------------------------------------------------------------------


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss
    out = 100 - 100 / (1 + rs)
    return out.where(loss != 0, 100.0).where(gain.notna())


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


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
