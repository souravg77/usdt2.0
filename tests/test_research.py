"""Trader-research log: cleaning, URL dedupe, the search turned off, the CLI and the Research page
(filters, escaping, only http(s) links). Findings are research only: nothing that trades reads them."""

import os

import pytest
from fastapi.testclient import TestClient

from aitrader.config import Settings
from aitrader.news import research
from aitrader.web import Auth, JobRunner, Paths, create_app
from aitrader.web.auth import COOKIE

PW = "correct horse battery"
NOW = 1_760_000_000_000  # 2025-10-09

FINDINGS = {"findings": [
    {"url": "https://www.reddit.com/r/algotrading/comments/abc/#top", "source": "Reddit r/algotrading",
     "source_date": "2025-09-30", "family": "backtesting-execution",
     "claim": "Hundreds of strategies were cut to ten after walk-forward checks.",
     "evidence": "anecdote", "credibility": "One user's account; no code or data shared."},
    {"url": "https://www.tradingview.com/script/xyz/", "source": "TradingView <script>alert(1)</script>",
     "source_date": "last week", "family": "Breakout", "claim": "Breakouts  fail\nmore in ranges.",
     "evidence": "Backtest shown", "credibility": "Backtest without fees."},
    {"url": "javascript:alert(1)", "family": "breakout", "claim": "bad link", "evidence": "anecdote"},
    {"url": "https://ex.com/a", "family": "martingale", "claim": "unknown family", "evidence": "anecdote"},
    {"url": "https://ex.com/b", "family": "divergence", "claim": "", "evidence": "anecdote"},
    {"url": "https://papers.example/p1", "family": "divergence", "claim": "RSI divergence is weak alone.",
     "evidence": "rumour", "credibility": "Working paper."},
]}


def test_clean_keeps_known_values_only():
    got = research.clean(FINDINGS, NOW)
    assert [f["url"] for f in got] == ["https://www.reddit.com/r/algotrading/comments/abc/#top",
                                       "https://www.tradingview.com/script/xyz/", "https://papers.example/p1"]
    a, b, c = got
    assert a["date_found"] == "2025-10-09" and a["source_date"] == "2025-09-30" and a["evidence"] == "anecdote"
    assert set(a) == {"url", "source", "date_found", "source_date", "family", "claim", "evidence", "credibility"}
    assert b["family"] == "breakout" and b["evidence"] == "backtest shown" and b["source_date"] == ""
    assert b["claim"] == "Breakouts fail more in ranges."
    assert c["evidence"] == "other" and c["source"] == "papers.example"


def test_add_dedupes_by_url(tmp_path):
    d = str(tmp_path / "news")
    assert len(research.add(research.clean(FINDINGS, NOW), d)) == 3
    again = {"findings": [dict(FINDINGS["findings"][0], url="HTTPS://WWW.REDDIT.COM/r/algotrading/comments/abc"),
                          {"url": "https://new.example/q", "family": "buy-the-dip", "claim": "Dips kept dipping.",
                           "evidence": "live record shown", "credibility": "Broker statements shown."}]}
    added = research.add(research.clean(again, NOW + 86_400_000), d)
    assert [f["url"] for f in added] == ["https://new.example/q"]
    rows = research.load(d)
    assert len(rows) == 4 and rows[0]["url"] == "https://new.example/q" and rows[0]["date_found"] == "2025-10-10"
    assert open(research.log_path(d)).read().count("\n") == 4


def test_search_is_turned_off(tmp_path, monkeypatch, capsys):
    """It searched the web through the Anthropic API, which the platform no longer uses."""
    from aitrader.cli import main
    from aitrader.ops import EventLog

    events = EventLog(str(tmp_path / "events.jsonl"), service="research")
    out = research.update(str(tmp_path / "news"), events=events)
    assert out["added"] == [] and out["error"] == research.OFF
    assert not os.path.exists(research.log_path(str(tmp_path / "news")))
    assert events.read()[-1]["level"] == "warning" and "turned off" in events.read()[-1]["text"]
    monkeypatch.chdir(tmp_path)
    assert main(["research", "update"]) == 1 and "turned off" in capsys.readouterr().err


def test_research_never_imported_by_trading_code():
    """Research findings are inputs for people only: no trading, risk or strategy code reads them."""
    import aitrader

    root = os.path.dirname(aitrader.__file__)
    for sub in ("trade", "strategies", "learn", "exchanges", "accounts", "ai"):
        for dirpath, _dirs, files in os.walk(os.path.join(root, sub)):
            for name in files:
                if name.endswith(".py"):
                    text = open(os.path.join(dirpath, name), encoding="utf-8").read()
                    assert "news.research" not in text and "import research" not in text, name
    for name in ("safety.py", "throttle.py"):
        assert "news.research" not in open(os.path.join(root, name), encoding="utf-8").read()


def test_cli_show(tmp_path, monkeypatch, capsys):
    from aitrader.cli import main

    monkeypatch.chdir(tmp_path)
    research.add(research.clean(FINDINGS, NOW))
    assert main(["research", "show", "--family", "divergence"]) == 0
    out = capsys.readouterr().out
    assert "RSI divergence is weak alone." in out and "Breakouts" not in out and "1 findings" in out


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "none.env"))
    monkeypatch.setattr(runner, "command", lambda step: ["sleep", "1"])
    paths = Paths(news=str(tmp_path / "news"))
    app = create_app(Settings(db_path=str(tmp_path / "x.db"), symbols=["BTC/USDT"]), Auth("admin", PW, "k"),
                     runner, paths)
    return TestClient(app), runner, paths


def test_research_page(client):
    c, runner, paths = client
    assert c.get("/research", follow_redirects=False).status_code == 303
    c.post("/login", data={"user": "admin", "password": PW})
    page = c.get("/research").text
    assert "No findings yet" in page and "Research input only" in page and "New searches are off" in page
    assert "Search the web now" not in page
    research.add([{"url": "javascript:alert(1)", "source": "evil", "date_found": "2025-10-09", "source_date": "",
                   "family": "breakout", "claim": "<img src=x onerror=alert(1)>", "evidence": "anecdote",
                   "credibility": ""}], paths.news)  # a hand-edited bad line is still shown safely
    research.add(research.clean(FINDINGS, NOW), paths.news)
    page = c.get("/research").text
    assert "Hundreds of strategies" in page and "RSI divergence" in page and "4 of 4 findings" in page
    assert "<script>alert" not in page and "&lt;script&gt;" in page and "<img src=x" not in page
    assert 'href="javascript:' not in page and 'href="https://papers.example/p1"' in page
    page = c.get("/research?family=divergence").text
    assert "RSI divergence" in page and "Hundreds of strategies" not in page and "1 of 4 findings" in page
    page = c.get("/research?evidence=anecdote").text
    assert "Hundreds of strategies" in page and "RSI divergence" not in page
    page = c.get("/research?family=<b>&evidence=nope").text  # unknown filters are ignored
    assert "4 of 4 findings" in page and "<b>" not in page.split("Strategy family")[1][:200]
    csrf = Auth("admin", PW, "k").csrf(c.cookies.get(COOKIE))
    assert c.post("/research/refresh", data={"csrf": csrf}).status_code in (404, 405)  # no search to start
    assert not [j for j in runner.list() if j["meta"].get("kind") == "research"]
