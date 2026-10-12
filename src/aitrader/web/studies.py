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

MAX_TRADES = 40


def _series(s: pd.Series, digits: int = 10, tail: int = 0) -> list:
    s = s.iloc[-tail:] if tail else s
    return [None if v is None or (isinstance(v, float) and not math.isfinite(v)) else round(float(v), digits)
            for v in s.tolist()]


def compute(candles: list[list], tail: int = 0) -> dict:
    """Overlays (drawn on the price), panels (drawn below it) and support/resistance levels.
    Indicators warm up on every candle; ``tail`` keeps only the last values of each series."""
    if len(candles) < 2:
        return {"overlays": {}, "panels": {}, "levels": []}
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
    }
    panels = {
        "rsi": series(ind.rsi(close), 2),
        "macd": series(m["macd"]), "macd_signal": series(m["signal"]), "macd_hist": series(m["hist"]),
        "adx": series(ind.adx(df), 2),
        "stoch_k": series(srsi["k"] * 100, 2), "stoch_d": series(srsi["d"] * 100, 2),
    }
    try:
        levels = ind.support_resistance(df.tail(300))
    except Exception:
        levels = []
    levels = sorted(levels, key=lambda lv: -lv["touches"])[:8]
    return {"overlays": overlays, "panels": panels,
            "levels": [{"price": round(lv["price"], 10), "touches": lv["touches"]} for lv in levels]}


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
