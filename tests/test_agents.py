"""Phase 5: agents A-L as typed interfaces, each with one message type and its own logger."""

import json
import logging
import math
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from aitrader.agents import (AGENTS, AuditorAgent, BreakoutAgent, DerivativesAgent, ExecutionAgent, IndicatorAgent,
                             MarketDataAgent, PortfolioAgent, RegimeAgent, ResearchAgent, ReversalAgent, RiskAgent,
                             TrendAgent, analyse)
from aitrader.agents.core import round_step
from aitrader.agents.messages import REGIME_ENUM, SCHEMA_VERSION
from aitrader.strategies import STRATEGIES

from test_strategies import synthetic, with_funding

HOUR = 3_600_000


def liquid(n=1500, **kw):
    df = synthetic(n=n, **kw)
    df["volume"] *= 1e5  # a market the regime classifier counts as liquid
    return df


def now_after(df):
    return int(df["ts"].iloc[-1]) + HOUR


def test_twelve_agents_each_with_its_own_logger():
    letters = [cls.letter for cls in AGENTS]
    assert letters == list("ABCDEFGHIJKL")
    for cls in AGENTS:
        a = cls()
        assert a.role and a.log.name == f"aitrader.agents.{cls.letter}"


def test_market_data_agent_flags_bad_data(caplog):
    df = liquid(300)
    ok = MarketDataAgent().run(df, "BTC/USDT", "1h", now_ms=now_after(df))
    assert ok.ok and ok.agent == "A" and ok.bars == 300
    bad = pd.concat([df.drop(df.index[100]), df.iloc[[-1]]])
    bad.iloc[-2, bad.columns.get_loc("close")] *= 1.5  # a one-bar jump of 50%
    quote = SimpleNamespace(feed_age_s=20.0)
    book = {"bids": [[100.0, 2.0]], "asks": [[100.1, 3.0]]}
    with caplog.at_level(logging.WARNING, logger="aitrader.agents.A"):
        snap = MarketDataAgent().run(bad, "BTC/USDT", "1h", now_ms=now_after(df) + 5 * HOUR, quote=quote, book=book)
    assert not snap.ok
    text = " ".join(snap.problems)
    for word in ("missing", "duplicate", "2 bars old", "ATR", "quiet for 20s"):
        assert word in text, word
    assert snap.feed_stale and snap.spread_bps == pytest.approx(9.995, rel=1e-3) and snap.depth_usdt > 0
    assert any(r.name == "aitrader.agents.A" for r in caplog.records)


def test_indicator_agent_reports_quality():
    df = liquid(600)
    rep = IndicatorAgent().run(df, "BTC/USDT", "1h")
    assert len(rep.results) == 20 and rep.invalid == ["funding"] and rep.quality == pytest.approx(0.95)
    short = IndicatorAgent().run(df.iloc[:40], "BTC/USDT", "1h")
    assert short.quality < 0.5 and "ema" in short.invalid


def _trend(drift):
    rng = np.random.default_rng(1)
    n = 600
    close = 100 * np.exp(np.cumsum(drift + rng.normal(0, 0.003, n)))
    df = pd.DataFrame({"ts": np.arange(n) * HOUR, "open": np.r_[close[0], close[:-1]], "high": close * 1.003,
                       "low": close * 0.997, "close": close, "volume": np.full(n, 1e6)})
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def test_trend_agent_direction_and_timeframe_consensus():
    up, down = _trend(0.004), _trend(-0.004)
    rep_up = IndicatorAgent().run(up, "X", "1h")
    view = TrendAgent().run(rep_up, higher={"4h": IndicatorAgent().run(up, "X", "4h"),
                                            "1d": IndicatorAgent().run(down, "X", "1d")})
    assert view.direction == 1 and view.strength > 0.2 and view.confidence > 0
    assert view.timeframes == {"1h": 1, "4h": 1, "1d": -1} and view.consensus == pytest.approx(2 / 3, abs=1e-3)
    assert TrendAgent().run(IndicatorAgent().run(down, "X", "1h")).direction == -1


def test_reversal_agent_never_calls_an_extreme_alone_a_signal():
    df = _trend(0.01)  # a steep climb: overbought, no bearish confirmation
    rep = IndicatorAgent().run(df, "X", "1h")
    view = ReversalAgent().run(df, rep)
    assert "overbought" in view.extremes.values() and view.candidate == 0
    assert view.notes and "not a signal" in view.notes[0]


def test_breakout_agent_sees_volume_and_breakouts():
    df = _trend(0.0)
    df.iloc[-1, df.columns.get_loc("volume")] = 5e6
    df.iloc[-1, df.columns.get_loc("close")] = df["high"].iloc[-30:].max() * 1.05
    df.iloc[-1, df.columns.get_loc("high")] = df["close"].iloc[-1] * 1.001
    view = BreakoutAgent().run(df, IndicatorAgent().run(df, "X", "1h"))
    assert view.volume_anomaly and view.relative_volume >= 2 and view.breakout == 1


def test_derivatives_agent_basis_funding_and_liquidation():
    snap = SimpleNamespace(last=101.0, mark=100.5, index=100.0, funding_rate=0.0008, open_interest=5.0)
    v = DerivativesAgent().run("BTC/USDT", snap, funding_history=[0.0001, 0.0001, 0.0002, 0.0008],
                               oi_history=[100, 120], position={"mark": 100.0, "liq_price": 105.0, "short": True})
    assert v.basis == pytest.approx(0.01) and v.mark_premium == pytest.approx(0.005)
    assert v.funding_velocity == pytest.approx(0.0008 - 0.0004 / 3) and v.open_interest_change == pytest.approx(0.2)
    assert v.liquidation_distance == pytest.approx(0.05) and v.round_trip_cost == pytest.approx(0.0021)
    assert len(v.warnings) == 3  # near liquidation, crowded longs, wide basis
    empty = DerivativesAgent().run("BTC/USDT")
    assert empty.basis is None and empty.warnings == []


def test_regime_agent_labels_and_no_trade():
    up = _trend(0.004)
    g = RegimeAgent().run(up, "X", "1h")
    assert g.regime == REGIME_ENUM[g.label] and g.eligible
    assert all(g.label in STRATEGIES[n].eligible_regimes for n in g.eligible)
    thin = up.assign(volume=1.0)
    g2 = RegimeAgent().run(thin, "X", "1h")
    assert g2.regime == "LOW_LIQUIDITY" and g2.no_trade and g2.no_trade_reason


def test_research_agent_versions_and_checks_stability():
    df = liquid(1200)
    rep = ResearchAgent().run(STRATEGIES["donchian_breakout"], df, "bybit", "BTC/USDT", "1h", folds=3)
    assert rep.agent == "H" and len(rep.dataset) == 16 and rep.folds and rep.sensitivity
    assert rep.sensitivity[0]["param"] == "(base)" and rep.params["n"] == 20
    again = ResearchAgent().run(STRATEGIES["donchian_breakout"], df, "bybit", "BTC/USDT", "1h", folds=3)
    assert again.dataset == rep.dataset  # same candles, same id


def test_portfolio_agent_caps_correlated_clusters():
    rng = np.random.default_rng(0)
    btc = np.cumsum(rng.normal(0, 0.01, 300))
    closes = {"BTC/USDT": pd.Series(np.exp(btc)),
              "SOL/USDT": pd.Series(np.exp(btc + rng.normal(0, 0.002, 300))),
              "AVAX/USDT": pd.Series(np.exp(btc + rng.normal(0, 0.002, 300))),
              "XRP/USDT": pd.Series(np.exp(np.cumsum(rng.normal(0, 0.01, 300))))}
    ranking = {"generated": 1, "pairs": [{"symbol": "BTC/USDT", "spread_bps": 0.1}, {"symbol": "SOL/USDT", "spread_bps": 1},
                                         {"symbol": "AVAX/USDT", "spread_bps": 2}, {"symbol": "XRP/USDT", "spread_bps": 1},
                                         {"symbol": "PEPE/USDT", "spread_bps": 30}]}
    v = PortfolioAgent().run(ranking, closes)
    assert v.selected == ["BTC/USDT", "SOL/USDT", "XRP/USDT"]
    assert "BTC cluster" in v.skipped["AVAX/USDT"] and "spread" in v.skipped["PEPE/USDT"]
    assert set(v.clusters["BTC"]) == {"BTC/USDT", "SOL/USDT", "AVAX/USDT"} and v.correlation["BTC/USDT"]["BTC/USDT"] == 1


def test_risk_agent_is_the_gatekeeper():
    j = RiskAgent()
    assert not j.run("BTC/USDT", 10_000, 10_000, 100.0, None, []).approved
    assert not j.run("BTC/USDT", 10_000, 10_000, 100.0, 95.0, [], stop_resting=False).approved
    ok = j.run("BTC/USDT", 10_000, 10_000, 100.0, 95.0, [])
    assert ok.approved and ok.risk_usdt == pytest.approx(25.0)  # 0.25% of equity
    short = j.run("BTC/USDT", 10_000, 10_000, 100.0, 105.0, [], short=True, leverage=10)
    assert short.leverage == 3.0 and any("leverage cut" in r for r in short.reasons)
    lost = [{"exit_ts": int(pd.Timestamp.now(tz="UTC").timestamp() * 1000), "pnl_usdt": -150.0}]
    breach = j.run("BTC/USDT", 9_850, 9_850, 100.0, 95.0, [], closed=lost)
    assert not breach.approved and breach.reasons


def test_execution_agent_rounds_and_reconciles():
    assert round_step(1.23456, 0.001) == 1.234 and round_step(0.0999, 0.01) == 0.09
    assert round_step(101.27, 0.5, "nearest") == 101.5
    k = ExecutionAgent()
    f = k.order_fields("live", "s1|bybit|BTC/USDT|1h", 123, "entry", 0.123456, 65_000.37, 0.0001, 0.1)
    assert f["qty"] == 0.1234 and f["price"] == 65_000.4 and f["orderLinkId"].startswith("ait")
    assert f["orderLinkId"] == k.order_fields("live", "s1|bybit|BTC/USDT|1h", 123, "entry", 1, None, 1, 1)["orderLinkId"]
    rep = k.run({"BTC": 0.5, "ETH": 2.0}, {"BTC": 0.2, "ETH": 2.0, "DOGE": 100.0})
    assert not rep.ok and rep.mismatches[0]["symbol"] == "BTC" and rep.unknown[0]["symbol"] == "DOGE"


def test_auditor_flags_degradation_and_slippage():
    live = [-0.5] * 20 + [0.2] * 10
    rep = AuditorAgent().run("s1", live, backtest_expectancy=0.4, paper_r=[0.3] * 10)
    assert rep.degraded and rep.live_expectancy == pytest.approx(statistics_mean(live))
    assert rep.slippage_r == pytest.approx(-0.1) and not rep.may_trade_live
    fine = AuditorAgent().run("s1", [0.4, 0.5, 0.3] * 5, backtest_expectancy=0.4)
    assert not fine.degraded


def statistics_mean(xs):
    return sum(xs) / len(xs)


def test_every_message_is_versioned_json():
    df = liquid(600)
    b = IndicatorAgent().run(df, "BTC/USDT", "1h")
    msgs = [MarketDataAgent().run(df, "BTC/USDT", "1h", now_ms=now_after(df)), b, TrendAgent().run(b),
            ReversalAgent().run(df, b), BreakoutAgent().run(df, b), DerivativesAgent().run("BTC/USDT"),
            RegimeAgent().run(df, "BTC/USDT", "1h"), RiskAgent().run("BTC/USDT", 1000, 1000, 100, 95, []),
            ExecutionAgent().run({}, {}), AuditorAgent().run("s", [], None)]
    for m in msgs:
        d = m.as_dict()
        assert d["schema"] == SCHEMA_VERSION and d["type"] == type(m).__name__ and d["agent"] == m.agent
        json.dumps(d, allow_nan=False)


def test_board_lists_candidates_and_reasons_for_abstaining():
    df = with_funding(liquid())
    board = analyse(df, "bybit", "BTC/USDT", "1h", now_ms=now_after(df))
    assert set(board.messages) == set("ABCDEFG")
    names = {x["strategy"] for x in board.candidates} | {x["strategy"] for x in board.abstentions}
    assert len(names) == len(board.candidates) + len(board.abstentions) >= 30
    assert all(x["reason"] for x in board.abstentions)
    for c in board.candidates:
        assert 0 <= c["confidence"] <= 1 and c["stop"] < c["entry"] < c["target"]
    # find a bar where a strategy fires and check it shows up as a candidate there
    strat = STRATEGIES["donchian_trend"]()
    sig = strat.generate(df, "bybit", "BTC/USDT", "1h")[-1]
    i = int(np.flatnonzero(df["ts"].to_numpy() + HOUR == sig.ts)[0])
    upto = df.iloc[:i + 1]
    b2 = analyse(upto, "bybit", "BTC/USDT", "1h", strategies=[strat], now_ms=sig.ts)
    assert [c["strategy"] for c in b2.candidates] == ["donchian_trend"]
    stale = analyse(upto, "bybit", "BTC/USDT", "1h", strategies=[strat], now_ms=sig.ts + 10 * HOUR)
    assert not stale.candidates and "market data" in stale.abstentions[0]["reason"]
    assert math.isfinite(board.ts)
