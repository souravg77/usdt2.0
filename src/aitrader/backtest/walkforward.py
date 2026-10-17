"""In-sample / out-of-sample / final-test splits and walk-forward parameter selection."""

from __future__ import annotations

import itertools

import pandas as pd

from ..strategies.base import Strategy
from .engine import BacktestConfig, run_backtest
from .metrics import metrics


def split_periods(df: pd.DataFrame, in_sample: float = 0.6, out_sample: float = 0.2):
    """Chronological split. The last part is the untouched final test: never tune on it."""
    n = len(df)
    a, b = int(n * in_sample), int(n * (in_sample + out_sample))
    return df.iloc[:a], df.iloc[a:b], df.iloc[b:]


def walk_forward(strategy_cls: type[Strategy], df: pd.DataFrame, exchange: str, symbol: str,
                 timeframe: str, grid: dict[str, list], train_bars: int, test_bars: int,
                 cfg: BacktestConfig | None = None, warmup: int = 250, objective="expectancy_r",
                 min_trades: int = 10) -> pd.DataFrame:
    """Optimise on a training window, test on the next window, roll forward by ``test_bars``.

    Each test window is run with ``warmup`` bars of history before it so indicators are ready,
    but only trades entered inside the test window count.
    """
    rows = []
    keys = list(grid)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*grid.values())] or [{}]
    start = 0
    while start + train_bars + test_bars <= len(df):
        train = df.iloc[start:start + train_bars]
        best, best_score = combos[0], float("-inf")
        for params in combos:
            m = metrics(run_backtest(strategy_cls(**params), train, exchange, symbol, timeframe, cfg))
            score = m[objective] if m["trades"] >= min_trades else float("-inf")
            if score > best_score:
                best, best_score = params, score
        t0 = start + train_bars
        window = df.iloc[max(0, t0 - warmup):t0 + test_bars]
        first_ts = int(df["ts"].iloc[t0])
        trades = [t for t in run_backtest(strategy_cls(**best), window, exchange, symbol,
                                          timeframe, cfg) if t.entry_ts >= first_ts]
        m = metrics(trades)
        rows.append({"test_start": pd.to_datetime(first_ts, unit="ms", utc=True),
                     "params": best, "train_score": best_score, **m})
        start += test_bars
    return pd.DataFrame(rows)


def sensitivity(strategy_cls: type[Strategy], df: pd.DataFrame, exchange: str, symbol: str,
                timeframe: str, cfg: BacktestConfig | None = None,
                bump: float = 0.2) -> pd.DataFrame:
    """Move each numeric parameter by -bump and +bump and record expectancy. A result that
    collapses under small changes is overfit (roadmap rejection rule)."""
    base = strategy_cls.default_params()
    rows = [{"param": "(base)", "value": None,
             **metrics(run_backtest(strategy_cls(), df, exchange, symbol, timeframe, cfg))}]
    for k, v in base.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or k == "risk_pct":
            continue
        for f in (1 - bump, 1 + bump):
            nv = type(v)(round(v * f)) if isinstance(v, int) else v * f
            if nv == v:
                continue
            m = metrics(run_backtest(strategy_cls(**{k: nv}), df, exchange, symbol, timeframe, cfg))
            rows.append({"param": k, "value": nv, **m})
    return pd.DataFrame(rows)
