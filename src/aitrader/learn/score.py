"""A signal's quality score, 0-100, from the NEXUS prompt's starting weights.

Each part is a number from 0 to 1 read from completed candles of the signal's own chart (and the
higher-timeframe trends), for the side the signal trades; the score is their weighted sum. The
weights are starting values, not proven ones. An engine's "Min signal score" (0 = off) skips signals
below it; every other check still applies, so a high score alone never opens a trade.
"""

from __future__ import annotations

import math

import pandas as pd

from ..indicators import atr, ema, macd, rsi, sma

WEIGHTS = {"trend": 25, "structure": 20, "momentum": 15, "volume": 15, "volatility": 10, "liquidity": 10,
           "confirmation": 5}
LABELS = {"trend": "regime and higher-timeframe trend", "structure": "price structure", "momentum": "momentum",
          "volume": "volume", "volatility": "volatility", "liquidity": "liquidity",
          "confirmation": "other strategies agreeing"}
ATR_PCT_OK = (0.3, 5.0)  # an ATR (% of price) in this band suits a trade; outside it is too quiet or too wild
TURNOVER_LOW, TURNOVER_HIGH = 10_000.0, 1_000_000.0  # USDT traded per candle: thin ... deep


def _clip(x: float) -> float:
    return 0.0 if x != x else max(0.0, min(1.0, x))


def parts(df: pd.DataFrame, short: bool, higher: dict | None = None) -> dict:
    """Every part but ``confirmation`` (added when the pass knows how many strategies agree)."""
    close = df["close"]
    c = float(close.iloc[-1])
    sign = -1.0 if short else 1.0
    out = {}
    # regime and trend: EMA 50 against EMA 200 on this chart, and the higher timeframes' trends
    e50, e200 = float(ema(close, 50).iloc[-1]), float(ema(close, 200).iloc[-1])
    chart = 0.5 if e200 != e200 else (1.0 if sign * (e50 - e200) > 0 else 0.0)
    want = "down" if short else "up"
    htf = [1.0 if t == want else 0.5 if t == "flat" else 0.0 for t in (higher or {}).values()]
    out["trend"] = (chart + (sum(htf) / len(htf) if htf else 0.5)) / 2
    # structure: on the right side of EMA 21 and of the middle of the last 20 candles' range
    e21 = float(ema(close, 21).iloc[-1])
    hi, lo = float(df["high"].iloc[-20:].max()), float(df["low"].iloc[-20:].min())
    out["structure"] = ((1.0 if sign * (c - e21) > 0 else 0.0) + (1.0 if sign * (c - (hi + lo) / 2) > 0 else 0.0)) / 2
    # momentum: MACD histogram on the trade's side, RSI on its side of 50 but not stretched
    hist = float(macd(close)["hist"].iloc[-1])
    r = float(rsi(close).iloc[-1])
    r_ok = (50 <= r <= 70) if not short else (30 <= r <= 50)
    out["momentum"] = ((1.0 if sign * hist > 0 else 0.0) + (1.0 if r_ok else 0.0)) / 2
    # volume: this candle against the 20 before it (1.5x or more is full marks)
    avg = float(sma(df["volume"], 20).iloc[-2]) if len(df) > 21 else float("nan")
    out["volume"] = _clip((float(df["volume"].iloc[-1]) / avg - 0.5) / 1.0) if avg and avg == avg else 0.5
    # volatility: ATR as a share of price inside a tradeable band
    a = float(atr(df).iloc[-1]) / c * 100 if c else float("nan")
    out["volatility"] = 1.0 if ATR_PCT_OK[0] <= a <= ATR_PCT_OK[1] else 0.3 if a == a else 0.5
    # liquidity: USDT traded per candle, on a log scale from thin to deep
    turnover = float((df["close"] * df["volume"]).iloc[-20:].mean())
    out["liquidity"] = _clip(math.log10(max(turnover, 1.0) / TURNOVER_LOW) / math.log10(TURNOVER_HIGH / TURNOVER_LOW))
    return {k: round(_clip(v), 3) for k, v in out.items()}


def total(p: dict, agreeing: int = 1) -> float:
    """The 0-100 score, with ``agreeing`` strategies signalling the same side on the coin this pass:
    the more of them, the more of the confirmation points (2 a third, 3 two thirds, 4 or more all)."""
    full = {**p, "confirmation": min(1.0, max(0, agreeing - 1) / 3)}
    return round(sum(WEIGHTS[k] * full.get(k, 0.0) for k in WEIGHTS), 1)


def weakest(p: dict, n: int = 2) -> str:
    """The parts that cost the most points, for the skip reason."""
    lost = sorted(((WEIGHTS[k] * (1 - p.get(k, 0.0)), k) for k in WEIGHTS if k != "confirmation"), reverse=True)
    return ", ".join(f"{LABELS[k]} {WEIGHTS[k] * p.get(k, 0.0):.0f}/{WEIGHTS[k]}" for _x, k in lost[:n])
