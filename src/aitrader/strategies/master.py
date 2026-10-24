"""The master prompt's strategies that weren't built yet, written as explicit long rules.

Same contract and caveats as ``families.py``: research candidates, not proven systems. Each one
states its setup, entry, stop, target, exits, the regimes it may trade in (``eligible_regimes``)
and when it must not trade; each starts as an untested candidate that has to pass the backtest
gate before it can paper trade. Thresholds are common textbook values chosen before any backtest.
Short versions are their twins (``strategies.mirror``) on Bybit USDT perpetuals.

Numbers (#1, #3, ...) are the master prompt's strategy list; ``strategies.catalog`` maps all 40.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import (
    adx,
    atr,
    bollinger,
    bullish_divergence,
    candle_patterns,
    directional_index,
    donchian,
    ema,
    ema_stack,
    ichimoku,
    macd,
    market_structure,
    relative_volume,
    rsi,
    sma,
    stoch_rsi,
    swing_points,
    vwap_bands,
)
from ..models import TIMEFRAME_MS
from .families import BASE, NO_TRADE, SOURCE, _exit_rule, _Family, _strong_downtrend, _tradeable, _volume_ok

TREND = ("trend-up", "uncertain")
TREND_RANGE = ("trend-up", "range", "uncertain")
RANGE = ("range", "uncertain")
DAY_MS = 86_400_000


def _swing_stop(df: pd.DataFrame, bars: int, pad_atr: float, max_atr: float | None = None) -> pd.Series:
    """Lowest low of the last ``bars`` bars minus ``pad_atr`` ATRs, no wider than ``max_atr`` ATRs."""
    a = atr(df)
    stop = df["low"].rolling(bars, min_periods=bars).min() - pad_atr * a
    if max_atr is not None:
        stop = np.maximum(stop, df["close"] - max_atr * a)
    return stop


def _mean_target(level: pd.Series, min_rr: float):
    """Target a level (e.g. the mean) only when it is at least ``min_rr`` R away."""
    lv = level.to_numpy(float)

    def target(i, entry, stop):
        return lv[i] if np.isfinite(lv[i]) and lv[i] - entry >= min_rr * (entry - stop) else None

    return target


def _spec(**kw):
    from .base import StrategySpec

    return StrategySpec(**kw)


def _df_key(df: pd.DataFrame) -> tuple:
    if not len(df):
        return (id(df), 0)
    return (id(df), len(df), int(df["ts"].iloc[-1]), float(df["close"].iloc[0]), float(df["close"].iloc[-1]))


class _Master(_Family):
    """Shared plumbing: ``setup`` gives the entry bars, ``stop_series`` the stops."""

    reason = ""

    def setup(self, df: pd.DataFrame, timeframe: str) -> pd.Series:  # pragma: no cover - abstract
        raise NotImplementedError

    def exit_key(self) -> str | None:
        return None

    def targets(self, df: pd.DataFrame):
        return self._fixed_r()

    def features(self, df: pd.DataFrame) -> dict:
        return {}

    def generate(self, df, exchange, symbol, timeframe):
        if len(df) < 3:
            return []
        cond = self.setup(df, timeframe) & _tradeable(df, timeframe, self.params)
        return self._signals(df, cond, self.stop_series(df), self.targets(df), exchange, symbol, timeframe,
                             self.exit_key(), self.reason, self.features(df))


# ---- trend following (#1-#8) --------------------------------------------------------------------


class EmaCrossover(_Master):
    spec = _spec(
        name="ema_crossover",
        source=SOURCE + ": #1 EMA crossover",
        timeframes=("1h", "4h"),
        regime="Trending up, or a trend just forming (regime trend-up or uncertain).",
        setup="EMA(20) crosses above EMA(50) on this bar, with the close above EMA(200).",
        entry="Market order at the next bar's open.",
        stop="Lowest low of the last 10 bars minus 0.25 x ATR(14), no wider than 3 x ATR(14).",
        target="3R fixed; exits earlier when EMA(20) crosses back under EMA(50).",
        filter="Only above EMA(200). " + NO_TRADE,
        size="0.5% of equity at risk; units = risk / (entry - stop).",
        decisions=("20/50 are common swing settings, not tuned.",
                   "The EMA(200) filter keeps it from buying crosses inside a downtrend."),
    )
    eligible_regimes = TREND
    reason = "EMA20 crossed above EMA50 above EMA200"

    @classmethod
    def default_params(cls):
        return {**BASE, "fast": 20, "slow": 50, "trend": 200, "swing_bars": 10, "max_stop_atr": 3.0,
                "target_r": 3.0}

    def setup(self, df, timeframe):
        p, c = self.params, df["close"]
        f, s = ema(c, p["fast"]), ema(c, p["slow"])
        return (f > s) & (f.shift(1) <= s.shift(1)) & (c > ema(c, p["trend"]))

    def stop_series(self, df):
        return _swing_stop(df, self.params["swing_bars"], 0.25, self.params["max_stop_atr"])

    def exit_key(self):
        f, s = self.params["fast"], self.params["slow"]
        return _exit_rule(f"ema_cross_down:{f}:{s}", lambda d: ema(d["close"], f) < ema(d["close"], s))


class SmaCrossover(_Master):
    spec = _spec(
        name="sma_crossover",
        source=SOURCE + ": #2 SMA crossover",
        timeframes=("4h", "1d"),
        regime="Trending up or a trend forming.",
        setup="SMA(20) crosses above SMA(50) with the close above SMA(200).",
        entry="Market order at the next bar's open.",
        stop="Lowest low of the last 10 bars minus 0.25 x ATR(14), no wider than 3 x ATR(14).",
        target="3R fixed; exits earlier when SMA(20) crosses back under SMA(50).",
        filter="Only above SMA(200). " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("Simple averages react later than EMAs: fewer, later signals.",),
    )
    eligible_regimes = TREND
    reason = "SMA20 crossed above SMA50 above SMA200"

    @classmethod
    def default_params(cls):
        return {**BASE, "fast": 20, "slow": 50, "trend": 200, "swing_bars": 10, "max_stop_atr": 3.0,
                "target_r": 3.0}

    def setup(self, df, timeframe):
        p, c = self.params, df["close"]
        f, s = sma(c, p["fast"]), sma(c, p["slow"])
        return (f > s) & (f.shift(1) <= s.shift(1)) & (c > sma(c, p["trend"]))

    def stop_series(self, df):
        return _swing_stop(df, self.params["swing_bars"], 0.25, self.params["max_stop_atr"])

    def exit_key(self):
        f, s = self.params["fast"], self.params["slow"]
        return _exit_rule(f"sma_cross_down:{f}:{s}", lambda d: sma(d["close"], f) < sma(d["close"], s))


class MacdTrend(_Master):
    spec = _spec(
        name="macd_trend",
        source=SOURCE + ": #3 MACD trend following",
        timeframes=("1h", "4h"),
        regime="Trending up: MACD above zero and close above EMA(200).",
        setup="MACD(12, 26, 9) line crosses above its signal line while both are above zero, "
        "close above EMA(200).",
        entry="Market order at the next bar's open.",
        stop="Lowest low of the last 10 bars minus 0.25 x ATR(14), no wider than 3 x ATR(14).",
        target="2.5R fixed; exits earlier when the MACD line crosses back under the signal.",
        filter="Crosses below zero are ignored (that's a bounce in a downtrend). " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("Above-zero crosses are trend continuations; below-zero ones are left to #25.",),
    )
    eligible_regimes = TREND
    reason = "MACD crossed up above zero"

    @classmethod
    def default_params(cls):
        return {**BASE, "fast": 12, "slow": 26, "signal": 9, "trend": 200, "swing_bars": 10,
                "max_stop_atr": 3.0, "target_r": 2.5}

    def setup(self, df, timeframe):
        p, c = self.params, df["close"]
        m = macd(c, p["fast"], p["slow"], p["signal"])
        return ((m["macd"] > m["signal"]) & (m["macd"].shift(1) <= m["signal"].shift(1))
                & (m["signal"] > 0) & (c > ema(c, p["trend"])))

    def stop_series(self, df):
        return _swing_stop(df, self.params["swing_bars"], 0.25, self.params["max_stop_atr"])

    def exit_key(self):
        p = self.params
        f, s, g = p["fast"], p["slow"], p["signal"]

        def under(d):
            m = macd(d["close"], f, s, g)
            return m["macd"] < m["signal"]

        return _exit_rule(f"macd_under_signal:{f}:{s}:{g}", under)

    def features(self, df):
        return {"macd_hist": macd(df["close"])["hist"]}


class AdxTrend(_Master):
    spec = _spec(
        name="adx_trend",
        source=SOURCE + ": #4 ADX-filtered trend following",
        timeframes=("1h", "4h"),
        regime="A strengthening trend: ADX(14) at least 25 and rising.",
        setup="ADX(14) crosses above 25 (a trend gaining strength) while +DI is above -DI, close "
        "above EMA(50).",
        entry="Market order at the next bar's open.",
        stop="Entry - 2 x ATR(14).",
        target="3R fixed; exits earlier when -DI rises back above +DI.",
        filter="No trade when ADX is under 25 (no trend to follow). " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("25 is Wilder's 'trending' threshold; crossing it marks a trend gaining strength.",),
    )
    eligible_regimes = TREND
    reason = "ADX crossed above 25 with +DI leading"

    @classmethod
    def default_params(cls):
        return {**BASE, "n": 14, "adx_min": 25.0, "trend": 50, "stop_atr": 2.0, "target_r": 3.0}

    def setup(self, df, timeframe):
        p = self.params
        di, a = directional_index(df, p["n"]), adx(df, p["n"])
        return ((a >= p["adx_min"]) & (a.shift(1) < p["adx_min"]) & (di["plus_di"] > di["minus_di"])
                & (df["close"] > ema(df["close"], p["trend"])))

    def stop_series(self, df):
        return df["close"] - self.params["stop_atr"] * atr(df)

    def exit_key(self):
        n = self.params["n"]

        def flipped(d):
            di = directional_index(d, n)
            return di["minus_di"] > di["plus_di"]

        return _exit_rule(f"minus_di_leads:{n}", flipped)

    def features(self, df):
        return {"adx": adx(df, self.params["n"])}


class IchimokuTrend(_Master):
    spec = _spec(
        name="ichimoku_trend",
        source=SOURCE + ": #6 Ichimoku trend confirmation",
        timeframes=("4h", "1d"),
        regime="A trend starting from a range or continuing up.",
        setup="Close moves above the cloud on this bar (it wasn't above on the previous bar), "
        "Tenkan above Kijun, and the close above the close 26 bars ago (the lagging span, read "
        "without looking ahead).",
        entry="Market order at the next bar's open.",
        stop="Kijun minus 0.25 x ATR(14) when Kijun is below the close, no wider than 3 x ATR(14); "
        "otherwise entry - 2 x ATR(14).",
        target="3R fixed; exits earlier on a close under the Kijun.",
        filter="All three Ichimoku conditions must agree. " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("9/26/52 are the standard settings.",
                   "The lagging span is compared as close vs close 26 bars ago, so nothing is read from the future."),
    )
    eligible_regimes = TREND_RANGE
    reason = "broke above the Ichimoku cloud with Tenkan over Kijun"

    @classmethod
    def default_params(cls):
        return {**BASE, "conversion": 9, "base": 26, "span": 52, "max_stop_atr": 3.0, "target_r": 3.0}

    def setup(self, df, timeframe):
        p = self.params
        ich = ichimoku(df, p["conversion"], p["base"], p["span"])
        c = df["close"]
        return ((ich["above_cloud"] == 1) & (ich["above_cloud"].shift(1) != 1)
                & (ich["tenkan"] > ich["kijun"]) & (c > c.shift(p["base"])))

    def stop_series(self, df):
        p = self.params
        kijun = ichimoku(df, p["conversion"], p["base"], p["span"])["kijun"]
        a, c = atr(df), df["close"]
        structural = np.maximum(kijun - 0.25 * a, c - p["max_stop_atr"] * a)
        return structural.where(kijun < c, c - 2 * a)

    def exit_key(self):
        p = self.params
        cv, b, sp = p["conversion"], p["base"], p["span"]
        return _exit_rule(f"close_under_kijun:{cv}:{b}:{sp}",
                          lambda d: d["close"] < ichimoku(d, cv, b, sp)["kijun"])


class DonchianTrend(_Master):
    spec = _spec(
        name="donchian_trend",
        source=SOURCE + ": #8 Donchian trend following",
        timeframes=("4h", "1d"),
        regime="A new long-term high: trending up, or breaking out of a range.",
        setup="Close above the highest high of the previous 55 bars (the Turtle long entry).",
        entry="Market order at the next bar's open.",
        stop="Entry - 2 x ATR(14) (the Turtles' 2N).",
        target="No fixed target in practice (6R cap); the trade rides until a close below the "
        "lowest low of the previous 20 bars.",
        filter="One entry per breakout (the previous bar must not have broken out). " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("55/20 are the classic Turtle system 2 entry and exit.",
                   "The 6R cap only exists because every signal needs a target; the 20-bar exit does the work."),
    )
    eligible_regimes = TREND_RANGE
    reason = "new 55-bar high"

    @classmethod
    def default_params(cls):
        return {**BASE, "entry_n": 55, "exit_n": 20, "stop_atr": 2.0, "target_r": 6.0}

    def setup(self, df, timeframe):
        up = donchian(df, self.params["entry_n"])["upper"]
        c = df["close"]
        return (c > up) & ~(c.shift(1) > up.shift(1))

    def stop_series(self, df):
        return df["close"] - self.params["stop_atr"] * atr(df)

    def exit_key(self):
        n = self.params["exit_n"]
        return _exit_rule(f"donchian_low:{n}", lambda d: d["close"] < donchian(d, n)["lower"])


# ---- breakout and volatility (#9-#16) -----------------------------------------------------------


class DonchianBreakout(_Master):
    spec = _spec(
        name="donchian_breakout",
        source=SOURCE + ": #9 Donchian channel breakout",
        timeframes=("1h", "4h"),
        regime="Trending up or leaving a range.",
        setup="Close above the highest high of the previous 20 bars on at least 1.5x the average "
        "volume of the previous 20 bars.",
        entry="Market order at the next bar's open.",
        stop="The channel's middle, no wider than 2.5 x ATR(14).",
        target="2R fixed; exits earlier on a close below EMA(20).",
        filter="Volume must confirm the break. " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("A shorter channel and a fixed target than #8: a breakout trade, not a trend ride.",),
    )
    eligible_regimes = TREND_RANGE
    reason = "20-bar breakout on volume"

    @classmethod
    def default_params(cls):
        return {**BASE, "n": 20, "vol_mult": 1.5, "max_stop_atr": 2.5, "target_r": 2.0}

    def setup(self, df, timeframe):
        p = self.params
        ch = donchian(df, p["n"])
        c = df["close"]
        return ((c > ch["upper"]) & ~(c.shift(1) > ch["upper"].shift(1))
                & (relative_volume(df, p["n"]) >= p["vol_mult"]) & _volume_ok(df, p["n"]))

    def stop_series(self, df):
        p = self.params
        return np.maximum(donchian(df, p["n"])["mid"], df["close"] - p["max_stop_atr"] * atr(df))

    def exit_key(self):
        return "close<ema20"


class OpeningRangeBreakout(_Master):
    spec = _spec(
        name="opening_range_breakout",
        source=SOURCE + ": #12 Opening-range breakout",
        timeframes=("5m", "15m"),
        regime="Any orderly market (not high-volatility).",
        setup="The session starts at a set UTC time (default 00:00, the crypto daily open). The "
        "opening range is the high and low of its first 60 minutes. Setup: the first close above "
        "the range high, within 6 hours of the session start.",
        entry="Market order at the next bar's open.",
        stop="The middle of the opening range, no wider than 3 x ATR(14).",
        target="2R fixed; anything still open closes when the session ends (24 hours).",
        filter="Only on timeframes shorter than the range; one breakout per session. " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("`session_start` (\"HH:MM\" UTC) and `range_minutes` are settings: e.g. 13:30 for the US open.",),
    )
    eligible_regimes = TREND_RANGE
    reason = "broke above the opening range"

    @classmethod
    def default_params(cls):
        return {**BASE, "session_start": "00:00", "range_minutes": 60, "max_entry_minutes": 360,
                "max_stop_atr": 3.0, "target_r": 2.0}

    def _range(self, df, step):
        p = self.params
        hh, mm = (int(x) for x in str(p["session_start"]).split(":"))
        offset = (hh * 60 + mm) * 60_000
        ts = df["ts"].astype("int64")
        session = (ts - offset) // DAY_MS
        minutes = ((ts - offset) % DAY_MS) // 60_000
        in_range = minutes + step // 60_000 <= p["range_minutes"]
        hi = df["high"].where(in_range).groupby(session).cummax().groupby(session).ffill()
        lo = df["low"].where(in_range).groupby(session).cummin().groupby(session).ffill()
        return session, minutes, hi, lo

    def setup(self, df, timeframe):
        p = self.params
        step = TIMEFRAME_MS[timeframe]
        if step // 60_000 >= p["range_minutes"]:
            return pd.Series(False, index=df.index)
        session, minutes, hi, lo = self._range(df, step)
        after = (minutes >= p["range_minutes"]) & (minutes < p["max_entry_minutes"])
        brk = after & (df["close"] > hi)
        first = brk.astype(int).groupby(session).cumsum() == 1
        return brk & first & hi.notna() & lo.notna()

    def stop_series(self, df):
        """The opening range's middle (the bar spacing gives the timeframe)."""
        step = int(np.median(np.diff(df["ts"].to_numpy(np.int64)))) if len(df) > 1 else 60_000
        _s, _m, hi, lo = self._range(df, step)
        return np.maximum((hi + lo) / 2, df["close"] - self.params["max_stop_atr"] * atr(df))

    def exit_key(self):
        return f"time_stop={getattr(self, '_session_bars', 24)}"

    def generate(self, df, exchange, symbol, timeframe):
        self._session_bars = max(1, DAY_MS // TIMEFRAME_MS[timeframe])
        return super().generate(df, exchange, symbol, timeframe)


class VolumeBreakout(_Master):
    spec = _spec(
        name="volume_breakout",
        source=SOURCE + ": #13 Volume-confirmed breakout",
        timeframes=("1h", "4h"),
        regime="Trending up or leaving a range.",
        setup="Close above the highest high of the previous 20 bars, on more than 2x the average "
        "volume of the previous 20 bars, closing in the top third of the bar.",
        entry="Market order at the next bar's open.",
        stop="The breakout bar's low minus 0.25 x ATR(14), no wider than 2.5 x ATR(14).",
        target="2R fixed; exits earlier on a close below EMA(20).",
        filter="Volume must be more than double its average. " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("2x volume is the master prompt's threshold.",),
    )
    eligible_regimes = TREND_RANGE
    reason = "breakout on 2x volume"

    @classmethod
    def default_params(cls):
        return {**BASE, "n": 20, "vol_mult": 2.0, "max_stop_atr": 2.5, "target_r": 2.0}

    def setup(self, df, timeframe):
        p = self.params
        rng = (df["high"] - df["low"]).replace(0, np.nan)
        top_third = (df["close"] - df["low"]) / rng >= 2 / 3
        return ((df["close"] > donchian(df, p["n"])["upper"]) & (relative_volume(df, p["n"]) > p["vol_mult"])
                & top_third & _volume_ok(df, p["n"]))

    def stop_series(self, df):
        a = atr(df)
        return np.maximum(df["low"] - 0.25 * a, df["close"] - self.params["max_stop_atr"] * a)

    def exit_key(self):
        return "close<ema20"

    def features(self, df):
        return {"rel_volume": relative_volume(df, self.params["n"])}


class VwapReclaim(_Master):
    spec = _spec(
        name="vwap_reclaim",
        source=SOURCE + ": #15 VWAP reclaim",
        timeframes=("15m", "1h"),
        regime="Intraday, in an uptrend or a range.",
        setup="At least 3 bars in a row closed below the day's VWAP (UTC session), then a close "
        "back above it, with the close above EMA(200).",
        entry="Market order at the next bar's open.",
        stop="Lowest low of the last 5 bars minus 0.25 x ATR(14), no wider than 2.5 x ATR(14).",
        target="2R fixed; exits earlier on a close below the VWAP's lower 1-sd band.",
        filter="Only once 3 bars of the session are in. " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("Breakdowns (the other half of #15) are the short twin's job.",),
    )
    eligible_regimes = TREND_RANGE
    reason = "reclaimed the day's VWAP"

    @classmethod
    def default_params(cls):
        return {**BASE, "below_bars": 3, "trend": 200, "swing_bars": 5, "max_stop_atr": 2.5, "target_r": 2.0}

    def setup(self, df, timeframe):
        p = self.params
        vb = vwap_bands(df, anchor="D")
        c = df["close"]
        below = (c < vb["vwap"]).astype(int)
        run = below.shift(1).rolling(p["below_bars"], min_periods=p["below_bars"]).sum() == p["below_bars"]
        return run & (c > vb["vwap"]) & (vb["bars"] > p["below_bars"]) & (c > ema(c, p["trend"]))

    def stop_series(self, df):
        return _swing_stop(df, self.params["swing_bars"], 0.25, self.params["max_stop_atr"])

    def exit_key(self):
        return _exit_rule("vwap_lower1", lambda d: d["close"] < vwap_bands(d, anchor="D")["lower1"])


class BreakoutRetest(_Master):
    spec = _spec(
        name="breakout_retest",
        source=SOURCE + ": #16 Support/resistance breakout and retest",
        timeframes=("1h", "4h"),
        regime="Trending up or leaving a range.",
        setup="A close above the previous 20 bars' high (the old resistance), then within 10 bars "
        "a pullback whose low comes back to within 0.3% of that level and closes above it on an "
        "up bar (resistance turned support).",
        entry="Market order at the next bar's open after the retest bar.",
        stop="The level minus 1 x ATR(14).",
        target="2R fixed; exits earlier on a close below EMA(20).",
        filter="One retest per breakout; a close back under the level cancels it. " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("The retest must hold on a closing basis; a wick through is fine.",),
    )
    eligible_regimes = TREND_RANGE
    reason = "breakout level held on the retest"

    @classmethod
    def default_params(cls):
        return {**BASE, "n": 20, "window": 10, "tol": 0.003, "stop_atr": 1.0, "target_r": 2.0}

    def _levels(self, df):
        p = self.params
        up = donchian(df, p["n"])["upper"].to_numpy(float)
        c, lo, o = (df[k].to_numpy(float) for k in ("close", "low", "open"))
        level = np.full(len(df), np.nan)
        hit = np.zeros(len(df), dtype=bool)
        active, since = np.nan, 0
        for i in range(1, len(df)):
            if np.isfinite(active):
                since += 1
                if c[i] < active or since > p["window"]:
                    active = np.nan
                elif since > 1 and lo[i] <= active * (1 + p["tol"]) and c[i] > active and c[i] > o[i]:
                    hit[i], level[i] = True, active
                    active = np.nan
                    continue
            if np.isfinite(up[i]) and c[i] > up[i] and not (c[i - 1] > up[i - 1]):
                active, since = up[i], 0
        return pd.Series(hit, index=df.index), pd.Series(level, index=df.index)

    def _cached(self, df):
        key = _df_key(df)
        if getattr(self, "_memo", (None,))[0] != key:
            self._memo = (key, self._levels(df))
        return self._memo[1]

    def setup(self, df, timeframe):
        return self._cached(df)[0]

    def stop_series(self, df):
        level = self._cached(df)[1].ffill()
        return level - self.params["stop_atr"] * atr(df)

    def exit_key(self):
        return "close<ema20"


# ---- mean reversion (#17-#24) -------------------------------------------------------------------


class RsiReversal(_Master):
    spec = _spec(
        name="rsi_reversal",
        source=SOURCE + ": #17 RSI reversal with rejection confirmation",
        timeframes=("1h", "4h"),
        regime="A range, or a dip inside an uptrend.",
        setup="RSI(14) was below 30 within the last 3 bars and crosses back above 30 on this bar, "
        "and the bar rejects the low: an up bar whose lower wick is at least half its body, or "
        "that closes above the previous bar's high.",
        entry="Market order at the next bar's open.",
        stop="Lowest low of the last 5 bars minus 0.25 x ATR(14).",
        target="SMA(20) (the mean) when at least 1R away, else no trade; time stop after 20 bars.",
        filter="Skip in a strong downtrend (ADX(14) > 35 with -DI above +DI). " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("Oversold alone is not a signal: the cross back up and the wick are the confirmation.",),
    )
    eligible_regimes = TREND_RANGE
    reason = "RSI back above 30 with a rejection wick"

    @classmethod
    def default_params(cls):
        return {**BASE, "n": 14, "low": 30.0, "recent": 3, "mean": 20, "swing_bars": 5, "downtrend_adx": 35.0,
                "min_rr": 1.0, "time_stop_bars": 20, "target_r": None}

    def setup(self, df, timeframe):
        p = self.params
        r = rsi(df["close"], p["n"])
        o, c, lo = df["open"], df["close"], df["low"]
        was_low = (r.shift(1) < p["low"]).astype(int).rolling(p["recent"], min_periods=1).max() > 0
        wick = np.minimum(o, c) - lo
        reject = (wick >= 0.5 * (c - o)) | (c > df["high"].shift(1))
        return (was_low & (r > p["low"]) & (r.shift(1) <= p["low"]) & (c > o) & reject
                & ~_strong_downtrend(df, p["downtrend_adx"]))

    def stop_series(self, df):
        return _swing_stop(df, self.params["swing_bars"], 0.25)

    def targets(self, df):
        return _mean_target(sma(df["close"], self.params["mean"]), self.params["min_rr"])

    def exit_key(self):
        return f"time_stop={self.params['time_stop_bars']}"


class StochRsiReversal(_Master):
    spec = _spec(
        name="stoch_rsi_reversal",
        source=SOURCE + ": #18 Stochastic RSI reversal",
        timeframes=("1h", "4h"),
        regime="A range, or a dip inside an uptrend.",
        setup="Stochastic RSI %K crosses above %D while both were below 0.2 on the previous bar, "
        "and the close is above SMA(200) or ADX(14) is under 20 (a range).",
        entry="Market order at the next bar's open.",
        stop="Lowest low of the last 5 bars minus 0.25 x ATR(14).",
        target="2R fixed; exits earlier when RSI(14) goes above 70; time stop after 15 bars.",
        filter="No trade in a strong downtrend. " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("The %K/%D cross is the confirmation; an oversold reading alone isn't traded.",),
    )
    eligible_regimes = TREND_RANGE
    reason = "Stoch RSI crossed up from oversold"

    @classmethod
    def default_params(cls):
        return {**BASE, "oversold": 0.2, "trend": 200, "range_adx": 20.0, "downtrend_adx": 35.0,
                "swing_bars": 5, "time_stop_bars": 15, "target_r": 2.0}

    def setup(self, df, timeframe):
        p = self.params
        s = stoch_rsi(df["close"])
        k, d = s["k"], s["d"]
        cross = (k > d) & (k.shift(1) <= d.shift(1)) & (k.shift(1) < p["oversold"]) & (d.shift(1) < p["oversold"])
        ctx = (df["close"] > sma(df["close"], p["trend"])) | (adx(df) < p["range_adx"])
        return cross & ctx & ~_strong_downtrend(df, p["downtrend_adx"])

    def stop_series(self, df):
        return _swing_stop(df, self.params["swing_bars"], 0.25)

    def exit_key(self):
        return f"rsi>70|time_stop={self.params['time_stop_bars']}"


class BollingerMeanReversion(_Master):
    spec = _spec(
        name="bollinger_mean_reversion",
        source=SOURCE + ": #19 Bollinger Band mean reversion",
        timeframes=("1h", "4h"),
        regime="A range: ADX(14) under 25.",
        setup="The previous bar closed below the lower Bollinger Band (20, 2); this bar closes back "
        "inside it, up on the bar.",
        entry="Market order at the next bar's open.",
        stop="Lowest low of the last 3 bars minus 0.25 x ATR(14).",
        target="The middle band when at least 1R away, else no trade; time stop after 20 bars.",
        filter="No trade when ADX(14) >= 25 (a trend can ride the band down). " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("Waiting for the close back inside avoids catching a falling band walk.",),
    )
    eligible_regimes = RANGE
    reason = "back inside the lower Bollinger Band"

    @classmethod
    def default_params(cls):
        return {**BASE, "n": 20, "k": 2.0, "adx_max": 25.0, "swing_bars": 3, "min_rr": 1.0,
                "time_stop_bars": 20, "target_r": None}

    def setup(self, df, timeframe):
        p = self.params
        b = bollinger(df["close"], p["n"], p["k"])
        c = df["close"]
        return ((c.shift(1) < b["lower"].shift(1)) & (c > b["lower"]) & (c > df["open"]) & (adx(df) < p["adx_max"]))

    def stop_series(self, df):
        return _swing_stop(df, self.params["swing_bars"], 0.25)

    def targets(self, df):
        p = self.params
        return _mean_target(bollinger(df["close"], p["n"], p["k"])["mid"], p["min_rr"])

    def exit_key(self):
        return f"time_stop={self.params['time_stop_bars']}"


class VwapMeanReversion(_Master):
    spec = _spec(
        name="vwap_mean_reversion",
        source=SOURCE + ": #20 VWAP mean reversion",
        timeframes=("15m", "1h"),
        regime="An intraday range: ADX(14) under 25.",
        setup="The previous bar closed below the day's VWAP lower 2-sd band; this bar closes back "
        "above it, up on the bar.",
        entry="Market order at the next bar's open.",
        stop="Lowest low of the last 3 bars minus 0.25 x ATR(14).",
        target="The VWAP when at least 1R away, else no trade; closes when the session ends.",
        filter="Only once 5 bars of the session are in; no trade when ADX(14) >= 25. " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("The 2-sd band marks a stretch; the VWAP is the session's fair price.",),
    )
    eligible_regimes = RANGE
    reason = "back above the VWAP -2sd band"

    @classmethod
    def default_params(cls):
        return {**BASE, "min_bars": 5, "adx_max": 25.0, "swing_bars": 3, "min_rr": 1.0, "target_r": None}

    def setup(self, df, timeframe):
        p = self.params
        vb = vwap_bands(df, anchor="D")
        c = df["close"]
        return ((c.shift(1) < vb["lower2"].shift(1)) & (c > vb["lower2"]) & (c > df["open"])
                & (vb["bars"] >= p["min_bars"]) & (adx(df) < p["adx_max"]))

    def stop_series(self, df):
        return _swing_stop(df, self.params["swing_bars"], 0.25)

    def targets(self, df):
        return _mean_target(vwap_bands(df, anchor="D")["vwap"], self.params["min_rr"])

    def generate(self, df, exchange, symbol, timeframe):
        self._session_bars = max(1, DAY_MS // TIMEFRAME_MS[timeframe])
        return super().generate(df, exchange, symbol, timeframe)

    def exit_key(self):
        return f"time_stop={getattr(self, '_session_bars', 24)}"


class FibonacciReaction(_Master):
    spec = _spec(
        name="fibonacci_reaction",
        source=SOURCE + ": #22 Fibonacci retracement reaction",
        timeframes=("1h", "4h"),
        regime="A pullback in an uptrend, or a range.",
        setup="Over the last 100 bars the lowest low came before the highest high (an up leg of at "
        "least 4 x ATR(14)). The bar's low reaches the 0.5-0.786 retracement and it closes back "
        "above the 0.618 level, up on the bar.",
        entry="Market order at the next bar's open.",
        stop="The 0.786 level minus 0.25 x ATR(14).",
        target="The leg's high when at least 1R away, else no trade; time stop after 30 bars.",
        filter="Legs smaller than 4 ATRs are ignored. " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("The swing is found automatically from the trailing window, never by hand.",),
    )
    eligible_regimes = TREND_RANGE
    reason = "bounced at the 0.5-0.618 retracement"

    @classmethod
    def default_params(cls):
        return {**BASE, "lookback": 100, "min_leg_atr": 4.0, "min_rr": 1.0, "time_stop_bars": 30, "target_r": None}

    def _levels(self, df):
        p = self.params
        hi, lo = df["high"].to_numpy(float), df["low"].to_numpy(float)
        c, o = df["close"].to_numpy(float), df["open"].to_numpy(float)
        a = atr(df).to_numpy(float)
        n, lb = len(df), p["lookback"]
        hit = np.zeros(n, dtype=bool)
        stop, top = np.full(n, np.nan), np.full(n, np.nan)
        for t in range(lb, n):
            if not (c[t] > o[t]) or not np.isfinite(a[t]):
                continue
            h_seg, l_seg = hi[t - lb:t], lo[t - lb:t]
            hi_i, lo_i = int(np.argmax(h_seg)), int(np.argmin(l_seg))
            H, L = h_seg[hi_i], l_seg[lo_i]
            span = H - L
            if hi_i <= lo_i or span < p["min_leg_atr"] * a[t]:
                continue
            f50, f618, f786 = H - 0.5 * span, H - 0.618 * span, H - 0.786 * span
            if f786 <= lo[t] <= f50 and c[t] > f618:
                hit[t], stop[t], top[t] = True, f786 - 0.25 * a[t], H
        return (pd.Series(hit, index=df.index), pd.Series(stop, index=df.index),
                pd.Series(top, index=df.index))

    def _cached(self, df):
        key = _df_key(df)
        if getattr(self, "_memo", (None,))[0] != key:
            self._memo = (key, self._levels(df))
        return self._memo[1]

    def setup(self, df, timeframe):
        return self._cached(df)[0]

    def stop_series(self, df):
        return self._cached(df)[1]

    def targets(self, df):
        return _mean_target(self._cached(df)[2], self.params["min_rr"])

    def exit_key(self):
        return f"time_stop={self.params['time_stop_bars']}"


def _value_areas(df: pd.DataFrame, lookback: int, bins: int, share: float, only: np.ndarray) -> tuple:
    """POC, VAH and VAL of the ``lookback`` bars *before* each bar flagged in ``only``."""
    n = len(df)
    tp = ((df["high"] + df["low"] + df["close"]) / 3).to_numpy(float)
    hi, lo, vol = (df[k].to_numpy(float) for k in ("high", "low", "volume"))
    poc, vah, val = (np.full(n, np.nan) for _ in range(3))
    for t in np.flatnonzero(only):
        if t < lookback:
            continue
        s = slice(t - lookback, t)
        edges = np.linspace(lo[s].min(), hi[s].max(), bins + 1)
        if edges[-1] <= edges[0]:
            continue
        idx = np.clip(np.digitize(tp[s], edges) - 1, 0, bins - 1)
        v = np.bincount(idx, weights=vol[s], minlength=bins)
        total = v.sum()
        if total <= 0:
            continue
        p = int(np.argmax(v))
        a_, b_, inside = p, p, v[p]
        while inside < share * total and (a_ > 0 or b_ < bins - 1):
            below = v[a_ - 1] if a_ > 0 else -1.0
            above = v[b_ + 1] if b_ < bins - 1 else -1.0
            if above >= below:
                b_ += 1
                inside += above
            else:
                a_ -= 1
                inside += below
        poc[t], vah[t], val[t] = (edges[p] + edges[p + 1]) / 2, edges[b_ + 1], edges[a_]
    return poc, vah, val


class ValueAreaReversion(_Master):
    spec = _spec(
        name="value_area_reversion",
        source=SOURCE + ": #23 Volume-profile value-area reversion",
        timeframes=("1h", "4h"),
        regime="A range (balanced market).",
        setup="Volume profile of the previous 100 bars (24 price buckets, 70% value area). The "
        "previous bar closed below the value area low; this bar closes back above it, up on the bar.",
        entry="Market order at the next bar's open.",
        stop="Lowest low of the last 3 bars minus 0.25 x ATR(14).",
        target="The point of control when at least 1R away, else no trade; time stop after 30 bars.",
        filter="The profile only uses bars before the signal bar. " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("Re-entering value after a failed break below it tends to rotate to the POC (auction theory).",),
    )
    eligible_regimes = RANGE
    reason = "back inside the value area"

    @classmethod
    def default_params(cls):
        return {**BASE, "lookback": 100, "bins": 24, "share": 0.70, "swing_bars": 3, "min_rr": 1.0,
                "time_stop_bars": 30, "target_r": None}

    def _areas(self, df):
        p = self.params
        c = df["close"].to_numpy(float)
        up = np.r_[False, c[1:] > c[:-1]] & (c > df["open"].to_numpy(float))
        cand = up | np.r_[up[1:], False]  # the bar before a candidate needs its VAL too
        return _value_areas(df, p["lookback"], p["bins"], p["share"], cand)

    def _cached(self, df):
        key = _df_key(df)
        if getattr(self, "_memo", (None,))[0] != key:
            self._memo = (key, tuple(pd.Series(x, index=df.index) for x in self._areas(df)))
        return self._memo[1]

    def setup(self, df, timeframe):
        _poc, _vah, val = self._cached(df)
        c = df["close"]
        return ((c.shift(1) < val.shift(1)) & (c > val) & (c > df["open"]) & (c > c.shift(1))
                & _volume_ok(df, 20))

    def stop_series(self, df):
        return _swing_stop(df, self.params["swing_bars"], 0.25)

    def targets(self, df):
        return _mean_target(self._cached(df)[0], self.params["min_rr"])

    def exit_key(self):
        return f"time_stop={self.params['time_stop_bars']}"


class RangeSupportBounce(_Master):
    spec = _spec(
        name="range_support_bounce",
        source=SOURCE + ": #24 Range-bound support/resistance trading",
        timeframes=("1h", "4h"),
        regime="A range: ADX(14) under 20 and a channel 3-12 ATRs wide.",
        setup="Channel = highest high and lowest low of the previous 50 bars. The bar's low is in "
        "the bottom 15% of the channel and it closes up on the bar, above the previous close.",
        entry="Market order at the next bar's open.",
        stop="The channel low minus 0.5 x ATR(14).",
        target="The channel middle when at least 1R away, else no trade; time stop after 30 bars.",
        filter="No trade when ADX(14) >= 20 or the channel is too narrow or too wide. " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("Selling at the top of the range is the short twin's job.",),
    )
    eligible_regimes = ("range",)
    reason = "bounced off range support"

    @classmethod
    def default_params(cls):
        return {**BASE, "n": 50, "adx_max": 20.0, "min_width_atr": 3.0, "max_width_atr": 12.0, "zone": 0.15,
                "stop_pad_atr": 0.5, "min_rr": 1.0, "time_stop_bars": 30, "target_r": None}

    def setup(self, df, timeframe):
        p = self.params
        ch, a = donchian(df, p["n"]), atr(df)
        width = (ch["upper"] - ch["lower"]) / a
        c = df["close"]
        in_zone = df["low"] <= ch["lower"] + p["zone"] * (ch["upper"] - ch["lower"])
        return (in_zone & (c > df["open"]) & (c > c.shift(1)) & (adx(df) < p["adx_max"])
                & width.between(p["min_width_atr"], p["max_width_atr"]))

    def stop_series(self, df):
        return donchian(df, self.params["n"])["lower"] - self.params["stop_pad_atr"] * atr(df)

    def targets(self, df):
        return _mean_target(donchian(df, self.params["n"])["mid"], self.params["min_rr"])

    def exit_key(self):
        return f"time_stop={self.params['time_stop_bars']}"


# ---- momentum and price action (#25-#32) --------------------------------------------------------


class MacdHistogramMomentum(_Master):
    spec = _spec(
        name="macd_histogram_momentum",
        source=SOURCE + ": #25 MACD histogram momentum",
        timeframes=("1h", "4h"),
        regime="Uptrend or range turning up.",
        setup="The MACD(12, 26, 9) histogram turns positive after at least 5 negative bars and has "
        "risen for 2 bars in a row, close above EMA(100).",
        entry="Market order at the next bar's open.",
        stop="Lowest low of the last 10 bars minus 0.25 x ATR(14), no wider than 3 x ATR(14).",
        target="2R fixed; exits earlier when the histogram turns negative again.",
        filter="Only above EMA(100). " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("The histogram measures the change in momentum; the run of negative bars avoids chop around zero.",),
    )
    eligible_regimes = TREND_RANGE
    reason = "MACD histogram turned positive"

    @classmethod
    def default_params(cls):
        return {**BASE, "neg_bars": 5, "trend": 100, "swing_bars": 10, "max_stop_atr": 3.0, "target_r": 2.0}

    def setup(self, df, timeframe):
        p = self.params
        h = macd(df["close"])["hist"]
        negs = (h.shift(1) < 0).astype(int).rolling(p["neg_bars"], min_periods=p["neg_bars"]).sum() == p["neg_bars"]
        return (h > 0) & negs & (h > h.shift(1)) & (h.shift(1) > h.shift(2)) & (df["close"] > ema(df["close"], p["trend"]))

    def stop_series(self, df):
        return _swing_stop(df, self.params["swing_bars"], 0.25, self.params["max_stop_atr"])

    def exit_key(self):
        return _exit_rule("macd_hist_negative", lambda d: macd(d["close"])["hist"] < 0)


class RsiDivergence(_Master):
    spec = _spec(
        name="rsi_divergence",
        source=SOURCE + ": #26 RSI divergence (regular bullish)",
        timeframes=("1h", "4h"),
        regime="A range or the end of a pullback.",
        setup="A regular bullish divergence: the newest confirmed swing low (2 bars each side) is "
        "lower than the previous one within 30 bars, while RSI(14) made a higher low. Signalled "
        "on the bar that confirms the swing, if RSI is below 50 and the bar closes up.",
        entry="Market order at the next bar's open.",
        stop="Lowest low of the last 5 bars minus 0.25 x ATR(14).",
        target="2R fixed; exits earlier when RSI(14) goes above 70.",
        filter="No trade in a strong downtrend (ADX > 35 with -DI leading). " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("RSI replaces the CCI of the older `cci_divergence` candidate, as the master prompt asks.",
                   "Bearish divergence is the short twin's job."),
    )
    eligible_regimes = TREND_RANGE
    reason = "bullish RSI divergence"

    @classmethod
    def default_params(cls):
        return {**BASE, "n": 14, "lookback": 30, "rsi_max": 50.0, "downtrend_adx": 35.0, "swing_bars": 5,
                "target_r": 2.0}

    def setup(self, df, timeframe):
        p = self.params
        r = rsi(df["close"], p["n"])
        div = bullish_divergence(df, r, p["lookback"]) > 0
        return div & (r < p["rsi_max"]) & (df["close"] > df["open"]) & ~_strong_downtrend(df, p["downtrend_adx"])

    def stop_series(self, df):
        return _swing_stop(df, self.params["swing_bars"], 0.25)

    def exit_key(self):
        return "rsi>70"


class CandlestickReversal(_Master):
    spec = _spec(
        name="candlestick_reversal",
        source=SOURCE + ": #30 Candlestick reversal patterns",
        timeframes=("1h", "4h"),
        regime="A range or a pullback in an uptrend.",
        setup="A bullish engulfing bar or a hammer whose low is within 0.5 x ATR(14) of the "
        "previous 20 bars' low (at support), with RSI(14) under 45.",
        entry="Market order at the next bar's open.",
        stop="Lowest low of the pattern (2 bars) minus 0.25 x ATR(14).",
        target="2R fixed; time stop after 20 bars.",
        filter="Patterns away from support are ignored. " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("A pattern means little in the middle of nowhere; support gives it a reason.",),
    )
    eligible_regimes = TREND_RANGE
    reason = "reversal candle at support"

    @classmethod
    def default_params(cls):
        return {**BASE, "support_n": 20, "near_atr": 0.5, "rsi_max": 45.0, "time_stop_bars": 20, "target_r": 2.0}

    def setup(self, df, timeframe):
        p = self.params
        pat = candle_patterns(df)
        support = df["low"].shift(1).rolling(p["support_n"], min_periods=p["support_n"]).min()
        near = (df["low"] - support).abs() <= p["near_atr"] * atr(df)
        return ((pat["bullish_engulfing"] > 0) | (pat["hammer"] > 0)) & near & (rsi(df["close"]) < p["rsi_max"])

    def stop_series(self, df):
        return _swing_stop(df, 2, 0.25)

    def exit_key(self):
        return f"time_stop={self.params['time_stop_bars']}"


class StructureBreak(_Master):
    spec = _spec(
        name="structure_break",
        source=SOURCE + ": #31 Market structure (higher high / higher low break)",
        timeframes=("1h", "4h"),
        regime="An uptrend in structure.",
        setup="Market structure is up (the last swing high and swing low are both higher than the "
        "ones before) and the close breaks above the last confirmed swing high.",
        entry="Market order at the next bar's open.",
        stop="The last confirmed swing low minus 0.25 x ATR(14), no wider than 3 x ATR(14).",
        target="2.5R fixed; exits earlier when structure turns down (lower high and lower low).",
        filter="Swings are fractals confirmed 2 bars later (no look-ahead). " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("The break of the swing high is the confirmation that the higher low held.",),
    )
    eligible_regimes = TREND_RANGE
    reason = "broke the last swing high in an up structure"

    @classmethod
    def default_params(cls):
        return {**BASE, "left": 2, "right": 2, "max_stop_atr": 3.0, "target_r": 2.5}

    def setup(self, df, timeframe):
        p = self.params
        sw = swing_points(df, p["left"], p["right"])
        last_high = sw["swing_high"].ffill()
        c = df["close"]
        return ((market_structure(df, p["left"], p["right"]) == 1) & (c > last_high)
                & (c.shift(1) <= last_high.shift(1)))

    def stop_series(self, df):
        p = self.params
        last_low = swing_points(df, p["left"], p["right"])["swing_low"].ffill()
        a = atr(df)
        return np.maximum(last_low - 0.25 * a, df["close"] - p["max_stop_atr"] * a)

    def exit_key(self):
        lf, rt = self.params["left"], self.params["right"]
        return _exit_rule(f"structure_down:{lf}:{rt}", lambda d: market_structure(d, lf, rt) == -1)


class LiquiditySweep(_Master):
    spec = _spec(
        name="liquidity_sweep",
        source=SOURCE + ": #32 Liquidity-sweep reversal",
        timeframes=("1h", "4h"),
        regime="A range or a pullback in an uptrend.",
        setup="The bar's low trades at least 0.1 x ATR(14) below the last confirmed swing low "
        "(stops below it get taken), then the bar closes back above that swing low, up on the bar, "
        "on above-average volume.",
        entry="Market order at the next bar's open.",
        stop="The sweep bar's low minus 0.25 x ATR(14).",
        target="2R fixed; time stop after 20 bars.",
        filter="The close must reclaim the level on the same bar. " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("A sweep that closes back inside is a failed breakdown: trapped sellers fuel the move up.",),
    )
    eligible_regimes = TREND_RANGE
    reason = "swept the swing low and reclaimed it"

    @classmethod
    def default_params(cls):
        return {**BASE, "left": 2, "right": 2, "sweep_atr": 0.1, "time_stop_bars": 20, "target_r": 2.0}

    def setup(self, df, timeframe):
        p = self.params
        level = swing_points(df, p["left"], p["right"])["swing_low"].ffill().shift(1)
        a = atr(df)
        c = df["close"]
        return ((df["low"] < level - p["sweep_atr"] * a) & (c > level) & (c > df["open"])
                & (relative_volume(df, 20) > 1.0))

    def stop_series(self, df):
        return df["low"] - 0.25 * atr(df)

    def exit_key(self):
        return f"time_stop={self.params['time_stop_bars']}"


# ---- crypto-specific (#34) and machine learning (#39) ---------------------------------------------


class FundingTrendFilter(_Master):
    spec = _spec(
        name="funding_trend_filter",
        source=SOURCE + ": #34 Funding-rate trend filter",
        timeframes=("1h", "4h"),
        regime="Uptrend with funding not crowded.",
        setup="EMA(20) > EMA(50) > EMA(200); the bar dips to EMA(20) (low at or below it) and closes "
        "back above it, up on the bar. The perpetual's funding rate must be known and below "
        "+0.03% per 8h: no long when longs are already paying a lot to hold.",
        entry="Market order at the next bar's open.",
        stop="Lower of the 5-bar low and entry - 2 x ATR(14).",
        target="2R fixed; exits earlier on a close below EMA(50).",
        filter="No trade without a fresh funding value, or when funding >= +0.03% per 8h. " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("Funding here is a veto on a plain trend pullback, which is what 'trend filter' means.",),
    )
    eligible_regimes = TREND
    needs_context = True
    reason = "trend pullback with funding not crowded"

    @classmethod
    def default_params(cls):
        return {**BASE, "funding_max": 0.0003, "swing_bars": 5, "stop_atr": 2.0, "target_r": 2.0}

    def setup(self, df, timeframe):
        if "funding_rate" not in df:
            return pd.Series(False, index=df.index)
        c = df["close"]
        e20 = ema(c, 20)
        funding = df["funding_rate"].astype(float)
        return ((ema_stack(c) == 1) & (df["low"] <= e20) & (c > e20) & (c > df["open"])
                & funding.notna() & (funding < self.params["funding_max"]))

    def stop_series(self, df):
        p = self.params
        return np.minimum(df["low"].rolling(p["swing_bars"], min_periods=p["swing_bars"]).min(),
                          df["close"] - p["stop_atr"] * atr(df))

    def exit_key(self):
        return "close<ema50"

    def generate(self, df, exchange, symbol, timeframe):
        if "funding_rate" not in df:
            return []
        return super().generate(df, exchange, symbol, timeframe)


class MlClassifier(_Master):
    spec = _spec(
        name="ml_classifier",
        source=SOURCE + ": #39 ML signal classification (logistic regression baseline)",
        timeframes=("1h", "4h"),
        regime="Any orderly market (not high-volatility).",
        setup="A logistic regression, retrained every 250 bars on past bars only, estimates the "
        "chance that price reaches +3 x ATR before -1.5 x ATR within 24 bars. Features: RSI, MACD "
        "histogram, ADX, Bollinger %B, distance from EMA(50), 1/5/20-bar returns, ATR % of price, "
        "relative volume. Setup: that chance crosses above 0.70.",
        entry="Market order at the next bar's open.",
        stop="Entry - 1.5 x ATR(14) (the same barrier the model was trained on).",
        target="2R (+3 x ATR); time stop after 24 bars.",
        filter="No prediction until 600 labelled bars exist; a row is only used for training once "
        "its 24-bar outcome is known (no leakage). " + NO_TRADE,
        size="0.5% of equity at risk.",
        decisions=("An interpretable baseline: a gradient-boosted model would have to beat it to replace it.",
                   "The model makes signals here; the separate AI filter (ai/) only judges other strategies' signals."),
    )
    eligible_regimes = TREND_RANGE
    reason = "classifier probability crossed 0.70"

    @classmethod
    def default_params(cls):
        return {**BASE, "horizon": 24, "up_atr": 3.0, "down_atr": 1.5, "min_train": 600, "retrain": 250,
                "threshold": 0.7, "target_r": 2.0}

    @staticmethod
    def feature_frame(df: pd.DataFrame) -> pd.DataFrame:
        c = df["close"]
        a = atr(df)
        b = bollinger(c)
        return pd.DataFrame({
            "rsi": (rsi(c) - 50) / 50, "macd_hist": macd(c)["hist"] / a, "adx": adx(df) / 100,
            "pct_b": (c - b["lower"]) / (b["upper"] - b["lower"]).replace(0, np.nan),
            "ema_dist": (c - ema(c, 50)) / a, "ret1": c.pct_change(1), "ret5": c.pct_change(5),
            "ret20": c.pct_change(20), "atr_pct": a / c, "rel_vol": relative_volume(df, 20),
        }, index=df.index)

    def labels(self, df: pd.DataFrame) -> pd.Series:
        """1 when the upper barrier is hit first within ``horizon`` bars, 0 otherwise; NaN for the
        last ``horizon`` bars, whose outcome isn't known yet."""
        p = self.params
        hi, lo, c = (df[k].to_numpy(float) for k in ("high", "low", "close"))
        a = atr(df).to_numpy(float)
        n, h = len(df), p["horizon"]
        out = np.full(n, np.nan)
        for t in range(n - h):
            if not np.isfinite(a[t]):
                continue
            up, dn = c[t] + p["up_atr"] * a[t], c[t] - p["down_atr"] * a[t]
            res = 0.0
            for j in range(t + 1, t + h + 1):
                if lo[j] <= dn:
                    break
                if hi[j] >= up:
                    res = 1.0
                    break
            out[t] = res
        return pd.Series(out, index=df.index)

    def probabilities(self, df: pd.DataFrame) -> pd.Series:
        """Walk-forward out-of-sample probability for every bar (NaN before the first model)."""
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        p = self.params
        X = self.feature_frame(df).replace([np.inf, -np.inf], np.nan)
        y = self.labels(df)
        ok = X.notna().all(axis=1).to_numpy()
        Xv, yv = X.to_numpy(float), y.to_numpy(float)
        prob = np.full(len(df), np.nan)
        h = p["horizon"]
        start = p["min_train"] + h
        for t0 in range(start, len(df), p["retrain"]):
            train = np.arange(0, t0 - h)  # labels known by bar t0 (their horizon has passed)
            train = train[ok[train] & np.isfinite(yv[train])]
            if len(train) < p["min_train"] or len(set(yv[train])) < 2:
                continue
            model = make_pipeline(StandardScaler(), LogisticRegression(max_iter=500))
            model.fit(Xv[train], yv[train])
            block = np.arange(t0, min(len(df), t0 + p["retrain"]))
            block = block[ok[block]]
            if len(block):
                prob[block] = model.predict_proba(Xv[block])[:, 1]
        return pd.Series(prob, index=df.index)

    def setup(self, df, timeframe):
        key = _df_key(df)
        if getattr(self, "_memo", (None,))[0] != key:
            self._memo = (key, self.probabilities(df))
        pr = self._memo[1]
        th = self.params["threshold"]
        return (pr >= th) & (pr.shift(1) < th)

    def stop_series(self, df):
        return df["close"] - self.params["down_atr"] * atr(df)

    def exit_key(self):
        return f"time_stop={self.params['horizon']}"


MASTER_STRATEGIES = (
    EmaCrossover, SmaCrossover, MacdTrend, AdxTrend, IchimokuTrend, DonchianTrend, DonchianBreakout,
    OpeningRangeBreakout, VolumeBreakout, VwapReclaim, BreakoutRetest, RsiReversal, StochRsiReversal,
    BollingerMeanReversion, VwapMeanReversion, FibonacciReaction, ValueAreaReversion, RangeSupportBounce,
    MacdHistogramMomentum, RsiDivergence, CandlestickReversal, StructureBreak, LiquiditySweep,
    FundingTrendFilter, MlClassifier,
)
