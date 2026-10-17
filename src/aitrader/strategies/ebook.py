"""Strategies from "Crypto Trading For Beginner Traders" (Finance Illustrated, 2021) and the
Binance Academy trading eBook, written as explicit rules.

The ebook describes its strategies in general terms only: no indicators, levels or exits. Each
class below records the written decision that turns that wording into a testable rule, so the
same chart always gives the same signal. Leverage and shorting from the ebook are left out on
purpose: the roadmap's pilot is spot only, no leverage. See docs/strategies.md.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import (
    adx,
    atr,
    atr_percentile,
    bollinger,
    ema,
    ema_stack,
    market_structure,
    parabolic_sar,
    rsi,
    sma,
    stoch_rsi,
    vwap,
)
from ..models import TIMEFRAME_MS, Side
from .base import Signal, Strategy, StrategySpec


def _close_ts(df: pd.DataFrame, i: int, timeframe: str) -> int:
    return int(df["ts"].iloc[i]) + TIMEFRAME_MS[timeframe]


class TrendPullback(Strategy):
    spec = StrategySpec(
        name="trend_pullback",
        source='Ebook #2 "Trend Trading"',
        timeframes=("1h", "4h"),
        setup="Uptrend: EMA20 > EMA50 > EMA200, market structure making higher highs and higher "
        "lows, ADX(14) >= 20.",
        entry="Price pulls back to touch EMA20 (bar low <= EMA20) and the bar closes back above "
        "EMA20 as a bullish candle. Market order at the next bar's open.",
        stop="Lower of the 10-bar low and entry - 2 x ATR(14).",
        target="2R fixed; the backtest also scores a trailing exit on a close below EMA50.",
        filter="Skip if the trend looks late: RSI(14) > 70 or close more than 3 x ATR above "
        "EMA50. Skip when ATR percentile > 95% (disorderly market).",
        size="0.5% of equity at risk; units = risk / (entry - stop).",
        decisions=(
            '"Identify upward trends" = EMA20>EMA50>EMA200 plus higher highs and higher lows.',
            '"As long as the trend isn\'t about to end" = RSI <= 70 and price within 3 ATR of EMA50.',
            '"Top gainers and losers" scanning is left to the symbol list, not coded here.',
            "Long only. Downtrend (short) signals are off until futures are allowed.",
        ),
    )

    @classmethod
    def default_params(cls):
        return {"fast": 20, "mid": 50, "slow": 200, "adx_min": 20.0, "rsi_max": 70.0,
                "max_ext_atr": 3.0, "stop_atr": 2.0, "target_r": 2.0, "atr_pct_max": 0.95,
                "risk_pct": 0.005}

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        close = df["close"]
        e_fast, e_mid = ema(close, p["fast"]), ema(close, p["mid"])
        stack = ema_stack(close, (p["fast"], p["mid"], p["slow"]))
        struct = market_structure(df)
        strength = adx(df)
        r = rsi(close)
        a = atr(df)
        a_pct = atr_percentile(df)
        srsi = stoch_rsi(close)["stoch_rsi"]
        swing_low = df["low"].rolling(10, min_periods=1).min()  # recent swing low proxy

        cond = (
            (stack == 1)
            & (struct == 1)
            & (strength >= p["adx_min"])
            & (df["low"] <= e_fast)
            & (close > e_fast)
            & (close > df["open"])
            & (r <= p["rsi_max"])
            & ((close - e_mid) <= p["max_ext_atr"] * a)
            & (a_pct.fillna(0) <= p["atr_pct_max"])
        )
        out = []
        for i in np.flatnonzero(cond.to_numpy()):
            entry = float(close.iloc[i])
            stop = min(float(swing_low.iloc[i]), entry - p["stop_atr"] * float(a.iloc[i]))
            if stop >= entry:
                continue
            out.append(Signal(
                ts=_close_ts(df, i, timeframe), strategy=self.spec.name, exchange=exchange,
                symbol=symbol, timeframe=timeframe, side=Side.BUY, entry=entry, stop=stop,
                target=entry + p["target_r"] * (entry - stop), trailing="close<ema50",
                risk_pct=p["risk_pct"], reason="EMA stack up, HH/HL, pullback to EMA20 held",
                features={"adx": float(strength.iloc[i]), "rsi": float(r.iloc[i]),
                          "stoch_rsi": float(srsi.iloc[i]),
                          "atr_pct": float(a_pct.iloc[i]) if pd.notna(a_pct.iloc[i]) else float("nan")},
            ))
        return out


class MomentumBreakout(Strategy):
    spec = StrategySpec(
        name="momentum_breakout",
        source='Ebook #1 "Day Trading"',
        timeframes=("5m", "15m"),
        setup="Close above the highest high of the previous 20 bars on volume >= 1.5 x its "
        "20-bar average, with the higher-timeframe trend up (close > EMA200 on this frame, "
        "which approximates the 1h EMA50 on 15m bars).",
        entry="Market order at the next bar's open.",
        stop="Entry - 1 x ATR(14).",
        target="1.5R fixed (the ebook's \"small price changes\").",
        filter="Skip if RSI(14) > 80 (blow-off), or if the breakout bar's range is > 3 x ATR "
        "(news spike; the event filter handles scheduled news).",
        size="0.5% of equity at risk. No leverage, although the ebook suggests up to 100x.",
        decisions=(
            '"Buy a coin and sell it once it\'s risen by a small amount" = 20-bar breakout with a '
            "1.5R target and a 1 ATR stop.",
            "Leverage (\"magnify your profits up to 100x\") is rejected: the risk engine forbids it in the pilot.",
            "Day trading's \"quick turnover\" = a time stop of 24 bars in the backtest.",
        ),
    )

    @classmethod
    def default_params(cls):
        return {"lookback": 20, "vol_mult": 1.5, "trend_ema": 200, "stop_atr": 1.0,
                "target_r": 1.5, "rsi_max": 80.0, "max_bar_atr": 3.0, "time_stop_bars": 24,
                "risk_pct": 0.005}

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        close = df["close"]
        prior_high = df["high"].shift(1).rolling(p["lookback"], min_periods=p["lookback"]).max()
        vol_avg = df["volume"].shift(1).rolling(p["lookback"], min_periods=p["lookback"]).mean()
        trend = ema(close, p["trend_ema"])
        a = atr(df)
        r = rsi(close)
        cond = (
            (close > prior_high)
            & (df["volume"] >= p["vol_mult"] * vol_avg)
            & (close > trend)
            & (r <= p["rsi_max"])
            & ((df["high"] - df["low"]) <= p["max_bar_atr"] * a)
        )
        out = []
        for i in np.flatnonzero(cond.to_numpy()):
            entry = float(close.iloc[i])
            stop = entry - p["stop_atr"] * float(a.iloc[i])
            out.append(Signal(
                ts=_close_ts(df, i, timeframe), strategy=self.spec.name, exchange=exchange,
                symbol=symbol, timeframe=timeframe, side=Side.BUY, entry=entry, stop=stop,
                target=entry + p["target_r"] * (entry - stop),
                trailing=f"time_stop={p['time_stop_bars']}", risk_pct=p["risk_pct"],
                reason=f"{p['lookback']}-bar breakout on {df['volume'].iloc[i] / vol_avg.iloc[i]:.1f}x volume",
                features={"rsi": float(r.iloc[i]),
                          "vol_ratio": float(df["volume"].iloc[i] / vol_avg.iloc[i])},
            ))
        return out


class BuyTheDip(Strategy):
    spec = StrategySpec(
        name="buy_the_dip",
        source='Ebook #4 "HODL" plus "Cycle of Crypto Trader Emotions"',
        timeframes=("1d", "4h"),
        setup="Long-term uptrend (close > SMA200) and a dip: close at least 10% below the "
        "20-bar high and RSI(14) < 35.",
        entry="The first bullish bar after the setup (close > open and close > previous high). "
        "Market order at the next bar's open.",
        stop="Entry - 3 x ATR(14) (wide, because this is a swing hold).",
        target="Sell into euphoria: exit when RSI(14) > 70 (scored in the backtest), with a 3R cap.",
        filter="BTC and ETH only by default (the ebook asks for \"solid technology and a good "
        "team\", which we can't code). No new entry while one is open on the same symbol.",
        size="0.5% of equity at risk.",
        decisions=(
            '"Always try and buy during a dip" = 10% below the 20-bar high and RSI < 35, inside a '
            "long-term uptrend (close > SMA200), so we don't buy a collapsing coin.",
            '"Sell at peak happiness" = exit when RSI(14) goes above 70.',
            'The ebook\'s "buy the rumor, sell the news" is not coded as a strategy. News handling '
            "belongs to the event filter.",
        ),
    )

    @classmethod
    def default_params(cls):
        return {"trend_sma": 200, "dip_pct": 0.10, "dip_lookback": 20, "rsi_max": 35.0,
                "setup_valid_bars": 5, "stop_atr": 3.0, "target_r": 3.0, "exit_rsi": 70.0,
                "symbols": ("BTC/USDT", "ETH/USDT"), "risk_pct": 0.005}

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        if p["symbols"] and symbol not in p["symbols"]:
            return []
        close = df["close"]
        trend = sma(close, p["trend_sma"])
        hi = df["high"].rolling(p["dip_lookback"], min_periods=p["dip_lookback"]).max()
        r = rsi(close)
        srsi = stoch_rsi(close)["stoch_rsi"]
        a = atr(df)
        setup = (close > trend) & (close <= hi * (1 - p["dip_pct"])) & (r < p["rsi_max"])
        recent_setup = setup.astype(int).rolling(p["setup_valid_bars"], min_periods=1).max() > 0
        trigger = (close > df["open"]) & (close > df["high"].shift(1))
        cond = (recent_setup.shift(1, fill_value=False) & trigger).to_numpy()
        out = []
        for i in np.flatnonzero(cond):
            entry = float(close.iloc[i])
            stop = entry - p["stop_atr"] * float(a.iloc[i])
            if not np.isfinite(stop) or stop >= entry:
                continue
            out.append(Signal(
                ts=_close_ts(df, i, timeframe), strategy=self.spec.name, exchange=exchange,
                symbol=symbol, timeframe=timeframe, side=Side.BUY, entry=entry, stop=stop,
                target=entry + p["target_r"] * (entry - stop), trailing=f"rsi>{p['exit_rsi']:g}",
                risk_pct=p["risk_pct"], reason="dip in long-term uptrend, bullish reversal bar",
                features={"rsi": float(r.iloc[i]), "stoch_rsi": float(srsi.iloc[i]),
                          "dip": float(1 - close.iloc[i] / hi.iloc[i])},
            ))
        return out


class BollingerSqueeze(Strategy):
    spec = StrategySpec(
        name="bollinger_squeeze",
        source='Binance Academy trading eBook: "Bollinger Bands Squeeze", "Trading volume", '
        '"VWAP", "Parabolic SAR"',
        timeframes=("1h", "4h"),
        setup="Squeeze: Bollinger(20, 2) band width in the lowest 20% of its last 120 bars, at "
        "some point in the last 5 bars.",
        entry="Close above the upper band (the breakout) on volume >= 1.5 x its 20-bar average, "
        "with close above the 50-bar VWAP. Market order at the next bar's open.",
        stop="Lower of the middle band and entry - 1 x ATR(14). Exits are stop-market orders.",
        target="2R fixed; the backtest also scores a Parabolic SAR trailing stop (close below SAR).",
        filter="Skip if the breakout bar's range is > 3 x ATR (news spike). Long breakouts only "
        "in the pilot (the book's squeeze is direction-neutral).",
        size="0.5% of equity at risk.",
        decisions=(
            '"Bands come really close to each other" = width in the bottom 20% of the last 120 bars.',
            '"Volume precedes price" / Dow Theory volume confirmation = volume >= 1.5 x average.',
            '"Above the VWAP may be considered bullish" = close > 50-bar rolling VWAP.',
            '"Parabolic SAR as a trailing stop-loss" = exit on a close below the SAR.',
            "Downside breakouts are ignored until shorting is allowed after the pilot.",
        ),
    )

    @classmethod
    def default_params(cls):
        return {"bb_n": 20, "bb_k": 2.0, "squeeze_lookback": 120, "squeeze_q": 0.2,
                "squeeze_recent": 5, "vol_n": 20, "vol_mult": 1.5, "vwap_n": 50, "stop_atr": 1.0,
                "target_r": 2.0, "max_bar_atr": 3.0, "risk_pct": 0.005}

    def generate(self, df, exchange, symbol, timeframe):
        p = self.params
        close = df["close"]
        bb = bollinger(close, p["bb_n"], p["bb_k"])
        width_floor = bb["width"].rolling(p["squeeze_lookback"], min_periods=p["squeeze_lookback"]) \
            .quantile(p["squeeze_q"])
        squeeze = (bb["width"] <= width_floor).astype(int)
        recent_squeeze = squeeze.rolling(p["squeeze_recent"], min_periods=1).max() > 0
        vol_avg = df["volume"].shift(1).rolling(p["vol_n"], min_periods=p["vol_n"]).mean()
        vw = vwap(df, p["vwap_n"])
        a = atr(df)
        cond = (
            recent_squeeze.shift(1, fill_value=False)
            & (close > bb["upper"])
            & (df["volume"] >= p["vol_mult"] * vol_avg)
            & (close > vw)
            & ((df["high"] - df["low"]) <= p["max_bar_atr"] * a)
        )
        sar = parabolic_sar(df)["sar"]
        out = []
        for i in np.flatnonzero(cond.to_numpy()):
            entry = float(close.iloc[i])
            stop = min(float(bb["mid"].iloc[i]), entry - p["stop_atr"] * float(a.iloc[i]))
            if not np.isfinite(stop) or stop >= entry:
                continue
            out.append(Signal(
                ts=_close_ts(df, i, timeframe), strategy=self.spec.name, exchange=exchange,
                symbol=symbol, timeframe=timeframe, side=Side.BUY, entry=entry, stop=stop,
                target=entry + p["target_r"] * (entry - stop), trailing="close<psar",
                risk_pct=p["risk_pct"],
                reason=f"squeeze breakout on {df['volume'].iloc[i] / vol_avg.iloc[i]:.1f}x volume",
                features={"bb_width": float(bb["width"].iloc[i]),
                          "vol_ratio": float(df["volume"].iloc[i] / vol_avg.iloc[i]),
                          "sar": float(sar.iloc[i])},
            ))
        return out


STRATEGIES: dict[str, type[Strategy]] = {
    s.spec.name: s for s in (TrendPullback, MomentumBreakout, BuyTheDip, BollingerSqueeze)
}
