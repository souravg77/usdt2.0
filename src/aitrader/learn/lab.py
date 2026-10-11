"""Backtest gate for strategies the agent drafts.

Each candidate runs on every stored market (exchange x symbol x one of its timeframes) using the
in-sample and out-of-sample periods from ``split_periods``. The final 20% of history stays
untouched, as in Phase 3. Trades are pooled across markets and the strategy moves to paper only
if it clears every bar in ``Gate``; otherwise it is rejected with the reasons.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ..backtest import BacktestConfig, metrics, run_backtest, split_periods
from ..data.store import CandleStore
from ..indicators import candles_to_frame
from .registry import Entry, make_strategy

MIN_BARS = 300


@dataclass(frozen=True)
class Gate:
    min_trades_in: int = 20
    min_trades_oos: int = 10
    min_expectancy_in: float = 0.0  # R per trade after costs
    min_expectancy_oos: float = 0.05
    min_profit_factor_oos: float = 1.1
    max_drawdown_oos: float = 15.0  # R

    def check(self, m_in: dict, m_oos: dict) -> list[str]:
        why = []
        if m_in["trades"] < self.min_trades_in:
            why.append(f"only {m_in['trades']} in-sample trades (need {self.min_trades_in})")
        if m_oos["trades"] < self.min_trades_oos:
            why.append(f"only {m_oos['trades']} out-of-sample trades (need {self.min_trades_oos})")
        if m_in["trades"] and m_in["expectancy_r"] <= self.min_expectancy_in:
            why.append(f"in-sample expectancy {m_in['expectancy_r']:+.3f}R")
        if m_oos["trades"]:
            if m_oos["expectancy_r"] < self.min_expectancy_oos:
                why.append(f"out-of-sample expectancy {m_oos['expectancy_r']:+.3f}R "
                           f"(need {self.min_expectancy_oos:+.2f}R)")
            if m_oos["profit_factor"] < self.min_profit_factor_oos:
                why.append(f"out-of-sample profit factor {m_oos['profit_factor']:.2f} "
                           f"(need {self.min_profit_factor_oos:.2f})")
            if m_oos["max_drawdown_r"] > self.max_drawdown_oos:
                why.append(f"out-of-sample drawdown {m_oos['max_drawdown_r']:.1f}R "
                           f"(max {self.max_drawdown_oos:.0f}R)")
        return why


def evaluate(entry: Entry, store: CandleStore, exchanges: list[str], symbols: list[str],
             cfg: BacktestConfig | None = None, gate: Gate | None = None) -> dict:
    """Backtest one registry entry and move it to paper or rejected. Returns the summary."""
    cfg, gate = cfg or BacktestConfig(), gate or Gate()
    strat = make_strategy(entry)
    trades_in, trades_oos, markets = [], [], []
    for ex in exchanges:
        for sym in symbols:
            for tf in strat.spec.timeframes:
                candles = store.load(ex, sym, tf)
                if len(candles) < MIN_BARS:
                    continue
                ins, oos, _final = split_periods(candles_to_frame(candles))
                t_in = run_backtest(strat, ins, ex, sym, tf, cfg)
                t_oos = run_backtest(strat, oos, ex, sym, tf, cfg)
                trades_in += t_in
                trades_oos += t_oos
                markets.append({"exchange": ex, "symbol": sym, "timeframe": tf,
                                "bars": len(candles), "trades_in": len(t_in),
                                "trades_oos": len(t_oos),
                                "exp_oos": metrics(t_oos)["expectancy_r"]})
    m_in, m_oos = metrics(trades_in), metrics(trades_oos)
    if not markets:
        why = [f"no stored candles for its timeframes {', '.join(strat.spec.timeframes)}"]
    else:
        why = gate.check(m_in, m_oos)
    summary = {"in_sample": _round(m_in), "out_of_sample": _round(m_oos), "markets": markets,
               "gate": asdict(gate), "failed": why,
               "costs": {"taker_fee": cfg.taker_fee, "maker_fee": cfg.maker_fee,
                         "slippage_bps": cfg.slippage_bps}}
    entry.backtest = summary
    if not markets:
        pass  # stays a candidate until there is data to test it on
    elif why:
        entry.set_status("rejected", "; ".join(why))
    else:
        entry.set_status("paper", f"passed backtest: {m_oos['trades']} out-of-sample trades, "
                                  f"{m_oos['expectancy_r']:+.2f}R each")
    return summary


def _round(m: dict) -> dict:
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()}
