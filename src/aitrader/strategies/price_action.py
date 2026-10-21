"""The four book strategies the rule language couldn't express, coded by hand.

From "How to Trade with Price Action" (Master Edition and Strategies). Their library entries
(``learn/library``) are marked ``coded_as`` the names below, so they load as built-in strategies
instead of being skipped:

- ``price_channel_two_bars_above``: the moving-average channel of bar highs and bar lows;
- ``pin_bar_reentry_long``: the pin bar trade, its stop-out, then the re-entry, in that order;
- ``m2b_two_legged_pullback_ema20``: Al Brooks' M2B, with the down-legs counted;
- ``high_close_doji_pivot_support``: a doji at the prior day's floor-pivot support (S1 or S2).

Like every strategy they are research candidates: each must clear the backtest gate before it
paper trades, and its short twin (``strategies.mirror``) trades the bearish version.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import adx, atr, directional_index, ema, sma
from .base import StrategySpec
from .families import BASE, NO_TRADE, _exit_rule, _Family, _tradeable


def _book(edition: str, pages: str) -> str:
    return f'"How to Trade with Price Action: {edition}" {pages}'


def pin_bars(df: pd.DataFrame, lookback: int = 10) -> pd.Series:
    """Bullish pin bars at a local low: the lower tail is at least 2/3 of the bar's range, the
    body sits in the top third, and the low undercuts the previous ``lookback`` bars' lows."""
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    body_low = df[["open", "close"]].min(axis=1)
    tail = body_low - df["low"]
    at_low = df["low"] < df["low"].shift(1).rolling(lookback).min()
    return ((tail >= 2 / 3 * rng) & at_low).fillna(False)


def floor_pivots(df: pd.DataFrame) -> pd.DataFrame:
    """The prior UTC day's floor pivots on every bar: PP = (H + L + C) / 3, S1 = 2PP - H,
    S2 = PP - (H - L), R1 = 2PP - L. NaN on the first day (no prior day) and after a day with
    no candles."""
    day = (df["ts"] // 86_400_000).astype("int64")
    daily = df.groupby(day).agg(high=("high", "max"), low=("low", "min"), close=("close", "last"))
    prev = daily.reindex(day - 1).set_index(df.index)
    pp = (prev["high"] + prev["low"] + prev["close"]) / 3
    return pd.DataFrame({"pp": pp, "s1": 2 * pp - prev["high"], "s2": pp - (prev["high"] - prev["low"]),
                         "r1": 2 * pp - prev["low"]}, index=df.index)


class PriceChannelTwoBars(_Family):
    spec = StrategySpec(
        name="price_channel_two_bars_above",
        source=_book("Master Edition", "pp. 42-43") + ": Bernstein's moving-average channel",
        timeframes=("5m", "15m", "1h"),
        regime="Any: two bars above the channel define the bull trend.",
        setup="The channel is SMA(20) of bar highs over SMA(20) of bar lows. This bar and the one before "
        "sit completely above it (low above the upper line), and the bar before them did not.",
        entry="Market order at the next bar's open.",
        stop="Below the last 3 bars' low.",
        target="4R fixed; exits earlier when a bar sits completely below the channel (high under the lower line).",
        filter=NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("The book gives no period; 20 like its other tools.",
                   "The book exits on the opposite signal only; a 4R target is added so every trade has one.",
                   "One bar below the channel exits (the book's bear trend needs two), for a faster exit."),
    )
    eligible_regimes = ("trend-up", "range", "uncertain")

    @classmethod
    def default_params(cls):
        return {**BASE, "n": 20, "target_r": 4.0}

    def generate(self, df, exchange, symbol, timeframe):
        p, n = self.params, int(self.params["n"])
        upper = sma(df["high"], n)
        above = df["low"] > upper
        cond = above & above.shift(1, fill_value=False) & ~above.shift(2, fill_value=True) & _tradeable(df, timeframe, p)
        stops = df["low"].rolling(3).min()
        key = _exit_rule(f"bar_below_low_channel:{n}", lambda d: d["high"] < sma(d["low"], n))
        return self._signals(df, cond, stops, self._fixed_r(), exchange, symbol, timeframe, key,
                             "two bars completely above the high/low channel")


class PinBarReentry(_Family):
    spec = StrategySpec(
        name="pin_bar_reentry_long",
        source=_book("Master Edition", "pp. 65-70") + ": the failed pin bar re-entry",
        timeframes=("15m", "30m", "1h"),
        regime="Any, but the re-entry must be above EMA(20).",
        setup="A bullish pin bar forms at a 10-bar low. Its buyers enter on a close above the pin's high "
        "(within 10 bars), then get stopped out: price trades below the pin's low (within 15 bars), but by "
        "no more than 1 ATR(14).",
        entry="Within 12 bars of that stop-out, the first close above the high of an up bar while above "
        "EMA(20). Market order at the next bar's open.",
        stop="Below the last 6 bars' low (the re-entry swing low).",
        target="2R fixed; exits earlier on a close below EMA(20); time stop after 30 bars.",
        filter="Void if price falls more than 1 ATR(14) under the pin's low. " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("Pin bar = lower tail at least 2/3 of the range, body in the top third, at a 10-bar low.",
                   "Waiting windows (10, 15 and 12 bars) and the 1 ATR undercut limit are ours; the book says "
                   "'not too far below' the pin low.",
                   "The book gives no stop or target for the re-entry: 6-bar low, 2R, 30-bar time stop."),
    )
    eligible_regimes = ("trend-up", "range", "uncertain")

    @classmethod
    def default_params(cls):
        return {**BASE, "pin_lookback": 10, "trigger_bars": 10, "stopout_bars": 15, "reentry_bars": 12,
                "max_undercut_atr": 1.0, "stop_bars": 6, "target_r": 2.0, "time_stop": 30}

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        o, h, lo, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        a = atr(df).to_numpy(float)
        e20 = ema(df["close"], 20).to_numpy(float)
        pins = pin_bars(df, int(p["pin_lookback"])).to_numpy(bool)
        cond = np.zeros(len(df), bool)
        pin = None  # {"hi", "lo", "stage", "since"}
        for t in range(1, len(df)):
            if pin is not None:
                waited = t - pin["since"]
                if pin["stage"] == 2 and lo[t] < pin["lo"] - p["max_undercut_atr"] * a[pin["out"]]:
                    pin = None  # fell too far: not a stop hunt any more
                elif pin["stage"] == 0:
                    if c[t] > pin["hi"]:
                        pin.update(stage=1, since=t)
                    elif waited >= p["trigger_bars"]:
                        pin = None
                elif pin["stage"] == 1:
                    if lo[t] < pin["lo"]:
                        if lo[t] < pin["lo"] - p["max_undercut_atr"] * a[t]:
                            pin = None
                        else:
                            pin.update(stage=2, since=t, out=t)
                    elif waited >= p["stopout_bars"]:
                        pin = None
                elif pin["stage"] == 2:
                    if c[t - 1] > o[t - 1] and c[t] > h[t - 1] and c[t] > e20[t]:
                        cond[t] = True
                        pin = None
                    elif waited >= p["reentry_bars"]:
                        pin = None
            if pins[t] and (pin is None or pin["stage"] == 0):
                pin = {"hi": h[t], "lo": lo[t], "stage": 0, "since": t}
        cond = pd.Series(cond, index=df.index) & _tradeable(df, timeframe, p)
        stops = df["low"].rolling(int(p["stop_bars"])).min()
        return self._signals(df, cond, stops, self._fixed_r(), exchange, symbol, timeframe,
                             f"close<ema20|time_stop={p['time_stop']}",
                             "re-entry after the pin bar's buyers were stopped out")


class M2bTwoLeggedPullback(_Family):
    spec = StrategySpec(
        name="m2b_two_legged_pullback_ema20",
        source=_book("Strategies", "pp. 39-42") + ": Al Brooks' M2B",
        timeframes=("5m", "15m", "1h"),
        regime="Strong uptrend: ADX(14) > 25, +DI above -DI and EMA(20) rising over 10 bars.",
        setup="Since the highest high of the last 20 bars the pullback has made two down-legs (a lower low "
        "starts a down-leg; a higher high in between starts an up-leg). The last bar of the second leg "
        "touched EMA(20) and closed above it.",
        entry="This bar closes above that test bar's high. Market order at the next bar's open.",
        stop="Below the test bar's low.",
        target="2R fixed; time stop after 36 bars.",
        filter=NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("The book buys on a stop one tick above the test bar; here a close above its high.",
                   "Stop and target are not in the book: the test bar's low, 2R."),
    )
    eligible_regimes = ("trend-up", "uncertain")

    @classmethod
    def default_params(cls):
        return {**BASE, "adx_min": 25.0, "swing_bars": 20, "target_r": 2.0, "time_stop": 36}

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        h, lo, c = (df[k].to_numpy(float) for k in ("high", "low", "close"))
        e20 = ema(df["close"], 20)
        di = directional_index(df)
        trend = ((adx(df) > p["adx_min"]) & (di["plus_di"] > di["minus_di"]) & (e20 > e20.shift(10))).to_numpy(bool)
        e = e20.to_numpy(float)
        n, w = len(df), int(p["swing_bars"])
        cond = np.zeros(n, bool)
        for t in range(w + 1, n):
            k = t - 1  # the bar that tested EMA(20)
            if not (trend[k] and lo[k] <= e[k] < c[k] and c[t] > h[k]):
                continue
            top = k - w + int(np.argmax(h[k - w:k]))
            if h[k] >= h[top]:
                continue
            legs, down = 0, False
            for j in range(top + 1, k + 1):
                if lo[j] < lo[j - 1] and not down:
                    legs, down = legs + 1, True
                elif h[j] > h[j - 1] and down:
                    down = False
            cond[t] = legs == 2 and down
        cond = pd.Series(cond, index=df.index) & _tradeable(df, timeframe, p)
        stops = df["low"].shift(1)
        return self._signals(df, cond, stops, self._fixed_r(), exchange, symbol, timeframe,
                             f"time_stop={p['time_stop']}", "bought the end of a two-legged pullback to EMA(20)")


class HighCloseDojiPivot(_Family):
    spec = StrategySpec(
        name="high_close_doji_pivot_support",
        source=_book("Strategies", "pp. 43-47") + ": John Person's high-close doji",
        timeframes=("5m", "15m"),
        regime="Any: the pivot level is the setup.",
        setup="The previous bar is a doji (body at most 10% of its range) whose low touched the prior UTC "
        "day's floor-pivot support S1 or S2 (within 0.2%) without closing below it.",
        entry="This bar closes above the doji's high (the high-close doji). Market order at the next open.",
        stop="Below the doji's and the bar before it's low.",
        target="The next pivot level up (S2 to S1, S1 to PP); skipped if that is less than 1R away. Time stop "
        "after 48 bars.",
        filter=NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("Pivots from the prior UTC day (Bybit's daily candle), PP = (H + L + C) / 3, "
                   "S1 = 2PP - H, S2 = PP - (H - L).",
                   "'At the level' = the doji's low within 0.2% of it and its close above 0.2% under it.",
                   "The book's quality filters (several dojis at the level, a failed low-close doji) are left out."),
    )
    eligible_regimes = ("trend-up", "range", "uncertain")

    @classmethod
    def default_params(cls):
        return {**BASE, "tol": 0.002, "doji_body": 0.1, "min_rr": 1.0, "time_stop": 48}

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        rng = (df["high"] - df["low"]).replace(0, np.nan)
        doji = ((df["close"] - df["open"]).abs() <= p["doji_body"] * rng).fillna(False)
        piv = floor_pivots(df)

        def touched(level: pd.Series) -> pd.Series:
            return (df["low"] <= level * (1 + p["tol"])) & (df["close"] >= level * (1 - p["tol"]))

        at_s1, at_s2 = touched(piv["s1"]), touched(piv["s2"])
        cond = (doji & (at_s1 | at_s2)).shift(1, fill_value=False) & (df["close"] > df["high"].shift(1))
        cond &= _tradeable(df, timeframe, p)
        stops = df["low"].rolling(2).min().shift(1)
        level = np.where(at_s2.shift(1, fill_value=False), piv["s1"].shift(1), piv["pp"].shift(1))

        def target(i, entry, stop):
            t = float(level[i])
            return t if np.isfinite(t) and t - entry >= p["min_rr"] * (entry - stop) else None

        return self._signals(df, cond, stops, target, exchange, symbol, timeframe,
                             f"time_stop={p['time_stop']}", "a high-close doji at the day's pivot support")


PRICE_ACTION_STRATEGIES = (PriceChannelTwoBars, PinBarReentry, M2bTwoLeggedPullback, HighCloseDojiPivot)
