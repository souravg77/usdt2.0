"""Price-structure strategies: fair value gap retest, order block retest, liquidity sweep reversal.

Ideas from the ApexTrade prompt Shawn shared (2026-10-15), written as explicit rules for this
project. They are research candidates: each must clear the same backtest gate as every other
strategy before it paper trades, and its short twin (``strategies.mirror``) trades the bearish
version on Bybit perpetuals. The thresholds are starting points chosen before any backtest.
"""

from __future__ import annotations

import numpy as np

from ..indicators import atr, ema, fair_value_gaps, liquidity_sweeps, order_blocks, retest, rsi
from .base import StrategySpec
from .families import BASE, NO_TRADE, _Family, _tradeable

SOURCE = "ApexTrade prompt (research candidate, rules written for this project)"


class FvgRetest(_Family):
    spec = StrategySpec(
        name="fvg_retest",
        source=SOURCE + ": fair value gap retest",
        timeframes=("1h", "4h", "15m"),
        regime="Uptrend: close > EMA50 > EMA200.",
        setup="A bullish fair value gap of the last 50 bars is still unfilled, and this bar dips into "
        "it and closes above its bottom.",
        entry="Market order at the next bar's open.",
        stop="Below the gap: its bottom minus 0.5 x ATR(14).",
        target="2.5R fixed; exits earlier on a close below EMA50.",
        filter="Skip if RSI(14) > 75. " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("A gap is filled (and no longer used) once a low reaches its bottom.",),
    )
    eligible_regimes = ("trend-up", "uncertain")

    @classmethod
    def default_params(cls):
        return {**BASE, "max_age": 50, "stop_atr": 0.5, "rsi_max": 75.0, "target_r": 2.5}

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        close = df["close"]
        e50, e200 = ema(close, 50), ema(close, 200)
        g = fair_value_gaps(df, p["max_age"])
        cond = ((retest(df, g["bull_top"], g["bull_bottom"]) > 0) & (close > e50) & (e50 > e200)
                & (rsi(close) <= p["rsi_max"]) & _tradeable(df, timeframe, p))
        stops = g["bull_bottom"].shift(1) - p["stop_atr"] * atr(df)
        return self._signals(df, cond, stops, self._fixed_r(), exchange, symbol, timeframe, "close<ema50",
                             "dipped into an open bullish fair value gap in an uptrend")


class OrderBlockRetest(_Family):
    spec = StrategySpec(
        name="order_block_retest",
        source=SOURCE + ": order block retest",
        timeframes=("1h", "4h", "15m"),
        regime="Uptrend: close > EMA50 > EMA200.",
        setup="A bullish order block (the last down candle before a close above the 10-bar high) is "
        "still valid, and this bar dips into it and closes above its bottom.",
        entry="Market order at the next bar's open.",
        stop="Below the block: its bottom minus 0.25 x ATR(14).",
        target="2.5R fixed; exits earlier on a close below EMA50.",
        filter="Skip if RSI(14) > 75. " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("A block stops counting after a close below its low, or after 50 bars.",),
    )
    eligible_regimes = ("trend-up", "uncertain")

    @classmethod
    def default_params(cls):
        return {**BASE, "break_bars": 10, "max_age": 50, "stop_atr": 0.25, "rsi_max": 75.0, "target_r": 2.5}

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        close = df["close"]
        e50, e200 = ema(close, 50), ema(close, 200)
        ob = order_blocks(df, p["break_bars"], p["max_age"])
        cond = ((retest(df, ob["bull_top"], ob["bull_bottom"]) > 0) & (close > e50) & (e50 > e200)
                & (rsi(close) <= p["rsi_max"]) & _tradeable(df, timeframe, p))
        stops = ob["bull_bottom"].shift(1) - p["stop_atr"] * atr(df)
        return self._signals(df, cond, stops, self._fixed_r(), exchange, symbol, timeframe, "close<ema50",
                             "dipped into a bullish order block in an uptrend")


class LiquiditySweepReversal(_Family):
    spec = StrategySpec(
        name="liquidity_sweep_reversal",
        source=SOURCE + ": liquidity sweep reversal",
        timeframes=("1h", "4h", "15m"),
        regime="Above EMA200 (the bigger trend is up).",
        setup="Liquidity sweep: this bar trades below the lowest low of the previous 20 bars (where stops rest) and "
        "closes back above it, with RSI(14) at most 45.",
        entry="Market order at the next bar's open.",
        stop="The sweep bar's low minus 0.25 x ATR(14).",
        target="2R fixed; time stop after 20 bars.",
        filter=NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("RSI at most 45 keeps it to sweeps after a pullback, not in the middle of a rally.",),
    )
    eligible_regimes = ("trend-up", "range", "uncertain")

    @classmethod
    def default_params(cls):
        return {**BASE, "lookback": 20, "rsi_max": 45.0, "stop_atr": 0.25, "target_r": 2.0, "time_stop": 20}

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        close = df["close"]
        sw = liquidity_sweeps(df, p["lookback"])
        cond = ((sw["low"] > 0) & (close > ema(close, 200)) & (rsi(close) <= p["rsi_max"])
                & _tradeable(df, timeframe, p))
        stops = df["low"] - p["stop_atr"] * atr(df)
        stops = stops.where(np.isfinite(stops))
        return self._signals(df, cond, stops, self._fixed_r(), exchange, symbol, timeframe,
                             f"time_stop={p['time_stop']}", "swept the 20-bar low and closed back above it")


class SweepThenGap(_Family):
    spec = StrategySpec(
        name="sweep_then_gap",
        source="Gemini \"institutional upgrades\" file Shawn shared (2026-10-17), rules written for this project: "
        "liquidity sweep followed by a fair value gap",
        timeframes=("1h", "4h", "15m"),
        regime="Any: the sweep itself is the setup.",
        setup="Two bars ago a candle swept the previous 15 bars' low and closed back above it; this bar's low "
        "is above that sweep candle's high, leaving a bullish fair value gap (a fast move up after the stop hunt).",
        entry="Market order at the next bar's open.",
        stop="The sweep candle's low minus 0.25 x ATR(14).",
        target="2.5R fixed; time stop after 30 bars.",
        filter=NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("Gemini's version enters with a limit order at the top of the gap; here it is a market order "
                   "at the next open like the other strategies (the limit-entry switch on the Trading page can "
                   "turn it into a limit order).",),
    )
    eligible_regimes = ("trend-up", "range", "uncertain")

    @classmethod
    def default_params(cls):
        return {**BASE, "lookback": 15, "stop_atr": 0.25, "target_r": 2.5, "time_stop": 30}

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        sw = liquidity_sweeps(df, p["lookback"])
        gap = df["low"] > df["high"].shift(2)  # the 3-bar bullish gap that starts at the sweep candle
        cond = (sw["low"].shift(2) > 0) & gap & _tradeable(df, timeframe, p)
        stops = df["low"].shift(2) - p["stop_atr"] * atr(df)
        stops = stops.where(np.isfinite(stops))
        return self._signals(df, cond, stops, self._fixed_r(), exchange, symbol, timeframe,
                             f"time_stop={p['time_stop']}", "swept the lows, then left a fair value gap going up")


SMC_STRATEGIES = (FvgRetest, OrderBlockRetest, LiquiditySweepReversal, SweepThenGap)
