"""Backtest gate for strategies the agent drafts.

Each candidate runs on every stored market (exchange x symbol x one of its timeframes) using the
in-sample and out-of-sample periods from ``split_periods``. The final 20% of history stays
untouched, as in Phase 3. Trades are pooled across markets and the strategy moves to paper only
if it clears every bar in ``Gate``; otherwise it is rejected with the reasons. The summary also
splits the out-of-sample trades by market regime (``by_regime_oos``); the gate does not use it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ..backtest import BacktestConfig, metrics, run_backtest, split_periods
from ..data.store import CandleStore
from ..indicators import candles_to_frame
from ..market.regime import breakdown, labels_by_close
from .registry import Entry, make_strategy

MIN_BARS = 300


@dataclass(frozen=True)
class Gate:
    min_trades_in: int = 50
    min_trades_oos: int = 30
    min_expectancy_in: float = 0.0  # R per trade after costs
    min_expectancy_oos: float = 0.05
    min_profit_factor_oos: float = 1.1
    max_drawdown_oos: float = 15.0  # R
    # Dozens of strategies are tested, so some look good by luck. The out-of-sample average must
    # also be clearly above zero: t = mean R / its standard error.
    min_t_oos: float = 2.0

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
            if m_oos.get("t_stat", self.min_t_oos) < self.min_t_oos:
                why.append(f"out-of-sample result could be luck (t={m_oos['t_stat']:.1f}, "
                           f"need {self.min_t_oos:g})")
            if m_oos["max_drawdown_r"] > self.max_drawdown_oos:
                why.append(f"out-of-sample drawdown {m_oos['max_drawdown_r']:.1f}R "
                           f"(max {self.max_drawdown_oos:.0f}R)")
        return why


def evaluate(entry: Entry, store: CandleStore, exchanges: list[str], symbols: list[str],
             cfg: BacktestConfig | None = None, gate: Gate | None = None, move: bool = True,
             regimes: dict | None = None) -> dict:
    """Backtest one registry entry and move it to paper or rejected (only record the result with
    ``move=False``). Returns the summary. ``regimes`` caches each market's regime labels when
    several entries are tested in a row."""
    cfg, gate = cfg or BacktestConfig(), gate or Gate()
    regimes = {} if regimes is None else regimes
    strat = make_strategy(entry)
    trades_in, trades_oos, markets, tagged = [], [], [], []
    for ex in exchanges:
        for sym in symbols:
            for tf in strat.spec.timeframes:
                candles = store.load(ex, sym, tf)
                if len(candles) < MIN_BARS:
                    continue
                df = candles_to_frame(candles)
                ins, oos, _final = split_periods(df)
                t_in = run_backtest(strat, ins, ex, sym, tf, cfg)
                t_oos = run_backtest(strat, oos, ex, sym, tf, cfg)
                key = (ex, sym, tf, len(candles), candles[-1].ts)
                if key not in regimes:
                    regimes[key] = labels_by_close(df, tf)
                tagged += [(regimes[key].get(t.signal_ts), t) for t in t_oos]
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
               "by_regime_oos": breakdown(tagged),
               "gate": asdict(gate), "failed": why,
               "costs": {"taker_fee": cfg.taker_fee, "maker_fee": cfg.maker_fee,
                         "slippage_bps": cfg.slippage_bps}}
    entry.backtest = summary
    if not move:
        pass
    elif not markets:
        pass  # stays a candidate until there is data to test it on
    elif why:
        entry.set_status("rejected", "; ".join(why))
    else:
        entry.set_status("paper", f"passed backtest: {m_oos['trades']} out-of-sample trades, "
                                  f"{m_oos['expectancy_r']:+.2f}R each")
    return summary


def _round(m: dict) -> dict:
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()}
