"""Indicators and the agent's own trades for the live chart on the Markets page.

Computed on the server with the same functions the strategies use (``aitrader.indicators``),
from the same candles the chart draws, so a line on the chart is exactly what a strategy sees.
Each series has one value per candle (``None`` while it warms up). Nothing here trades.
"""

from __future__ import annotations

import json
import math

import pandas as pd

from .. import indicators as ind
from ..indicators import core
from ..indicators.results import fibonacci_result

MAX_TRADES = 40
PROFILE_BARS = 100  # volume profile and Fibonacci swing: the last 100 bars, as the strategies use
PATTERNS = {"bullish_engulfing": "E", "hammer": "H", "doji": "D", "inside_bar": "I"}
NR_BARS = 7  # "N": the narrowest range of the last 7 candles (NR7), as the strategies' nr(7)


def _series(s: pd.Series, digits: int = 10, tail: int = 0) -> list:
    s = s.iloc[-tail:] if tail else s
    return [None if v is None or (isinstance(v, float) and not math.isfinite(v)) else round(float(v), digits)
            for v in s.tolist()]


def compute(candles: list[list], tail: int = 0) -> dict:
    """Overlays (drawn on the price), panels (drawn below it) and support/resistance levels.
    Indicators warm up on every candle; ``tail`` keeps only the last values of each series."""
    if len(candles) < 2:
        return {"overlays": {}, "panels": {}, "levels": [], "fib": None, "profile": None, "patterns": []}
    df = pd.DataFrame([r[:6] for r in candles], columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.astype({"open": float, "high": float, "low": float, "close": float, "volume": float})
    close = df["close"]

    def series(x: pd.Series, digits: int = 10) -> list:
        return _series(x, digits, tail)

    bb = ind.bollinger(close)
    st = ind.supertrend(df)
    ich = ind.ichimoku(df)
    sar = ind.parabolic_sar(df)
    m = ind.macd(close)
    srsi = ind.stoch_rsi(close)
    kc = ind.keltner(df)
    dc = ind.donchian(df)
    fvg = ind.fair_value_gaps(df)
    ob = ind.order_blocks(df)
    sw = ind.liquidity_sweeps(df)
    overlays = {
        "ema20": series(ind.ema(close, 20)),
        "ema50": series(ind.ema(close, 50)),
        "ema200": series(ind.ema(close, 200)),
        "bb_upper": series(bb["upper"]), "bb_mid": series(bb["mid"]), "bb_lower": series(bb["lower"]),
        "vwap": series(ind.vwap(df, anchor="D")),
        "supertrend": series(st["supertrend"]), "supertrend_dir": series(st["direction"]),
        "tenkan": series(ich["tenkan"]), "kijun": series(ich["kijun"]),
        "span_a": series(ich["span_a"]), "span_b": series(ich["span_b"]),
        "psar": series(sar["sar"]), "psar_trend": series(sar["trend"]),
        "kc_upper": series(kc["upper"]), "kc_mid": series(kc["mid"]), "kc_lower": series(kc["lower"]),
        "sma20": series(ind.sma(close, 20)), "sma50": series(ind.sma(close, 50)),
        "sma200": series(ind.sma(close, 200)),
        "dc_upper": series(dc["upper"]), "dc_lower": series(dc["lower"]),
        # the newest open zone at each candle (what the strategies' in_bull_fvg / in_bull_ob test)
        "fvg_bull_top": series(fvg["bull_top"]), "fvg_bull_bottom": series(fvg["bull_bottom"]),
        "fvg_bear_top": series(fvg["bear_top"]), "fvg_bear_bottom": series(fvg["bear_bottom"]),
        "ob_bull_top": series(ob["bull_top"]), "ob_bull_bottom": series(ob["bull_bottom"]),
        "ob_bear_top": series(ob["bear_top"]), "ob_bear_bottom": series(ob["bear_bottom"]),
        "sweep": series(sw["low"] - sw["high"], 0),  # +1 swept the lows, -1 swept the highs
    }
    panels = {
        "rsi": series(ind.rsi(close), 2),
        "macd": series(m["macd"]), "macd_signal": series(m["signal"]), "macd_hist": series(m["hist"]),
        "adx": series(ind.adx(df), 2),
        "stoch_k": series(srsi["k"] * 100, 2), "stoch_d": series(srsi["d"] * 100, 2),
        "atr": series(ind.atr(df)), "cci": series(ind.cci(df), 2), "willr": series(ind.williams_r(df), 2),
        "fi": series(ind.force_index(df)), "obv": series(ind.obv(df)), "mfi": series(ind.mfi(df), 2),
        "cmf": series(ind.cmf(df), 4), "roc": series(close.pct_change(10) * 100, 2),
    }
    try:
        levels = ind.support_resistance(df.tail(300))
    except Exception:
        levels = []
    levels = sorted(levels, key=lambda lv: -lv["touches"])[:8]
    pats = core.candle_patterns(df)
    rng = df["high"] - df["low"]
    nr = (rng < rng.shift(1).rolling(NR_BARS - 1, min_periods=NR_BARS - 1).min()).tolist()
    marks = ["".join(c for col, c in PATTERNS.items() if row[col]) + ("N" if nr[i] else "") or None
             for i, (_, row) in enumerate(pats.iterrows())]
    return {"overlays": overlays, "panels": panels,
            "levels": [{"price": round(lv["price"], 10), "touches": lv["touches"]} for lv in levels],
            "fib": _fib(df), "profile": _profile(df.tail(PROFILE_BARS)),
            "patterns": marks[-tail:] if tail else marks}


def _fib(df: pd.DataFrame) -> dict | None:
    """The Fibonacci retracement levels of the last PROFILE_BARS bars' swing, as
    ``fibonacci_result`` (the strategies' version) measures them."""
    try:
        r = fibonacci_result(df, lookback=PROFILE_BARS)
    except Exception:
        return None
    v = r.features
    if not r.is_valid or "swing_high" not in v:
        return None
    return {"leg": r.metadata.get("leg"), "high": v["swing_high"], "low": v["swing_low"],
            "levels": [{"ratio": float(k[6:]), "price": p} for k, p in v.items()
                       if k.startswith("level_") and math.isfinite(p)]}


def _profile(win: pd.DataFrame) -> dict | None:
    """Volume per price bucket over the last PROFILE_BARS bars, with the point of control and
    the 70% value area (high and low)."""
    if len(win) < 2 or win["volume"].sum() <= 0:
        return None
    prof = core.volume_profile(win)
    va = core.value_area(win)
    return {"bins": [{"low": float(r.low), "high": float(r.high), "volume": float(r.volume)}
                     for r in prof.itertuples()],
            "poc": va["poc"], "vah": va["vah"], "val": va["val"]}


def paper_trades(paper_state_path: str, symbol: str) -> dict:
    """The paper trader's open positions and latest closed trades on ``symbol`` (any strategy,
    any timeframe): entries, stops, targets and exits to mark on the chart."""
    try:
        with open(paper_state_path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return {"open": [], "closed": []}

    def on_symbol(key: str) -> bool:
        parts = str(key).split("|")
        return len(parts) >= 3 and parts[2] == symbol

    opened = []
    for key, pos in (state.get("open") or {}).items():
        if not on_symbol(key) or not pos.get("filled"):
            continue
        sig = pos.get("signal") or {}
        opened.append({"strategy": pos.get("entry_id"), "side": sig.get("side", "buy"),
                       "entry_ts": pos.get("entry_ts"), "entry": pos.get("entry"), "stop": sig.get("stop"),
                       "target": sig.get("target"), "mark_r": pos.get("mark_r")})
    closed = [{"strategy": t.get("entry_id"), "side": t.get("side", "buy"), "entry_ts": t.get("entry_ts"),
               "entry": t.get("entry"), "exit_ts": t.get("exit_ts"), "exit": t.get("exit"), "r": t.get("r")}
              for t in (state.get("trades") or []) if on_symbol(t.get("key", ""))][-MAX_TRADES:]
    return {"open": opened[-MAX_TRADES:], "closed": closed}
