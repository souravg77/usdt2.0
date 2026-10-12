"""Agents A-L: one class per role, each with typed inputs, one typed message out
(``agents.messages``) and its own logger (``aitrader.agents.<letter>``), so a problem in one is
logged and contained instead of breaking the rest.

They wrap the modules that do the work (indicators, regime, strategies, risk, pairs, backtests,
the promotion ladder); nothing here places an order. The risk manager (J) only answers whether
and how big; the executor still needs its own checks and the live lock.
"""

from __future__ import annotations

import hashlib
import logging
import math
import statistics
import time
from typing import Any

import numpy as np
import pandas as pd

from ..indicators import atr, bullish_divergence, missing_candles, relative_volume, rsi
from ..indicators.results import evaluate_all
from ..models import TIMEFRAME_MS
from .messages import (REGIME_ENUM, AuditReport, BreakoutView, DerivativesView, ExecutionReport, FeatureReport,
                       MarketSnapshot, PortfolioView, RegimeView, ResearchReport, ReversalView, RiskDecision,
                       TrendView)

STALE_FEED_S = 15.0  # master prompt: a stream quiet for more than 15 s is stale
ABNORMAL_ATR = 8.0  # a one-bar move larger than this many ATRs is flagged as abnormal


def _now_ms() -> int:
    return int(time.time() * 1000)


def _close_ts(df: pd.DataFrame, timeframe: str) -> int:
    return int(df["ts"].iloc[-1]) + TIMEFRAME_MS[timeframe] if len(df) else _now_ms()


class Agent:
    letter = "?"
    role = ""

    def __init__(self):
        self.log = logging.getLogger(f"aitrader.agents.{self.letter}")


# ---- A: market data -----------------------------------------------------------------------------


class MarketDataAgent(Agent):
    letter, role = "A", "Market data: validation, stale data, order book"

    def __init__(self, stale_s: float = STALE_FEED_S, abnormal_atr: float = ABNORMAL_ATR):
        super().__init__()
        self.stale_s, self.abnormal_atr = stale_s, abnormal_atr

    def run(self, df: pd.DataFrame, symbol: str, timeframe: str, now_ms: int | None = None,
            quote=None, book: dict | None = None) -> MarketSnapshot:
        """``df``: closed candles. ``quote``: the stream's latest quote (``received`` age read
        through ``feed_age_s``), or None. ``book``: {"bids": [[price, size], ...], "asks": ...}."""
        now_ms = now_ms or _now_ms()
        step = TIMEFRAME_MS[timeframe]
        problems: list[str] = []
        if not len(df):
            return MarketSnapshot("A", now_ms, symbol, timeframe, problems=["no candles"])
        missing = int(missing_candles(df, step).sum())
        dupes = int(df["ts"].duplicated().sum())
        stale = now_ms - (int(df["ts"].iloc[-1]) + step) > 2 * step
        a = atr(df)
        move = (df["close"] - df["close"].shift(1)).abs()
        abnormal = int((move > self.abnormal_atr * a.shift(1)).sum())
        if missing:
            problems.append(f"{missing} missing candle(s)")
        if dupes:
            problems.append(f"{dupes} duplicate candle(s)")
        if not df["ts"].is_monotonic_increasing:
            problems.append("candles out of order")
        if stale:
            problems.append("the newest closed candle is more than 2 bars old")
        if abnormal:
            problems.append(f"{abnormal} one-bar move(s) over {self.abnormal_atr:g} ATR")
        age = getattr(quote, "feed_age_s", None) if quote is not None else None
        feed_stale = age is not None and age > self.stale_s
        if feed_stale:
            problems.append(f"price stream quiet for {age:.0f}s (over {self.stale_s:g}s)")
        spread = depth = None
        if book and book.get("bids") and book.get("asks"):
            bid, ask = float(book["bids"][0][0]), float(book["asks"][0][0])
            spread = (ask - bid) / ((ask + bid) / 2) * 1e4 if ask >= bid > 0 else None
            mid = (ask + bid) / 2
            band = [(p, s) for p, s in (*book["bids"], *book["asks"]) if abs(float(p) - mid) <= 0.01 * mid]
            depth = float(sum(float(p) * float(s) for p, s in band))
            if spread is None:
                problems.append("crossed order book")
        for p in problems:
            self.log.warning("%s %s: %s", symbol, timeframe, p)
        return MarketSnapshot("A", _close_ts(df, timeframe), symbol, timeframe, len(df), float(df["close"].iloc[-1]),
                              missing, dupes, bool(stale), age, bool(feed_stale), abnormal, spread, depth, problems)


# ---- B: indicators ------------------------------------------------------------------------------


class IndicatorAgent(Agent):
    letter, role = "B", "Indicator research: the 20 standard indicators and their quality"

    def run(self, df: pd.DataFrame, symbol: str, timeframe: str, funding_rate: float | None = None) -> FeatureReport:
        res = evaluate_all(df, funding_rate) if len(df) else {}
        valid = [r for r in res.values() if r.is_valid]
        invalid = sorted(n for n, r in res.items() if not r.is_valid)
        if invalid:
            self.log.info("%s %s: not valid yet: %s", symbol, timeframe, ", ".join(invalid))
        return FeatureReport("B", _close_ts(df, timeframe), symbol, timeframe,
                             {n: r.as_dict() for n, r in res.items()},
                             round(len(valid) / max(1, len(res)), 3), invalid,
                             round(1 - statistics.fmean(r.confidence for r in valid), 3) if valid else 1.0)


# ---- C, D, E: trend, reversal, breakout ---------------------------------------------------------

TREND_MEMBERS = ("ema", "sma", "macd", "adx", "supertrend", "ichimoku")


def _vote(report: FeatureReport, members) -> tuple[int, float, dict]:
    votes, score, weight = {}, 0.0, 0.0
    for name in members:
        r = report.results.get(name)
        if not r or not r["is_valid"]:
            continue
        votes[name] = r["direction"]
        score += r["direction"] * max(r["confidence"], 0.1)
        weight += 1
    if not weight:
        return 0, 0.0, votes
    s = score / weight
    return (1 if s >= 0.3 else -1 if s <= -0.3 else 0), min(1.0, abs(s)), votes


class TrendAgent(Agent):
    letter, role = "C", "Trend: direction, strength, multi-timeframe consensus"

    def run(self, report: FeatureReport, higher: dict[str, FeatureReport] | None = None) -> TrendView:
        d, conf, votes = _vote(report, TREND_MEMBERS)
        adx_r = report.results.get("adx") or {}
        strength = (adx_r.get("features") or {}).get("adx_norm") if adx_r.get("is_valid") else 0.0
        tfs = {report.timeframe: d}
        for tf, rep in (higher or {}).items():
            tfs[tf] = _vote(rep, TREND_MEMBERS)[0]
        agree = sum(1 for v in tfs.values() if v == d and d != 0) / len(tfs)
        return TrendView("C", report.ts, report.symbol, d, float(strength or 0.0), round(conf, 3), votes, tfs,
                         round(agree, 3))


class ReversalAgent(Agent):
    letter, role = "D", "Reversal: ranges, divergences, oscillator extremes"

    OSCILLATORS = ("rsi", "mfi", "williams_r", "stoch_rsi")

    def run(self, df: pd.DataFrame, report: FeatureReport, recent: int = 5) -> ReversalView:
        adx_r = report.results.get("adx") or {}
        ranging = bool(adx_r.get("is_valid") and not adx_r["metadata"].get("trending"))
        r = rsi(df["close"]) if len(df) else pd.Series(dtype=float)
        div = None
        if len(df) > 40:
            if bullish_divergence(df, r).iloc[-recent:].any():
                div = "bullish"
            else:
                from ..strategies.mirror import flip

                f = flip(df)
                if bullish_divergence(f, rsi(f["close"])).iloc[-recent:].any():
                    div = "bearish"
        extremes = {n: report.results[n]["metadata"].get("zone") for n in self.OSCILLATORS
                    if report.results.get(n, {}).get("is_valid")}
        low = [n for n, z in extremes.items() if z == "oversold"]
        high = [n for n, z in extremes.items() if z == "overbought"]
        rejected_up = len(df) > 1 and df["close"].iloc[-1] > df["open"].iloc[-1] and df["close"].iloc[-1] > df["high"].iloc[-2]
        rejected_dn = len(df) > 1 and df["close"].iloc[-1] < df["open"].iloc[-1] and df["close"].iloc[-1] < df["low"].iloc[-2]
        candidate, notes = 0, []
        if low and (div == "bullish" or rejected_up):
            candidate = 1
        elif high and (div == "bearish" or rejected_dn):
            candidate = -1
        elif low or high:
            notes.append("oscillator extreme without confirmation: not a signal on its own")
        return ReversalView("D", report.ts, report.symbol, ranging, div, extremes, candidate, notes)


class BreakoutAgent(Agent):
    letter, role = "E", "Breakout and volatility: squeezes, volume, expansion, failed breakouts"

    def run(self, df: pd.DataFrame, report: FeatureReport) -> BreakoutView:
        res = report.results
        bb = res.get("bollinger") or {}
        squeeze = bool(bb.get("metadata", {}).get("squeeze"))
        a_pct = (res.get("atr") or {}).get("features", {}).get("atr_percentile")
        from ..indicators import atr_percentile

        rank = atr_percentile(df) if len(df) else pd.Series(dtype=float)
        expansion = bool(a_pct is not None and math.isfinite(a_pct) and a_pct >= 0.6 and len(rank) > 5
                         and rank.iloc[-1] > rank.iloc[-6])
        rv = float(relative_volume(df).iloc[-1]) if len(df) > 21 else math.nan
        anomaly = math.isfinite(rv) and rv >= 2.0
        dirs = [res[n]["direction"] for n in ("donchian", "bollinger", "keltner") if res.get(n, {}).get("is_valid")]
        breakout = 1 if dirs.count(1) >= 2 else -1 if dirs.count(-1) >= 2 else 0
        failed = False
        if len(df) > 25:
            from ..indicators import donchian

            up = donchian(df, 20)["upper"]
            broke = (df["close"] > up).iloc[-4:-1].any()
            failed = bool(broke and df["close"].iloc[-1] < up.iloc[-1])
        return BreakoutView("E", report.ts, report.symbol, squeeze, expansion, bool(anomaly), breakout, failed, rv)


# ---- F: derivatives ------------------------------------------------------------------------------


class DerivativesAgent(Agent):
    letter, role = "F", "Derivatives: basis, funding velocity, open interest, liquidation risk, costs"

    def __init__(self, perp_fee: float = 0.00055, slippage_bps: float = 5.0, liq_warn: float = 0.10):
        super().__init__()
        self.perp_fee, self.slippage_bps, self.liq_warn = perp_fee, slippage_bps, liq_warn

    def run(self, symbol: str, snapshot=None, funding_history: list[float] | None = None,
            oi_history: list[float] | None = None, position: dict | None = None, ts: int | None = None) -> DerivativesView:
        """``snapshot``: anything with last, mark, index, funding_rate, open_interest (e.g.
        ``data.derivs.DerivSnapshot``). ``position``: {"mark", "liq_price", "short"}."""
        def g(name):
            v = getattr(snapshot, name, None) if snapshot is not None else None
            return float(v) if v is not None and math.isfinite(float(v)) else None

        last, mark, index, fr = g("last"), g("mark"), g("index"), g("funding_rate")
        basis = (last - index) / index if last is not None and index else None
        premium = (mark - index) / index if mark is not None and index else None
        hist = [float(x) for x in (funding_history or []) if x is not None and math.isfinite(float(x))]
        if fr is None and hist:
            fr = hist[-1]
        velocity = fr - statistics.fmean(hist[-4:-1]) if fr is not None and len(hist) >= 4 else None
        oi = [float(x) for x in (oi_history or []) if x]
        oi_change = oi[-1] / oi[0] - 1 if len(oi) >= 2 and oi[0] else None
        liq_dist = None
        warnings = []
        if position and position.get("liq_price") and position.get("mark"):
            m, liq = float(position["mark"]), float(position["liq_price"])
            liq_dist = (liq - m) / m if position.get("short", True) else (m - liq) / m
            if liq_dist < self.liq_warn:
                warnings.append(f"position {liq_dist:.1%} from liquidation")
        if fr is not None and abs(fr) >= 0.0005:
            warnings.append(f"funding {fr * 100:+.3f}% per 8h: crowded {'longs' if fr > 0 else 'shorts'}")
        if basis is not None and abs(basis) >= 0.005:
            warnings.append(f"basis {basis:+.2%}: perpetual far from spot")
        cost = 2 * self.perp_fee + 2 * self.slippage_bps / 10_000
        return DerivativesView("F", ts or _now_ms(), symbol, basis, premium, fr,
                               fr * 3 * 365 if fr is not None else None, velocity, oi_change, liq_dist, cost, warnings)


# ---- G: regime ----------------------------------------------------------------------------------


class RegimeAgent(Agent):
    letter, role = "G", "Market regime: label, eligible strategies, no-trade decision"

    def run(self, df: pd.DataFrame, symbol: str, timeframe: str, strategies=None) -> RegimeView:
        from ..market.regime import classify
        from ..strategies.catalog import tradeable

        reg = classify(df, timeframe)
        pool = strategies if strategies is not None else [e.cls for e in tradeable()]
        eligible = sorted(c.spec.name for c in pool if reg.label in c.eligible_regimes)
        no_trade, why = False, ""
        if reg.label == "low-liquidity":
            no_trade, why = True, reg.reason
        elif reg.label == "uncertain" and reg.confidence == 0:
            no_trade, why = True, reg.reason
        elif not eligible:
            no_trade, why = True, f"no strategy is meant for {reg.label}"
        if no_trade:
            self.log.info("%s %s: no trade (%s)", symbol, timeframe, why)
        return RegimeView("G", _close_ts(df, timeframe), symbol, reg.label, REGIME_ENUM.get(reg.label, "UNCERTAIN"),
                          reg.confidence, reg.reason, eligible, no_trade, why)


# ---- H: research --------------------------------------------------------------------------------


def dataset_fingerprint(df: pd.DataFrame) -> str:
    """Short hash of the candles (time and OHLCV): the same data always gives the same id."""
    cols = [c for c in ("ts", "open", "high", "low", "close", "volume") if c in df]
    return hashlib.sha256(np.ascontiguousarray(df[cols].to_numpy(float)).tobytes()).hexdigest()[:16]


class ResearchAgent(Agent):
    letter, role = "H", "Strategy research: walk-forward, parameter sensitivity, versioned results"

    def run(self, strategy_cls, df: pd.DataFrame, exchange: str, symbol: str, timeframe: str,
            folds: int = 4, warmup: int = 250, bump: float = 0.2) -> ResearchReport:
        from ..backtest.walkforward import sensitivity, walk_forward

        n = len(df)
        test = max(50, (n - warmup) // max(1, folds))
        wf = walk_forward(strategy_cls, df, exchange, symbol, timeframe, {}, train_bars=warmup, test_bars=test,
                          warmup=warmup)
        fold_rows = [{"start": str(r["test_start"]), "trades": int(r["trades"]),
                      "expectancy_r": float(r["expectancy_r"])} for _, r in wf.iterrows()]
        sens = sensitivity(strategy_cls, df, exchange, symbol, timeframe, bump=bump)
        sens_rows = [{"param": r["param"], "value": r["value"], "trades": int(r["trades"]),
                      "expectancy_r": float(r["expectancy_r"])} for _, r in sens.iterrows()]
        base = sens_rows[0]["expectancy_r"] if sens_rows else 0.0
        moved = [r["expectancy_r"] for r in sens_rows[1:] if r["trades"]]
        stable = bool(moved) and all((x > 0) == (base > 0) for x in moved)
        notes = []
        if not stable:
            notes.append("expectancy changes sign when a parameter moves 20%: likely overfit")
        if sum(f["trades"] for f in fold_rows) < 30:
            notes.append("fewer than 30 out-of-sample trades: too few to judge")
        return ResearchReport("H", _close_ts(df, timeframe), strategy_cls.spec.name, dataset_fingerprint(df),
                              strategy_cls.default_params(), fold_rows, sens_rows, stable, notes)


# ---- I: pair selection and portfolio -------------------------------------------------------------


class PortfolioAgent(Agent):
    letter, role = "I", "Pair selection: liquidity ranking, correlation clusters, spread"

    def __init__(self, cluster_corr: float = 0.7, max_per_cluster: int = 2, max_spread_bps: float = 10.0):
        super().__init__()
        self.cluster_corr, self.max_per_cluster, self.max_spread_bps = cluster_corr, max_per_cluster, max_spread_bps

    def run(self, ranking: dict, closes: dict[str, pd.Series], top: int = 10) -> PortfolioView:
        """``ranking``: ``market.pairs.rank_pairs`` output. ``closes``: symbol -> close series
        (same timestamps). Pairs are picked in ranking order; at most ``max_per_cluster`` from the
        BTC cluster and from the ETH cluster (coins whose returns correlate >= ``cluster_corr``)."""
        rets = pd.DataFrame({s: c.pct_change() for s, c in closes.items()}).dropna(how="all")
        corr = rets.corr() if len(rets.columns) > 1 else pd.DataFrame()
        clusters: dict[str, list[str]] = {}
        for leader in ("BTC/USDT", "ETH/USDT"):
            if leader in corr:
                clusters[leader.split("/")[0]] = sorted(s for s in corr if corr.loc[leader, s] >= self.cluster_corr)
        selected, skipped, used = [], {}, {k: 0 for k in clusters}
        for row in ranking.get("pairs", []):
            sym = row["symbol"]
            if len(selected) >= top:
                break
            sp = row.get("spread_bps")
            if sp is not None and sp > self.max_spread_bps:
                skipped[sym] = f"spread {sp:g} bps over {self.max_spread_bps:g}"
                continue
            full = next((k for k, members in clusters.items() if sym in members and used[k] >= self.max_per_cluster), None)
            if full:
                skipped[sym] = f"{full} cluster already has {self.max_per_cluster}"
                continue
            for k, members in clusters.items():
                if sym in members:
                    used[k] += 1
            selected.append(sym)
        mat = {a: {b: round(float(corr.loc[a, b]), 3) for b in corr} for a in corr} if len(corr) else {}
        return PortfolioView("I", int(ranking.get("generated") or _now_ms()), selected, clusters, mat, skipped)


# ---- J: risk ------------------------------------------------------------------------------------


class RiskAgent(Agent):
    letter, role = "J", "Deterministic risk manager: the gatekeeper for every entry"

    def __init__(self, engine=None):
        super().__init__()
        from ..trade.risk import RiskEngine

        self.engine = engine or RiskEngine()

    def run(self, symbol: str, equity: float, free_cash: float, entry: float, stop: float | None,
            open_positions: list[dict], closed: list[dict] | None = None, short: bool = False,
            leverage: float = 1.0, signal_risk: float = 0.005, size_mult: float = 1.0,
            stop_resting: bool = True, ts: int | None = None) -> RiskDecision:
        """``stop_resting``: the executor can place the stop on the exchange (it always does);
        an entry that can't have one is refused."""
        from ..trade.futures import MAX_LEVERAGE

        reasons: list[str] = []
        ts = ts or _now_ms()
        if stop is None or not math.isfinite(stop):
            return RiskDecision("J", ts, symbol, False, reasons=["no stop: every entry needs one"])
        if not stop_resting:
            return RiskDecision("J", ts, symbol, False, reasons=["the stop can't rest on the exchange"])
        lev = min(max(1.0, leverage), MAX_LEVERAGE) if short else 1.0
        if short and leverage > MAX_LEVERAGE:
            reasons.append(f"leverage cut from {leverage:g}x to {MAX_LEVERAGE:g}x")
        breach = self.engine.loss_breach(equity, closed or [])
        if breach:
            return RiskDecision("J", ts, symbol, False, leverage=lev, reasons=[breach])
        s = self.engine.size(symbol, equity, free_cash, entry, stop, signal_risk, size_mult, open_positions,
                             short=short, leverage=lev)
        ok = s.units > 0
        if not ok:
            self.log.info("%s refused: %s", symbol, "; ".join(s.reasons))
        return RiskDecision("J", ts, symbol, ok, s.units, s.notional, s.risk_usdt, lev, reasons + list(s.reasons))


# ---- K: execution and reconciliation -------------------------------------------------------------


def round_step(value: float, step: float, mode: str = "down") -> float:
    """``value`` on the exchange's grid (lot size or tick size). Quantities round down, so an
    order never asks for more than was sized."""
    if not step or step <= 0:
        return value
    q = value / step
    n = math.floor(q + 1e-9) if mode == "down" else math.ceil(q - 1e-9) if mode == "up" else round(q)
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(n * step, decimals + 2)


class ExecutionAgent(Agent):
    letter, role = "K", "Execution: precision, idempotent order ids, ledger reconciliation"

    def order_fields(self, mode: str, key: str, signal_ts: int, leg: str, qty: float, price: float | None,
                     qty_step: float, tick: float) -> dict[str, Any]:
        from ..trade.executor import client_id

        return {"orderLinkId": client_id(mode, key, signal_ts, leg), "qty": round_step(qty, qty_step, "down"),
                "price": None if price is None else round_step(price, tick, "nearest")}

    def run(self, ledger: dict[str, float], holdings: dict[str, float], tolerance: float = 0.02,
            dust: float = 0.0, ts: int | None = None) -> ExecutionReport:
        """Compare what the ledger says is held (symbol -> amount) with the account. A shortfall
        is a mismatch; an amount held that the ledger doesn't know about is reported as unknown."""
        mismatches, unknown = [], []
        for sym, expected in ledger.items():
            held = float(holdings.get(sym, 0.0))
            if held < expected * (1 - tolerance):
                mismatches.append({"symbol": sym, "ledger": expected, "account": held})
        for sym, held in holdings.items():
            if sym not in ledger and held > dust:
                unknown.append({"symbol": sym, "account": float(held)})
        for m in mismatches:
            self.log.error("ledger %s %g vs account %g", m["symbol"], m["ledger"], m["account"])
        return ExecutionReport("K", ts or _now_ms(), mismatches, unknown, not mismatches)


# ---- L: auditor ---------------------------------------------------------------------------------


class AuditorAgent(Agent):
    letter, role = "L", "Auditor: live vs backtest, degradation, promotion gate"

    def __init__(self, window: int = 30, max_se: float = 2.0):
        super().__init__()
        self.window, self.max_se = window, max_se

    def run(self, strategy: str, live_r: list[float], backtest_expectancy: float | None,
            paper_r: list[float] | None = None, entry=None, ts: int | None = None) -> AuditReport:
        """``live_r``: R of the strategy's closed trades, oldest first. ``paper_r``: the paper twin
        of each of those trades (same order) for the slippage gap. ``entry``: its registry entry,
        for the promotion ladder."""
        notes = []
        recent = live_r[-self.window:]
        live = statistics.fmean(recent) if recent else None
        degraded = False
        if live is not None and backtest_expectancy is not None and len(recent) >= 10:
            se = statistics.stdev(recent) / math.sqrt(len(recent)) if len(recent) > 1 else 0.0
            if se and live < backtest_expectancy - self.max_se * se:
                degraded = True
                notes.append(f"last {len(recent)} trades {live:+.2f}R vs backtest {backtest_expectancy:+.2f}R "
                             f"(more than {self.max_se:g} standard errors below)")
        slip = None
        if paper_r:
            pairs = list(zip(live_r[-len(paper_r):], paper_r))
            slip = statistics.fmean(a - b for a, b in pairs) if pairs else None
        state, may = "", False
        if entry is not None:
            from ..learn.ladder import ladder_state

            state, note = ladder_state(entry)
            may = state == "live_enabled"
            if note:
                notes.append(note)
        if degraded:
            self.log.warning("%s degraded: %s", strategy, notes[0])
        return AuditReport("L", ts or _now_ms(), strategy, len(live_r), live, backtest_expectancy, slip, degraded,
                           state, may, notes)


AGENTS = (MarketDataAgent, IndicatorAgent, TrendAgent, ReversalAgent, BreakoutAgent, DerivativesAgent,
          RegimeAgent, ResearchAgent, PortfolioAgent, RiskAgent, ExecutionAgent, AuditorAgent)
