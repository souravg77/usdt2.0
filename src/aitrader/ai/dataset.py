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
from ..backtest.engine import simulate_signal
from ..indicators import candles_to_frame
from ..models import TIMEFRAME_MS, Side
from ..strategies.base import Signal, Strategy
from .features import ALL, history_features, market_features, signal_bar, signal_features

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
    """Simulate every signal ``strategy`` gives on ``df`` and return one row per signal."""
    cfg = cfg or BacktestConfig()
    feats = market_features(df) if feats is None else feats
    frecords = feats.to_dict("records")
    index_of = {int(t): i for i, t in enumerate(df["ts"].to_numpy())}
    rows, cache = [], {}
    for sig in strategy.generate(df, exchange, symbol, timeframe):
        i = signal_bar(sig, index_of)
        if i is None:
            continue
        t = simulate_signal(sig, df, i + 1, cfg, cache)
        if t is None:
            continue
        row = signal_features(sig, strategy_id, frecords[i], [])
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
    order = np.lexsort((frame["signal_ts"].to_numpy(),
                        frame["timeframe"].astype(str).to_numpy(),
                        frame["symbol"].astype(str).to_numpy(),
                        frame["exchange"].astype(str).to_numpy(),
                        frame["strategy"].astype(str).to_numpy()))
    keys = frame[["strategy", "exchange", "symbol", "timeframe"]].astype(str).agg("|".join, axis=1).to_numpy()
    entry, exit_ = frame["entry_ts"].to_numpy(), frame["exit_ts"].to_numpy()
    busy: dict[str, int] = {}
    for j in order:
        if mask[j] and entry[j] > busy.get(keys[j], -1):
            taken[j] = True
            busy[keys[j]] = exit_[j]
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
                  include_final: bool = False, paper_state: str | None = None) -> pd.DataFrame:
    """Backtest every (strategy id, strategy) on every stored market and return the rows."""
    rows: list[dict] = []
    markets: dict[tuple, tuple] = {}
    for sid, strat in strategies:
        for ex in exchanges:
            for sym in symbols:
                for tf in strat.spec.timeframes:
                    key = (ex, sym, tf)
                    if key not in markets:
                        candles = store.load(ex, sym, tf)
                        if len(candles) < MIN_BARS:
                            markets[key] = (None, None)
                        else:
                            df = candles_to_frame(candles)
                            if not include_final:
                                ins, oos, _final = split_periods(df)
                                df = pd.concat([ins, oos])
                            markets[key] = (df, market_features(df))
                    df, feats = markets[key]
                    if df is None:
                        continue
                    rows += signal_rows(strat, sid, df, ex, sym, tf, cfg, feats)
    if paper_state:
        rows += paper_rows(store, paper_state, cfg)
    return finish(rows)


def finish(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=INFO + ALL)
    frame = pd.DataFrame(rows)
    # a paper trade replaces the backtest row for the same signal
    frame["_paper"] = frame["source"].eq("paper")
    frame = (frame.sort_values("_paper")
             .drop_duplicates(["strategy", "exchange", "symbol", "timeframe", "signal_ts"], keep="last")
             .drop(columns="_paper").sort_values("signal_ts"))
    frame = add_history(frame)
    return frame[INFO + ALL]


def paper_rows(store, state_path: str, cfg: BacktestConfig | None = None) -> list[dict]:
    """Rows for the closed trades in a ``learn paper`` state file."""
    if not os.path.exists(state_path):
        return []
    with open(state_path, encoding="utf-8") as fh:
        trades = json.load(fh).get("trades", [])
    rows = []
    for t in trades:
        sid, ex, sym, tf = t["key"].split("|")
        step = TIMEFRAME_MS[tf]
        candles = store.load(ex, sym, tf, t["signal_ts"] - PAPER_WARMUP * step,
                             t["exit_ts"] + LABEL_HORIZON * step)
        if not candles:
            continue
        df = candles_to_frame(candles)
        index_of = {int(x): i for i, x in enumerate(df["ts"].to_numpy())}
        i = index_of.get(t["signal_ts"] - step)
        if i is None or i + 1 >= len(df):
            continue
        sig = Signal(ts=t["signal_ts"], strategy=t["strategy"], exchange=ex, symbol=sym,
                     timeframe=tf, side=Side.BUY, entry=float(df["close"].iloc[i]),
                     stop=t["stop"], target=t["target"])
        feats = market_features(df.iloc[:i + 1]).iloc[-1].to_dict()
        row = signal_features(sig, sid, feats, [])
        row.update(source="paper", exchange=ex, symbol=sym, signal_ts=t["signal_ts"],
                   entry_ts=t["entry_ts"], exit_ts=t["exit_ts"], exit_reason=t["exit_reason"],
                   r=t["r"], hit_1r=reached_1r(df, i + 1, t["entry"], sig.stop, sig.entry - sig.stop))
        rows.append(row)
    return rows


def save(frame: pd.DataFrame, path: str = DEFAULT_DATASET) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    frame.to_csv(path, index=False)


def load(path: str = DEFAULT_DATASET) -> pd.DataFrame:
    return pd.read_csv(path, dtype={"strategy": str, "timeframe": str, "exchange": str,
                                    "symbol": str, "source": str})
