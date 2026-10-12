"""Backtest gate for strategies the agent drafts.

Each candidate runs on every stored market (exchange x symbol x one of its timeframes). History is
cut in time order with ``split_periods``: in sample (first 60%), out of sample (next 20%) and the
final holdout (last 20%). Trades are pooled across markets and judged in this order, against the
bars in ``Gate``, all fixed before any test runs:

1. in sample                -> BACKTESTED
2. out of sample, walk-forward over rolling windows of the first 80%, and a Monte Carlo stress
   test of the out-of-sample trade sequence            -> OUT-OF-SAMPLE VALIDATED
3. the final holdout, looked at only once everything above has passed  -> PAPER

A failure in 1 or 2 rejects the strategy with the reasons. A failure on the holdout sends it to
"under review" instead: it passed everything it was judged on, and the untouched data disagreed,
which a person should look at. The holdout result is recorded either way, with how many times it
has been looked at, and is never used to change the strategy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ..backtest import BacktestConfig, metrics, monte_carlo, run_backtest, split_periods, walk_forward
from ..data.context import ContextStore, attach_context, has_context
from ..data.store import CandleStore
from ..indicators import candles_to_frame
from .registry import Entry, make_strategy

MIN_BARS = 300


def with_context(strat, df, store, exchange: str, symbol: str, timeframe: str):
    """``df`` plus funding / open-interest columns for a strategy that reads them
    (``needs_context``), when the database has context rows; otherwise ``df`` unchanged."""
    if not getattr(strat, "needs_context", False) or df is None or not has_context(store):
        return df
    return attach_context(df, ContextStore(store), exchange, symbol, timeframe)


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
    # Walk-forward: the first 80% of each market (never the holdout) after a warm-up is cut into
    # ``wf_windows`` windows, each run on its own as unseen data; windows are pooled across
    # markets. Most counted windows must make money after costs.
    wf_windows: int = 6
    wf_warmup_bars: int = 250
    min_wf_window_trades: int = 5  # a window with fewer trades is too small to count either way
    min_wf_windows: int = 4  # counted windows needed to judge at all
    min_wf_positive_share: float = 0.6  # 4 of 6, 3 of 5, 3 of 4
    # Monte Carlo: draw ``mc_horizon`` trades (the paper stage's 100) from the out-of-sample
    # results ``mc_runs`` times with a fixed seed.
    mc_runs: int = 2000
    mc_horizon: int = 100
    mc_seed: int = 7
    max_mc_drawdown_p95: float = 15.0  # R: 19 runs in 20 stay under the out-of-sample limit
    max_mc_loss_probability: float = 0.15  # chance that 100 trades end below zero
    # The untouched final holdout: a confirmation, so it asks less than out of sample.
    min_trades_holdout: int = 20
    min_expectancy_holdout: float = 0.0  # R per trade after costs, must be above this

    def check(self, m_in: dict, m_oos: dict) -> list[str]:
        return self.check_in_sample(m_in) + self.check_out_of_sample(m_oos)

    def check_in_sample(self, m_in: dict) -> list[str]:
        why = []
        if m_in["trades"] < self.min_trades_in:
            why.append(f"only {m_in['trades']} in-sample trades (need {self.min_trades_in})")
        if m_in["trades"] and m_in["expectancy_r"] <= self.min_expectancy_in:
            why.append(f"in-sample expectancy {m_in['expectancy_r']:+.3f}R")
        return why

    def check_out_of_sample(self, m_oos: dict) -> list[str]:
        why = []
        if m_oos["trades"] < self.min_trades_oos:
            why.append(f"only {m_oos['trades']} out-of-sample trades (need {self.min_trades_oos})")
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

    def check_walk_forward(self, wf: dict) -> list[str]:
        if wf["counted"] < self.min_wf_windows:
            return [f"walk-forward: only {wf['counted']} windows with {self.min_wf_window_trades}+ "
                    f"trades (need {self.min_wf_windows})"]
        if wf["positive_share"] < self.min_wf_positive_share:
            return [f"walk-forward: {wf['positive']} of {wf['counted']} windows above 0R after costs "
                    f"(need {self.min_wf_positive_share:.0%})"]
        return []

    def check_monte_carlo(self, mc: dict) -> list[str]:
        why = []
        if mc["max_drawdown_r_p95"] > self.max_mc_drawdown_p95:
            why.append(f"Monte Carlo: 95th-percentile drawdown over {mc['horizon']} trades "
                       f"{mc['max_drawdown_r_p95']:.1f}R (max {self.max_mc_drawdown_p95:g}R)")
        if mc["loss_probability"] > self.max_mc_loss_probability:
            why.append(f"Monte Carlo: {mc['loss_probability']:.0%} chance {mc['horizon']} trades end "
                       f"below zero (max {self.max_mc_loss_probability:.0%})")
        return why

    def check_holdout(self, m: dict) -> list[str]:
        why = []
        if m["trades"] < self.min_trades_holdout:
            why.append(f"final holdout: only {m['trades']} trades (need {self.min_trades_holdout})")
        if m["trades"] and m["expectancy_r"] <= self.min_expectancy_holdout:
            why.append(f"final holdout expectancy {m['expectancy_r']:+.3f}R "
                       f"(need above {self.min_expectancy_holdout:+.2f}R)")
        return why


def evaluate(entry: Entry, store: CandleStore, exchanges: list[str], symbols: list[str],
             cfg: BacktestConfig | None = None, gate: Gate | None = None, move: bool = True) -> dict:
    """Backtest one registry entry and move it along the ladder: to paper, under review or
    rejected (only record the result with ``move=False``). Returns the summary."""
    cfg, gate = cfg or BacktestConfig(), gate or Gate()
    strat = make_strategy(entry)
    trades_in, trades_oos, markets, finals, windows = [], [], [], [], {}
    for ex in exchanges:
        for sym in symbols:
            for tf in strat.spec.timeframes:
                candles = store.load(ex, sym, tf)
                if len(candles) < MIN_BARS:
                    continue
                df = with_context(strat, candles_to_frame(candles), store, ex, sym, tf)
                ins, oos, final = split_periods(df)
                t_in = run_backtest(strat, ins, ex, sym, tf, cfg)
                t_oos = run_backtest(strat, oos, ex, sym, tf, cfg)
                trades_in += t_in
                trades_oos += t_oos
                _add_windows(windows, strat, df.iloc[:len(ins) + len(oos)], ex, sym, tf, cfg, gate)
                finals.append((ex, sym, tf, final))
                markets.append({"exchange": ex, "symbol": sym, "timeframe": tf,
                                "bars": len(candles), "trades_in": len(t_in),
                                "trades_oos": len(t_oos),
                                "exp_oos": metrics(t_oos)["expectancy_r"]})
    m_in, m_oos = metrics(trades_in), metrics(trades_oos)
    wf = _walk_forward_summary(windows, gate)
    mc = monte_carlo([t.r for t in trades_oos], gate.mc_runs, gate.mc_horizon, gate.mc_seed)
    if not markets:
        why_in = [f"no stored candles for its timeframes {', '.join(strat.spec.timeframes)}"]
        why_oos = []
    else:
        why_in = gate.check_in_sample(m_in)
        why_oos = gate.check_out_of_sample(m_oos) + gate.check_walk_forward(wf)
        if trades_oos:
            why_oos += gate.check_monte_carlo(mc)
    why = why_in + why_oos
    looks = ((entry.backtest or {}).get("holdout") or {}).get("looks", 0)
    holdout = {"evaluated": False, "looks": looks,
               "note": "not looked at: the strategy has to pass everything else first"}
    why_h: list[str] = []
    if markets and not why:  # only now is the untouched final slice run, once
        trades_h = []
        for (ex, sym, tf, final), mk in zip(finals, markets):
            t_h = run_backtest(strat, final, ex, sym, tf, cfg)
            trades_h += t_h
            mk["trades_holdout"] = len(t_h)
        m_h = metrics(trades_h)
        why_h = gate.check_holdout(m_h)
        holdout = {"evaluated": True, "looks": looks + 1, "passed": not why_h, "failed": why_h,
                   **_round(m_h)}
    summary = {"in_sample": _round(m_in), "out_of_sample": _round(m_oos), "markets": markets,
               "walk_forward": wf, "monte_carlo": _round(mc), "holdout": holdout,
               "gate": asdict(gate), "failed": why + why_h,
               "costs": {"taker_fee": cfg.taker_fee, "maker_fee": cfg.maker_fee,
                         "slippage_bps": cfg.slippage_bps}}
    entry.backtest = summary
    if not move or not markets:
        return summary  # without data it stays where it is until there is data to test it on
    if why_in:
        entry.set_status("rejected", "; ".join(why_in))
        return summary
    entry.set_status("backtested", f"in sample: {m_in['trades']} trades, {m_in['expectancy_r']:+.2f}R each")
    if why_oos:
        entry.set_status("rejected", "; ".join(why_oos))
        return summary
    entry.set_status("oos_validated",
                     f"out of sample: {m_oos['trades']} trades, {m_oos['expectancy_r']:+.2f}R each; "
                     f"walk-forward {wf['positive']}/{wf['counted']} windows above 0R; Monte Carlo "
                     f"p95 drawdown {mc['max_drawdown_r_p95']:.1f}R, "
                     f"{mc['loss_probability']:.0%} chance of a losing {mc['horizon']} trades")
    if why_h:
        entry.set_status("under_review", "; ".join(why_h))
    else:
        entry.set_status("paper", f"passed backtest: {m_oos['trades']} out-of-sample trades, "
                                  f"{m_oos['expectancy_r']:+.2f}R each; final holdout "
                                  f"{holdout['trades']} trades, {holdout['expectancy_r']:+.2f}R each")
    return summary


def _add_windows(windows: dict, strat, df, ex: str, sym: str, tf: str, cfg: BacktestConfig,
                 gate: Gate) -> None:
    """Run the rolling unseen windows of one market and add each window's trades and R to
    ``windows`` by window number. Nothing is tuned: the strategy's rules are fixed."""
    test_bars = (len(df) - gate.wf_warmup_bars) // gate.wf_windows
    if test_bars < 1:
        return
    rows = walk_forward(lambda **_: strat, df, ex, sym, tf, grid={}, train_bars=gate.wf_warmup_bars,
                        test_bars=test_bars, cfg=cfg, warmup=gate.wf_warmup_bars)
    for i, row in enumerate(rows.itertuples()):
        w = windows.setdefault(i, {"window": i + 1, "trades": 0, "total_r": 0.0})
        w["trades"] += int(row.trades)
        w["total_r"] += float(row.total_r)


def _walk_forward_summary(windows: dict, gate: Gate) -> dict:
    rows = []
    for _i, w in sorted(windows.items()):
        exp = w["total_r"] / w["trades"] if w["trades"] else 0.0
        rows.append({"window": w["window"], "trades": w["trades"], "total_r": round(w["total_r"], 4),
                     "expectancy_r": round(exp, 4), "counted": w["trades"] >= gate.min_wf_window_trades})
    counted = [r for r in rows if r["counted"]]
    positive = sum(1 for r in counted if r["expectancy_r"] > 0)
    return {"windows": rows, "counted": len(counted), "positive": positive,
            "positive_share": round(positive / len(counted), 4) if counted else 0.0}


def _round(m: dict) -> dict:
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()}
