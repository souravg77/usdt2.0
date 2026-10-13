"""Keep only the best strategies paper trading, and the indicators they use."""

from aitrader.learn import Entry, Registry
from aitrader.learn.keep import (BENCHED, indicators, indicators_used, keep_top, rank, saved_n, save_n,
                                 score)


def bt(trades, exp, failed=()):
    return {"out_of_sample": {"trades": trades, "expectancy_r": exp}, "failed": list(failed)}


def entry(eid, trades=0, exp=0.0, failed=(), paper=None, status="paper", name="trend_pullback"):
    e = Entry(id=eid, kind="builtin", name=name, status=status)
    e.backtest = bt(trades, exp, failed) if trades or failed else {}
    e.paper = paper or {}
    return e


def make(tmp_path, *entries):
    reg = Registry(str(tmp_path / "reg.json"))
    for e in entries:
        reg.add(e)
    return reg


def test_few_trades_count_for_less_and_passed_strategies_rank_first():
    lucky = entry("lucky", trades=3, exp=2.0)
    steady = entry("steady", trades=120, exp=0.4)
    assert score(steady) > score(lucky)  # 3 lucky trades don't beat 120 good ones
    assert score(entry("p", trades=50, exp=0.2, paper={"trades": 50, "expectancy_r": -0.6})) < 0
    assert score(entry("none")) is None


def test_keep_top_benches_the_rest_and_can_bring_them_back(tmp_path):
    reg = make(tmp_path, *[entry(f"s{i}", trades=100, exp=i / 10) for i in range(25)],
               entry("failed", trades=500, exp=1.5, failed=["drawdown"]),
               entry("untested"),
               entry("poor", trades=100, exp=0.9, status="retired"))
    reg.get("poor").history.append({"why": "paper loss limit"})
    out = keep_top(reg, 10)
    kept = [k["id"] for k in out["kept"]]
    assert kept == [f"s{i}" for i in range(24, 14, -1)]  # passed first: the failed one ranks last
    assert reg.get("failed").status == "retired" and reg.get("untested").status == "retired"
    assert reg.get("s3").history[-1]["why"].startswith(BENCHED)
    assert reg.get("poor").status == "retired"  # retired for its paper results: stays retired
    assert len(reg.with_status("paper")) == 10 and out["untested"] == 1

    # s3 does well in paper trading: the next ranking brings it back
    reg.get("s3").paper = {"trades": 400, "expectancy_r": 3.0}
    out = keep_top(reg, 10)
    assert "s3" in out["returned"] and reg.get("s3").status == "paper" and "s15" in out["benched"]
    assert reg.get("poor") not in [e for e, _ in rank(reg)]


def test_keep_top_keeps_n_even_when_results_are_negative(tmp_path):
    reg = make(tmp_path, *[entry(f"s{i}", trades=100, exp=-0.1 * i) for i in range(12)])
    out = keep_top(reg, 10)
    assert len(out["kept"]) == 10 and len(out["benched"]) == 2 and out["kept"][0]["id"] == "s0"


def test_indicators_come_from_the_rules():
    assert indicators(entry("a", name="trend_pullback")) == {"ema", "adx", "rsi", "sr", "atr"}  # market structure
    assert indicators(entry("b", name="supertrend_trend")) >= {"st"}
    assert "ema" not in indicators(entry("f", name="fibonacci_reaction"))  # "smaller" isn't SMA
    assert indicators(entry("d", name="range_support_bounce")) >= {"sr", "adx"}  # its channel
    assert {"fib"} <= indicators(entry("f", name="fibonacci_reaction"))
    assert {"vp"} <= indicators(entry("v", name="value_area_reversion"))
    assert {"kc"} <= indicators(entry("k", name="keltner_breakout"))
    assert {"pat"} <= indicators(entry("h", name="hammer_reversal"))
    assert {"cci"} <= indicators(entry("c", name="cci_divergence"))
    assert {"willr"} <= indicators(entry("w", name="williams_r_reversal"))
    rule = Entry(id="r", kind="rule", name="r",
                 spec={"description": "MACD and Bollinger in the text only",
                       "decisions": ["volume confirmation approximated with rsi(14) < 30"],
                       "uncodable": ["a parabolic SAR exit"],
                       "entry": [{"left": "close", "op": ">", "right": "vwap(20)"},
                                 {"left": "rsi(14)", "op": "<", "right": "30"}]})
    assert indicators(rule) == {"vwap", "rsi"}  # the description and the notes don't count
    div = Entry(id="d", kind="rule", name="d", spec={"entry": [{"left": "rsi_bull_div(14,30)", "op": ">=", "right": 1}]})
    assert indicators(div) == {"rsi"}
    short = Entry(id="short-r", kind="short", name="short: r",
                  spec={"base_id": "r", "base_kind": "rule", "base_name": "r", "base_spec": rule.spec})
    assert indicators(short) == {"vwap", "rsi"}


def test_indicators_used_counts_paper_strategies(tmp_path):
    reg = make(tmp_path, entry("a", name="trend_pullback"), entry("b", name="buy_the_dip"),
               entry("c", name="supertrend_trend", status="retired"))
    used = indicators_used(reg)
    assert used["ema"] == 2 and used["rsi"] == 2 and used["adx"] == 1 and "st" not in used
    assert list(used)[:3] == ["atr", "ema", "rsi"]  # most used first, then by name


def test_saved_n_is_clamped(tmp_path):
    path = str(tmp_path / "learn" / "registry.json")
    assert saved_n(path) == 15
    assert save_n(path, 40) == 20 and saved_n(path) == 20
    assert save_n(path, 3) == 10


def test_cli_keep_top(tmp_path, monkeypatch, capsys):
    from aitrader.cli import main

    monkeypatch.chdir(tmp_path)
    reg = make(tmp_path, *[entry(f"s{i}", trades=100, exp=i / 10) for i in range(14)])
    reg.save()
    args = ["--env-file", "none", "learn", "--registry", reg.path, "keep-top"]
    assert main(args + ["--n", "12"]) == 0
    text = capsys.readouterr().out
    assert "the best 12 strategies" in text and "2 taken out" in text and "indicators they use: adx (12)" in text
    assert len(Registry(reg.path).with_status("paper")) == 12
    assert main(args) == 0 and saved_n(reg.path) == 12  # the last number is remembered
