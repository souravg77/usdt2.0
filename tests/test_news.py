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


# ---- X (Twitter) ------------------------------------------------------------------------------

from aitrader.news.x import XReader, XClient, XError, mentions  # noqa: E402


class FakeX:
    def __init__(self):
        self.urls = []
        self.next_id = 100

    def __call__(self, url, token):
        assert token == "tok"
        self.urls.append(url)
        if "/users/by?" in url:
            return {"data": [{"id": "1", "username": "elonmusk", "name": "Elon Musk",
                              "public_metrics": {"followers_count": 200_000_000}},
                             {"id": "2", "username": "realDonaldTrump", "name": "Donald J. Trump"}]}
        if "/users/1/tweets" in url:
            self.next_id += 1
            return {"data": [{"id": str(self.next_id), "text": "Dogecoin to the moon! $BTC too",
                              "created_at": "2026-10-12T01:00:00Z", "public_metrics": {"like_count": 50000}}]}
        if "/users/2/tweets" in url:
            return {}
        if "/tweets/search/recent" in url:
            return {"data": [{"id": "9", "text": "Big ETH upgrade soon", "author_id": "7",
                              "public_metrics": {"like_count": 900, "retweet_count": 100}},
                             {"id": "8", "text": "eth", "author_id": "6", "public_metrics": {"like_count": 1}}],
                    "includes": {"users": [{"id": "7", "username": "cryptoguru", "name": "Guru",
                                            "public_metrics": {"followers_count": 500_000}},
                                           {"id": "6", "username": "tiny", "public_metrics": {"followers_count": 50}}]}}
        raise AssertionError(url)


def test_mentions():
    assert mentions("Dogecoin to the moon! $BTC too", ["BTC/USDT", "DOGE/USDT", "ETH/USDT"]) == ["BTC/USDT", "DOGE/USDT"]
    assert mentions("ethereal vibes, SOLAR power", ["ETH/USDT", "SOL/USDT"]) == []
    assert mentions("#SOL and ether", ["ETH/USDT", "SOL/USDT"]) == ["ETH/USDT", "SOL/USDT"]


def test_x_reader_watch_search_suggest_and_cap(tmp_path):
    fake = FakeX()
    r = XReader(str(tmp_path / "x.json"), XClient("tok", get=fake), daily_reads=100, clock=lambda: NOW / 1000)
    new = r.check_watch(SYMS)
    assert [p["username"] for p in new] == ["elonmusk"] and new[0]["pairs"] == ["BTC/USDT"]
    assert "max_results=5" in fake.urls[1] and "since_id" not in fake.urls[1]
    r.check_watch(SYMS)
    assert "since_id=101" in [u for u in fake.urls if "/users/1/" in u][-1]
    assert sum(1 for u in fake.urls if "/users/by?" in u) == 1  # ids looked up once
    r.search_pairs(SYMS)
    q = [u for u in fake.urls if "search/recent" in u][0]
    assert "%24BTC" in q and "bitcoin" in q and "-is%3Aretweet" in q
    assert [s["username"] for s in r.state["suggested"]] == ["cryptoguru"]  # tiny account left out
    assert r.reads_today() == 2 + 1 + 1 + 4  # 2 user lookups, 2 new posts, 2 searches x 2
    r.add("@cryptoguru")
    assert r.state["suggested"] == [] and r.state["watch"][-1] == {"username": "cryptoguru"}
    with pytest.raises(ValueError):
        r.add("not a handle!")
    r.remove("cryptoguru")
    assert [w["username"] for w in r.state["watch"]] == ["elonmusk", "realDonaldTrump"]
    capped = XReader(str(tmp_path / "x.json"), XClient("tok", get=fake), daily_reads=11, clock=lambda: NOW / 1000)
    capped.search_pairs(SYMS)
    assert any("cap" in e for e in capped.state["errors"])
    tomorrow = XReader(str(tmp_path / "x.json"), XClient("tok", get=fake), clock=lambda: NOW / 1000 + 86400)
    assert tomorrow.reads_today() == 0


def test_x_client_needs_token(monkeypatch):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    with pytest.raises(XError, match="Settings"):
        XClient()


def test_brief_gets_x_posts(tmp_path):
    fake = FakeX()
    r = XReader(str(tmp_path / "news" / "x.json"), XClient("tok", get=fake))
    api = FakeAPI([{"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "t",
                                                            "name": "report_market_news", "input": REPORT}]}])
    update(SYMS, str(tmp_path / "news"), fetch=fake_fetch, post=api, x=r)
    prompt = api.bodies[0]["messages"][0]["content"]
    assert "@elonmusk" in prompt and "Dogecoin to the moon" in prompt and "Big ETH upgrade" in prompt


def test_news_page_x_section(client, monkeypatch):
    c, _runner, paths = client
    c.post("/login", data={"user": "admin", "password": PW})
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    page = c.get("/news").text
    assert "isn't connected yet" in page and "@elonmusk" in page and "@realDonaldTrump" in page
    csrf = Auth("admin", PW, "k").csrf(c.cookies.get(COOKIE))
    r = c.post("/news/x/watch", data={"csrf": csrf, "username": "@saylor"}, follow_redirects=False)
    assert "Watching" in r.headers["location"] or "Watching%20%40saylor" in r.headers["location"]
    assert "@saylor" in c.get("/news").text
    bad = c.post("/news/x/watch", data={"csrf": csrf, "username": "<script>"}, follow_redirects=False)
    assert "error=" in bad.headers["location"]
    c.post("/news/x/watch", data={"csrf": csrf, "username": "saylor", "remove": "1"})
    assert "@saylor" not in c.get("/news").text
    assert c.post("/news/x/watch", data={"csrf": "x", "username": "a"}).status_code == 403


def test_settings_check_x():
    from aitrader.accounts import KeyRejected, KeyUnverified
    from aitrader.web.settings_pages import check_x

    class E(Exception):
        def __init__(self, code):
            self.code = code

    def opener(code):
        def o(req, timeout):
            assert req.headers["Authorization"] == "Bearer t"
            raise E(code)
        return o

    with pytest.raises(KeyRejected):
        check_x("t", opener=opener(401))
    with pytest.raises(KeyUnverified):
        check_x("t", opener=opener(402))


def test_x_search_takes_turns_over_many_pairs(tmp_path):
    fake = FakeX()
    r = XReader(str(tmp_path / "x.json"), XClient("tok", get=fake), daily_reads=10_000)
    pairs = [f"C{i}/USDT" for i in range(7)]
    r.search_pairs(pairs, max_pairs=5)
    r.search_pairs(pairs, max_pairs=5)
    searched = [u.split("query=")[1].split("%29")[0] for u in fake.urls if "search/recent" in u]
    assert len(searched) == 10 and "C5" in searched[5] and "C0" in searched[7]


def test_news_sites(client, tmp_path):
    from aitrader.news.sites import DEFAULT_SITES, load_sites, normalize

    assert normalize("https://m.economictimes.com/markets/cryptocurrency/") == \
        "economictimes.indiatimes.com/markets/cryptocurrency"
    for bad in ("javascript:alert(1)", "not a site", "ftp://x.com"):
        with pytest.raises(ValueError):
            normalize(bad)
    c, _runner, paths = client
    c.post("/login", data={"user": "admin", "password": PW})
    page = c.get("/news").text
    assert "www.cnbc.com/cryptoworld" in page and "coinmarketcap.com" in page
    csrf = Auth("admin", PW, "k").csrf(c.cookies.get(COOKIE))
    c.post("/news/sites", data={"csrf": csrf, "site": "https://decrypt.co/news"})
    c.post("/news/sites", data={"csrf": csrf, "site": "coinmarketcap.com", "remove": "1"})
    assert load_sites(paths.news) == [s for s in DEFAULT_SITES if s != "coinmarketcap.com"] + ["decrypt.co/news"]
    api = FakeAPI([{"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "t",
                                                            "name": "report_market_news", "input": REPORT}]}])
    update(SYMS, paths.news, fetch=fake_fetch, post=api, x=None)
    prompt = api.bodies[0]["messages"][0]["content"]
    assert "Check these news sites first" in prompt and "decrypt.co/news" in prompt and "coinmarketcap" not in prompt
