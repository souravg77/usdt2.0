"""Market regime classifier (agent G): what kind of market each closed bar belongs to.

Every bar gets one label, checked in this order (the first that applies wins):

- ``low-liquidity``: under ``min_daily_usdt`` traded per day on average (close x volume)
- ``high-volatility``: ATR as a share of price is higher than ``high_vol_rank`` of the last 500 bars
- ``trend-up``: ADX at least ``trend_adx``, +DI over -DI, price above a rising EMA200
- ``trend-down``: ADX at least ``trend_adx``, -DI over +DI, price below a falling EMA200
- ``range``: ADX under ``trend_adx`` (no clear trend)
- ``uncertain``: not enough history yet, or a trend reading whose signals disagree

Only closed candles are used and every input is a rolling or exponential indicator, so the label
of a bar never changes when later bars arrive (no look-ahead). The label describes the market; it
does not block trades. Results can be split by it later (``breakdown``).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

from ..indicators import adx, atr, directional_index, ema
from ..models import TIMEFRAME_MS

REGIMES = ("trend-up", "trend-down", "range", "high-volatility", "low-liquidity", "uncertain")
DAY_MS = 86_400_000


@dataclass(frozen=True)
class RegimeConfig:
    trend_adx: float = 20.0  # ADX at or above this counts as trending (Wilder: 20-25)
    ema_period: int = 200
    slope_bars: int = 10  # EMA200 rising = higher than it was this many bars ago
    high_vol_rank: float = 0.9  # ATR% above 90% of the last ``vol_lookback`` bars
    vol_lookback: int = 500
    min_daily_usdt: float = 1_000_000.0  # average quote turnover per day


@dataclass(frozen=True)
class Regime:
    label: str
    confidence: float  # 0-1: how clearly the bar fits its label
    reason: str  # short plain-language explanation, e.g. "ADX 31, price above rising EMA200"
    ts: int | None = None  # close time (UTC ms) of the bar it describes

    def as_dict(self) -> dict:
        return asdict(self)


def regime_features(df: pd.DataFrame, timeframe: str, cfg: RegimeConfig | None = None) -> pd.DataFrame:
    cfg = cfg or RegimeConfig()
    close = df["close"]
    di = directional_index(df)
    trend_ema = ema(close, cfg.ema_period)
    atr_share = atr(df) / close
    bars_per_day = max(1.0, DAY_MS / TIMEFRAME_MS[timeframe])
    window = int(max(20, min(bars_per_day, 2000)))
    turnover = (close * df["volume"]).rolling(window, min_periods=20).mean() * bars_per_day
    return pd.DataFrame({
        "adx": adx(df), "plus_di": di["plus_di"], "minus_di": di["minus_di"],
        "close": close, "ema": trend_ema, "slope": trend_ema - trend_ema.shift(cfg.slope_bars),
        "atr_share": atr_share,
        "vol_rank": atr_share.rolling(cfg.vol_lookback, min_periods=50).rank(pct=True),
        "daily_usdt": turnover,
    }, index=df.index)


def _labels(f: pd.DataFrame, cfg: RegimeConfig) -> np.ndarray:
    ready = f[["adx", "plus_di", "minus_di", "ema", "slope"]].notna().all(axis=1).to_numpy()
    up = ((f["adx"] >= cfg.trend_adx) & (f["plus_di"] > f["minus_di"]) & (f["close"] > f["ema"])
          & (f["slope"] > 0)).to_numpy()
    down = ((f["adx"] >= cfg.trend_adx) & (f["minus_di"] > f["plus_di"]) & (f["close"] < f["ema"])
            & (f["slope"] < 0)).to_numpy()
    flat = (f["adx"] < cfg.trend_adx).to_numpy()
    thin = (f["daily_usdt"] < cfg.min_daily_usdt).to_numpy()  # NaN compares False: not judged yet
    wild = (f["vol_rank"] >= cfg.high_vol_rank).to_numpy()
    return np.select(
        [thin, wild, ~ready, up, down, flat],
        ["low-liquidity", "high-volatility", "uncertain", "trend-up", "trend-down", "range"],
        default="uncertain",
    )


def regime_labels(df: pd.DataFrame, timeframe: str, cfg: RegimeConfig | None = None) -> pd.Series:
    """One label per bar (same index as ``df``), computed from that bar and the ones before it."""
    cfg = cfg or RegimeConfig()
    if df is None or not len(df):
        return pd.Series([], dtype=object)
    return pd.Series(_labels(regime_features(df, timeframe, cfg), cfg), index=df.index)


def labels_by_close(df: pd.DataFrame, timeframe: str, cfg: RegimeConfig | None = None) -> dict[int, str]:
    """Bar close time -> label. A signal's ``ts`` is its bar's close time, so this tags signals."""
    labels = regime_labels(df, timeframe, cfg)
    step = TIMEFRAME_MS[timeframe]
    return dict(zip((df["ts"].to_numpy(np.int64) + step).tolist(), labels.tolist()))


def classify(df: pd.DataFrame, timeframe: str, cfg: RegimeConfig | None = None) -> Regime:
    """The regime of the newest bar in ``df`` (pass closed candles only), with a reason."""
    cfg = cfg or RegimeConfig()
    if df is None or not len(df):
        return Regime("uncertain", 0.0, "no candles")
    f = regime_features(df, timeframe, cfg)
    label = str(_labels(f, cfg)[-1])
    row = f.iloc[-1]
    ts = int(df["ts"].iloc[-1]) + TIMEFRAME_MS[timeframe]
    a = row["adx"]
    if label == "low-liquidity":
        conf = 1 - row["daily_usdt"] / cfg.min_daily_usdt
        reason = f"about {_usdt(row['daily_usdt'])} USDT traded a day (floor {_usdt(cfg.min_daily_usdt)})"
    elif label == "high-volatility":
        conf = (row["vol_rank"] - cfg.high_vol_rank) / max(1e-9, 1 - cfg.high_vol_rank)
        reason = (f"ATR {row['atr_share'] * 100:.1f}% of price, at the {row['vol_rank'] * 100:.0f}th "
                  f"percentile of the last {cfg.vol_lookback} bars")
    elif label in ("trend-up", "trend-down"):
        conf = (a - cfg.trend_adx + 5) / 25
        side = "above rising" if label == "trend-up" else "below falling"
        reason = f"ADX {a:.0f}, price {side} EMA{cfg.ema_period}"
    elif label == "range":
        conf = (cfg.trend_adx - a + 5) / 20
        where = "above" if row["close"] > row["ema"] else "below"
        reason = f"ADX {a:.0f}: no clear trend (price {where} EMA{cfg.ema_period})"
    elif not np.isfinite(row["ema"]) or not np.isfinite(a) or not np.isfinite(row["slope"]):
        conf = 0.0
        reason = f"only {len(df)} closed bars; need about {cfg.ema_period + cfg.slope_bars} to judge"
    else:
        conf = 0.3
        where = "above" if row["close"] > row["ema"] else "below"
        slope = "rising" if row["slope"] > 0 else "falling"
        di = "+DI" if row["plus_di"] > row["minus_di"] else "-DI"
        reason = f"ADX {a:.0f} but signals disagree: price {where} {slope} EMA{cfg.ema_period}, {di} leads"
    return Regime(label, round(float(np.clip(np.nan_to_num(conf), 0.2 if label != "uncertain" else 0.0, 1.0)), 2),
                  reason, ts)


def breakdown(rows) -> dict:
    """Results split by regime. ``rows`` are (label, Trade) pairs; trades without a label count
    as "unknown" (e.g. paper trades from before the classifier existed)."""
    from ..backtest import metrics

    groups: dict = defaultdict(list)
    for label, trade in rows:
        groups[label or "unknown"].append(trade)
    out = {}
    for label in [*REGIMES, *sorted(set(groups) - set(REGIMES))]:
        if label not in groups:
            continue
        m = metrics(groups[label])
        out[label] = {"trades": m["trades"], "win_rate": round(m["win_rate"], 4),
                      "expectancy_r": round(m["expectancy_r"], 4), "total_r": round(m["total_r"], 3)}
    return out


def current_regimes(store, symbols: list[str], timeframes: list[str], exchanges: list[str],
                    now_ms: int | None = None, bars: int = 700, cfg: RegimeConfig | None = None) -> list[dict]:
    """The regime of the newest closed bar for each symbol and timeframe, from stored candles.
    Uses the first exchange in ``exchanges`` that has candles for the market."""
    import time

    from ..indicators import candles_to_frame

    now_ms = now_ms or int(time.time() * 1000)
    out = []
    for sym in symbols:
        for tf in timeframes:
            step = TIMEFRAME_MS[tf]
            row = {"symbol": sym, "timeframe": tf, "exchange": None, "label": None, "confidence": 0.0,
                   "reason": "no stored candles", "ts": None}
            for ex in exchanges:
                candles = [c for c in store.load(ex, sym, tf, start=now_ms - bars * step)
                           if c.ts + step <= now_ms]  # closed bars only
                if candles:
                    row.update(classify(candles_to_frame(candles), tf, cfg).as_dict(), exchange=ex)
                    break
            out.append(row)
    return out


def _usdt(x: float) -> str:
    if not np.isfinite(x):
        return "?"
    for div, unit in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(x) >= div:
            return f"{x / div:.1f}{unit}"
    return f"{x:.0f}"
