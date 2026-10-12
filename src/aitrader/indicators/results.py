"""The 20 standard indicators with one output shape, ``IndicatorResult`` (Agent B's contract).

Each function reads a candle frame (``candles_to_frame``) of **closed** candles and describes its
last bar:

- ``features``: numbers scaled so they compare across coins and timeframes (distances in ATRs
  or percent, oscillators mapped to -1..+1), plus the raw value.
- ``direction``: +1 bullish, -1 bearish, 0 neutral or not applicable (ATR and volume profile
  inside value say nothing about direction).
- ``confidence``: 0..1, how clear the reading is. It is a description of the indicator, not a
  probability that a trade wins.
- ``is_valid``: False during warm-up, when a candle is missing inside the indicator's window, or
  when the value isn't a finite number. A strategy must not act on an invalid result.
- ``warmup_period``: candles the indicator needs before it can be valid.
- ``metadata``: thresholds and zones (overbought, squeeze, value area, ...).

An overbought or oversold reading is reported in ``metadata["zone"]`` and never on its own as a
direction: RSI at 75 in a strong trend is momentum, not a sell signal.

Point in time: every value uses only the bars in the frame, and ``closed_only`` drops a candle
that hasn't closed yet, so a live decision never reads a forming bar.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from . import core

EXTREME_FUNDING = 0.0005  # 0.05% per 8 hours: crowded positioning (5x the usual 0.01%)


@dataclass(frozen=True)
class IndicatorResult:
    name: str
    features: dict[str, float]
    direction: int  # -1 bearish, 0 neutral, +1 bullish
    confidence: float  # 0..1
    is_valid: bool
    warmup_period: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"name": self.name, "features": self.features, "direction": self.direction,
                "confidence": self.confidence, "is_valid": self.is_valid,
                "warmup_period": self.warmup_period, "metadata": self.metadata}


def closed_only(df: pd.DataFrame, now_ms: int, step_ms: int) -> pd.DataFrame:
    """``df`` without candles that are still forming at ``now_ms`` (open time + step > now)."""
    return df[df["ts"].astype("int64") + int(step_ms) <= int(now_ms)]


# ---- helpers ------------------------------------------------------------------------------------


def _last(s: pd.Series) -> float:
    if not len(s):
        return math.nan
    v = s.iloc[-1]
    try:
        return float(v)
    except (TypeError, ValueError):
        return math.nan


def _finite(*vals) -> bool:
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in vals)


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    if not math.isfinite(x):
        return 0.0
    return float(min(hi, max(lo, x)))


def _sign(x: float, dead: float = 0.0) -> int:
    if not math.isfinite(x) or abs(x) <= dead:
        return 0
    return 1 if x > 0 else -1


def _valid(df: pd.DataFrame, warmup: int, *vals: float) -> bool:
    """Warm-up done, no candle missing inside the last ``warmup`` bars, values finite."""
    w = max(1, int(warmup))
    if len(df) < w or not _finite(*vals):
        return False
    return bool(core.missing_candles(df.iloc[-w:]).iloc[1:].eq(0).all())


def _atr(df: pd.DataFrame, n: int = 14) -> float:
    return _last(core.atr(df, n))


def _result(name, features, direction, confidence, valid, warmup, **meta) -> IndicatorResult:
    feats = {k: (float(v) if v is not None else math.nan) for k, v in features.items()}
    if not valid:
        direction, confidence = 0, 0.0
    return IndicatorResult(name, feats, int(direction), round(_clip(confidence), 4), bool(valid),
                           int(warmup), meta)


def _osc_zone(value: float, low: float, high: float) -> str:
    if not math.isfinite(value):
        return "unknown"
    return "overbought" if value >= high else "oversold" if value <= low else "neutral"


# ---- trend --------------------------------------------------------------------------------------


def ema_result(df: pd.DataFrame, n: int = 50, slope_bars: int = 5) -> IndicatorResult:
    """Close above a rising EMA: +1; below a falling one: -1; otherwise 0. Confidence grows with
    the distance from the EMA in ATRs (3 ATRs = 1)."""
    warm = 3 * n  # an EMA seeded on the first bar needs ~3n bars to forget the seed
    e = core.ema(df["close"], n)
    v, a, c = _last(e), _atr(df), _last(df["close"])
    slope = (v - _last(e.shift(slope_bars))) / a if a else math.nan
    dist = (c - v) / a if a else math.nan
    d = _sign(dist) if _sign(dist) == _sign(slope) else 0
    return _result("ema", {"ema": v, "dist_atr": dist, "slope_atr": slope}, d, abs(dist) / 3,
                   _valid(df, warm, v, dist, slope), warm, period=n)


def sma_result(df: pd.DataFrame, n: int = 200, slope_bars: int = 10) -> IndicatorResult:
    """The trend baseline: close above a rising SMA is +1, below a falling one -1."""
    warm = n + slope_bars
    m = core.sma(df["close"], n)
    v, a, c = _last(m), _atr(df), _last(df["close"])
    slope = (v - _last(m.shift(slope_bars))) / a if a else math.nan
    dist = (c - v) / a if a else math.nan
    d = _sign(dist) if _sign(dist) == _sign(slope) else 0
    return _result("sma", {"sma": v, "dist_atr": dist, "slope_atr": slope, "dist_pct": (c - v) / v if v else math.nan},
                   d, abs(dist) / 3, _valid(df, warm, v, dist, slope), warm, period=n)


def adx_result(df: pd.DataFrame, n: int = 14, trend_min: float = 20.0) -> IndicatorResult:
    """Trend strength (ADX) with the DI filter: direction is the stronger DI, only when ADX is at
    least ``trend_min``. Confidence: ADX 20 -> 0, 50 -> 1."""
    warm = 3 * n
    v = _last(core.adx(df, n))
    di = core.directional_index(df, n)
    plus, minus = _last(di["plus_di"]), _last(di["minus_di"])
    d = _sign(plus - minus) if v >= trend_min else 0
    return _result("adx", {"adx": v, "adx_norm": v / 100, "plus_di": plus, "minus_di": minus,
                           "di_spread": (plus - minus) / 100},
                   d, (v - trend_min) / 30, _valid(df, warm, v, plus, minus), warm,
                   trend_min=trend_min, trending=bool(v >= trend_min) if math.isfinite(v) else False)


def ichimoku_result(df: pd.DataFrame, conversion: int = 9, base: int = 26, span: int = 52) -> IndicatorResult:
    """Price vs the cloud, Tenkan vs Kijun, and Chikou (today's close vs the close ``base`` bars
    ago, the point-in-time form of the lagging span). Direction: the side at least two of the
    three agree on; confidence: share agreeing."""
    warm = span + base
    ich = core.ichimoku(df, conversion, base, span)
    tenkan, kijun = _last(ich["tenkan"]), _last(ich["kijun"])
    a_, b_ = _last(ich["span_a"]), _last(ich["span_b"])
    c = _last(df["close"])
    past = _last(df["close"].shift(base))
    votes = [int(_last(ich["above_cloud"]) or 0), _sign(tenkan - kijun), _sign(c - past)]
    total = sum(votes)
    d = _sign(total) if abs(total) >= 2 else 0
    a = _atr(df)
    top, bottom = max(a_, b_), min(a_, b_)
    return _result("ichimoku", {"tenkan": tenkan, "kijun": kijun, "span_a": a_, "span_b": b_,
                                "chikou_change": c / past - 1 if past else math.nan,
                                "cloud_dist_atr": ((c - top) if c > top else (c - bottom) if c < bottom else 0.0) / a
                                if a else math.nan},
                   d, abs(total) / 3, _valid(df, warm, tenkan, kijun, a_, b_, past), warm,
                   votes={"cloud": votes[0], "tenkan_kijun": votes[1], "chikou": votes[2]},
                   in_cloud=bool(bottom <= c <= top) if _finite(top, bottom) else None)


def supertrend_result(df: pd.DataFrame, n: int = 10, mult: float = 3.0) -> IndicatorResult:
    """Supertrend's own direction; confidence is the distance from the line in ATRs (2 = 1).
    The first bars after warm-up start from an arbitrary +1, so warm-up is 3n."""
    warm = 3 * n
    st = core.supertrend(df, n, mult)
    line, d = _last(st["supertrend"]), int(_last(st["direction"]) or 0)
    a, c = _atr(df, n), _last(df["close"])
    dist = abs(c - line) / a if a else math.nan
    flipped = len(st) > 1 and int(st["direction"].iloc[-2]) != d
    return _result("supertrend", {"line": line, "dist_atr": dist, "direction": float(d)}, d, dist / 2,
                   _valid(df, warm, line, dist), warm, flipped=bool(flipped), mult=mult)


# ---- momentum -----------------------------------------------------------------------------------


def rsi_result(df: pd.DataFrame, n: int = 14, low: float = 30, high: float = 70) -> IndicatorResult:
    """Momentum: above 55 is +1, below 45 is -1. Overbought/oversold is only a zone."""
    warm = 3 * n
    v = _last(core.rsi(df["close"], n))
    norm = (v - 50) / 50
    return _result("rsi", {"rsi": v, "rsi_norm": norm}, _sign(v - 50, 5), abs(norm),
                   _valid(df, warm, v), warm, zone=_osc_zone(v, low, high), oversold=low, overbought=high)


def macd_result(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> IndicatorResult:
    """Direction when the MACD line and the histogram agree (above zero and still pulling away
    from the signal line: +1; the mirror: -1); 0 when they disagree. Line and histogram are
    scaled by ATR. Confidence: a histogram of half an ATR = 1."""
    warm = 3 * slow + signal
    m = core.macd(df["close"], fast, slow, signal)
    line, sig, hist = _last(m["macd"]), _last(m["signal"]), _last(m["hist"])
    a = _atr(df)
    prev = _last(m["hist"].shift(1))
    return _result("macd", {"macd": line, "signal": sig, "hist": hist,
                            "macd_atr": line / a if a else math.nan, "hist_atr": hist / a if a else math.nan},
                   _sign(hist) if _sign(hist) == _sign(line) else 0, abs(hist) / (0.5 * a) if a else 0.0, _valid(df, warm, line, sig, hist), warm,
                   cross=("up" if prev <= 0 < hist else "down" if prev >= 0 > hist else None)
                   if _finite(prev, hist) else None, hist_rising=bool(hist > prev) if _finite(prev, hist) else None)


def stoch_rsi_result(df: pd.DataFrame, n: int = 14, k: int = 3, d: int = 3) -> IndicatorResult:
    """%K above %D is +1, below -1; confidence from the gap (0.2 = 1). Above 0.8 / below 0.2 are
    zones."""
    warm = 4 * n + k + d
    s = core.stoch_rsi(df["close"], n, k, d)
    kv, dv = _last(s["k"]), _last(s["d"])
    pk, pd_ = _last(s["k"].shift(1)), _last(s["d"].shift(1))
    cross = None
    if _finite(kv, dv, pk, pd_):
        cross = "up" if pk <= pd_ and kv > dv else "down" if pk >= pd_ and kv < dv else None
    return _result("stoch_rsi", {"k": kv, "d": dv, "k_norm": 2 * kv - 1, "k_minus_d": kv - dv},
                   _sign(kv - dv, 0.01), abs(kv - dv) / 0.2, _valid(df, warm, kv, dv), warm,
                   zone=_osc_zone(kv, 0.2, 0.8), cross=cross)


def mfi_result(df: pd.DataFrame, n: int = 14, low: float = 20, high: float = 80) -> IndicatorResult:
    """Volume-weighted RSI: above 55 is +1, below 45 -1; 80/20 are zones."""
    warm = n + 1
    v = _last(core.mfi(df, n))
    norm = (v - 50) / 50
    return _result("mfi", {"mfi": v, "mfi_norm": norm}, _sign(v - 50, 5), abs(norm),
                   _valid(df, warm, v), warm, zone=_osc_zone(v, low, high), oversold=low, overbought=high)


def williams_r_result(df: pd.DataFrame, n: int = 14) -> IndicatorResult:
    """Williams %R (-100..0): above -40 is +1, below -60 -1; -20 / -80 are zones."""
    warm = n
    v = _last(core.williams_r(df, n))
    norm = (v + 50) / 50
    return _result("williams_r", {"williams_r": v, "wr_norm": norm}, _sign(v + 50, 10), abs(norm),
                   _valid(df, warm, v), warm, zone=_osc_zone(v, -80, -20))


# ---- volatility ---------------------------------------------------------------------------------


def bollinger_result(df: pd.DataFrame, n: int = 20, k: float = 2.0, squeeze_pct: float = 0.2,
                     lookback: int = 120) -> IndicatorResult:
    """%B and bandwidth. A close outside a band gives that side's direction (a breakout, not a
    reversal call); confidence from how far %B is from the middle. ``squeeze`` when bandwidth is
    in the lowest ``squeeze_pct`` of the last ``lookback`` bars."""
    warm = n + lookback
    b = core.bollinger(df["close"], n, k)
    up, lo, mid, width = _last(b["upper"]), _last(b["lower"]), _last(b["mid"]), _last(b["width"])
    c = _last(df["close"])
    pctb = (c - lo) / (up - lo) if up != lo else math.nan
    rank = _last(b["width"].rolling(lookback, min_periods=lookback).rank(pct=True))
    d = 1 if pctb > 1 else -1 if pctb < 0 else 0
    return _result("bollinger", {"upper": up, "lower": lo, "mid": mid, "pct_b": pctb, "bandwidth": width,
                                 "bandwidth_rank": rank},
                   d, abs(pctb - 0.5) * 2 if math.isfinite(pctb) else 0.0, _valid(df, n, up, lo, pctb), n,
                   squeeze=bool(rank <= squeeze_pct) if math.isfinite(rank) else None, k=k,
                   squeeze_ready=len(df) >= warm)


def atr_result(df: pd.DataFrame, n: int = 14, lookback: int = 500) -> IndicatorResult:
    """Volatility only (direction is always 0): ATR in percent of price and its percentile over
    ``lookback`` bars. Confidence: how extreme the percentile is."""
    warm = 3 * n
    a, c = _atr(df, n), _last(df["close"])
    pct = _last(core.atr_percentile(df, n, lookback))
    regime = ("unknown" if not math.isfinite(pct) else "high" if pct >= 0.8 else "low" if pct <= 0.2
              else "normal")
    return _result("atr", {"atr": a, "atr_pct": a / c if c else math.nan, "atr_percentile": pct}, 0,
                   abs(pct - 0.5) * 2 if math.isfinite(pct) else 0.0, _valid(df, warm, a), warm,
                   regime=regime)


def donchian_result(df: pd.DataFrame, n: int = 20) -> IndicatorResult:
    """A close above the previous ``n`` bars' high is +1, below their low -1. Confidence: how far
    past the band in ATRs (1 ATR = 1); ``position`` is where the close sits in the channel."""
    warm = n + 1
    ch = core.donchian(df, n)
    up, lo, c, a = _last(ch["upper"]), _last(ch["lower"]), _last(df["close"]), _atr(df)
    d = 1 if c > up else -1 if c < lo else 0
    beyond = (c - up if d > 0 else lo - c if d < 0 else 0.0) / a if a else math.nan
    return _result("donchian", {"upper": up, "lower": lo, "mid": _last(ch["mid"]),
                                "position": (c - lo) / (up - lo) if up != lo else math.nan,
                                "width_atr": (up - lo) / a if a else math.nan, "beyond_atr": beyond},
                   d, beyond, _valid(df, warm, up, lo), warm, period=n)


def keltner_result(df: pd.DataFrame, n: int = 20, mult: float = 2.0, atr_n: int = 10) -> IndicatorResult:
    """ATR envelope around an EMA: outside a band is that side's direction; ``position`` is
    -1 at the lower band, +1 at the upper."""
    warm = 3 * n
    k = core.keltner(df, n, mult, atr_n)
    up, lo, mid, c = _last(k["upper"]), _last(k["lower"]), _last(k["mid"]), _last(df["close"])
    pos = (c - mid) / (up - mid) if up != mid else math.nan
    d = 1 if c > up else -1 if c < lo else 0
    return _result("keltner", {"upper": up, "lower": lo, "mid": mid, "position": pos}, d,
                   abs(pos) if d else abs(pos) / 2, _valid(df, warm, up, lo, pos), warm, mult=mult)


# ---- volume -------------------------------------------------------------------------------------


def vwap_result(df: pd.DataFrame, anchor: str | None = "D", n: int = 20, min_bars: int = 3) -> IndicatorResult:
    """Distance from the (daily, UTC) VWAP in standard deviations: above is +1, below -1;
    confidence: 2 sd = 1. Valid once ``min_bars`` bars of the session are in."""
    vb = core.vwap_bands(df, n, anchor)
    v, sd, c = _last(vb["vwap"]), _last(vb["sd"]), _last(df["close"])
    bars = int(_last(vb["bars"]) or 0)
    z = (c - v) / sd if sd else math.nan
    warm = min_bars if anchor else n
    valid = _finite(v) and bars >= warm and _valid(df, min(bars, len(df)) or 1, v)
    return _result("vwap", {"vwap": v, "z": z, "dist_pct": (c - v) / v if v else math.nan},
                   _sign(c - v) if math.isfinite(v) else 0, abs(z) / 2 if math.isfinite(z) else 0.0, valid, warm,
                   anchor=anchor, bars=bars, **{k: _last(vb[k]) for k in ("upper1", "lower1", "upper2", "lower2")})


def obv_result(df: pd.DataFrame, n: int = 20) -> IndicatorResult:
    """OBV against its own ``n``-bar EMA, in standard deviations of its changes: rising flow +1,
    falling -1 (half a sd dead zone); confidence: 2 sd = 1."""
    warm = 3 * n
    o = core.obv(df)
    base = core.ema(o, n)
    sd = _last(o.diff().rolling(n, min_periods=n).std())
    z = (_last(o) - _last(base)) / sd if sd else math.nan
    return _result("obv", {"obv": _last(o), "obv_z": z}, _sign(z, 0.5), abs(z) / 2, _valid(df, warm, z), warm)


def cmf_result(df: pd.DataFrame, n: int = 20, dead: float = 0.05) -> IndicatorResult:
    """Chaikin Money Flow: buying pressure above +0.05 is +1, selling below -0.05 -1;
    confidence: 0.25 = 1."""
    v = _last(core.cmf(df, n))
    return _result("cmf", {"cmf": v}, _sign(v, dead), abs(v) / 0.25, _valid(df, n, v), n, dead_zone=dead)


def volume_profile_result(df: pd.DataFrame, lookback: int = 100, bins: int = 24) -> IndicatorResult:
    """Point of control and 70% value area of the last ``lookback`` bars (trailing, so nothing
    later leaks in). A close above the value area high is +1, below the low -1, inside 0.
    Confidence: distance outside in ATRs (1 = 1)."""
    win = df.iloc[-lookback:]
    va = core.value_area(win, bins) if len(win) else {"poc": math.nan, "vah": math.nan, "val": math.nan}
    c, a = _last(df["close"]), _atr(df)
    d = 1 if c > va["vah"] else -1 if c < va["val"] else 0
    out = (c - va["vah"] if d > 0 else va["val"] - c if d < 0 else 0.0) / a if a else math.nan
    return _result("volume_profile", {"poc": va["poc"], "vah": va["vah"], "val": va["val"],
                                      "poc_dist_atr": (c - va["poc"]) / a if a else math.nan},
                   d, out, _valid(df, lookback, va["poc"], va["vah"], va["val"]), lookback,
                   inside_value=bool(d == 0), bins=bins)


def fibonacci_result(df: pd.DataFrame, lookback: int = 100, near: float = 0.03) -> IndicatorResult:
    """Retracement levels of the last ``lookback`` bars' swing (highest high and lowest low;
    the later of the two sets the leg's direction). Direction is the leg's; confidence is how
    close the close is to a level (within ``near`` of the swing = 1 at the level)."""
    win = df.iloc[-lookback:]
    if not len(win):
        return _result("fibonacci", {}, 0, 0.0, False, lookback)
    hi_i, lo_i = int(np.argmax(win["high"].to_numpy())), int(np.argmin(win["low"].to_numpy()))
    hi, lo = float(win["high"].iloc[hi_i]), float(win["low"].iloc[lo_i])
    up = hi_i > lo_i  # low first, then high: an up leg, pullbacks are measured down from the high
    span = hi - lo
    levels = core.fib_levels(lo, hi) if up else {r: lo + r * span for r in core.FIB_RATIOS}
    c = _last(df["close"])
    depth = ((hi - c) / span if up else (c - lo) / span) if span else math.nan
    nearest = min(levels, key=lambda r: abs(levels[r] - c)) if span else None
    gap = abs(levels[nearest] - c) / span if nearest is not None else math.nan
    return _result("fibonacci", {"swing_high": hi, "swing_low": lo, "retracement": depth,
                                 **{f"level_{r}": p for r, p in levels.items()}},
                   1 if up else -1, 1 - gap / near if math.isfinite(gap) else 0.0,
                   _valid(df, lookback, hi, lo, depth) and span > 0, lookback,
                   leg="up" if up else "down", nearest_level=nearest)


# ---- derivatives --------------------------------------------------------------------------------


def funding_result(df: pd.DataFrame | None = None, rate: float | None = None, hours: float = 8.0,
                   lookback: int = 90, extreme: float = EXTREME_FUNDING) -> IndicatorResult:
    """Perpetual funding: the current rate (``rate``, or the last ``funding_rate`` in ``df``),
    its yearly equivalent and its z-score over the last ``lookback`` readings. Contrarian bias:
    funding at or above ``extreme`` (crowded longs) is -1, at or below -``extreme`` +1; anything
    in between is 0. Confidence: |rate| / (2 x extreme)."""
    hist = pd.Series(dtype=float)
    if df is not None and "funding_rate" in df:
        hist = df["funding_rate"].dropna()
        hist = hist[hist.ne(hist.shift())]  # one value per funding period, not per candle
    r = float(rate) if rate is not None else _last(hist)
    sd = float(hist.iloc[-lookback:].std()) if len(hist) >= 10 else math.nan
    z = (r - float(hist.iloc[-lookback:].mean())) / sd if sd else math.nan
    d = -1 if r >= extreme else 1 if r <= -extreme else 0
    return _result("funding", {"rate": r, "annualized": r * (24 / hours) * 365 if math.isfinite(r) else math.nan,
                               "z": z},
                   d, abs(r) / (2 * extreme), _finite(r), 1, extreme=extreme,
                   bias="crowded longs" if d < 0 else "crowded shorts" if d > 0 else "normal")


INDICATORS: dict[str, Callable[..., IndicatorResult]] = {
    "ema": ema_result, "sma": sma_result, "vwap": vwap_result, "rsi": rsi_result, "macd": macd_result,
    "stoch_rsi": stoch_rsi_result, "adx": adx_result, "bollinger": bollinger_result, "atr": atr_result,
    "donchian": donchian_result, "ichimoku": ichimoku_result, "supertrend": supertrend_result,
    "obv": obv_result, "mfi": mfi_result, "cmf": cmf_result, "volume_profile": volume_profile_result,
    "fibonacci": fibonacci_result, "keltner": keltner_result, "williams_r": williams_r_result,
    "funding": funding_result,
}


def evaluate_all(df: pd.DataFrame, funding_rate: float | None = None) -> dict[str, IndicatorResult]:
    """All 20 standard indicators on the last closed bar of ``df``."""
    out = {name: fn(df) for name, fn in INDICATORS.items() if name != "funding"}
    out["funding"] = funding_result(df, funding_rate)
    return out
