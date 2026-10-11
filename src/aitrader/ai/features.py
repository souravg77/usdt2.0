"""Features the trade filter sees for one signal (roadmap Phase 4, step 2).

Everything here is computed from bars up to and including the signal bar, so a feature never
peeks at the bar the order would fill on. The same functions feed the training set and the live
paper trader, so the model sees identical inputs in both places.

Funding rate and open-interest change are read from ``funding_rate`` / ``open_interest`` columns
when the candle frame has them (``data.context.attach_context`` adds them from the market-context
table; the training set does so whenever that table has rows). Otherwise they are left missing
(the model handles missing values natively).
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..indicators import adx, atr, atr_percentile, directional_index, ema, relative_volume, rsi
from ..models import TIMEFRAME_MS
from ..strategies.base import Signal

CATEGORICAL = ["strategy", "timeframe"]
MARKET = [
    "atr_pct", "atr_pctile", "adx", "di_spread", "trend_slope", "dist_ema200", "regime",
    "dist_high50", "dist_low50", "range_pos200", "dist_round", "rsi", "rel_volume", "ret_24",
    "hour_sin", "hour_cos", "weekday", "funding_rate", "oi_change",
]
SIGNAL = ["stop_atr", "target_r"]
HISTORY = ["streak", "recent_r"]
NUMERIC = MARKET + SIGNAL + HISTORY
ALL = CATEGORICAL + NUMERIC
RECENT_TRADES = 5


def market_features(df: pd.DataFrame) -> pd.DataFrame:
    """One row of market context per bar of ``df`` (same positions)."""
    close = df["close"]
    a = atr(df, 14)
    out = pd.DataFrame(index=df.index)
    out["atr_pct"] = a / close
    out["atr_pctile"] = atr_percentile(df, 14, 500)
    trend = adx(df, 14)
    di = directional_index(df, 14)
    out["adx"] = trend
    out["di_spread"] = di["plus_di"] - di["minus_di"]
    e50, e200 = ema(close, 50), ema(close, 200)
    out["trend_slope"] = (e50 / e50.shift(10) - 1) / out["atr_pct"]
    out["dist_ema200"] = (close - e200) / a
    up = (close > e200) & (e50 > e200) & (trend > 20)
    down = (close < e200) & (e50 < e200) & (trend > 20)
    regime = pd.Series(np.select([up, down], [1.0, -1.0], 0.0), index=df.index)
    out["regime"] = regime.where(e200.notna())  # +1 up trend, -1 down trend, 0 range
    out["dist_high50"] = (df["high"].rolling(50).max() - close) / a
    out["dist_low50"] = (close - df["low"].rolling(50).min()) / a
    hi200, lo200 = df["high"].rolling(200).max(), df["low"].rolling(200).min()
    out["range_pos200"] = (close - lo200) / (hi200 - lo200).replace(0, np.nan)
    step = close.map(_round_step)
    out["dist_round"] = (close - (close / step).round() * step).abs() / a
    out["rsi"] = rsi(close, 14)
    out["rel_volume"] = relative_volume(df, 20)
    out["ret_24"] = (close / close.shift(24) - 1) / out["atr_pct"]
    hours = pd.DatetimeIndex(pd.to_datetime(df["ts"], unit="ms", utc=True))
    angle = 2 * np.pi * (hours.hour + hours.minute / 60) / 24
    out["hour_sin"], out["hour_cos"] = np.sin(angle), np.cos(angle)
    out["weekday"] = hours.weekday.astype(float)
    out["funding_rate"] = df["funding_rate"] if "funding_rate" in df else np.nan
    out["oi_change"] = (df["open_interest"].pct_change(24, fill_method=None)
                        if "open_interest" in df else np.nan)
    return out.replace([np.inf, -np.inf], np.nan)


def _round_step(price: float) -> float:
    """Spacing of the 'round number' levels near a price: 5 units of the second digit
    (60,000 -> 5,000; 2,400 -> 500; 0.52 -> 0.05)."""
    if not price or price <= 0 or math.isnan(price):
        return float("nan")
    return 5 * 10 ** (math.floor(math.log10(price)) - 1)


def signal_bar(sig: Signal, index_of: dict[int, int]) -> int | None:
    """Position of the bar that produced ``sig`` (its close time is ``sig.ts``)."""
    return index_of.get(sig.ts - TIMEFRAME_MS[sig.timeframe])


def history_features(recent_r: list[float]) -> dict:
    """Win/loss streak and average R of the strategy's previous trades (oldest first), using
    only trades that had closed before the signal."""
    if not recent_r:
        return {"streak": 0.0, "recent_r": np.nan}
    win = recent_r[-1] > 0
    n = 0
    for r in reversed(recent_r):
        if (r > 0) != win:
            break
        n += 1
    last = recent_r[-RECENT_TRADES:]
    return {"streak": float(min(n, 10) if win else -min(n, 10)),
            "recent_r": float(np.mean(last))}


def signal_features(sig: Signal, strategy_id: str, market_row: dict, recent_r: list[float]) -> dict:
    """The full feature row for one signal. ``market_row`` is the signal bar's row of
    ``market_features``."""
    row = {"strategy": strategy_id, "timeframe": sig.timeframe}
    row.update({k: float(market_row[k]) for k in MARKET})
    atr_abs = row["atr_pct"] * sig.entry
    row["stop_atr"] = (sig.entry - sig.stop) / atr_abs if atr_abs > 0 else np.nan  # NaN-safe
    target_r = sig.r_multiple_target
    row["target_r"] = float(target_r) if target_r is not None else np.nan
    row.update(history_features(recent_r))
    return row
