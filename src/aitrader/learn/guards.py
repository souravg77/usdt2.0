"""Each paper engine's extra trading rules, set on the Trading page (ideas from the ApexTrade prompt).

- **Loss breakers** (on unless a person turns them off): when an engine's loss today, closed and
  open trades together, reaches ``day_pause_pct`` of the wallet it had at midnight, it opens nothing
  new for ``pause_hours``. When its loss this week (from Monday midnight) reaches ``week_close_pct``,
  it closes all its open positions and opens nothing new until next Monday.
- **Minimum reward** (``min_rr``, 0 = off): a signal whose fixed target is less than this many times
  its risk away is skipped. A signal that exits on its own rule (no fixed target) is not judged.
- **Furthest take-profit** (``max_rr``, 0 = off; Futures AI starts at 2): a fixed target further
  than this many times the risk is pulled in to it, so the trade takes its profit sooner. Open
  trades follow a new setting at their next check. A signal with no fixed target is not changed.
- **Higher-timeframe trend** (``trend``): a long is skipped while the 4h or daily trend points down,
  a short while it points up (5m to 1h signals check 4h and 1d, 4h signals check 1d). A trend is up
  when the close is above EMA 50 and EMA 20 is above EMA 50, down when both are below.
- **Split take-profit** (``split_tp``): half the position is closed halfway to the target, the
  rest at the target (``backtest.engine``).
- **Breakeven after the first half** (``breakeven``, with the split take-profit): once the first half
  is taken, the stop of the rest moves to the entry price plus fees and slippage, so the trade can no
  longer end in a loss.
- **Exposure caps**: at most ``max_long`` long and ``max_short`` short positions at once, and at
  most ``max_correlated`` positions on the same side whose hourly returns over the last week move
  together with the new pair (correlation of at least ``corr_min``), so one Bitcoin move can't hit
  every position at once.
- **Limit entries** (``maker_entry``): enter with a post-only limit order at the signal price or
  the open, paying the maker fee instead of taker fee and slippage; when the entry candle never
  trades through that price the order is cancelled and the trade is missed (``engine.maker_fill``).
- **Strategies that must agree** (``agree``, 1 = off): a coin is only traded when at least this many
  different strategies signal the same side on it at the same candle close (any timeframe), and
  then as one trade, by the agreeing signal with the furthest target. The live trader mirrors the
  paper trades, so it follows the same rule.
- **Timeframes** (``timeframes``): the candle timeframes the engine opens new trades on (all of
  them unless a person narrows it; an empty choice means all). Open trades on the others still run
  to their exits.
- **Amount per trade** (``trade_usdt``, 0 = automatic): each new position uses this many USDT, the
  purchase amount on Spot and the margin on Futures (so the position is that times the leverage),
  instead of being sized from the strategy's risk. Still never more than the wallet has free.

Settings live in ``paper_guards.json`` next to the paper state; the engines' breaker state lives in
the paper state itself. Paper only: the live trader keeps its own limits.
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timedelta

from . import wallet

TIMEFRAMES = ("5m", "15m", "30m", "1h", "4h", "1d")  # the ones paper trading can use
DEFAULTS = {"breaker": True, "day_pause_pct": 3.0, "pause_hours": 24.0, "week_close_pct": 6.0,
            "min_rr": 0.0, "max_rr": 0.0, "trend": False, "split_tp": False, "breakeven": False, "maker_entry": False,
            "max_long": 8.0, "max_short": 8.0, "max_correlated": 3.0, "corr_min": 0.8, "trade_usdt": 0.0,
            "agree": 1.0, "risk_pct": 0.0, "open_risk_pct": 0.0, "loss_streak": 0.0, "streak_ok_at": 0.0,
            "min_score": 0.0,
            "timeframes": list(TIMEFRAMES)}
RANGES = {"day_pause_pct": (0.5, 50.0), "pause_hours": (1.0, 168.0), "week_close_pct": (1.0, 90.0),
          "min_rr": (0.0, 10.0), "max_rr": (1.0, 20.0), "max_long": (1.0, 200.0), "max_short": (1.0, 200.0),
          "max_correlated": (1.0, 50.0), "corr_min": (0.5, 0.99),
          "trade_usdt": (5.0, 1_000_000.0), "agree": (1.0, 10.0),
          # from the NEXUS prompt: risk per trade and all open trades' risk (% of the wallet), and the
          # losing streak that stops new entries until a person resumes them
          "risk_pct": (0.05, 5.0), "open_risk_pct": (0.1, 50.0), "loss_streak": (1.0, 20.0),
          "streak_ok_at": (0.0, 1e15), "min_score": (1.0, 100.0)}
OFF_AT_ZERO = ("min_rr", "max_rr", "trade_usdt", "risk_pct", "open_risk_pct", "loss_streak", "streak_ok_at", "min_score")  # 0 = off
WHOLE = ("max_long", "max_short", "max_correlated", "agree", "loss_streak")
# an engine's own starting values (Shawn: Futures AI takes profit at 2R at most)
MARKET_DEFAULTS = {"futures": {"max_rr": 2.0}}
CORR_BARS = 168  # hourly returns of the last week
SWITCHES = ("breaker", "trend", "split_tp", "breakeven", "maker_entry")

HIGHER = {"5m": ("4h", "1d"), "15m": ("4h", "1d"), "30m": ("4h", "1d"), "1h": ("4h", "1d"), "4h": ("1d",)}
TREND_BARS = 120  # candles of a higher timeframe read for its trend (EMA 50 needs ~60 to settle)


def path_for(state_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(state_path)), "paper_guards.json")


def _clean(d: dict, market: str = "") -> dict:
    out = {}
    for k, v in {**DEFAULTS, **MARKET_DEFAULTS.get(market, {})}.items():
        got = d.get(k, v)
        if k == "timeframes":
            if isinstance(got, str):
                got = got.split(",")
            chosen = [t for t in TIMEFRAMES if isinstance(got, (list, tuple)) and t in got]
            out[k] = chosen or list(TIMEFRAMES)  # none chosen (or never set): every timeframe
            continue
        if k in SWITCHES:
            out[k] = got is not False and got not in (0, "0", "")
            continue
        try:
            num = float(got)
        except (TypeError, ValueError):
            num = v
        if not math.isfinite(num):  # "nan" or "inf" typed in the form: the default, not the limit
            num = v
        lo, hi = RANGES[k]
        out[k] = num if k in OFF_AT_ZERO and num == 0 else max(lo, min(hi, num))
        if k in WHOLE:
            out[k] = float(int(out[k]))
    return out


def load(path: str) -> dict:
    """{"spot": {...}, "futures": {...}}: each engine's rules (the defaults where none were set)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    return {m: _clean(data.get(m) if isinstance(data.get(m), dict) else {}, m) for m in wallet.MARKETS}


def save(path: str, market: str, values: dict, by: str = "") -> dict:
    if market not in wallet.MARKETS:
        raise ValueError(f"unknown market {market!r}")
    for k in RANGES:  # a typed number must be a real number
        if k in values and values[k] not in (None, "") and not math.isfinite(float(values[k])):
            raise ValueError(f"{k} must be a number")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        data = data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        data = {}
    mine = data.get(market) if isinstance(data.get(market), dict) else {}
    mine.update({k: v for k, v in values.items() if k in DEFAULTS and v not in (None, "")})
    data[market] = {**_clean(mine, market), "by": by, "at": int(time.time() * 1000)}
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)
    return load(path)[market]


# ---- loss breakers ------------------------------------------------------------------------------

def week_start_ms(now_ms: int) -> int:
    """Monday midnight of this week on the dashboard's clock (India by default)."""
    from ..clock import zone

    now = datetime.fromtimestamp(now_ms / 1000, zone())
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(monday.timestamp() * 1000)


def open_pnl(state: dict, market: str) -> float:
    """What the engine's open positions are up or down now, at their last marked price."""
    total = 0.0
    for key, o in (state.get("open") or {}).items():
        if not o.get("filled") or ((key.endswith("|futures") or wallet.market_of(o) == "futures")
                                   != (market == "futures")):
            continue
        sig = o.get("signal") or {}
        total += float(o.get("mark_r") or 0.0) * abs(sig.get("entry", 0) - sig.get("stop", 0)) * o.get("units", 0)
    return total


def _closed_since(state: dict, market: str, since: int) -> float:
    return sum(t.get("pnl_usdt", 0.0) for t in state.get("trades", [])
               if t.get("exit_ts", 0) >= since and wallet.market_of(t) == market)


def update_breaker(state: dict, w: dict, market: str, g: dict, now_ms: int) -> dict | None:
    """Trip, keep or clear this engine's breaker; returns it while it holds. Saved in the paper state
    as state["breakers"][market] = {"kind", "until", "why", "close"}."""
    held = state.setdefault("breakers", {})
    if not g["breaker"]:
        held.pop(market, None)
        return None
    br = held.get(market)
    holds = bool(br) and now_ms < br.get("until", 0)
    if holds and br.get("kind") == "week":
        return br
    name = "Futures AI" if market == "futures" else "Spot AI"
    bal = wallet.balance(state, w, market)
    live = open_pnl(state, market)
    week0 = week_start_ms(now_ms)
    since = int(w.get("since", 0))  # a reset wallet starts its week afresh
    week = _closed_since(state, market, max(week0, since))
    week_base = bal - week
    # checked even while a day pause holds: open trades can keep losing during the pause
    if week_base > 0 and week + live <= -g["week_close_pct"] / 100 * week_base:
        held[market] = {"kind": "week", "at": now_ms, "until": week0 + 7 * 86_400_000, "close": True,
                        "why": f"{name} lost {-(week + live):,.2f} USDT this week, past its weekly limit of "
                               f"{g['week_close_pct']:g}%: all its positions closed, no new trades until Monday"}
        return held[market]
    if holds:
        return br
    held.pop(market, None)
    day = wallet.today_pnl(state, market, now_ms, since)
    day_base = bal - day
    if day_base > 0 and day + live <= -g["day_pause_pct"] / 100 * day_base:
        held[market] = {"kind": "day", "at": now_ms, "until": now_ms + int(g["pause_hours"] * 3_600_000),
                        "close": False,
                        "why": f"{name} lost {-(day + live):,.2f} USDT today, its {g['day_pause_pct']:g}% pause "
                               f"limit: no new trades for {g['pause_hours']:g} hours"}
        return held[market]
    return None


def block(state: dict, market: str, now_ms: int) -> str:
    """Why the breaker stops this engine's new trades now, or ""."""
    br = (state.get("breakers") or {}).get(market)
    if br and now_ms < br.get("until", 0):
        from ..clock import local

        return f"{br['why']} (until {local(br['until'] / 1000)})"
    return ""


def losing_streak(state: dict, market: str, since: float = 0) -> int:
    """How many of this engine's newest closed trades (closed after ``since``) lost money in a row."""
    n = 0
    for t in reversed(state.get("trades", [])):
        if wallet.market_of(t) != market or t.get("exit_ts", 0) <= since:
            continue
        if t.get("pnl_usdt", t.get("r", 0.0)) >= 0:
            break
        n += 1
    return n


def streak_block(state: dict, market: str, g: dict) -> str:
    """Why a losing streak stops this engine's new trades until a person resumes them, or ""."""
    need = int(g.get("loss_streak") or 0)
    if need <= 0:
        return ""
    n = losing_streak(state, market, g.get("streak_ok_at") or 0)
    if n < need:
        return ""
    name = "Futures AI" if market == "futures" else "Spot AI"
    return (f"{name} lost {n} trades in a row (its limit is {need}): no new trades until you review them and "
            "press Resume after the losing streak on the Trading page")


def open_risk(state: dict, market: str, skip: str = "") -> float:
    """USDT the engine's filled open positions would lose together if every stop were hit."""
    total = 0.0
    for key, o in (state.get("open") or {}).items():
        if key == skip or not o.get("filled") or ((key.endswith("|futures") or wallet.market_of(o) == "futures")
                                                  != (market == "futures")):
            continue
        stop = (o.get("signal") or {}).get("stop", o.get("entry", 0))
        total += abs(o.get("entry", 0) - stop) * o.get("units", 0)
    return total


# ---- exposure caps ------------------------------------------------------------------------------

def correlation(a, b) -> float:
    """Correlation of two hourly-return series over the hours both have (0 with too few)."""
    import pandas as pd

    both = pd.concat([a, b], axis=1, join="inner").dropna()
    if len(both) < 48 or (both.std() == 0).any():  # too few hours, or a flat series: unrelated
        return 0.0
    c = both.iloc[:, 0].corr(both.iloc[:, 1])
    return 0.0 if c != c else float(c)


# ---- higher-timeframe trend ---------------------------------------------------------------------

def trend_of(df) -> str:
    """"up", "down" or "flat" from the newest closed candle: close and EMA 20 against EMA 50."""
    from ..indicators import ema

    if df is None or len(df) < 60:
        return "flat"
    close = df["close"]
    e20, e50 = float(ema(close, 20).iloc[-1]), float(ema(close, 50).iloc[-1])
    c = float(close.iloc[-1])
    if c > e50 and e20 > e50:
        return "up"
    if c < e50 and e20 < e50:
        return "down"
    return "flat"


def against(side_short: bool, trends: dict) -> str:
    """The higher timeframe whose trend points against the trade, or ""."""
    bad = "up" if side_short else "down"
    return next((tf for tf, t in trends.items() if t == bad), "")
