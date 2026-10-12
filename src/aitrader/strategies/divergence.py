"""David Carli, "The Power of Divergence" (5th ed., 2020): CCI divergence plus trendline break.

The book's one strategy, coded as written where it is exact (pp. 37-49, 62):
- Bullish divergence: price makes a lower low (A then C) while CCI(20) makes a higher low.
- Rule 1: both CCI lows in the oversold zone below -150 (the second may sit slightly outside).
- Rule 2: CCI stays below zero between the two lows (a shallow poke is tolerated).
- Rule 3: with B the rebound high between the lows, (B - C) / (B - A) is from 1.13 to 1.618.
- The divergence is not a signal on its own: enter only when price closes above the falling
  trendline over the highs into C. No trade after a gap up.
- Stop a little below C. Target the 127.2% extension C + 1.272 x (B - C); reward must be at
  least 1:1. (The book takes half off there and runs the rest to 161.8%; one target is used here.)
- Daily charts, sometimes 4h, never lower. 0.5% risk per trade.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import atr, cci
from ..models import TIMEFRAME_MS, Side
from .base import Signal, Strategy, StrategySpec


class CciDivergence(Strategy):
    spec = StrategySpec(
        name="cci_divergence",
        source='David Carli, "The Power of Divergence" pp. 37-49, 62',
        timeframes=("1d", "4h"),
        setup="Bullish CCI(20) divergence: swing low C below swing low A, CCI higher at C; both "
        "CCI lows under -150 (C may be up to 20 points outside); CCI never above +25 between them; "
        "(B - C)/(B - A) from 1.13 to 1.618, B = highest high between the lows; lows 5-60 bars apart.",
        entry="Within 20 bars of C being confirmed, the first close above the falling trendline "
        "through the swing highs into C (B and the last lower swing high after it, or the high of "
        "bar C). Not on a gap-up bar. Market order at the next open.",
        stop="Low of C - 0.2 x ATR(14) (the book: 15-20 pips under the low).",
        target="C + 1.272 x (B - C); skipped if that is less than 1R away.",
        filter="Void if price trades below C before the break.",
        size="0.5% of equity at risk.",
        decisions=(
            "Swing points are fractals with 3 bars each side (the book doesn't define them).",
            "Lows 5-60 bars apart; the signal must come within 20 bars of C (not in the book).",
            "'Slightly outside the zone' = second CCI low below -130; 'shallow poke' over zero = CCI up to +25.",
            "Only the cautious close-above-trendline entry is coded; the aggressive intrabar entry needs tick data.",
            "One target at 127.2% instead of half there and half at 161.8% (no partial exits yet).",
            "Long side only; the bearish divergence short is off in the pilot.",
        ),
    )
    eligible_regimes = ("trend-up", "range", "uncertain")

    @classmethod
    def default_params(cls):
        return {"cci_n": 20, "zone": -150.0, "zone_tol": 20.0, "zero_cap": 25.0, "pivot": 3,
                "min_sep": 5, "max_sep": 60, "fib_lo": 1.13, "fib_hi": 1.618, "trigger_bars": 20,
                "target_ext": 1.272, "stop_atr": 0.2, "min_rr": 1.0, "target_r": None,
                "risk_pct": 0.005}

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        hi, lo, cl = (df[k].to_numpy(float) for k in ("high", "low", "close"))
        c = cci(df, p["cci_n"]).to_numpy(float)
        a = atr(df).to_numpy(float)
        n, k = len(df), p["pivot"]
        swing_lows: list[int] = []
        swing_highs: list[int] = []
        setup = None  # dict for the active divergence
        out = []
        for t in range(2 * k, n):
            j = t - k  # pivot candidate confirmed on bar t
            win = slice(j - k, t + 1)
            if hi[j] == hi[win].max():
                swing_highs.append(j)
            if lo[j] == lo[win].min():
                setup = self._divergence(j, t, swing_lows, hi, lo, c) or setup
                swing_lows.append(j)
            if setup is None:
                continue
            if t - setup["confirmed"] > p["trigger_bars"] or lo[t] < lo[setup["c"]]:
                setup = None
                continue
            line = self._trendline(setup, swing_highs, hi, t)
            if not (cl[t] > line and (t == 0 or cl[t - 1] <= self._trendline(setup, swing_highs, hi, t - 1)
                                      or t == setup["confirmed"])):
                continue
            if lo[t] > hi[t - 1]:  # gap up: the book says don't buy here
                continue
            entry = cl[t]
            stop = lo[setup["c"]] - p["stop_atr"] * a[t]
            target = lo[setup["c"]] + p["target_ext"] * (hi[setup["b"]] - lo[setup["c"]])
            if not np.isfinite(stop) or stop >= entry or (target - entry) < p["min_rr"] * (entry - stop):
                setup = None
                continue
            out.append(Signal(
                ts=int(df["ts"].iloc[t]) + TIMEFRAME_MS[timeframe], strategy=self.spec.name,
                exchange=exchange, symbol=symbol, timeframe=timeframe, side=Side.BUY,
                entry=float(entry), stop=float(stop), target=float(target), risk_pct=p["risk_pct"],
                reason="bullish CCI divergence, close above the trendline",
                features={"cci_a": float(c[setup["a"]]), "cci_c": float(c[setup["c"]]),
                          "fib": float(setup["fib"])},
            ))
            setup = None
        return self.regime_filter(df, timeframe, out)

    def _divergence(self, cidx, confirmed, swing_lows, hi, lo, c):
        p = self.params
        if not swing_lows or np.isnan(c[cidx]):
            return None
        aidx = swing_lows[-1]
        sep = cidx - aidx
        if not p["min_sep"] <= sep <= p["max_sep"] or np.isnan(c[aidx]):
            return None
        if not (lo[cidx] < lo[aidx] and c[cidx] > c[aidx]):
            return None
        if not (c[aidx] < p["zone"] and c[cidx] < p["zone"] + p["zone_tol"]):
            return None
        between = c[aidx:cidx + 1]
        if np.nanmax(between) > p["zero_cap"]:
            return None
        bidx = aidx + int(np.argmax(hi[aidx:cidx + 1]))
        ab = hi[bidx] - lo[aidx]
        if ab <= 0:
            return None
        fib = (hi[bidx] - lo[cidx]) / ab
        if not p["fib_lo"] <= fib <= p["fib_hi"]:
            return None
        return {"a": aidx, "b": bidx, "c": cidx, "confirmed": confirmed, "fib": fib}

    @staticmethod
    def _trendline(setup, swing_highs, hi, t) -> float:
        """Falling line from B through the last lower swing high known by bar t (or bar C's high)."""
        b = setup["b"]
        later = [h for h in swing_highs if b < h < t and hi[h] < hi[b]]
        x2 = later[-1] if later else setup["c"]
        if x2 == b:
            return hi[b]
        slope = (hi[x2] - hi[b]) / (x2 - b)
        return hi[b] + slope * (t - b)
