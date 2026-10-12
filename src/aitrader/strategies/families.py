"""Strategy families from the master prompt (section 4) that fit long-only spot trading.

These are research candidates, not proven systems. Each one is written as explicit rules with an
entry, exit, stop, sizing input, market regime and the conditions under which it must not trade,
and each starts in the registry as an untested candidate that has to clear the same backtest gate
(``learn test``) as every other strategy before it can paper trade. The thresholds are starting
points chosen before any backtest, not tuned values.

Rules shared by every strategy here:
- Long spot entries only: no shorting, no leverage, no futures orders. Bearish versions of these
  setups are left out.
- No trade while a candle is missing in the last ``clean_bars`` bars (``indicator_valid``): the
  indicators would cover more time than they claim to.
- No trade when ATR(14) is above the 95th percentile of its last 500 values (disorderly market).
- Signals use closed bars only; the order goes in at the next bar's open.

Left out on purpose, because they need a short leg, leverage, futures or a second venue: spot /
perpetual basis trading, cross-exchange spread arbitrage, pairs trading and cointegration, and the
short side of the funding-rate reversal (funding very positive). See docs/strategies.md.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import (
    adx,
    atr,
    atr_percentile,
    bollinger,
    cmf,
    directional_index,
    ema,
    indicator_valid,
    keltner,
    mfi,
    obv,
    rsi,
    sma,
    supertrend,
    williams_r,
)
from ..models import TIMEFRAME_MS, Side
from .base import Signal, Strategy, StrategySpec

SOURCE = "Master prompt section 4 (research candidate, rules written for this project)"
NO_TRADE = ("No trade while a candle is missing in the last 100 bars, or when ATR(14) is above "
            "its 95th percentile of the last 500 bars.")


def _close_ts(df: pd.DataFrame, i: int, timeframe: str) -> int:
    return int(df["ts"].iloc[i]) + TIMEFRAME_MS[timeframe]


def _tradeable(df: pd.DataFrame, timeframe: str, p: dict) -> pd.Series:
    """The shared must-not-trade checks: clean candle history and an orderly market."""
    clean = indicator_valid(df, p["clean_bars"], TIMEFRAME_MS[timeframe])
    calm = atr_percentile(df).fillna(1.0) <= p["atr_pct_max"]
    return clean & calm


def _exit_rule(key: str, fn) -> str:
    """Register an exit condition with the backtest engine (the paper trader rebuilds the
    strategy before managing a trade, so the rule is there after a restart too)."""
    from ..backtest.engine import CUSTOM_EXITS  # here, not at the top: the engine imports strategies

    CUSTOM_EXITS[key] = fn
    return key


def _strong_downtrend(df: pd.DataFrame, level: float) -> pd.Series:
    di = directional_index(df)
    return (adx(df) > level) & (di["minus_di"] > di["plus_di"])


BASE = {"clean_bars": 100, "atr_pct_max": 0.95, "risk_pct": 0.005}


class _Family(Strategy):
    """Shared plumbing: one long signal per flagged bar with a stop below the entry."""

    def _signals(self, df, cond, stops, targets, exchange, symbol, timeframe, trailing, reason,
                 features=None) -> list[Signal]:
        close = df["close"].to_numpy(float)
        out = []
        for i in np.flatnonzero(cond.fillna(False).to_numpy(bool)):
            entry, stop = float(close[i]), float(stops.iloc[i])
            if not np.isfinite(stop) or stop <= 0 or stop >= entry:
                continue
            if (entry - stop) / entry < 0.002:  # fees alone would eat most of 1R
                continue
            target = targets(i, entry, stop)
            if target is None or not np.isfinite(target) or target <= entry:
                continue
            out.append(Signal(
                ts=_close_ts(df, i, timeframe), strategy=self.spec.name, exchange=exchange,
                symbol=symbol, timeframe=timeframe, side=Side.BUY, entry=entry, stop=stop,
                target=float(target), trailing=trailing(i) if callable(trailing) else trailing,
                risk_pct=self.params["risk_pct"], reason=reason,
                features={k: float(v.iloc[i]) for k, v in (features or {}).items()
                          if pd.notna(v.iloc[i])},
            ))
        return out

    def _fixed_r(self):
        r = self.params["target_r"]
        return lambda _i, entry, stop: entry + r * (entry - stop)


# ---- trend following ----------------------------------------------------------------------------


class SupertrendTrend(_Family):
    spec = StrategySpec(
        name="supertrend_trend",
        source=SOURCE + ": #5 Supertrend following",
        timeframes=("1h", "4h"),
        regime="Trending up: close above EMA200 and ADX(14) >= 20.",
        setup="Supertrend(10, 3) flips from down to up on this bar, close above EMA200, "
        "ADX(14) >= 20.",
        entry="Market order at the next bar's open after the flip bar closes.",
        stop="The Supertrend line (its lower band) at the signal bar, but no wider than "
        "4 x ATR(14). Invalidated when price closes back under it.",
        target="3R fixed; exits earlier at the next open once Supertrend flips back down.",
        filter="Skip if ADX < 20 (no trend) or RSI(14) > 80. " + NO_TRADE,
        size="0.5% of equity at risk; units = risk / (entry - stop).",
        decisions=(
            "ATR period 10 and multiplier 3 are the common chart defaults, not tuned.",
            "The EMA200 and ADX filters keep the strategy out of sideways chop, where Supertrend flips often.",
            "Long only: down flips close the trade but never open a short.",
        ),
    )

    @classmethod
    def default_params(cls):
        return {**BASE, "st_n": 10, "st_mult": 3.0, "trend_ema": 200, "adx_min": 20.0,
                "rsi_max": 80.0, "max_stop_atr": 4.0, "target_r": 3.0}

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        close = df["close"]
        st = supertrend(df, p["st_n"], p["st_mult"])
        a = atr(df)
        cond = ((st["direction"] == 1) & (st["direction"].shift(1) == -1)
                & (close > ema(close, p["trend_ema"])) & (adx(df) >= p["adx_min"])
                & (rsi(close) <= p["rsi_max"]) & _tradeable(df, timeframe, p))
        stops = np.maximum(st["supertrend"], close - p["max_stop_atr"] * a)
        n, m = p["st_n"], p["st_mult"]
        key = _exit_rule(f"supertrend_down:{n}:{m:g}",
                         lambda d: supertrend(d, n, m)["direction"] == -1)
        return self._signals(df, cond, stops, self._fixed_r(), exchange, symbol, timeframe, key,
                             "Supertrend flipped up in an uptrend", {"adx": adx(df)})


class MtfTrendAlignment(_Family):
    spec = StrategySpec(
        name="mtf_trend_alignment",
        source=SOURCE + ": #7 Multi-timeframe trend alignment",
        timeframes=("1h", "15m"),
        regime="Up on both frames: the higher frame (4 x this one: 4h for 1h bars, 1h for 15m "
        "bars) has EMA20 > EMA50 and close > EMA50; this frame has EMA20 > EMA50.",
        setup="Higher-frame uptrend, built from this frame's own candles using only higher-frame "
        "bars that have fully closed.",
        entry="Close crosses back above EMA20 on this frame (a pullback ending) while both "
        "frames are up. Market order at the next open.",
        stop="Lower of the 10-bar low and entry - 2 x ATR(14).",
        target="2R fixed; exits earlier on a close below EMA50 of this frame.",
        filter="Skip when the latest closed higher-frame bar is missing any of its candles, or "
        "RSI(14) > 75. " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=(
            "The engine gives a strategy one timeframe, so the higher frame is rebuilt from it "
            "(4 bars per higher bar) instead of loading a second series.",
            "A higher-frame bar counts only after its last candle has closed, so there is no look-ahead.",
        ),
    )

    @classmethod
    def default_params(cls):
        return {**BASE, "ratio": 4, "fast": 20, "slow": 50, "rsi_max": 75.0, "stop_atr": 2.0,
                "swing_bars": 10, "target_r": 2.0}

    def higher_trend(self, df: pd.DataFrame, timeframe: str) -> pd.Series:
        """True on bars where the latest *closed* higher-frame bar is complete and trending up."""
        p = self.params
        step = TIMEFRAME_MS[timeframe]
        big = step * p["ratio"]
        group = df["ts"].astype("int64") // big
        htf = df.groupby(group).agg(close=("close", "last"), bars=("close", "size"))
        e_fast, e_slow = ema(htf["close"], p["fast"]), ema(htf["close"], p["slow"])
        up = (e_fast > e_slow) & (htf["close"] > e_slow) & (htf["bars"] == p["ratio"])
        # the higher bar that has closed by this bar's close: group k closes at (k + 1) * big
        last_closed = (df["ts"].astype("int64") + step) // big - 1
        return pd.Series(up.reindex(last_closed.to_numpy()).fillna(False).to_numpy(bool),
                         index=df.index)

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        close = df["close"]
        e_fast, e_slow = ema(close, p["fast"]), ema(close, p["slow"])
        a = atr(df)
        cond = (self.higher_trend(df, timeframe) & (e_fast > e_slow)
                & (close > e_fast) & (close.shift(1) <= e_fast.shift(1))
                & (rsi(close) <= p["rsi_max"]) & _tradeable(df, timeframe, p))
        swing = df["low"].rolling(p["swing_bars"], min_periods=p["swing_bars"]).min()
        stops = np.minimum(swing, close - p["stop_atr"] * a)
        return self._signals(df, cond, stops, self._fixed_r(), exchange, symbol, timeframe,
                             f"close<ema{p['slow']}", "higher and lower frame both up, pullback over")


# ---- breakout and volatility --------------------------------------------------------------------


class KeltnerBreakout(_Family):
    spec = StrategySpec(
        name="keltner_breakout",
        source=SOURCE + ": #11 Keltner Channel breakout",
        timeframes=("1h", "4h"),
        regime="Trending or expanding: close above EMA200.",
        setup="Close crosses above the upper Keltner channel (EMA20 + 2 x ATR(10)) with close "
        "above EMA200 and volume >= 1.2 x its 20-bar average.",
        entry="Market order at the next bar's open.",
        stop="Lower of the channel middle (EMA20) and entry - 1.5 x ATR(14).",
        target="2R fixed; exits earlier on a close back below the channel middle (EMA20).",
        filter="Skip if the breakout bar's range is > 3 x ATR (news spike) or RSI(14) > 80. "
        + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=(
            "Keltner(20, 2, ATR 10) are the usual chart defaults.",
            "Volume >= 1.2x average so a breakout on no interest is skipped.",
            "Downside breakouts are ignored (no shorting).",
        ),
    )

    @classmethod
    def default_params(cls):
        return {**BASE, "kc_n": 20, "kc_mult": 2.0, "kc_atr": 10, "trend_ema": 200,
                "vol_n": 20, "vol_mult": 1.2, "stop_atr": 1.5, "max_bar_atr": 3.0,
                "rsi_max": 80.0, "target_r": 2.0}

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        close = df["close"]
        kc = keltner(df, p["kc_n"], p["kc_mult"], p["kc_atr"])
        a = atr(df)
        vol_avg = df["volume"].shift(1).rolling(p["vol_n"], min_periods=p["vol_n"]).mean()
        cond = ((close > kc["upper"]) & (close.shift(1) <= kc["upper"].shift(1))
                & (close > ema(close, p["trend_ema"])) & (df["volume"] >= p["vol_mult"] * vol_avg)
                & ((df["high"] - df["low"]) <= p["max_bar_atr"] * a) & (rsi(close) <= p["rsi_max"])
                & _tradeable(df, timeframe, p))
        stops = np.minimum(kc["mid"], close - p["stop_atr"] * a)
        return self._signals(df, cond, stops, self._fixed_r(), exchange, symbol, timeframe,
                             f"close<ema{p['kc_n']}", "close above the upper Keltner channel")


class AtrExpansionBreakout(_Family):
    spec = StrategySpec(
        name="atr_expansion_breakout",
        source=SOURCE + ": #14 ATR expansion breakout",
        timeframes=("1h", "4h"),
        regime="Volatility expanding out of a quiet market: the previous bar's ATR percentile "
        "<= 60%, and close above EMA50.",
        setup="This bar's true range >= 2 x the previous bar's ATR(14), it closes in the top "
        "quarter of its range and above the highest high of the previous 20 bars.",
        entry="Market order at the next bar's open.",
        stop="The expansion bar's low minus 0.1 x ATR(14). Invalidated if price falls back "
        "through the bar that started the move.",
        target="2R fixed; time stop after 24 bars.",
        filter="Skip if the bar's range is > 4 x ATR (likely a news spike that often reverses), "
        "or if volatility was already high before the bar. " + NO_TRADE,
        size="0.5% of equity at risk (the stop is wide, so the position is small).",
        decisions=(
            "'Expansion' = true range at least 2x the prior ATR; 'from quiet' = prior ATR "
            "percentile at most 60%.",
            "Close in the top 25% of the range so the bar was bought into its close.",
        ),
    )

    @classmethod
    def default_params(cls):
        return {**BASE, "expand_mult": 2.0, "max_bar_atr": 4.0, "quiet_pct": 0.6,
                "lookback": 20, "trend_ema": 50, "close_pos": 0.75, "stop_pad_atr": 0.1,
                "time_stop_bars": 24, "target_r": 2.0}

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        close, high, low = df["close"], df["high"], df["low"]
        a = atr(df)
        prev_atr = a.shift(1)
        tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()],
                       axis=1).max(axis=1)
        rng = (high - low).replace(0, np.nan)
        prior_high = high.shift(1).rolling(p["lookback"], min_periods=p["lookback"]).max()
        cond = ((tr >= p["expand_mult"] * prev_atr) & ((high - low) <= p["max_bar_atr"] * prev_atr)
                & ((close - low) / rng >= p["close_pos"]) & (close > prior_high)
                & (atr_percentile(df).shift(1) <= p["quiet_pct"])
                & (close > ema(close, p["trend_ema"])) & _tradeable(df, timeframe, p))
        stops = low - p["stop_pad_atr"] * a
        return self._signals(df, cond, stops, self._fixed_r(), exchange, symbol, timeframe,
                             f"time_stop={p['time_stop_bars']}", "range expansion breakout",
                             {"tr_atr": tr / prev_atr})


class VolatilityRegimeSwitch(_Family):
    spec = StrategySpec(
        name="volatility_regime_switch",
        source=SOURCE + ": #38 Volatility-regime switching",
        timeframes=("1h", "4h"),
        regime="Chooses its rules by regime. Trend: ADX(14) >= 25, close > EMA200, EMA50 > "
        "EMA200, ATR percentile <= 85%. Range: ADX(14) < 20, ATR percentile <= 60%, close > "
        "SMA200. Anything else (including high volatility): no trade.",
        setup="Trend regime: close above the highest high of the previous 20 bars. Range "
        "regime: close crosses back above the lower Bollinger band (20, 2).",
        entry="Market order at the next bar's open.",
        stop="Trend: entry - 2 x ATR(14). Range: entry - 1.5 x ATR(14).",
        target="Trend: 3R, exits earlier on a close below EMA50. Range: the Bollinger middle "
        "band at the signal bar (skipped if that is under 0.8R away), time stop 24 bars.",
        filter="No trade when ATR percentile > 85% or when neither regime holds. " + NO_TRADE,
        size="0.5% of equity at risk in either regime.",
        decisions=(
            "ADX and ATR percentile decide the regime; the thresholds are common rules of thumb.",
            "Range trades also need close > SMA200 so a falling market is not bought.",
        ),
    )

    @classmethod
    def default_params(cls):
        return {**BASE, "trend_adx": 25.0, "range_adx": 20.0, "trend_vol_max": 0.85,
                "range_vol_max": 0.6, "lookback": 20, "trend_stop_atr": 2.0,
                "range_stop_atr": 1.5, "trend_target_r": 3.0, "min_rr": 0.8,
                "range_time_stop": 24, "target_r": None}

    def regimes(self, df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
        p = self.params
        close = df["close"]
        strength, vol = adx(df), atr_percentile(df)
        e50, e200 = ema(close, 50), ema(close, 200)
        trend = ((strength >= p["trend_adx"]) & (close > e200) & (e50 > e200)
                 & (vol <= p["trend_vol_max"]))
        rng = (strength < p["range_adx"]) & (vol <= p["range_vol_max"]) & (close > sma(close, 200))
        return trend, rng

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        close = df["close"]
        a = atr(df)
        bb = bollinger(close)
        trend, rng = self.regimes(df)
        ok = _tradeable(df, timeframe, p)
        prior_high = df["high"].shift(1).rolling(p["lookback"], min_periods=p["lookback"]).max()
        trend_hit = trend & (close > prior_high) & ok
        range_hit = (rng & (close > bb["lower"]) & (close.shift(1) <= bb["lower"].shift(1)) & ok
                     & ~trend_hit)
        stops = pd.Series(np.where(trend_hit, close - p["trend_stop_atr"] * a,
                                   close - p["range_stop_atr"] * a), index=df.index)
        mid = bb["mid"].to_numpy(float)
        is_trend = trend_hit.to_numpy(bool)

        def target(i, entry, stop):
            if is_trend[i]:
                return entry + p["trend_target_r"] * (entry - stop)
            return mid[i] if mid[i] - entry >= p["min_rr"] * (entry - stop) else None

        return self._signals(
            df, trend_hit | range_hit, stops, target, exchange, symbol, timeframe,
            lambda i: "close<ema50" if is_trend[i] else f"time_stop={p['range_time_stop']}",
            "regime switch", {"adx": adx(df), "regime_trend": trend.astype(float)})


# ---- mean reversion -----------------------------------------------------------------------------


class WilliamsRReversal(_Family):
    spec = StrategySpec(
        name="williams_r_reversal",
        source=SOURCE + ": Williams %R reversal (mean-reversion family)",
        timeframes=("1h", "4h"),
        regime="Pullbacks inside a long-term uptrend: close above SMA200.",
        setup="Williams %R(14) crosses back above -80 from below (leaving oversold) with close "
        "above SMA200.",
        entry="Market order at the next bar's open.",
        stop="Lowest low of the last 5 bars minus 0.25 x ATR(14).",
        target="Exit at the next open once %R goes above -20 (overbought), capped at 2R; time "
        "stop after 30 bars.",
        filter="Skip in a strong downtrend (ADX(14) > 35 with -DI above +DI). " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=(
            "Oversold below -80 and overbought above -20 are the indicator's usual levels.",
            "Only bought above SMA200 so a falling market is not bought on every bounce.",
        ),
    )

    @classmethod
    def default_params(cls):
        return {**BASE, "wr_n": 14, "oversold": -80.0, "overbought": -20.0, "trend_sma": 200,
                "swing_bars": 5, "stop_pad_atr": 0.25, "downtrend_adx": 35.0,
                "time_stop_bars": 30, "target_r": 2.0}

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        close = df["close"]
        wr = williams_r(df, p["wr_n"])
        a = atr(df)
        cond = ((wr > p["oversold"]) & (wr.shift(1) <= p["oversold"])
                & (close > sma(close, p["trend_sma"])) & ~_strong_downtrend(df, p["downtrend_adx"])
                & _tradeable(df, timeframe, p))
        stops = df["low"].rolling(p["swing_bars"], min_periods=p["swing_bars"]).min() - p["stop_pad_atr"] * a
        n, level = p["wr_n"], p["overbought"]
        key = _exit_rule(f"williams_r_above:{n}:{level:g}", lambda d: williams_r(d, n) > level)
        return self._signals(df, cond, stops, self._fixed_r(), exchange, symbol, timeframe,
                             f"{key}|time_stop={p['time_stop_bars']}", "Williams %R left oversold",
                             {"williams_r": wr})


class MaDeviationReversion(_Family):
    spec = StrategySpec(
        name="ma_deviation_reversion",
        source=SOURCE + ": #21 Moving-average deviation reversion",
        timeframes=("1h", "4h"),
        regime="Overstretched dip inside a long-term uptrend: close above SMA200.",
        setup="Within the last 3 bars, close was at least 2.5 x ATR(14) below SMA(20).",
        entry="The first up bar (close > open and close > previous close) after that, still "
        "above SMA200. Market order at the next open.",
        stop="Lowest low of the last 5 bars minus 0.25 x ATR(14).",
        target="SMA(20) at the signal bar (the mean); skipped if that is less than 1R away. "
        "Time stop after 20 bars.",
        filter="Skip in a strong downtrend (ADX(14) > 35 with -DI above +DI). " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=(
            "Deviation is measured in ATRs so the rule means the same on every coin.",
            "The target is the average price at the signal, not a moving target.",
        ),
    )

    @classmethod
    def default_params(cls):
        return {**BASE, "ma_n": 20, "dev_atr": 2.5, "setup_bars": 3, "trend_sma": 200,
                "swing_bars": 5, "stop_pad_atr": 0.25, "downtrend_adx": 35.0, "min_rr": 1.0,
                "time_stop_bars": 20, "target_r": None}

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        close = df["close"]
        mean = sma(close, p["ma_n"])
        a = atr(df)
        stretched = ((close - mean) / a.replace(0, np.nan)) <= -p["dev_atr"]
        recent = stretched.astype(int).rolling(p["setup_bars"], min_periods=1).max() > 0
        cond = (recent & (close > df["open"]) & (close > close.shift(1))
                & (close > sma(close, p["trend_sma"])) & ~_strong_downtrend(df, p["downtrend_adx"])
                & _tradeable(df, timeframe, p))
        stops = df["low"].rolling(p["swing_bars"], min_periods=p["swing_bars"]).min() - p["stop_pad_atr"] * a
        mid = mean.to_numpy(float)

        def target(i, entry, stop):
            return mid[i] if mid[i] - entry >= p["min_rr"] * (entry - stop) else None

        return self._signals(df, cond, stops, target, exchange, symbol, timeframe,
                             f"time_stop={p['time_stop_bars']}", "stretched below its average, turning up")


# ---- momentum and volume ------------------------------------------------------------------------


def _volume_ok(df: pd.DataFrame, n: int) -> pd.Series:
    """Volume indicators mean nothing over bars with no trading at all."""
    return df["volume"].rolling(n, min_periods=n).min() > 0


class ObvBreakout(_Family):
    spec = StrategySpec(
        name="obv_breakout",
        source=SOURCE + ": #27 OBV breakout confirmation",
        timeframes=("1h", "4h"),
        regime="Uptrend: close above EMA50.",
        setup="Close above the highest high of the previous 20 bars AND On-Balance Volume above "
        "its own highest value of the previous 20 bars (volume confirms the price breakout).",
        entry="Market order at the next bar's open.",
        stop="Entry - 1.5 x ATR(14).",
        target="2R fixed; exits earlier on a close below EMA50.",
        filter="Skip if RSI(14) > 80, or any bar in the last 20 had zero volume. " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=(
            "OBV's level depends on where the data starts, so only its own 20-bar high is used.",
        ),
    )

    @classmethod
    def default_params(cls):
        return {**BASE, "lookback": 20, "trend_ema": 50, "stop_atr": 1.5, "rsi_max": 80.0,
                "target_r": 2.0}

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        close = df["close"]
        n = p["lookback"]
        o = obv(df)
        prior_high = df["high"].shift(1).rolling(n, min_periods=n).max()
        cond = ((close > prior_high) & (o > o.shift(1).rolling(n, min_periods=n).max())
                & (close > ema(close, p["trend_ema"])) & (rsi(close) <= p["rsi_max"])
                & _volume_ok(df, n) & _tradeable(df, timeframe, p))
        stops = close - p["stop_atr"] * atr(df)
        return self._signals(df, cond, stops, self._fixed_r(), exchange, symbol, timeframe,
                             f"close<ema{p['trend_ema']}", "price and OBV both at 20-bar highs")


class MfiMomentum(_Family):
    spec = StrategySpec(
        name="mfi_momentum",
        source=SOURCE + ": #28 MFI momentum confirmation",
        timeframes=("1h", "4h"),
        regime="Uptrend: close > EMA50 > EMA200.",
        setup="MFI(14) crosses above 50 after being below 40 at some point in the last 10 bars "
        "(money flow turning from weak to strong).",
        entry="Market order at the next bar's open.",
        stop="Lower of the 10-bar low and entry - 2 x ATR(14).",
        target="2R fixed; exits earlier on a close below EMA50.",
        filter="Skip if MFI > 80 (already overbought), or any bar in the last 14 had zero "
        "volume. " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=(
            "50 is MFI's midline; 'weak first' = below 40 within 10 bars, so a flat MFI near 50 doesn't count.",
        ),
    )

    @classmethod
    def default_params(cls):
        return {**BASE, "mfi_n": 14, "cross": 50.0, "weak": 40.0, "weak_bars": 10,
                "mfi_max": 80.0, "stop_atr": 2.0, "swing_bars": 10, "target_r": 2.0}

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        close = df["close"]
        m = mfi(df, p["mfi_n"])
        e50, e200 = ema(close, 50), ema(close, 200)
        was_weak = (m < p["weak"]).astype(int).rolling(p["weak_bars"], min_periods=1).max() > 0
        cond = ((m > p["cross"]) & (m.shift(1) <= p["cross"]) & was_weak & (m <= p["mfi_max"])
                & (close > e50) & (e50 > e200) & _volume_ok(df, p["mfi_n"])
                & _tradeable(df, timeframe, p))
        swing = df["low"].rolling(p["swing_bars"], min_periods=p["swing_bars"]).min()
        stops = np.minimum(swing, close - p["stop_atr"] * atr(df))
        return self._signals(df, cond, stops, self._fixed_r(), exchange, symbol, timeframe,
                             "close<ema50", "MFI turned up through 50 in an uptrend", {"mfi": m})


class CmfPressure(_Family):
    spec = StrategySpec(
        name="cmf_pressure",
        source=SOURCE + ": #29 CMF buying/selling pressure (buying side only)",
        timeframes=("1h", "4h"),
        regime="Uptrend: close above EMA50.",
        setup="Chaikin Money Flow(20) crosses above +0.05 (buying pressure) and the bar closes "
        "above the previous bar's high.",
        entry="Market order at the next bar's open.",
        stop="Entry - 2 x ATR(14).",
        target="2R fixed; exits earlier at the next open once CMF(20) drops below -0.05 "
        "(selling pressure).",
        filter="Skip if RSI(14) > 80, or any bar in the last 20 had zero volume. " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=(
            "+/-0.05 rather than 0 so noise around zero does not trigger entries and exits.",
            "Selling pressure only closes a trade; it never opens a short.",
        ),
    )

    @classmethod
    def default_params(cls):
        return {**BASE, "cmf_n": 20, "enter": 0.05, "leave": -0.05, "trend_ema": 50,
                "stop_atr": 2.0, "rsi_max": 80.0, "target_r": 2.0}

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        close = df["close"]
        c = cmf(df, p["cmf_n"])
        cond = ((c > p["enter"]) & (c.shift(1) <= p["enter"]) & (close > df["high"].shift(1))
                & (close > ema(close, p["trend_ema"])) & (rsi(close) <= p["rsi_max"])
                & _volume_ok(df, p["cmf_n"]) & _tradeable(df, timeframe, p))
        stops = close - p["stop_atr"] * atr(df)
        n, leave = p["cmf_n"], p["leave"]
        key = _exit_rule(f"cmf_below:{n}:{leave:g}", lambda d: cmf(d, n) < leave)
        return self._signals(df, cond, stops, self._fixed_r(), exchange, symbol, timeframe, key,
                             "buying pressure: CMF up through +0.05", {"cmf": c})


# ---- crypto-specific ----------------------------------------------------------------------------


class FundingExtremeLong(_Family):
    spec = StrategySpec(
        name="funding_extreme_long",
        source=SOURCE + ": #33 Funding-rate extreme reversal (long side only)",
        timeframes=("1h", "4h"),
        regime="Crowded shorts: the perpetual funding rate is very negative, so shorts are "
        "paying longs and a squeeze can follow. Read from the stored market context (Bybit "
        "USDT-perpetual funding, as the context filter does); the trade itself is a spot buy.",
        setup="Funding rate at or below -0.03% per 8h at the signal bar (the newest value "
        "stamped by the bar's close and still fresh).",
        entry="Close crosses above EMA20 while funding is that negative (price starting to turn "
        "up). Market order at the next open.",
        stop="Lower of the 10-bar low and entry - 2 x ATR(14).",
        target="2R fixed; exits earlier on a close below EMA50; time stop after 48 bars.",
        filter="No trade without a fresh funding value (missing or stale data means no signal, "
        "never a guess). Skip if RSI(14) > 70. " + NO_TRADE,
        size="0.5% of equity at risk. Funding very positive would be the short side: not traded.",
        decisions=(
            "-0.03% per 8h is about three times the usual +0.01% base rate, on the other side.",
            "The funding series needs `aitrader context` history; without it the strategy "
            "makes no trades, so the backtest gate rejects it for lack of trades.",
        ),
    )
    needs_context = True

    @classmethod
    def default_params(cls):
        return {**BASE, "funding_max": -0.0003, "fast": 20, "slow": 50, "stop_atr": 2.0,
                "swing_bars": 10, "rsi_max": 70.0, "time_stop_bars": 48, "target_r": 2.0}

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        if "funding_rate" not in df:
            return []
        close = df["close"]
        e_fast = ema(close, p["fast"])
        funding = df["funding_rate"].astype(float)
        cond = (funding.notna() & (funding <= p["funding_max"])
                & (close > e_fast) & (close.shift(1) <= e_fast.shift(1))
                & (rsi(close) <= p["rsi_max"]) & _tradeable(df, timeframe, p))
        swing = df["low"].rolling(p["swing_bars"], min_periods=p["swing_bars"]).min()
        stops = np.minimum(swing, close - p["stop_atr"] * atr(df))
        return self._signals(df, cond, stops, self._fixed_r(), exchange, symbol, timeframe,
                             f"close<ema{p['slow']}|time_stop={p['time_stop_bars']}",
                             "funding very negative, price turning up", {"funding_rate": funding})


# ---- ensemble -----------------------------------------------------------------------------------


class EnsembleVote(_Family):
    spec = StrategySpec(
        name="ensemble_vote",
        source=SOURCE + ": #40 Ensemble voting (equal votes; no selection by past results)",
        timeframes=("1h", "4h"),
        regime="Whatever its members need; it adds no regime of its own beyond the shared "
        "no-trade checks.",
        setup="Members vote by family: trend (supertrend_trend, trend_pullback, "
        "mtf_trend_alignment), breakout (keltner_breakout, atr_expansion_breakout, "
        "bollinger_squeeze) and volume (obv_breakout, mfi_momentum, cmf_pressure). A family "
        "votes when any of its members signalled in the last 3 bars. Members run only on their "
        "own timeframes.",
        entry="A member signals on this bar and at least 2 different families are voting. "
        "Market order at the next open.",
        stop="Entry - 2 x ATR(14).",
        target="2R fixed; exits earlier on a close below EMA50.",
        filter="Two members of the same family count as one vote, because they read the same "
        "kind of evidence. " + NO_TRADE,
        size="0.5% of equity at risk, the same as one strategy (agreement does not raise size).",
        decisions=(
            "Votes are equal. Weighting members by their backtest results would leak the test "
            "period into the rule, so it is left to the separate backtest gate.",
            "Only strategies already in this code vote; book rule specs do not.",
        ),
    )

    FAMILIES = {
        "trend": ("supertrend_trend", "trend_pullback", "mtf_trend_alignment"),
        "breakout": ("keltner_breakout", "atr_expansion_breakout", "bollinger_squeeze"),
        "volume": ("obv_breakout", "mfi_momentum", "cmf_pressure"),
    }

    @classmethod
    def default_params(cls):
        return {**BASE, "window": 3, "min_families": 2, "stop_atr": 2.0, "target_r": 2.0}

    def votes(self, df, exchange, symbol, timeframe) -> tuple[pd.DataFrame, pd.Series]:
        """Per family, 1 on bars where it is voting; and True on bars where any member signalled."""
        from . import STRATEGIES

        p = self.params
        pos = {int(t) + TIMEFRAME_MS[timeframe]: i for i, t in enumerate(df["ts"].to_numpy())}
        fresh = np.zeros(len(df), dtype=bool)
        out = {}
        for fam, names in self.FAMILIES.items():
            hit = np.zeros(len(df))
            for name in names:
                cls = STRATEGIES.get(name)
                if cls is None or timeframe not in cls.spec.timeframes:
                    continue
                for sig in cls().generate(df, exchange, symbol, timeframe):
                    i = pos.get(sig.ts)
                    if i is not None:
                        hit[i] = 1.0
                        fresh[i] = True
            out[fam] = pd.Series(hit, index=df.index).rolling(p["window"], min_periods=1).max()
        return pd.DataFrame(out), pd.Series(fresh, index=df.index)

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        fam, fresh = self.votes(df, exchange, symbol, timeframe)
        count = fam.sum(axis=1)
        cond = fresh & (count >= p["min_families"]) & _tradeable(df, timeframe, p)
        stops = df["close"] - p["stop_atr"] * atr(df)
        return self._signals(df, cond, stops, self._fixed_r(), exchange, symbol, timeframe,
                             "close<ema50", "families agreeing: vote", {"families": count})


FAMILY_STRATEGIES: tuple[type[Strategy], ...] = (
    SupertrendTrend, MtfTrendAlignment, KeltnerBreakout, AtrExpansionBreakout,
    VolatilityRegimeSwitch, WilliamsRReversal, MaDeviationReversion, ObvBreakout, MfiMomentum,
    CmfPressure, FundingExtremeLong, EnsembleVote,
)
