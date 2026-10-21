"""Fee filter: skip trades whose stop is so close that fees and slippage would eat the risk, and
the Replay's Top 10/20/30 ranked on the chosen timeframe."""

import pytest

from aitrader.backtest import BacktestConfig, run_backtest
from aitrader.backtest.engine import cost_r, too_costly
from aitrader.data.store import CandleStore
from aitrader.learn import Entry, PaperConfig, PaperTrader, Registry, RetireRule, validate_spec
from aitrader.learn.rules import RuleStrategy
from aitrader.models import Side
from aitrader.strategies.base import Signal
from aitrader.web.replay_pages import timeframe_ranking

from test_learn import EVERY_UP_BAR, H, fill_store
from test_strategies import synthetic

TIGHT = dict(EVERY_UP_BAR, name="tight", stop={"type": "percent", "pct": 0.002})


def sig(entry, stop, side=Side.BUY):
    return Signal(ts=0, exchange="bybit", symbol="BTC/USDT", timeframe="5m", side=side, entry=entry, stop=stop,
                  target=None, strategy="t")


def test_cost_share_of_the_risk():
    cfg = BacktestConfig()  # 0.1% spot fee and 0.05% slippage each way
    # 0.2% stop: the round trip costs about 1.5 times the risk
    assert cost_r(sig(100, 99.8), cfg) == pytest.approx(199.8 * 0.0015 / 0.2)
    assert too_costly(sig(100, 99.8), cfg)
    assert cost_r(sig(100, 98), cfg) == pytest.approx(198 * 0.0015 / 2) and not too_costly(sig(100, 98), cfg)
    # shorts pay the cheaper perp fee
    assert cost_r(sig(100, 101, Side.SELL), cfg) == pytest.approx(201 * 0.00105 / 1)
    assert not too_costly(sig(100, 99.8), BacktestConfig(max_cost_r=0))  # 0 turns it off
    assert cost_r(sig(100, 100), cfg) == float("inf")


def test_backtest_skips_trades_fees_would_eat():
    df = synthetic(n=900)
    strat = RuleStrategy(validate_spec(TIGHT))
    costly = []
    assert run_backtest(strat, df, "bybit", "BTC/USDT", "1h", BacktestConfig(), costly=costly) == []
    assert costly
    off = run_backtest(strat, df, "bybit", "BTC/USDT", "1h", BacktestConfig(max_cost_r=0))
    assert off and sum(t.r for t in off) / len(off) < -0.5  # what those trades did: fees ate them
    wide = RuleStrategy(validate_spec(EVERY_UP_BAR))  # 2% stop: costs ~0.15R, kept
    assert run_backtest(wide, df, "bybit", "BTC/USDT", "1h", BacktestConfig())


def test_paper_skips_trades_fees_would_eat(tmp_path):
    df = synthetic(n=700)
    store = CandleStore(str(tmp_path / "c.db"))
    fill_store(store, df)
    reg_path = str(tmp_path / "reg.json")
    reg = Registry(reg_path)
    reg.add(Entry(id="tight", kind="rule", name="tight", spec=validate_spec(TIGHT), status="paper"))
    reg.save()
    trader = PaperTrader(store, reg_path, ["bybit"], ["BTC/USDT"],
                         PaperConfig(retire=RetireRule(min_trades=10_000, max_drawdown=1e9)),
                         state_path=str(tmp_path / "s.json"))
    filtered = []
    for i in range(600, len(df)):
        ev = trader.tick(now_ms=int(df.ts.iloc[i]) + H)
        assert not ev["opened"]
        filtered += ev["filtered"]
    assert filtered and trader.state["open"] == {}
    sigs = trader.state["signals"]  # every signal and why it was skipped, for the Trading page
    assert len(sigs) == len(filtered) and all(x["skipped"] == "fees too high for the stop" for x in sigs)
    assert "limit 0.25R" in sigs[0]["detail"] and trader.state["blocked"] == {}
    lp = trader.state["last_pass"]  # what the last pass looked at
    assert lp["strategies"] == 1 and lp["pairs"] == 1 and lp["markets"] == 1 and lp["newest_candle"]

    # a big news event: the block names it, so the page can say which and when
    from types import SimpleNamespace
    trader.calendar = SimpleNamespace(refresh=lambda: True, decide=lambda at: SimpleNamespace(
        allow_entries=False, size_multiplier=0.0, reason="CPI m/m (USD) at 18:00: no new entries before release"))
    trader.tick(now_ms=int(df.ts.iloc[-1]) + 2 * H)
    assert trader.state["blocked"]["why"] == ("the economic-calendar filter: CPI m/m (USD) at 18:00: "
                                              "no new entries before release")
    store.close()


def test_trading_page_says_why_no_trade_opened():
    from aitrader.web.app import _signals_section

    now = 1_800_000_000_000
    assert "No strategy gave a signal in the last 24 hours" in _signals_section({}, now)
    paper = {"blocked": {"why": "a pause or the kill switch"},
             "signals": [{"ts": now - 90_000_000, "key": "old|bybit|BTC/USDT|1h", "skipped": ""},  # over a day ago
                         {"ts": now - 3_600_000, "key": "a|bybit|BTC/USDT|1h", "side": "buy", "entry": 100,
                          "stop": 99.5, "skipped": "fees too high for the stop", "detail": "0.60R of the risk"},
                         {"ts": now - 1_800_000, "key": "b|bybit|ETH/USDT|4h", "side": "sell", "entry": 10,
                          "stop": 10.5, "skipped": ""}]}
    html = _signals_section(paper, now)
    assert "blocked right now by a pause or the kill switch" in html
    assert "<b>2</b> signals" in html and ">1</b> opened" in html and "fees too high for the stop: 1" in html
    assert "0.60R of the risk" in html and "old" not in html
    paper["last_pass"] = {"at": now, "strategies": 10, "pairs": 19, "markets": 40, "newest_candle": now - 600_000}
    assert "10 strategies on 19 pairs" in _signals_section(paper, now) and "10 min ago" in _signals_section(paper, now)
    paper["last_pass"]["newest_candle"] = now - 4 * 3_600_000  # the collector stopped
    assert "prices are not updating" in _signals_section(paper, now)
    # Shawn: only 4h charts read (Futures AI WIN picks): a 4h candle 135 min old is on time
    paper["last_pass"].update(newest_candle=now - 135 * 60_000, step=4 * 3_600_000)
    assert "prices are not updating" not in _signals_section(paper, now)
    paper["last_pass"]["newest_candle"] = now - 5 * 3_600_000
    assert "prices are not updating" in _signals_section(paper, now)
    # while entries are blocked the charts aren't read: that's not a collector problem
    paper["last_pass"] = {"at": now, "strategies": 10, "pairs": 20, "markets": 0, "newest_candle": None}
    html = _signals_section(paper, now)
    assert "collector" not in html and "read again once new entries are allowed" in html


def test_top_strategies_ranked_on_the_chosen_timeframe():
    rows = [
        {"id": "great_1h", "markets": [{"timeframe": "1h", "trades": 200, "total_r": 80},
                                       {"timeframe": "5m", "trades": 300, "total_r": -90}]},
        {"id": "ok_5m", "markets": [{"timeframe": "5m", "trades": 100, "total_r": 10}]},
        {"id": "lucky_5m", "markets": [{"timeframe": "5m", "trades": 2, "total_r": 2}]},  # 1R a trade, but too few to trust
        {"id": "no_5m", "markets": [{"timeframe": "4h", "trades": 50, "total_r": 30}]},
    ]
    assert timeframe_ranking(rows, "5m") == ["ok_5m", "lucky_5m", "great_1h"]
    assert timeframe_ranking(rows, "1h") == ["great_1h"]
    assert timeframe_ranking([], "5m") == []
