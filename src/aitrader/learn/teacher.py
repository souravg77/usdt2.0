"""The backtest teacher: which market situations each strategy wins in, and where a 1R
profit-take catches more of its trades.

Every signal a strategy gave in the backtest is tagged with the market situation at the signal
bar, read from the same features the AI filter uses:

- trend: ``up`` / ``range`` / ``down`` (EMA 50/200 and ADX, ``ai.features`` regime),
- volatility: ``low`` / ``normal`` / ``high`` (ATR percentile over the last 500 bars),
- volume: ``quiet`` / ``normal`` / ``heavy`` (volume against its recent average).

The lessons are learned on the first ``LEARN_SHARE`` of each strategy's signals (in time order)
and then checked on the rest, which they never saw. The final 20% of every market stays out
altogether: it belongs to the go/no-go gate.

For each strategy and situation the teacher says

- ``trade``: at least ``MIN_TRADES`` signals and a positive result per trade after costs, shrunk
  toward 0 so a few lucky trades don't count (``PRIOR`` trades of 0R are mixed in),
- ``avoid``: at least ``MIN_TRADES`` signals and a negative shrunk result,
- ``unproven``: too few signals to say; the signal is taken as before.

and, where taking profit at 1R beat the strategy's own exit by ``MIN_EXIT_EDGE`` R per trade,
``exit_1r``. That raises the win rate (more trades close green) and is kept only when it also
raised the result per trade.

The lessons are a candidate until a person turns them on (dashboard button or
``learn teach-on``), and they can be turned on only when, on the unseen part, following them beat
the strategies without them: higher result per trade over at least ``MIN_CHECK_TRADES`` trades.
When on, paper trading skips ``avoid`` signals and adds the 1R profit-take where the lesson says
so. Live trading never reads them: live strategies are frozen by the go-live approval.

None of this promises a profit. A situation that paid in the past can stop paying.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import replace

import numpy as np
import pandas as pd

from ..models import TIMEFRAME_MS, Side

log = logging.getLogger(__name__)

DEFAULT_DIR = "data/learn/teacher"
LEARN_SHARE = 0.7
MIN_TRADES = 20
PRIOR = 10
MIN_EXIT_EDGE = 0.02  # R per trade
MIN_CHECK_TRADES = 30
MIN_BARS = 300

TRENDS = ("up", "range", "down")
VOLATILITY = ("low", "normal", "high")
VOLUME = ("quiet", "normal", "heavy")


def situation(feat: dict) -> str | None:
    """``trend|volatility|volume`` for one row of ``ai.features.market_features``; None while
    the indicators are still warming up."""
    reg, vol, rv = feat.get("regime"), feat.get("atr_pctile"), feat.get("rel_volume")
    if any(v is None or (isinstance(v, float) and math.isnan(v)) for v in (reg, vol, rv)):
        return None
    trend = "up" if reg > 0 else "down" if reg < 0 else "range"
    volatility = "low" if vol < 1 / 3 else "high" if vol > 2 / 3 else "normal"
    volume = "quiet" if rv < 0.8 else "heavy" if rv > 1.5 else "normal"
    return f"{trend}|{volatility}|{volume}"


def describe(key: str) -> str:
    trend, volatility, volume = key.split("|")
    trend = {"up": "uptrend", "down": "downtrend", "range": "range"}[trend]
    return f"{trend}, {volatility} volatility, {volume} volume"


# ---- collecting the backtest signals ----------------------------------------------------------


def one_r_outcome(sig, df: pd.DataFrame, pos: int, trade) -> float:
    """The trade's result in R had it also taken profit at 1R (``sig.entry`` plus the planned
    risk). The stop wins when one bar touches both; the strategy's own exit wins when it came
    first, and a target closer than 1R is kept."""
    risk = abs(sig.entry - sig.stop)
    if sig.target is not None and abs(sig.target - sig.entry) <= risk:
        return float(trade.r)
    exit_i = int(np.searchsorted(df["ts"].to_numpy(), trade.exit_ts))
    if trade.exit_reason in ("trail", "time"):  # those exits fill at the open of their bar
        exit_i -= 1
    hi, lo = df["high"].to_numpy()[pos:exit_i + 1], df["low"].to_numpy()[pos:exit_i + 1]
    if sig.side is Side.SELL:
        level = sig.entry - risk
        hit, stopped = np.flatnonzero(lo <= level), np.flatnonzero(hi >= sig.stop)
        gross = (trade.entry - level) / risk
    else:
        level = sig.entry + risk
        hit, stopped = np.flatnonzero(hi >= level), np.flatnonzero(lo <= sig.stop)
        gross = (level - trade.entry) / risk
    if not len(hit) or (len(stopped) and stopped[0] <= hit[0]):
        return float(trade.r)
    return float(gross - trade.cost_r)


def signal_rows(strategy, strategy_id: str, df: pd.DataFrame, exchange: str, symbol: str,
                timeframe: str, cfg=None, feats: pd.DataFrame | None = None) -> list[dict]:
    """One row per signal: its situation, its result and its result with a 1R profit-take."""
    from ..ai.features import market_features, signal_bar
    from ..backtest import BacktestConfig
    from ..backtest.engine import simulate_signal

    cfg = cfg or BacktestConfig()
    feats = market_features(df) if feats is None else feats
    frecords = feats.to_dict("records")
    index_of = {int(t): i for i, t in enumerate(df["ts"].to_numpy())}
    rows, cache = [], {}
    for sig in strategy.generate(df, exchange, symbol, timeframe):
        i = signal_bar(sig, index_of)
        if i is None:
            continue
        key = situation(frecords[i])
        t = simulate_signal(sig, df, i + 1, cfg, cache)
        if key is None or t is None:
            continue
        rows.append({"strategy": strategy_id, "exchange": exchange, "symbol": symbol,
                     "timeframe": timeframe, "side": "short" if sig.side is Side.SELL else "long",
                     "signal_ts": int(sig.ts), "situation": key, "r": float(t.r),
                     "r_1r": one_r_outcome(sig, df, i + 1, t)})
    return rows


def collect(store, strategies: list[tuple[str, object]], exchanges: list[str], symbols: list[str],
            cfg=None, progress=None) -> pd.DataFrame:
    """Backtest every (id, strategy) on every stored market, the final 20% left out. ``progress``
    (a function taking a line of text) hears about each strategy as it starts."""
    from ..ai.features import market_features
    from ..backtest import split_periods
    from ..indicators import candles_to_frame
    from .lab import with_context

    rows: list[dict] = []
    markets: dict = {}
    for n, (sid, strat) in enumerate(strategies, 1):
        if progress:
            progress(f"[{n}/{len(strategies)}] {sid} ({len(rows)} signals so far)")
        for ex in exchanges:
            for sym in symbols:
                for tf in strat.spec.timeframes:
                    key = (ex, sym, tf)
                    if key not in markets:
                        candles = store.load(ex, sym, tf)
                        if len(candles) < MIN_BARS:
                            markets[key] = (None, None)
                        else:
                            ins, oos, _final = split_periods(candles_to_frame(candles))
                            df = pd.concat([ins, oos])
                            markets[key] = (df, market_features(df))
                    df, feats = markets[key]
                    if df is None:
                        continue
                    try:
                        sdf = with_context(strat, df, store, ex, sym, tf)
                        rows += signal_rows(strat, sid, sdf, ex, sym, tf, cfg, feats)
                    except Exception:  # one broken strategy must not stop the lesson
                        log.exception("teacher: %s failed on %s %s %s", sid, ex, sym, tf)
    return pd.DataFrame(rows, columns=["strategy", "exchange", "symbol", "timeframe", "side", "signal_ts",
                                       "situation", "r", "r_1r"])


# ---- learning and checking --------------------------------------------------------------------


def _stats(r: pd.Series) -> dict:
    n = int(len(r))
    return {"trades": n, "win_rate": round(float((r > 0).mean()), 4) if n else None,
            "expectancy_r": round(float(r.mean()), 4) if n else None,
            "shrunk_r": round(float(r.sum()) / (n + PRIOR), 4)}


def split(frame: pd.DataFrame, share: float = LEARN_SHARE) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Each strategy's signals in time order: the first ``share`` to learn from, the rest to
    check on."""
    learn, check = [], []
    for _sid, g in frame.sort_values("signal_ts").groupby("strategy", sort=False):
        cut = int(len(g) * share)
        learn.append(g.iloc[:cut])
        check.append(g.iloc[cut:])
    empty = frame.iloc[:0]
    return (pd.concat(learn) if learn else empty), (pd.concat(check) if check else empty)


def lessons_from(frame: pd.DataFrame) -> dict:
    """{strategy: {situation: lesson}} from the signals in ``frame``."""
    out: dict = {}
    for (sid, key), g in frame.groupby(["strategy", "situation"]):
        own, one = _stats(g["r"]), _stats(g["r_1r"])
        verdict = "unproven"
        if own["trades"] >= MIN_TRADES:
            verdict = "trade" if own["shrunk_r"] > 0 else "avoid" if own["shrunk_r"] < 0 else "unproven"
        exit_1r = (own["trades"] >= MIN_TRADES and one["expectancy_r"] - own["expectancy_r"] >= MIN_EXIT_EDGE)
        out.setdefault(sid, {})[key] = {"verdict": verdict, "exit_1r": bool(exit_1r), "own": own, "with_1r": one}
    return out


def apply(frame: pd.DataFrame, lessons: dict) -> pd.Series:
    """Each signal's result when the lessons are followed (NaN = skipped)."""
    out = []
    for sid, key, r, r1 in frame[["strategy", "situation", "r", "r_1r"]].itertuples(index=False):
        lesson = lessons.get(sid, {}).get(key)
        if lesson is None:
            out.append(r)
        elif lesson["verdict"] == "avoid":
            out.append(np.nan)
        else:
            out.append(r1 if lesson["exit_1r"] else r)
    return pd.Series(out, index=frame.index, dtype=float)


def teach(frame: pd.DataFrame, now: float | None = None) -> dict:
    """Learn on the early signals, check on the later ones, and say whether the lessons may be
    turned on."""
    learn, check = split(frame)
    lessons = lessons_from(learn)
    followed = apply(check, lessons).dropna()
    before, after = _stats(check["r"]), _stats(followed)
    for sid, by_key in lessons.items():  # how each lesson did on the signals it never saw
        seen = check[check["strategy"] == sid]
        for key, lesson in by_key.items():
            g = seen[seen["situation"] == key]
            lesson["check"] = {**_stats(g["r"]), "with_1r": _stats(g["r_1r"])["expectancy_r"]}
    passed = (after["trades"] >= MIN_CHECK_TRADES and before["expectancy_r"] is not None
              and after["expectancy_r"] > before["expectancy_r"])
    why = "" if passed else (
        f"fewer than {MIN_CHECK_TRADES} trades left on the unseen part" if after["trades"] < MIN_CHECK_TRADES
        else "following the lessons did not beat the strategies without them on the unseen part")
    counts = {v: sum(1 for by in lessons.values() for x in by.values() if x["verdict"] == v)
              for v in ("trade", "avoid", "unproven")}
    return {"version": time.strftime("t%Y%m%d-%H%M%S", time.gmtime(now or time.time())),
            "at": now or time.time(), "signals": int(len(frame)),
            "learn_signals": int(len(learn)), "check_signals": int(len(check)),
            "check": {"without": before, "with": after}, "passed": bool(passed), "why": why,
            "counts": counts, "exit_1r": sum(1 for by in lessons.values() for x in by.values() if x["exit_1r"]),
            "lessons": lessons}


def best_for(lessons: dict, key: str, limit: int = 5) -> list[dict]:
    """Strategies proven in situation ``key``, best result per trade first."""
    rows = [{"strategy": sid, **by[key]} for sid, by in lessons.items()
            if key in by and by[key]["verdict"] == "trade"]
    rows.sort(key=lambda x: x["own"]["shrunk_r"], reverse=True)
    return rows[:limit]


# ---- candidate, on and off --------------------------------------------------------------------


class Teacher:
    """``candidate.json`` (the newest lessons) and ``active.json`` (the lessons paper trading
    follows; written only by ``turn_on``)."""

    def __init__(self, root: str = DEFAULT_DIR, events=None, clock=time.time):
        self.root, self.events, self.clock = root, events, clock

    def _path(self, name: str) -> str:
        return os.path.join(self.root, name)

    def _load(self, name: str) -> dict:
        try:
            with open(self._path(name), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save(self, name: str, data: dict) -> None:
        os.makedirs(self.root, exist_ok=True)
        tmp = self._path(name) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, self._path(name))

    @property
    def candidate(self) -> dict:
        return self._load("candidate.json")

    @property
    def active(self) -> dict:
        return self._load("active.json")

    def save_candidate(self, result: dict) -> None:
        self._save("candidate.json", result)
        self._emit("teacher", f"backtest teacher {result['version']}: "
                   + ("passed the check, ready to turn on" if result["passed"] else f"not usable: {result['why']}"))

    def turn_on(self, by: str) -> dict:
        if not by:
            raise ValueError("say who turns it on")
        cand = self.candidate
        if not cand:
            raise ValueError("no lessons yet: run the teacher first")
        if not cand.get("passed"):
            raise ValueError(f"these lessons can't be turned on: {cand.get('why') or 'they failed the check'}")
        self._save("active.json", {**cand, "turned_on_by": by, "turned_on_at": self.clock()})
        self._emit("teacher", f"backtest teacher {cand['version']} turned on by {by}", "warning")
        return cand

    def turn_off(self, by: str = "") -> None:
        if self.active:
            self._save("active.json", {})
            self._emit("teacher", "backtest teacher turned off" + (f" by {by}" if by else ""), "warning")

    def _emit(self, kind: str, text: str, level: str = "info") -> None:
        if self.events is not None:
            self.events.emit(kind, text, level=level)


class TeacherFilter:
    """What paper trading asks for each new signal. Reads ``active.json`` again when it changes,
    so turning the lessons on or off takes effect on the next signal."""

    def __init__(self, root: str = DEFAULT_DIR):
        self.teacher = Teacher(root)
        self._mtime, self._lessons, self._version = None, {}, None

    def _refresh(self) -> None:
        path = self.teacher._path("active.json")
        mtime = os.path.getmtime(path) if os.path.exists(path) else None
        if mtime != self._mtime:
            act = self.teacher.active
            self._mtime, self._lessons, self._version = mtime, act.get("lessons", {}), act.get("version")

    def check(self, sig, entry_id: str, df: pd.DataFrame) -> dict:
        """{"take", "signal" (with a 1R target when the lesson says so), "situation", "verdict",
        "version"}."""
        self._refresh()
        out = {"take": True, "signal": sig, "situation": None, "verdict": None, "version": self._version}
        if not self._lessons:
            return out
        from ..ai.features import market_features

        key = situation(market_features(df).iloc[-1].to_dict())
        lesson = self._lessons.get(entry_id, {}).get(key) if key else None
        out.update(situation=key, verdict=lesson["verdict"] if lesson else "unproven")
        if lesson and lesson["verdict"] == "avoid":
            out["take"] = False
        elif lesson and lesson["exit_1r"]:
            risk = abs(sig.entry - sig.stop)
            level = sig.entry - risk if sig.side is Side.SELL else sig.entry + risk
            closer = sig.target is None or abs(level - sig.entry) < abs(sig.target - sig.entry)
            if closer:
                out["signal"] = replace(sig, target=level)
        return out


def now_situations(store, exchange: str, symbols: list[str], timeframe: str = "1h",
                   now_ms: int | None = None) -> dict:
    """The current situation of each pair on its newest closed ``timeframe`` bar."""
    from ..ai.features import market_features
    from ..indicators import candles_to_frame

    step = TIMEFRAME_MS[timeframe]
    now_ms = now_ms or int(time.time() * 1000)
    out = {}
    for sym in symbols:
        candles = store.load(exchange, sym, timeframe, now_ms - 700 * step, now_ms)
        df = candles_to_frame(candles) if candles else None
        if df is not None:
            df = df[df["ts"] + step <= now_ms]
        if df is None or len(df) < 250:
            out[sym] = None
            continue
        out[sym] = situation(market_features(df).iloc[-1].to_dict())
    return out
