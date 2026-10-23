"""The training set: one row per signal with its features and its outcome (roadmap Phase 4,
step 1).

Rows come from two places:

- backtests of the strategies on stored candles. By default only the in-sample and
  out-of-sample periods are used; the final 20% of each market's history stays untouched for the
  Phase 6 go/no-go test, as in Phase 3.
- closed paper trades from ``learn paper`` (``source = "paper"``).

Every signal is simulated on its own with the backtest's fill rules, so a row exists even when
the unfiltered strategy would have skipped it because a trade was already open. ``taken`` marks
the signals the unfiltered strategy really trades (one position per strategy and market). The
label ``hit_1r`` is 1 when price reaches 1R above the fill before it touches the stop (stop
first when one bar touches both), the target the roadmap names for the model.
"""

from __future__ import annotations

import json
import logging
import os

import numpy as np
import pandas as pd

from ..backtest import BacktestConfig, split_periods
from ..backtest.engine import as_perp, simulate_signal
from ..data.context import ContextStore, attach_context, has_context
from ..indicators import candles_to_frame
from ..models import TIMEFRAME_MS, Side
from ..strategies.base import Signal, Strategy
from .features import ALL, as_long, history_features, market_features, signal_bar, signal_features

log = logging.getLogger(__name__)

DEFAULT_DATASET = "data/ai/dataset.csv"
LABEL = "hit_1r"
INFO = ["source", "exchange", "symbol", "signal_ts", "entry_ts", "exit_ts", "exit_reason", "r",
        LABEL, "taken"]
MIN_BARS = 300
LABEL_HORIZON = 500  # bars; a signal that reaches neither level by then counts as a miss
PAPER_WARMUP = 600  # bars of history loaded before a paper trade to compute its features


def reached_1r(df: pd.DataFrame, pos: int, fill: float, stop: float, risk: float,
               horizon: int = LABEL_HORIZON) -> int:
    """1 if the high reaches ``fill + risk`` before the low reaches ``stop`` (from bar ``pos``)."""
    end = min(len(df), pos + horizon)
    hi = df["high"].to_numpy()[pos:end]
    lo = df["low"].to_numpy()[pos:end]
    hit = np.flatnonzero(hi >= fill + risk)
    stopped = np.flatnonzero(lo <= stop)
    if not len(hit):
        return 0
    return int(not len(stopped) or hit[0] < stopped[0])


def signal_rows(strategy: Strategy, strategy_id: str, df: pd.DataFrame, exchange: str,
                symbol: str, timeframe: str, cfg: BacktestConfig | None = None,
                feats: pd.DataFrame | None = None, source: str = "backtest") -> list[dict]:
    """Simulate every signal ``strategy`` gives on ``df`` and return one row per signal. A short
    twin's signals are its long rules on the flipped chart (``strategies.mirror``), costed as perps,
    so they become rows in the same form as the longs (``features.as_long``)."""
    from ..strategies.mirror import ShortMirror, flip

    cfg = cfg or BacktestConfig()
    if isinstance(strategy, ShortMirror):
        return signal_rows(strategy.base, strategy_id, flip(df), exchange, symbol, timeframe,
                           as_perp(cfg), None, source)
    feats = market_features(df) if feats is None else feats
    fcols, fvals = list(feats.columns), feats.to_numpy()  # a row is made only for a signal bar
    index_of = {int(t): i for i, t in enumerate(df["ts"].to_numpy())}
    rows, cache = [], {}
    for sig in strategy.generate(df, exchange, symbol, timeframe):
        i = signal_bar(sig, index_of)
        if i is None:
            continue
        t = simulate_signal(sig, df, i + 1, cfg, cache)
        if t is None:
            continue
        row = signal_features(sig, strategy_id, dict(zip(fcols, fvals[i])), [])
        row.update(source=source, exchange=exchange, symbol=symbol, signal_ts=sig.ts,
                   entry_ts=t.entry_ts, exit_ts=t.exit_ts, exit_reason=t.exit_reason, r=t.r,
                   hit_1r=reached_1r(df, i + 1, t.entry, sig.stop, sig.entry - sig.stop))
        rows.append(row)
    return rows


def take_sequential(frame: pd.DataFrame, mask=None) -> np.ndarray:
    """Which rows a strategy trades when it holds one position per market at a time and only
    considers the rows where ``mask`` is true. Returns a boolean array aligned with ``frame``."""
    mask = np.ones(len(frame), bool) if mask is None else np.asarray(mask, bool)
    taken = np.zeros(len(frame), bool)
    if not len(frame):
        return taken
    # one integer per (strategy, exchange, symbol, timeframe): positions never interact across them.
    # (A "a|b|c|d" text key per row needed well over a gigabyte for a million rows: every
    # `ai retrain` on the 2 GB server was killed for running out of memory.)
    key = frame.groupby(["strategy", "exchange", "symbol", "timeframe"], sort=False,
                        observed=True, dropna=False).ngroup().to_numpy()
    order = np.lexsort((frame["signal_ts"].to_numpy(), key))
    entry, exit_ = frame["entry_ts"].to_numpy(), frame["exit_ts"].to_numpy()
    busy = np.full(key.max() + 1, -1, dtype=np.int64)
    for j in order:
        if mask[j] and entry[j] > busy[key[j]]:
            taken[j] = True
            busy[key[j]] = exit_[j]
    return taken


def add_history(frame: pd.DataFrame) -> pd.DataFrame:
    """Fill ``taken`` and the win/loss-streak features. A strategy's history is the trades it
    actually took (any market) that had closed before the signal bar closed."""
    frame = frame.reset_index(drop=True)
    frame["taken"] = take_sequential(frame)
    streak, recent = np.zeros(len(frame)), np.full(len(frame), np.nan)
    for _sid, g in frame.groupby("strategy", sort=False):
        done = g[g["taken"]].sort_values("exit_ts")
        exits, rs = done["exit_ts"].to_numpy(), done["r"].to_numpy()
        for j, ts in zip(g.index, g["signal_ts"].to_numpy()):
            k = np.searchsorted(exits, ts, side="left")  # trades with exit_ts < ts
            h = history_features(list(rs[max(0, k - 10):k]))
            streak[j], recent[j] = h["streak"], h["recent_r"]
    frame["streak"], frame["recent_r"] = streak, recent
    return frame


def build_dataset(store, strategies: list[tuple[str, Strategy]], exchanges: list[str],
                  symbols: list[str], cfg: BacktestConfig | None = None,
                  include_final: bool = False, paper_state: str | None = None,
                  futures_store=None) -> pd.DataFrame:
    """Backtest every (strategy id, strategy) on every stored market and return the rows.

    One market's candles are in memory at a time: each is loaded, every strategy that trades its
    timeframe runs on it, its rows become a compact table and the candles are let go before the
    next market. (Keeping every market loaded while going strategy by strategy ran the server out
    of memory with a few years of 5-minute candles on a few dozen pairs.)"""
    by_tf: dict[str, list[tuple[str, Strategy]]] = {}
    for sid, strat in strategies:
        for tf in strat.spec.timeframes:
            by_tf.setdefault(tf, []).append((sid, strat))
    context = ContextStore(store) if has_context(store) else None  # funding / open interest
    chunks: list[pd.DataFrame] = []
    for tf, strats in by_tf.items():
        for ex in exchanges:
            for sym in symbols:
                candles = store.load(ex, sym, tf)
                if len(candles) < MIN_BARS:
                    continue
                df = candles_to_frame(candles)
                del candles
                if context is not None:
                    df = attach_context(df, context, ex, sym, tf)
                if not include_final:
                    ins, oos, _final = split_periods(df)
                    df = pd.concat([ins, oos])
                feats = market_features(df)
                rows: list[dict] = []
                for sid, strat in strats:
                    rows += signal_rows(strat, sid, df, ex, sym, tf, cfg, feats)
                del df, feats
                if rows:
                    chunks.append(_compact(pd.DataFrame(rows)))
    if paper_state:
        rows = paper_rows(store, paper_state, cfg, futures_store)
        if rows:
            chunks.append(_compact(pd.DataFrame(rows)))
    return finish(chunks)


def _compact(frame: pd.DataFrame) -> pd.DataFrame:
    """The features as 32-bit floats: half the memory, far more precision than they carry."""
    floats = [c for c in frame.columns if frame[c].dtype == np.float64 and c not in ("r",)]
    frame[floats] = frame[floats].astype(np.float32)
    return frame


def finish(rows) -> pd.DataFrame:
    """The training set from row dicts or from tables of rows."""
    if rows and isinstance(rows[0], pd.DataFrame):
        frame = pd.concat(rows, ignore_index=True)
    elif rows:
        frame = pd.DataFrame(rows)
    else:
        return pd.DataFrame(columns=INFO + ALL)
    # a paper trade replaces the backtest row for the same signal
    frame["_paper"] = frame["source"].eq("paper")
    frame = (frame.sort_values("_paper", kind="stable")
             .drop_duplicates(["strategy", "exchange", "symbol", "timeframe", "signal_ts"], keep="last")
             .drop(columns="_paper").sort_values("signal_ts", kind="stable"))
    frame = add_history(frame)
    return frame[INFO + ALL]


def paper_rows(store, state_path: str, cfg: BacktestConfig | None = None, futures_store=None) -> list[dict]:
    """Rows for the closed trades in a ``learn paper`` state file. Futures AI's trades are read on
    the perpetuals' candles (``futures_store``), Spot AI's on spot ones."""
    if not os.path.exists(state_path):
        return []
    with open(state_path, encoding="utf-8") as fh:
        trades = json.load(fh).get("trades", [])
    rows = []
    for t in trades:
        sid, ex, sym, tf = t["key"].split("|")[:4]
        step = TIMEFRAME_MS[tf]
        db = futures_store if futures_store is not None and t.get("market") == "futures" else store
        candles = db.load(ex, sym, tf, t["signal_ts"] - PAPER_WARMUP * step,
                             t["exit_ts"] + LABEL_HORIZON * step)
        if not candles:
            continue
        df = candles_to_frame(candles)
        index_of = {int(x): i for i, x in enumerate(df["ts"].to_numpy())}
        i = index_of.get(t["signal_ts"] - step)
        if i is None or i + 1 >= len(df):
            continue
        sig = Signal(ts=t["signal_ts"], strategy=t["strategy"], exchange=ex, symbol=sym,
                     timeframe=tf, side=Side(t.get("side") or "buy"), entry=float(df["close"].iloc[i]),
                     stop=t["stop"], target=t["target"])
        fill = t["entry"]
        if sig.side is Side.SELL:  # a short: the long on the flipped chart, as the filter judges it
            sig, df = as_long(sig, df)
            sig = Signal(**{**sig.__dict__, "entry": float(df["close"].iloc[i])})
            fill = 1.0 / fill
        feats = market_features(df.iloc[:i + 1]).iloc[-1].to_dict()
        row = signal_features(sig, sid, feats, [])
        row.update(source="paper", exchange=ex, symbol=sym, signal_ts=t["signal_ts"],
                   entry_ts=t["entry_ts"], exit_ts=t["exit_ts"], exit_reason=t["exit_reason"],
                   r=t["r"], hit_1r=reached_1r(df, i + 1, fill, sig.stop, sig.entry - sig.stop))
        rows.append(row)
    return rows


def save(frame: pd.DataFrame, path: str = DEFAULT_DATASET) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    frame.to_csv(path, index=False)


def load(path: str = DEFAULT_DATASET) -> pd.DataFrame:
    return pd.read_csv(path, dtype={"strategy": str, "timeframe": str, "exchange": str,
                                    "symbol": str, "source": str})
