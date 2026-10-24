"""Spot AI and Futures AI each have their own AI trade filter and backtest teacher."""

import json

import pandas as pd

from aitrader.ai.dataset import paper_rows
from aitrader.cli import main
from aitrader.data.store import CandleStore
from aitrader.learn import Entry, PaperConfig, PaperTrader, Registry, RetireRule, validate_spec
from aitrader.learn.picks import learns_for
from aitrader.learn.wallet import engine_path
from test_learn import EVERY_UP_BAR, H, fill_store
from test_strategies import synthetic


def test_futures_ai_keeps_its_own_files():
    assert engine_path("data/ai/models", "spot") == "data/ai/models"
    assert engine_path("data/ai/models", "futures") == "data/ai/models_futures"
    assert engine_path("data/ai/models/", "futures") == "data/ai/models_futures"
    assert engine_path("data/ai/dataset.csv", "futures") == "data/ai/dataset_futures.csv"
    assert engine_path("data/learn/teacher", "futures") == "data/learn/teacher_futures"


def test_each_engine_learns_only_from_what_it_can_trade():
    plain = Entry(id="a", kind="builtin", name="ema_crossover")
    short = Entry(id="short-a", kind="short", name="short: a", spec={"base_id": "a"})
    spot_win = Entry(id="win4h-a", kind="builtin", name="WIN 4h: a", spec={"pick": "4h"})
    fut_win = Entry(id="fwin4h-a", kind="builtin", name="WIN 4h Futures: a",
                    spec={"pick": "4h", "pick_market": "futures"})
    fut_win_short = Entry(id="short-fwin4h-a", kind="short", name="short: x",
                          spec={"base_spec": {"pick": "4h", "pick_market": "futures"}})
    assert [learns_for(e, "spot") for e in (plain, short, spot_win, fut_win, fut_win_short)] == \
           [True, False, True, False, False]
    assert [learns_for(e, "futures") for e in (plain, short, spot_win, fut_win, fut_win_short)] == \
           [True, True, False, True, True]


class _Filter:
    def __init__(self, take):
        self.take, self.asked = take, 0

    def check(self, sig, strategy_id, df, recent_r):
        self.asked += 1
        return {"take": self.take, "size": 1.0, "prob": 0.9 if self.take else 0.1, "version": "v"}


def test_paper_asks_each_engine_its_own_filter(tmp_path):
    df = synthetic(n=800)
    store = CandleStore(str(tmp_path / "c.db"))
    fill_store(store, df)
    fstore = CandleStore(str(tmp_path / "f.db"))
    fill_store(fstore, df)
    reg_path = str(tmp_path / "reg.json")
    reg = Registry(reg_path)
    reg.add(Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR), status="paper"))
    reg.save()
    spot, fut = _Filter(True), _Filter(False)
    trader = PaperTrader(store, reg_path, ["bybit"], ["BTC/USDT"],
                         PaperConfig(retire=RetireRule(min_trades=10_000, max_drawdown=1e9)),
                         state_path=str(tmp_path / "s.json"), ai_filter={"spot": spot, "futures": fut},
                         futures_store=fstore)
    for i in range(600, 700):
        trader.tick(now_ms=int(df.ts.iloc[i]) + H)
    markets = {t["market"] for t in trader.state["trades"]} | {
        "futures" if k.endswith("|futures") else "spot" for k in trader.state["open"]}
    assert spot.asked > 5 and fut.asked > 5  # both engines saw the signals...
    assert markets == {"spot"}  # ...and only Futures AI's own filter turned its trades down


def test_training_rows_come_from_one_engines_trades(tmp_path):
    df = synthetic(n=900)
    store, fstore = CandleStore(str(tmp_path / "c.db")), CandleStore(str(tmp_path / "f.db"))
    fill_store(store, df)
    fill_store(fstore, df)
    i = 700
    close = float(df["close"].iloc[i])
    base = {"strategy": "s", "signal_ts": int(df.ts.iloc[i]) + H, "entry_ts": int(df.ts.iloc[i + 1]),
            "exit_ts": int(df.ts.iloc[i + 5]), "exit_reason": "stop", "entry": float(df["open"].iloc[i + 1]),
            "r": -1.0}
    trades = [{**base, "key": "s|bybit|BTC/USDT|1h", "side": "buy", "market": "spot",
               "stop": close * 0.98, "target": close * 1.04},
              {**base, "key": "s|bybit|BTC/USDT|1h|futures", "side": "buy", "perp": True, "market": "futures",
               "stop": close * 0.98, "target": close * 1.04},
              {**base, "key": "short-s|bybit|BTC/USDT|1h", "side": "sell", "market": "futures",
               "stop": close * 1.02, "target": close * 0.96}]
    (tmp_path / "s.json").write_text(json.dumps({"trades": trades}))
    path = str(tmp_path / "s.json")
    assert len(paper_rows(store, path, futures_store=fstore)) == 3
    assert len(paper_rows(store, path, futures_store=fstore, market="spot")) == 1
    assert len(paper_rows(None, path, futures_store=fstore, market="futures")) == 2


def test_cli_trains_futures_ai_on_the_perpetuals(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AITRADER_FUTURES_DB_PATH", raising=False)
    fstore = CandleStore("data/aitrader_futures.db")  # only the perpetuals have candles
    fill_store(fstore, synthetic(n=3000, seed=7), sym="SOL/USDT")
    fstore.close()
    CandleStore("data/aitrader.db").close()
    reg = Registry("data/learn/registry.json")
    reg.add(Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR), status="paper"))
    reg.add(Entry(id="short-up", kind="short", name="short: up_bar", status="paper",
                  spec={"base_id": "up", "base_kind": "rule", "base_name": "up_bar",
                        "base_spec": validate_spec(EVERY_UP_BAR)}))
    reg.save()
    assert main(["--env-file", "none", "ai", "--market", "futures", "dataset"]) == 0
    frame = pd.read_csv("data/ai/dataset_futures.csv")
    assert set(frame["strategy"]) == {"up", "short-up"} and set(frame["symbol"]) == {"SOL/USDT"}
    # Spot AI's set has no spot candles here, and would never hold the short anyway
    main(["--env-file", "none", "ai", "dataset"])
    spot = pd.read_csv("data/ai/dataset.csv")
    assert "short-up" not in set(spot.get("strategy", []))
    assert main(["--env-file", "none", "learn", "teach-off", "--market", "futures", "--by", "t"]) == 0
    assert "Futures AI" in capsys.readouterr().out


def test_futures_ai_has_its_own_strategy_test(tmp_path, monkeypatch, capsys):
    from aitrader.learn.keep import keep_top

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AITRADER_FUTURES_DB_PATH", raising=False)
    fstore = CandleStore("data/aitrader_futures.db")  # only the perpetuals have candles
    fill_store(fstore, synthetic(n=3000, seed=7), sym="SOL/USDT")
    fstore.close()
    CandleStore("data/aitrader.db").close()
    reg = Registry("data/learn/registry.json")
    reg.add(Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR), status="paper",
                  backtest={"out_of_sample": {"trades": 50, "expectancy_r": 0.4}}))
    reg.add(Entry(id="short-up", kind="short", name="short: up_bar",
                  spec={"base_id": "up", "base_kind": "rule", "base_name": "up_bar",
                        "base_spec": validate_spec(EVERY_UP_BAR)}))
    reg.save()
    assert main(["--env-file", "none", "learn", "test", "--market", "futures"]) == 0
    out = capsys.readouterr().out
    assert "Futures AI: backtesting on the perpetuals" in out
    reg = Registry("data/learn/registry.json")
    short = reg.get("short-up")
    # the short twin is Futures AI's alone: its perpetual test is its backtest and moves it
    assert short.backtest_futures["markets"][0]["symbol"] == "SOL/USDT"
    assert short.backtest == short.backtest_futures and short.status != "research"
    assert main(["--env-file", "none", "learn", "test", "--paper", "--market", "futures", "up"]) == 0
    reg = Registry("data/learn/registry.json")
    up = reg.get("up")
    # the shared long keeps its spot result and status; Futures AI's own result sits beside it
    assert up.status == "paper" and up.backtest["out_of_sample"]["expectancy_r"] == 0.4
    assert up.backtest_futures["market"] == "futures" and up.backtest_futures["markets"][0]["symbol"] == "SOL/USDT"
    # Spot AI's test leaves the short twin out
    assert main(["--env-file", "none", "learn", "test", "short-up"]) == 0
    assert "Spot AI never trades them" in capsys.readouterr().out
    # each engine ranks on its own test
    up.backtest_futures = {"out_of_sample": {"trades": 50, "expectancy_r": -0.9}, "failed": ["bad"]}
    spot = keep_top(reg, 10, market="spot")
    fut = keep_top(reg, 10, market="futures")
    assert [k["score"] > 0 for k in spot["kept"] if k["id"] == "up"] == [True]
    assert [(k["score"] < 0, k["passed"]) for k in fut["kept"] if k["id"] == "up"] == [(True, False)]


def test_futures_test_covers_strategies_only_tested_on_spot(tmp_path, monkeypatch, capsys):
    from aitrader.learn.keep import has_own_result

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AITRADER_FUTURES_DB_PATH", raising=False)
    fstore = CandleStore("data/aitrader_futures.db")
    fill_store(fstore, synthetic(n=3000, seed=7), sym="SOL/USDT")
    fstore.close()
    CandleStore("data/aitrader.db").close()
    reg = Registry("data/learn/registry.json")
    spot_result = {"out_of_sample": {"trades": 50, "expectancy_r": -0.1}, "failed": ["weak"]}
    # rejected on spot, never tried on the perpetuals: neither `--paper` nor plain `test` picks it
    reg.add(Entry(id="up", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR), status="rejected",
                  backtest=spot_result))
    reg.add(Entry(id="done", kind="rule", name="up_bar", spec=validate_spec(EVERY_UP_BAR), status="paper",
                  backtest=spot_result, backtest_futures={"out_of_sample": {"trades": 9}}))
    reg.save()
    assert not has_own_result(reg.get("up"), "futures") and has_own_result(reg.get("up"), "spot")
    assert main(["--env-file", "none", "learn", "test", "--missing", "--market", "futures"]) == 0
    out = capsys.readouterr().out
    assert "1 strategies have no perpetual result yet" in out and "testing up" in out and "testing done" not in out
    reg = Registry("data/learn/registry.json")
    up = reg.get("up")
    assert has_own_result(up, "futures") and up.status == "rejected"  # Spot AI's verdict stays
    assert up.backtest == spot_result
    assert main(["--env-file", "none", "learn", "test", "--missing", "--market", "futures"]) == 0
    assert "nothing to test" in capsys.readouterr().out
