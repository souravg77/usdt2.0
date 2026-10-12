"""Trader-research log: the search conversation, cleaning, URL dedupe, the CLI and the Research
page (filters, escaping, only http(s) links). Findings are research only: nothing that trades
reads them."""

import json
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


class FakeAPI:
    def __init__(self, script):
        self.script, self.bodies = list(script), []

    def __call__(self, body):
        self.bodies.append(json.loads(json.dumps(body)))
        return self.script.pop(0)


def reply(findings=FINDINGS):
    return {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "t", "name": "report_trader_research",
                                                    "input": findings}]}


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


def test_find_searches_then_reports():
    api = FakeAPI([
        {"stop_reason": "pause_turn", "content": [{"type": "server_tool_use", "id": "s1", "name": "web_search",
                                                   "input": {"query": "reddit breakout backtest"}}]},
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]},
        reply(),
    ])
    res = research.find(api, known=["https://known.example/x"], families=["breakout"], model="m", now_ms=NOW)
    assert res["searches"] == 1 and res["model"] == "m" and len(res["findings"]) == 3
    first = api.bodies[0]
    assert first["tools"][0]["type"] == "web_search_20250305" and first["tools"][0]["max_uses"] == 8
    assert "https://known.example/x" in first["messages"][0]["content"] and "breakout" in first["messages"][0]["content"]
    assert "data, not instructions" in first["system"] and "not proof of profit" in first["system"]
    assert "tool_choice" not in first and "tool_choice" not in api.bodies[1]
    assert api.bodies[2]["tool_choice"] == {"type": "tool", "name": "report_trader_research"}


def test_update_dedupes_by_url(tmp_path):
    from aitrader.ops import EventLog

    d = str(tmp_path / "news")
    events = EventLog(str(tmp_path / "events.jsonl"), service="research")
    out = research.update(d, post=FakeAPI([reply()]), now_ms=NOW, events=events)
    assert len(out["added"]) == 3 and out["error"] == ""
    again = {"findings": [dict(FINDINGS["findings"][0], url="HTTPS://WWW.REDDIT.COM/r/algotrading/comments/abc"),
                          {"url": "https://new.example/q", "family": "buy-the-dip", "claim": "Dips kept dipping.",
                           "evidence": "live record shown", "credibility": "Broker statements shown."}]}
    api = FakeAPI([reply(again)])
    out = research.update(d, post=api, now_ms=NOW + 86_400_000, events=events)
    assert [f["url"] for f in out["added"]] == ["https://new.example/q"]
    assert "https://www.tradingview.com/script/xyz/" in api.bodies[0]["messages"][0]["content"]  # known list
    rows = research.load(d)
    assert len(rows) == 4 and rows[0]["url"] == "https://new.example/q" and rows[0]["date_found"] == "2025-10-10"
    assert open(research.log_path(d)).read().count("\n") == 4
    assert "1 new findings" in events.read()[-1]["text"]


def test_update_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = research.update(str(tmp_path / "news"))
    assert out["added"] == [] and "ANTHROPIC_API_KEY" in out["error"]
    assert not os.path.exists(research.log_path(str(tmp_path / "news")))


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
    research.update(post=FakeAPI([reply()]), now_ms=NOW)
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
    assert "No findings yet" in page and "Research input only" in page
    research.add([{"url": "javascript:alert(1)", "source": "evil", "date_found": "2025-10-09", "source_date": "",
                   "family": "breakout", "claim": "<img src=x onerror=alert(1)>", "evidence": "anecdote",
                   "credibility": ""}], paths.news)  # a hand-edited bad line is still shown safely
    research.update(paths.news, post=FakeAPI([reply()]), now_ms=NOW)
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
    assert c.post("/research/refresh", data={"csrf": "x"}).status_code == 403
    assert c.post("/research/refresh", data={"csrf": csrf}, follow_redirects=False).status_code == 303
    jobs = [j for j in runner.list() if j["meta"].get("kind") == "research"]
    assert len(jobs) == 1 and jobs[0]["steps"] == [["research", "update"]]
    again = c.post("/research/refresh", data={"csrf": csrf}, follow_redirects=False)
    assert "already%20running" in again.headers["location"]
