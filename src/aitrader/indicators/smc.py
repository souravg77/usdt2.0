"""Price-structure zones: fair value gaps, order blocks and liquidity sweeps.

All three use only bars that have closed, so a value at bar ``i`` never looks ahead.

- **Fair value gap (FVG).** Three bars where the third bar's low is above the first bar's high
  (bullish) leave a gap that price skipped through. The zone runs from the first bar's high
  (bottom) to the third bar's low (top). It stays open until a later bar's low reaches its bottom
  (filled), or for ``max_age`` bars. Bearish gaps mirror this: the third bar's high is below the
  first bar's low.
- **Order block.** The last down candle before an up candle that closes above the highest high of
  the previous ``n`` bars (a break of structure). The zone is that down candle's low to high. It
  stays valid until a close below its low, or for ``max_age`` bars. Bearish blocks mirror this.
- **Liquidity sweep.** A bar trades below the lowest low of the previous ``n`` bars (where stop
  orders rest) and closes back above it. A sweep of highs mirrors this.

Each zone function returns, per bar, the newest zone still open at that bar's close (NaN when
there is none), so a rule can ask "did this bar dip into the last open gap?"
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _newest_open(n: int, forms, closes_zone, max_age: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Walk the bars once: ``forms(i)`` -> (bottom, top) of a zone formed at bar i, or None;
    ``closes_zone(i, bottom, top)`` -> True when bar i ends that zone (a zone also ends after
    ``max_age`` bars). Returns (new, top, bottom): top/bottom are the newest zone open after bar i."""
    new = np.zeros(n)
    top = np.full(n, np.nan)
    bot = np.full(n, np.nan)
    zones: list[list] = []  # [bottom, top, born]
    for i in range(n):
        zones = [z for z in zones if i - z[2] <= max_age and not closes_zone(i, z[0], z[1])]
        z = forms(i)
        if z is not None:
            zones.append([z[0], z[1], i])
            new[i] = 1.0
        if zones:
            top[i], bot[i] = zones[-1][1], zones[-1][0]
    return new, top, bot


def fair_value_gaps(df: pd.DataFrame, max_age: int = 50) -> pd.DataFrame:
    """Columns: bull_new, bull_top, bull_bottom, bear_new, bear_top, bear_bottom."""
    h, lo = df["high"].to_numpy(float), df["low"].to_numpy(float)
    n = len(df)

    def bull(i):
        if i >= 2 and lo[i] > h[i - 2]:
            return h[i - 2], lo[i]
        return None

    def bear(i):
        if i >= 2 and h[i] < lo[i - 2]:
            return h[i], lo[i - 2]
        return None

    def filled(kind):
        return lambda i, bottom, top: lo[i] <= bottom if kind == "bull" else h[i] >= top

    out = {}
    for kind, forms in (("bull", bull), ("bear", bear)):
        new, top, bot = _newest_open(n, forms, filled(kind), max_age)
        out[f"{kind}_new"], out[f"{kind}_top"], out[f"{kind}_bottom"] = new, top, bot
    return pd.DataFrame(out, index=df.index)


def order_blocks(df: pd.DataFrame, n: int = 10, max_age: int = 50) -> pd.DataFrame:
    """Columns: bull_new, bull_top, bull_bottom, bear_new, bear_top, bear_bottom."""
    o, h = df["open"].to_numpy(float), df["high"].to_numpy(float)
    lo, c = df["low"].to_numpy(float), df["close"].to_numpy(float)
    prior_high = df["high"].shift(1).rolling(n, min_periods=n).max().to_numpy(float)
    prior_low = df["low"].shift(1).rolling(n, min_periods=n).min().to_numpy(float)
    size = len(df)

    def bull(i):
        if i >= 1 and c[i - 1] < o[i - 1] and c[i] > o[i] and c[i] > prior_high[i]:
            return lo[i - 1], h[i - 1]
        return None

    def bear(i):
        if i >= 1 and c[i - 1] > o[i - 1] and c[i] < o[i] and c[i] < prior_low[i]:
            return lo[i - 1], h[i - 1]
        return None

    def broken(kind):
        return lambda i, bottom, top: c[i] < bottom if kind == "bull" else c[i] > top

    out = {}
    for kind, forms in (("bull", bull), ("bear", bear)):
        new, top, bot = _newest_open(size, forms, broken(kind), max_age)
        out[f"{kind}_new"], out[f"{kind}_top"], out[f"{kind}_bottom"] = new, top, bot
    return pd.DataFrame(out, index=df.index)


def liquidity_sweeps(df: pd.DataFrame, n: int = 20) -> pd.DataFrame:
    """Columns: low (1 when this bar swept the previous ``n`` bars' low and closed back above it),
    high (the mirror for highs)."""
    prior_low = df["low"].shift(1).rolling(n, min_periods=n).min()
    prior_high = df["high"].shift(1).rolling(n, min_periods=n).max()
    low = (df["low"] < prior_low) & (df["close"] > prior_low)
    high = (df["high"] > prior_high) & (df["close"] < prior_high)
    return pd.DataFrame({"low": low.astype(float), "high": high.astype(float)}, index=df.index)


def retest(df: pd.DataFrame, top: pd.Series, bottom: pd.Series, bull: bool = True) -> pd.Series:
    """1 when this bar trades into the zone that was open at the previous bar and closes on the
    zone's own side (above its bottom for a bullish zone, below its top for a bearish one)."""
    t, b = top.shift(1), bottom.shift(1)
    if bull:
        hit = (df["low"] <= t) & (df["close"] > b)
    else:
        hit = (df["high"] >= b) & (df["close"] < t)
    return hit.fillna(False).astype(float)
