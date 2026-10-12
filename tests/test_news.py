"""News and web search: free feeds, the AI brief (web search + report tool), the news filter, the
paper trader honouring it, and the News page."""

import json

import pytest
from fastapi.testclient import TestClient

from aitrader.config import Settings
from aitrader.news import NewsFilter, clean, update, write_brief
from aitrader.news import feeds
from aitrader.web import Auth, JobRunner, Paths, create_app
from aitrader.web.auth import COOKIE

PW = "correct horse battery"
NOW = 1_760_000_000_000
SYMS = ["BTC/USDT", "ETH/USDT"]

RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Bitcoin ETF inflows jump</title><link>https://news.example/etf</link>
<pubDate>Thu, 09 Oct 2025 08:00:00 GMT</pubDate></item>
<item><title>Old story</title><link>https://news.example/old</link><pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>
<item><title>No link</title></item>
</channel></rss>"""
ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Border clashes escalate</title>
<link href="https://world.example/war"/><updated>2025-10-09T06:00:00Z</updated></entry></feed>"""


def fake_fetch(url):
    if url == feeds.FEAR_GREED_URL:
        return json.dumps({"data": [{"value": "22", "value_classification": "Extreme Fear"},
                                    {"value": "35", "value_classification": "Fear"}]}).encode()
    if url == feeds.TRENDING_URL:
        return json.dumps({"coins": [{"item": {"symbol": "pepe", "name": "Pepe", "market_cap_rank": 30}}]}).encode()
    if "bbc" in url:
        return ATOM
    if "aljazeera" in url:
        raise TimeoutError("slow")
    return RSS


def test_feeds_gather_and_fail_soft():
    got = feeds.gather(fake_fetch, now_ms=NOW)
    assert got["fear_greed"] == {"value": 22, "label": "Extreme Fear", "yesterday": 35}
    assert got["trending"][0]["symbol"] == "PEPE"
    titles = [h["title"] for h in got["crypto_headlines"]]
    assert "Bitcoin ETF inflows jump" in titles and "Old story" not in titles and "No link" not in titles
    assert got["world_headlines"][0]["title"] == "Border clashes escalate"
    assert got["errors"] == ["Al Jazeera: TimeoutError"]


REPORT = {"market_risk": "elevated", "market_risk_why": "border war escalating", "summary": "Risk-off.",
          "world_events": [{"title": "Border clashes escalate", "impact": "high", "direction": "risk-off",
                            "url": "https://world.example/war"},
                           {"title": "bad url", "impact": "huge", "direction": "up", "url": "javascript:alert(1)"}],
          "pairs": [{"symbol": "ETH", "sentiment": 3, "demand": "hype", "risk": "high", "why": "exchange hack",
                     "events": [{"date": "2025-10-20", "title": "Upgrade", "url": "https://eth.example"}],
                     "sources": ["https://a.example", "ftp://b"]},
                    {"symbol": "DOGE/USDT", "sentiment": 0, "demand": "rising", "risk": "low", "why": "x"}]}


def test_clean_keeps_known_values_only():
    c = clean(REPORT, SYMS)
    assert c["market_risk"] == "elevated"
    assert c["world_events"][1] == {"title": "bad url", "impact": "low", "direction": "unclear", "date": "",
                                    "url": ""}
    eth = c["pairs"][1]
    assert eth["symbol"] == "ETH/USDT" and eth["sentiment"] == 1.0 and eth["risk"] == "high"
    assert eth["sources"] == ["https://a.example"]
    assert [p["symbol"] for p in c["pairs"]] == SYMS  # DOGE dropped, BTC filled in as normal
    assert c["pairs"][0]["risk"] == "normal"


class FakeAPI:
    """Search, pause, then finish without the tool: the brief asks for it."""

    def __init__(self, script):
        self.script, self.bodies = list(script), []

    def __call__(self, body):
        self.bodies.append(body)
        return self.script.pop(0)


def test_write_brief_searches_then_reports():
    api = FakeAPI([
        {"stop_reason": "pause_turn", "content": [{"type": "server_tool_use", "id": "s1", "name": "web_search",
                                                   "input": {"query": "bitcoin news"}}]},
        {"stop_reason": "end_turn", "content": [{"type": "server_tool_use", "id": "s2", "name": "web_search",
                                                 "input": {"query": "war"}}, {"type": "text", "text": "done"}]},
        {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "t", "name": "report_market_news",
                                                 "input": REPORT}]},
    ])
    b = write_brief(SYMS, feeds.gather(fake_fetch, NOW), api, model="m")
    assert b["searches"] == 2 and b["market_risk"] == "elevated" and b["model"] == "m"
    first = api.bodies[0]
    assert first["tools"][0]["type"] == "web_search_20250305" and first["tools"][0]["max_uses"] == 6
    assert "Extreme Fear" in first["messages"][0]["content"] and "Border clashes" in first["messages"][0]["content"]
    assert "tool_choice" not in first and "tool_choice" not in api.bodies[1]
    assert len(api.bodies[1]["messages"]) == 2  # paused turn sent back unchanged
    assert api.bodies[2]["tool_choice"] == {"type": "tool", "name": "report_market_news"}
    assert all("force" not in m for body in api.bodies for m in body["messages"])


def test_update_saves_and_filter_decides(tmp_path):
    from aitrader.ops import EventLog

    events = EventLog(str(tmp_path / "events.jsonl"), service="news")
    api = FakeAPI([{"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "t",
                                                            "name": "report_market_news", "input": REPORT}]}])
    out = update(SYMS, str(tmp_path / "news"), fetch=fake_fetch, post=api, now_ms=NOW, events=events)
    assert out["brief"]["market_risk"] == "elevated"
    assert (tmp_path / "news" / "history.jsonl").read_text().count("\n") == 1
    ev = events.read()[-1]
    assert ev["kind"] == "news" and ev["level"] == "warning" and "ETH/USDT" in ev["text"]
    f = NewsFilter(str(tmp_path / "news"), cache_s=0)
    btc, eth = f.decide("BTC/USDT", NOW + 1000), f.decide("ETH/USDT", NOW + 1000)
    assert btc.allow_entries and btc.size_multiplier == 0.5
    assert not eth.allow_entries and eth.size_multiplier == 0
    stale = f.decide("ETH/USDT", NOW + 13 * 3_600_000)
    assert stale.allow_entries and stale.size_multiplier == 1 and "stale" in stale.reasons[0]


def test_update_without_key_keeps_feeds(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = update(SYMS, str(tmp_path / "news"), fetch=fake_fetch, now_ms=NOW)
    assert out["brief"] is None and "ANTHROPIC_API_KEY" in out["brief_error"]
    assert out["feeds"]["fear_greed"]["value"] == 22
    d = NewsFilter(str(tmp_path / "news")).decide("BTC/USDT", NOW)
    assert d.allow_entries and d.size_multiplier == 1


def test_paper_trader_applies_news(tmp_path):
    from aitrader.data.store import CandleStore
    from aitrader.learn.paper import PaperTrader
    from aitrader.news import NewsDecision

    class Fixed:
        def __init__(self, d):
            self.d = d

        def decide(self, *a):
            return self.d

    t = PaperTrader(CandleStore(str(tmp_path / "c.db")), str(tmp_path / "r.json"), ["binance"], SYMS,
                    state_path=str(tmp_path / "s.json"), news=Fixed(NewsDecision(True, 0.5)),
                    context=Fixed(NewsDecision(True, 0.5)))
    assert t._context("binance", "BTC/USDT", NOW) == 0.25
    t.news = Fixed(NewsDecision(False, 0.0))
    assert t._context("binance", "BTC/USDT", NOW) == 0.0


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "none.env"))
    monkeypatch.setattr(runner, "command", lambda step: ["sleep", "1"])
    paths = Paths(news=str(tmp_path / "news"))
    app = create_app(Settings(db_path=str(tmp_path / "x.db"), symbols=SYMS), Auth("admin", PW, "k"), runner, paths)
    return TestClient(app), runner, paths


def test_news_page(client):
    c, runner, paths = client
    assert c.get("/news", follow_redirects=False).status_code == 303
    c.post("/login", data={"user": "admin", "password": PW})
    assert "No news yet" in c.get("/news").text
    api = FakeAPI([{"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "t",
                                                            "name": "report_market_news", "input": REPORT}]}])
    update(SYMS, paths.news, fetch=fake_fetch, post=api)
    page = c.get("/news").text
    assert "Border clashes escalate" in page and "ETH/USDT: new buys paused" in page
    assert "Extreme Fear" in page and "javascript:" not in page
    csrf = Auth("admin", PW, "k").csrf(c.cookies.get(COOKIE))
    assert c.post("/news/refresh", data={"csrf": "x"}).status_code == 403
    r = c.post("/news/refresh", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    jobs = [j for j in runner.list() if j["meta"].get("kind") == "news"]
    assert len(jobs) == 1 and jobs[0]["steps"] == [["news", "update"]]
    again = c.post("/news/refresh", data={"csrf": csrf}, follow_redirects=False)
    assert "already%20running" in again.headers["location"]
