"""Strategy metrics from the roadmap (Phase 3) and the day/month results calendar.

Sharpe and Sortino are per trade, in R (mean R over its standard deviation, or over the downside
deviation), not annualised: trades are irregular in time, so a yearly figure would be made up.
Recovery is counted in trades from the bottom of the worst drawdown back to the earlier peak
(None if it has not recovered yet). Costs are split into fees, slippage and funding, in R.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .engine import Trade, trades_frame

MIN_RELIABLE_TRADES = 100


def metrics(trades: list[Trade]) -> dict:
    r = np.array([t.r for t in trades], dtype=float)
    n = len(r)
    if n == 0:
        return {"trades": 0, "expectancy_r": 0.0, "win_rate": 0.0, "avg_win_r": 0.0,
                "avg_loss_r": 0.0, "profit_factor": 0.0, "max_drawdown_r": 0.0,
                "total_r": 0.0, "cost_r_per_trade": 0.0, "t_stat": 0.0, "reliable": False,
                "sharpe": 0.0, "sortino": 0.0, "recovery_trades": 0, "fee_r_per_trade": 0.0,
                "slippage_r_per_trade": 0.0, "funding_r_per_trade": 0.0}
    wins, losses = r[r > 0], r[r <= 0]
    equity = np.cumsum(r)
    dd = np.maximum.accumulate(np.r_[0.0, equity])[1:] - equity
    gross_loss = -losses.sum()
    sd = r.std(ddof=1) if n > 1 else 0.0
    downside = np.sqrt(np.mean(np.minimum(r, 0.0) ** 2))
    return {
        "trades": n,
        "expectancy_r": float(r.mean()),
        "win_rate": float(len(wins) / n),
        "avg_win_r": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss_r": float(losses.mean()) if len(losses) else 0.0,
        "profit_factor": float(wins.sum() / gross_loss) if gross_loss > 0 else float("inf"),
        "max_drawdown_r": float(dd.max()),
        "total_r": float(r.sum()),
        "cost_r_per_trade": float(np.mean([t.cost_r for t in trades])),
        # how many standard errors the average R is above zero (luck rarely gets past 2)
        "t_stat": float(r.mean() / r.std(ddof=1) * np.sqrt(n)) if n > 1 and r.std(ddof=1) > 0 else 0.0,
        "reliable": n >= MIN_RELIABLE_TRADES,
        "sharpe": float(r.mean() / sd) if sd > 0 else 0.0,
        "sortino": float(r.mean() / downside) if downside > 0 else 0.0,
        "recovery_trades": recovery_trades(r),
        "fee_r_per_trade": _avg(trades, "fee_r"),
        "slippage_r_per_trade": _avg(trades, "slippage_r"),
        "funding_r_per_trade": _avg(trades, "funding_r"),
    }


def _avg(trades, attr: str) -> float:
    return float(np.mean([getattr(t, attr, 0.0) for t in trades]))


def recovery_trades(r) -> int | None:
    """Trades from the bottom of the worst drawdown until equity is back at the peak before it
    (0 without a drawdown, None if it has not recovered by the last trade)."""
    equity = np.r_[0.0, np.cumsum(np.asarray(r, dtype=float))]
    peaks = np.maximum.accumulate(equity)
    dd = peaks - equity
    if len(dd) == 0 or dd.max() <= 0:
        return 0
    trough = int(dd.argmax())
    back = np.nonzero(equity[trough:] >= peaks[trough])[0]
    return int(back[0]) if len(back) else None


def monte_carlo(r, runs: int = 2000, horizon: int = 100, seed: int = 7) -> dict:
    """Trade-sequence stress test: draw ``horizon`` trades at random (with replacement) from the
    R results ``r``, ``runs`` times, with a fixed seed so the result repeats. Returns the 95th
    percentile of the worst drawdown, the chance the run ends below zero, and the 95th percentile
    of the longest losing streak."""
    r = np.asarray(r, dtype=float)
    if len(r) == 0:
        return {"runs": 0, "horizon": horizon, "seed": seed, "max_drawdown_r_p95": 0.0,
                "loss_probability": 1.0, "losing_streak_p95": 0}
    rng = np.random.default_rng(seed)
    draws = rng.choice(r, size=(runs, horizon), replace=True)
    equity = np.cumsum(draws, axis=1)
    peaks = np.maximum.accumulate(np.concatenate([np.zeros((runs, 1)), equity], axis=1), axis=1)[:, 1:]
    worst_dd = (peaks - equity).max(axis=1)
    streaks = np.zeros(runs, dtype=int)
    run_len = np.zeros(runs, dtype=int)
    for col in (draws <= 0).T:
        run_len = np.where(col, run_len + 1, 0)
        streaks = np.maximum(streaks, run_len)
    return {"runs": runs, "horizon": horizon, "seed": seed,
            "max_drawdown_r_p95": float(np.percentile(worst_dd, 95)),
            "loss_probability": float(np.mean(equity[:, -1] < 0)),
            "losing_streak_p95": int(np.percentile(streaks, 95))}


def results_calendar(trades: list[Trade], freq: str = "M") -> pd.DataFrame:
    """Sum of R and trade count per day ("D") or month ("M"), by exit time (UTC)."""
    df = trades_frame(trades)
    if df.empty:
        return pd.DataFrame(columns=["period", "trades", "total_r", "win_rate"])
    fmt = "%Y-%m" if freq == "M" else "%Y-%m-%d"
    df["period"] = df["exit_time"].dt.strftime(fmt)
    g = df.groupby("period")["r"]
    return pd.DataFrame({
        "trades": g.size(), "total_r": g.sum(), "win_rate": g.apply(lambda s: (s > 0).mean()),
    }).reset_index()
