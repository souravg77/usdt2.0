"""Stress test: a second opinion that tries to break a strategy before it reaches paper trading.

It runs after a strategy has passed out of sample, walk-forward and Monte Carlo, and before the
final holdout is looked at. Every scenario uses only the out-of-sample part (the holdout stays
untouched) and asks the same question: **does the strategy still make money when things go
worse than the backtest assumed?**

| Scenario | What changes | Passes when |
|---|---|---|
| ``harsh_costs`` | fees x2, slippage x3, funding x3, backtests re-run | still above 0R per trade |
| ``stop_gaps`` | every 4th stop-out gaps through the stop and loses 0.5R more (a crash, a thin book) | still above 0R per trade |
| ``no_best_trades`` | the best 5% of trades removed (a pump it happened to catch) | still above 0R per trade |
| ``no_best_market`` | the pair and timeframe that made the most removed | still above 0R per trade |
| ``losing_streak`` | Monte Carlo of 100 trades | 19 runs in 20 have at most 12 losses in a row |

The numbers are fixed in ``Gate`` before any strategy is run against them. The stress test only
ever rejects: it cannot pass a strategy the rest of the gate failed.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from ..backtest import BacktestConfig, run_backtest
from ..backtest.metrics import metrics

SCENARIOS = ("harsh_costs", "stop_gaps", "no_best_trades", "no_best_market", "losing_streak")


def harsh(cfg: BacktestConfig, cost_mult: float, slip_mult: float, funding_mult: float) -> BacktestConfig:
    """``cfg`` with every cost made worse. The too-costly skip (``max_cost_r``) is turned off: the
    higher costs would otherwise skip the very trades they should hurt. ``stress_test`` keeps only
    the trades the normal costs took, so the scenario changes what each trade pays, not which run."""
    return replace(cfg, taker_fee=cfg.taker_fee * cost_mult, maker_fee=cfg.maker_fee * cost_mult,
                   perp_taker_fee=cfg.perp_taker_fee * cost_mult, perp_maker_fee=cfg.perp_maker_fee * cost_mult,
                   slippage_bps=cfg.slippage_bps * slip_mult,
                   funding_rate_8h=cfg.funding_rate_8h * funding_mult,
                   perp_funding_8h=cfg.perp_funding_8h * funding_mult, max_cost_r=0.0)


def _exp(r) -> float:
    r = list(r)
    return float(np.mean(r)) if r else 0.0


def stress_test(parts, trades, cfg: BacktestConfig, gate, mc: dict) -> dict:
    """Run every scenario. ``parts``: (strategy, out-of-sample frame, exchange, symbol, timeframe)
    per market; ``trades``: the out-of-sample trades the normal costs gave; ``mc``: their Monte
    Carlo result. Returns each scenario's numbers and ``failed``, the reasons it was rejected."""
    out: dict = {"scenarios": {}, "failed": []}
    sc = out["scenarios"]

    def judge(name: str, exp: float, n: int, note: str) -> None:
        ok = exp > 0
        sc[name] = {"trades": n, "expectancy_r": round(exp, 4), "passed": ok, "what": note}
        if not ok:
            out["failed"].append(f"stress test {name.replace('_', ' ')}: {exp:+.3f}R per trade ({note})")

    worse = harsh(cfg, gate.stress_cost_mult, gate.stress_slip_mult, gate.stress_funding_mult)
    taken = {(t.symbol, t.timeframe, t.signal_ts) for t in trades}
    hard = []
    for strat, df, ex, sym, tf in parts:
        hard += [t for t in run_backtest(strat, df, ex, sym, tf, worse)
                 if (t.symbol, t.timeframe, t.signal_ts) in taken]
    judge("harsh_costs", metrics(hard)["expectancy_r"], len(hard),
          f"fees x{gate.stress_cost_mult:g}, slippage x{gate.stress_slip_mult:g}, "
          f"funding x{gate.stress_funding_mult:g}")

    gapped, stops = [], 0
    for t in sorted(trades, key=lambda t: (t.exit_ts, t.symbol)):
        r = t.r
        if t.exit_reason in ("stop", "stop_gap"):
            stops += 1
            if stops % gate.stress_gap_every == 0:
                r -= gate.stress_gap_extra_r
        gapped.append(r)
    judge("stop_gaps", _exp(gapped), len(gapped),
          f"every {gate.stress_gap_every}th stop-out loses {gate.stress_gap_extra_r:g}R more")

    rs = sorted((t.r for t in trades), reverse=True)
    cut = max(1, int(round(len(rs) * gate.stress_top_share)))
    judge("no_best_trades", _exp(rs[cut:]), len(rs) - cut,
          f"best {cut} of {len(rs)} trades removed")

    by_market: dict = {}
    for t in trades:
        by_market.setdefault((t.symbol, t.timeframe), []).append(t.r)
    if len(by_market) >= 2:
        best = max(by_market, key=lambda k: sum(by_market[k]))
        rest = [r for k, v in by_market.items() if k != best for r in v]
        judge("no_best_market", _exp(rest), len(rest), f"{best[0]} {best[1]} removed")
    else:
        sc["no_best_market"] = {"trades": len(trades), "passed": True, "skipped": True,
                                "what": "only one market traded: nothing to remove"}

    streak = int(mc.get("losing_streak_p95", 0))
    ok = streak <= gate.stress_max_losing_streak
    sc["losing_streak"] = {"losing_streak_p95": streak, "passed": ok,
                           "what": f"at most {gate.stress_max_losing_streak} losses in a row in 19 of 20 runs"}
    if not ok:
        out["failed"].append(f"stress test losing streak: {streak} losses in a row in the worst 5% of "
                             f"runs (max {gate.stress_max_losing_streak})")
    out["passed"] = not out["failed"]
    return out
