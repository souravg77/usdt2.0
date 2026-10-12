"""Market regime classifier (agent G) and pair ranking (agent I), with the Pairs page."""

import json
import time

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from aitrader.backtest import BacktestConfig, run_backtest
from aitrader.backtest.engine import Trade
from aitrader.config import Settings
from aitrader.data.store import CandleStore
from aitrader.learn import Entry, Gate, PaperConfig, PaperTrader, Registry, RetireRule, RuleError, evaluate
from aitrader.learn.report import backtest_report
from aitrader.learn.rules import RuleStrategy, spec_id, validate_spec
from aitrader.market import REGIMES, breakdown, classify, labels_by_close, regime_labels
from aitrader.market import pairs
from aitrader.market.regime import current_regimes
from aitrader.web import Auth, JobRunner, Paths, create_app
from aitrader.web.auth import COOKIE

from test_learn import EVERY_UP_BAR, H, fill_store
from test_strategies import synthetic

PW = "correct horse battery"
DAY = 86_400_000


def frame(close, volume=1e5, wick=0.002, tf_ms=H):
    close = np.asarray(close, float)
    open_ = np.r_[close[0], close[:-1]]
    df = pd.DataFrame({"ts": np.arange(len(close)) * tf_ms, "open": open_,
                       "high": np.maximum(open_, close) * (1 + wick),
                       "low": np.minimum(open_, close) * (1 - wick),
                       "close": close, "volume": np.full(len(close), float(volume))})
    df.index = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


NOISE = np.random.default_rng(1).standard_normal(800)


# ---- regime classifier ----------------------------------------------------------------------------


def test_regime_labels_each_kind_of_market_with_a_reason():
    up = classify(frame(100 * np.exp(np.cumsum(0.003 + 0.004 * NOISE))), "1h")
    assert up.label == "trend-up" and "ADX" in up.reason and "above rising EMA200" in up.reason
    assert 0.2 <= up.confidence <= 1 and up.ts == 800 * H  # the close of the newest bar
    down = classify(frame(100 * np.exp(np.cumsum(-0.003 + 0.004 * NOISE))), "1h")
    assert down.label == "trend-down" and "below falling EMA200" in down.reason
    flat = classify(frame(100 * (1 + 0.004 * NOISE)), "1h")
    assert flat.label == "range" and "no clear trend" in flat.reason
    calm = 100 * np.exp(np.cumsum(0.002 * NOISE))
    calm[-15:] = calm[-16] * np.exp(np.cumsum(0.05 * NOISE[-15:]))
    wild = classify(frame(calm), "1h")
    assert wild.label == "high-volatility" and "ATR" in wild.reason
    thin = classify(frame(100 * np.exp(np.cumsum(0.003 + 0.004 * NOISE)), volume=1), "1h")
    assert thin.label == "low-liquidity" and "USDT traded a day" in thin.reason
    young = classify(frame(100 * np.exp(np.cumsum(0.003 + 0.004 * NOISE)))[:100], "1h")
    assert young.label == "uncertain" and "only 100 closed bars" in young.reason
    assert classify(frame([1.0])[:0], "1h").label == "uncertain"
    assert {up.label, down.label, flat.label, wild.label, thin.label, young.label} == set(REGIMES)


def test_regime_has_no_look_ahead():
    df = synthetic(n=1500)
    df["volume"] *= 1000
    full = regime_labels(df, "1h")
    assert set(full) <= set(REGIMES) and full.nunique() >= 3
    for k in (150, 260, 700, 1100, 1500):
        # a bar's label is the same whether or not later bars exist
        assert classify(df.iloc[:k], "1h").label == full.iloc[k - 1]
        assert regime_labels(df.iloc[:k], "1h").tolist() == full.iloc[:k].tolist()
    by_close = labels_by_close(df, "1h")
    assert by_close[int(df.ts.iloc[10]) + H] == full.iloc[10]


def test_breakdown_splits_results_by_regime():
    def t(r):
        return Trade("s", "BTC/USDT", "1h", 0, 0, 100, 99, None, r=r)

    out = breakdown([("trend-up", t(1.0)), ("trend-up", t(-1.0)), ("range", t(2.0)), (None, t(0.5))])
    assert list(out) == ["trend-up", "range", "unknown"]
    assert out["trend-up"] == {"trades": 2, "win_rate": 0.5, "expectancy_r": 0.0, "total_r": 0.0}
    assert out["unknown"]["trades"] == 1


def test_current_regimes_reads_closed_stored_candles(tmp_path):
    store = CandleStore(str(tmp_path / "c.db"))
    df = synthetic(n=800)
    fill_store(store, df, ex="binance")
    now = int(df.ts.iloc[-1]) + H
    rows = current_regimes(store, ["BTC/USDT", "ETH/USDT"], ["1h", "4h"], ["bybit", "binance"], now_ms=now)
    btc = rows[0]
    assert btc["exchange"] == "binance" and btc["label"] in REGIMES and btc["ts"] == now
    assert [r["reason"] for r in rows[1:]] == ["no stored candles"] * 3
    # a bar that has not closed yet is left out
    assert current_regimes(store, ["BTC/USDT"], ["1h"], ["binance"], now_ms=now - 1)[0]["ts"] == now - H
    store.close()


# ---- regime on paper trades, the lab and the report -----------------------------------------------


def paper_setup(tmp_path, symbols):
    df = synthetic(n=900)
    store = CandleStore(str(tmp_path / "c.db"))
    fill_store(store, df)
    reg_path = str(tmp_path / "reg.json")
    reg = Registry(reg_path)
    reg.add(Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR), status="paper"))
    reg.save()
    trader = PaperTrader(store, reg_path, ["binance"], symbols,
                         PaperConfig(retire=RetireRule(min_trades=10_000, max_drawdown=1e9)),
                         state_path=str(tmp_path / "state.json"))
    return df, store, reg_path, trader


def test_paper_records_the_regime_of_each_signal_and_trade(tmp_path):
    df, store, reg_path, trader = paper_setup(tmp_path, ["BTC/USDT"])
    opened = []
    for i in range(600, 760):
        ev = trader.tick(now_ms=int(df.ts.iloc[i]) + H)
        opened += [trader.state["open"][k] for k in ev["opened"] if k in trader.state["open"]]
    assert opened and all(o["regime"]["label"] in REGIMES and o["regime"]["reason"] for o in opened)
    # the label matches the classifier run on all bars up to the signal bar (closed candles only)
    sig_ts = opened[0]["signal"]["ts"]
    assert opened[0]["regime"]["ts"] == sig_ts and opened[0]["regime"]["label"] == labels_by_close(df, "1h")[sig_ts]
    trades = trader.state["trades"]
    assert trades and all(t["regime"] in REGIMES for t in trades)
    by = Registry(reg_path).get("up").paper["by_regime"]
    assert sum(v["trades"] for v in by.values()) == len(trades)
    store.close()


def test_paper_reads_a_symbol_function_each_tick(tmp_path):
    calls = []

    def symbols():
        calls.append(1)
        if len(calls) == 2:
            raise OSError("ranking file unreadable")
        return ["BTC/USDT"]

    df, store, _reg, trader = paper_setup(tmp_path, symbols)
    for i in range(600, 640):
        trader.tick(now_ms=int(df.ts.iloc[i]) + H)
    assert len(calls) == 40 and trader.state["last_signal"]  # a broken read only skips one tick
    store.close()


def test_lab_and_report_split_results_by_regime(tmp_path):
    store = CandleStore(str(tmp_path / "c.db"))
    fill_store(store, synthetic(n=2000))
    e = Entry(id="r", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR))
    cache: dict = {}
    res = evaluate(e, store, ["binance"], ["BTC/USDT"], gate=Gate(), move=False, regimes=cache)
    assert cache and sum(v["trades"] for v in res["by_regime_oos"].values()) == res["out_of_sample"]["trades"]
    rep = backtest_report([e], store, ["binance"], ["BTC/USDT"], years=5, now_ms=2000 * H)
    s = rep["strategies"][0]
    assert sum(v["trades"] for v in s["by_regime"].values()) == s["all"]["trades"]
    assert sum(v["trades"] for v in rep["by_regime"].values()) == rep["totals"]["trades"]
    store.close()


def test_strategy_can_opt_out_of_regimes():
    spec = validate_spec(EVERY_UP_BAR)
    assert "avoid_regimes" not in spec  # existing strategies are unchanged, ids too
    avoid = validate_spec(dict(EVERY_UP_BAR, avoid_regimes=["trend-down", "range"]))
    assert avoid["avoid_regimes"] == ["range", "trend-down"] and spec_id(avoid) != spec_id(spec)
    with pytest.raises(RuleError, match="avoid_regimes"):
        validate_spec(dict(EVERY_UP_BAR, avoid_regimes=["bear"]))
    df = synthetic(n=1500)
    df["volume"] *= 1000
    labels = labels_by_close(df, "1h")
    every = RuleStrategy(spec).generate(df, "binance", "BTC/USDT", "1h")
    some = RuleStrategy(avoid).generate(df, "binance", "BTC/USDT", "1h")
    assert 0 < len(some) < len(every)
    assert all(labels[s.ts] not in ("trend-down", "range") for s in some)
    none = RuleStrategy(dict(EVERY_UP_BAR, avoid_regimes=list(REGIMES))).generate(df, "binance", "BTC/USDT", "1h")
    assert none == [] and run_backtest(RuleStrategy(avoid), df, "binance", "BTC/USDT", "1h", BacktestConfig())


# ---- pair ranking ---------------------------------------------------------------------------------

NOW = 1_760_000_000_000


def _m(base, quote="USDT", spot=True, active=True, created=None):
    return {"base": base, "quote": quote, "spot": spot, "active": active, "created": created, "info": {}}


def _t(last, turnover, spread_bps, rng_pct=5.0):
    half = last * spread_bps / 2e4
    return {"last": last, "bid": last - half, "ask": last + half, "quoteVolume": turnover,
            "high": last * (1 + rng_pct / 200), "low": last * (1 - rng_pct / 200), "percentage": 1.0}


class FakeBybit:
    def __init__(self):
        self.markets = {
            "BTC/USDT": _m("BTC", created=NOW - 2000 * DAY), "ETH/USDT": _m("ETH"), "SOL/USDT": _m("SOL"),
            "USDC/USDT": _m("USDC"), "BTC3L/USDT": _m("BTC3L"), "NEW/USDT": _m("NEW", created=NOW - 5 * DAY),
            "DUST/USDT": _m("DUST"), "WIDE/USDT": _m("WIDE"), "THIN/USDT": _m("THIN"),
            "OFF/USDT": _m("OFF", active=False), "ETH/BTC": _m("ETH", quote="BTC"),
            "BTC/USDT:USDT": _m("BTC", spot=False),
        }
        self.tickers = {
            "BTC/USDT": _t(60_000, 1e9, 0.2), "ETH/USDT": _t(3000, 5e8, 0.5),
            "SOL/USDT": _t(150, 4e8, 0.7, rng_pct=40), "USDC/USDT": _t(1, 5e8, 0.1),
            "BTC3L/USDT": _t(2, 1e8, 1), "NEW/USDT": _t(1, 5e7, 5), "DUST/USDT": _t(1, 1e5, 5),
            "WIDE/USDT": _t(1, 1e7, 120), "THIN/USDT": _t(1, 1e7, 5), "OFF/USDT": _t(1, 1e7, 5),
            "ETH/BTC": _t(0.05, 1e3, 5),
        }
        self.books = []
        self.closed = False

    async def load_markets(self):
        return self.markets

    async def fetch_tickers(self):
        return self.tickers

    async def fetch_order_book(self, symbol, limit=None):
        self.books.append(symbol)
        t = self.tickers[symbol]
        size = 10.0 if symbol == "THIN/USDT" else 1e7 / t["last"]
        return {"bids": [[t["bid"], size / 2], [t["bid"] * 0.995, size / 2], [t["bid"] * 0.9, 1e9]],
                "asks": [[t["ask"], size / 2], [t["ask"] * 1.005, size / 2], [t["ask"] * 1.1, 1e9]]}

    async def close(self):
        self.closed = True


def registry_with_results(tmp_path) -> str:
    path = str(tmp_path / "reg.json")
    passed = {"id": "a", "backtest": {"failed": [], "markets": [
        {"exchange": "bybit", "symbol": "ETH/USDT", "timeframe": "1h", "trades_oos": 50, "exp_oos": 0.3}]}}
    failed = {"id": "b", "backtest": {"failed": ["too few"], "markets": [
        {"exchange": "bybit", "symbol": "BTC/USDT", "timeframe": "1h", "trades_oos": 500, "exp_oos": -1}]}}
    json.dump({"version": 1, "strategies": [passed, failed]}, open(path, "w"))
    return path


async def test_ranking_filters_scores_and_explains(tmp_path):
    client = FakeBybit()
    paper = tmp_path / "paper.json"
    json.dump({"trades": [{"key": "a|bybit|ETH/USDT|1h", "r": 1.0}]}, open(paper, "w"))
    rk = await pairs.rank_pairs(client, registry_path=registry_with_results(tmp_path),
                                paper_state=str(paper), now_ms=NOW)
    assert rk["generated"] == NOW and rk["exchange"] == "bybit" and rk["market_type"] == "spot"
    ranked = [p["symbol"] for p in rk["pairs"]]
    # ETH trades a little less than BTC but has validated results on the platform, so it leads
    assert ranked == ["ETH/USDT", "BTC/USDT", "SOL/USDT"]
    assert [p["rank"] for p in rk["pairs"]] == [1, 2, 3]
    plain = await pairs.rank_pairs(FakeBybit(), now_ms=NOW)
    assert [p["symbol"] for p in plain["pairs"]] == ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    why = {x["symbol"]: x["why"] for x in rk["excluded"]}
    assert why["USDC/USDT"] == "stablecoin against a stablecoin"
    assert why["BTC3L/USDT"] == "leveraged token"
    assert why["NEW/USDT"].startswith("listed 5 days ago")
    assert "turnover" in why["DUST/USDT"] and "spread" in why["WIDE/USDT"]
    assert "depth" in why["THIN/USDT"] and why["OFF/USDT"].startswith("not trading")
    assert "ETH/BTC" not in why and "BTC/USDT:USDT" not in why  # other markets are not listed
    eth = rk["pairs"][0]
    assert eth["validated"] == {"trades": 51, "expectancy_r": round((50 * 0.3 + 1) / 51, 4),
                                "oos_trades": 50, "paper_trades": 1}
    assert any("51 validated trades" in r for r in eth["reasons"])
    btc = rk["pairs"][1]
    assert btc["validated"] is None  # results of a strategy that failed the gate do not count
    assert btc["depth_usdt"] > 25_000 and btc["listed_days"] == 2000
    assert any("depth" in r for r in btc["reasons"]) and any("turnover" in r for r in btc["reasons"])
    sol = rk["pairs"][2]
    assert any("fast" in r for r in sol["reasons"])  # very volatile: loses points, never gains
    assert "survivorship" in rk["note"]
    assert "THIN/USDT" in client.books

    path = pairs.save_ranking(rk, str(tmp_path / "p" / "ranking.json"))
    assert pairs.load_ranking(path)["pairs"] == rk["pairs"]
    assert "BTC/USDT" in pairs.ranking_text(rk)


def test_token_filters():
    assert pairs.is_stablecoin("usdc") and pairs.is_stablecoin("FDUSD") and not pairs.is_stablecoin("BTC")
    for base in ("BTC3L", "ETH3S", "ADA2L"):
        assert pairs.is_leveraged_token(base)
    for base in ("BTC", "JUP", "C98", "1INCH", "L3", "SOL"):
        assert not pairs.is_leveraged_token(base)


def test_configured_symbols_falls_back_to_the_fixed_list(tmp_path):
    fixed = ["BTC/USDT", "ETH/USDT"]
    path = str(tmp_path / "ranking.json")
    ranked = {"AITRADER_PAIRS": "ranked", "AITRADER_PAIRS_TOP": "2"}
    assert pairs.configured_symbols(fixed, env={}, path=path) == fixed  # default: fixed list
    assert pairs.configured_symbols(fixed, env=ranked, path=path, now_ms=NOW) == fixed  # missing
    pairs.save_ranking({"generated": NOW, "pairs": [{"symbol": s} for s in ("SOL/USDT", "XRP/USDT", "ADA/USDT")]},
                       path)
    assert pairs.configured_symbols(fixed, env=ranked, path=path, now_ms=NOW + H) == ["SOL/USDT", "XRP/USDT"]
    assert pairs.configured_symbols(fixed, env={}, path=path, now_ms=NOW) == fixed  # not turned on
    stale = NOW + 49 * H
    assert pairs.configured_symbols(fixed, env=ranked, path=path, now_ms=stale) == fixed
    assert pairs.configured_symbols(fixed, env={**ranked, "AITRADER_PAIRS_MAX_AGE_HOURS": "72"}, path=path,
                                    now_ms=stale) == ["SOL/USDT", "XRP/USDT"]
    assert pairs.configured_symbols(fixed, env=ranked, path=path, now_ms=NOW,
                                    keep=["DOGE/USDT", "SOL/USDT"]) == ["SOL/USDT", "XRP/USDT", "DOGE/USDT"]
    pairs.save_ranking({"generated": NOW, "pairs": []}, path)
    assert pairs.configured_symbols(fixed, env=ranked, path=path, now_ms=NOW) == fixed  # empty


def test_open_paper_symbols(tmp_path):
    p = tmp_path / "s.json"
    json.dump({"open": {"a|binance|SOL/USDT|1h": {}, "b|binance|BTC/USDT|4h": {}}}, open(p, "w"))
    assert pairs.open_paper_symbols(str(p)) == ["BTC/USDT", "SOL/USDT"]
    assert pairs.open_paper_symbols(str(tmp_path / "none.json")) == []


def test_cli_pairs_rank_show_and_regimes(tmp_path, monkeypatch, capsys):
    from aitrader import cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AITRADER_DB_PATH", str(tmp_path / "c.db"))
    fake = FakeBybit()
    monkeypatch.setattr(pairs, "public_client", lambda exchange_id="bybit": fake)
    out = str(tmp_path / "ranking.json")
    assert cli.main(["--env-file", "", "pairs", "rank", "--out", out]) == 0
    assert fake.closed and json.load(open(out))["pairs"][0]["symbol"] == "BTC/USDT"
    assert cli.main(["--env-file", "", "pairs", "show", "--out", out]) == 0
    assert "BTC/USDT" in capsys.readouterr().out
    assert cli.main(["--env-file", "", "pairs", "show", "--out", str(tmp_path / "none.json")]) == 1
    store = CandleStore(str(tmp_path / "c.db"))
    df = synthetic(n=800)
    df["ts"] += int(time.time() * 1000) - 800 * H
    fill_store(store, df)
    store.close()
    capsys.readouterr()
    assert cli.main(["--env-file", "", "pairs", "regimes", "--symbol", "BTC/USDT", "--timeframe", "1h"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("BTC/USDT") and any(r in line for r in REGIMES)


# ---- the Pairs page -------------------------------------------------------------------------------


def app_client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "none.env"))
    monkeypatch.setattr(runner, "command", lambda step: ["sleep", "1"])
    paths = Paths(pairs=str(tmp_path / "pairs" / "ranking.json"), paper_state=str(tmp_path / "paper.json"),
                  report=str(tmp_path / "report.json"))
    app = create_app(Settings(db_path=str(tmp_path / "c.db"), symbols=["BTC/USDT", "ETH/USDT"]),
                     Auth("admin", PW, "k"), runner, paths)
    return TestClient(app), runner, paths


def test_pairs_page_needs_login(tmp_path, monkeypatch):
    client, _runner, _paths = app_client(tmp_path, monkeypatch)
    assert client.get("/pairs", follow_redirects=False).status_code == 303
    assert client.post("/pairs/rank", follow_redirects=False).status_code == 303


async def test_pairs_page_shows_ranking_regimes_and_results(tmp_path, monkeypatch):
    client, runner, paths = app_client(tmp_path, monkeypatch)
    client.post("/login", data={"user": "admin", "password": PW})
    page = client.get("/pairs").text
    assert "No pair ranking yet" in page and 'href="/pairs"' in page and "BTC/USDT" in page
    assert "survivorship bias" in page

    rk = await pairs.rank_pairs(FakeBybit(), now_ms=NOW)
    rk["generated"] = int(time.time() * 1000)
    pairs.save_ranking(rk, paths.pairs)
    store = CandleStore(str(tmp_path / "c.db"))
    df = synthetic(n=800)
    df["ts"] += int(time.time() * 1000) - 800 * H
    fill_store(store, df, ex="binance")
    store.close()
    json.dump({"trades": [{"key": "a|binance|BTC/USDT|1h", "r": 1.5, "regime": "trend-up"},
                          {"key": "a|binance|BTC/USDT|1h", "r": -1.0}]}, open(paths.paper_state, "w"))
    json.dump({"by_regime": {"range": {"trades": 40, "win_rate": 0.4, "expectancy_r": -0.05, "total_r": -2.0}}},
              open(paths.report, "w"))
    page = client.get("/pairs").text
    assert "Ranked pairs" in page and "Excluded (" in page and "leveraged token" in page
    assert page.index("BTC/USDT") < page.index("SOL/USDT")
    store = CandleStore(str(tmp_path / "c.db"))
    [now] = current_regimes(store, ["BTC/USDT"], ["1h"], ["binance"])
    store.close()
    assert f'>1h {now["label"]}</span>' in page and now["exchange"] == "binance"
    assert "Paper trades" in page and "trend-up" in page and "unknown" in page
    assert "Backtest report" in page and "-0.050" in page

    csrf = Auth("admin", PW, "k").csrf(client.cookies.get(COOKIE))
    assert client.post("/pairs/rank", data={"csrf": "bad"}).status_code == 403
    r = client.post("/pairs/rank", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and "Ranking%20started" in r.headers["location"]
    [job] = [j for j in runner.list() if j["meta"].get("kind") == "pairs"]
    assert job["steps"] == [["pairs", "rank", "--out", paths.pairs]]
    again = client.post("/pairs/rank", data={"csrf": csrf}, follow_redirects=False)
    assert "already%20running" in again.headers["location"]
