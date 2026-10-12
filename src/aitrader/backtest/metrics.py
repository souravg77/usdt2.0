"""Strategy metrics from the roadmap (Phase 3) and the day/month results calendar."""

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
                "total_r": 0.0, "cost_r_per_trade": 0.0, "t_stat": 0.0, "reliable": False}
    wins, losses = r[r > 0], r[r <= 0]
    equity = np.cumsum(r)
    dd = np.maximum.accumulate(np.r_[0.0, equity])[1:] - equity
    gross_loss = -losses.sum()
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
    }


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
