"""News: free feeds, the local AI model's brief, the news filter, the paper trader honouring it,
and the News page."""

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
<item><title>Ethereum exchange hack drains a hot wallet</title><link>https://news.example/hack</link>
<pubDate>Thu, 09 Oct 2025 07:00:00 GMT</pubDate></item>
<item><title>Old story</title><link>https://news.example/old</link><pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>
<item><title>No link</title></item>
</channel></rss>"""
ATOM = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Border clashes escalate</title>
<link href="https://world.example/war"/><updated>2025-10-09T06:00:00Z</updated></entry></feed>"""


BYBIT = {"retCode": 0, "retMsg": "OK", "result": {"total": 3, "list": [
    {"title": "Bybit Will Delist the ETHFI/USDT Spot Trading Pair", "url": "https://announcements.bybit.com/en/a1",
     "type": {"title": "Delistings", "key": "delistings"}, "tags": ["Spot"], "description": "Trading stops soon",
     "publishTime": 1_759_990_000_000, "dateTimestamp": 1_759_990_000_000},
    {"title": "New Listing: XYZ/USDT", "url": "https://announcements.bybit.com/en/a2",
     "type": {"title": "New Listings", "key": "new_crypto"}, "tags": ["Spot", "Spot Listings"],
     "publishTime": 1_759_995_000_000},
    {"title": "Old maintenance", "url": "https://announcements.bybit.com/en/a3",
     "type": {"title": "Maintenance Updates"}, "publishTime": 1_700_000_000_000}]}}


def fake_fetch(url):
    if url == feeds.FEAR_GREED_URL:
        return json.dumps({"data": [{"value": "22", "value_classification": "Extreme Fear"},
                                    {"value": "35", "value_classification": "Fear"}]}).encode()
    if url == feeds.TRENDING_URL:
        return json.dumps({"coins": [{"item": {"symbol": "pepe", "name": "Pepe", "market_cap_rank": 30}}]}).encode()
    if url.startswith("https://api.bybit.com/v5/announcements"):
        return json.dumps(BYBIT if "page=1" in url else {"retCode": 0, "result": {"list": []}}).encode()
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
    # Shawn: read all the news from Bybit's news API (the last 3 days, newest first)
    by = got["bybit_announcements"]
    assert [b["title"] for b in by] == ["New Listing: XYZ/USDT", "Bybit Will Delist the ETHFI/USDT Spot Trading Pair"]
    assert by[1]["source"] == "Bybit: Delistings" and by[1]["tags"] == ["Spot"] and by[1]["published"] == 1_759_990_000_000
    assert by[0]["starts"] is None and by[0]["ends"] is None  # plain news: no event window
    assert feeds.gather(lambda u: b"{}" if "bybit" in u else fake_fetch(u), NOW)["bybit_announcements"] == []
    bad = feeds.gather(lambda u: b'{"retCode": 10006, "retMsg": "Too many visits"}' if "bybit" in u
                       else fake_fetch(u), NOW)
    assert "Bybit announcements: ValueError" in bad["errors"]


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


class FakeChat:
    """The local model: answers every call with ``answer`` and keeps what it was asked."""

    def __init__(self, answer=REPORT):
        self.answer, self.calls = answer, []

    def __call__(self, system, user, schema):
        self.calls.append({"system": system, "user": user, "schema": schema})
        return self.answer


def test_write_brief_asks_the_local_model(monkeypatch):
    monkeypatch.setenv("AITRADER_LOCAL_MODEL", "tiny:1b")
    chat = FakeChat()
    b = write_brief(SYMS, feeds.gather(fake_fetch, NOW), chat)
    assert b["market_risk"] == "elevated" and b["model"] == "tiny:1b" and b["searches"] == 0
    asked = chat.calls[0]
    assert "Extreme Fear" in asked["user"] and "Border clashes" in asked["user"]
    assert "Bybit Will Delist the ETHFI/USDT" in asked["user"]
    assert asked["user"].startswith("Pairs: BTC/USDT, ETH/USDT\n") and b["pairs"][1]["risk"] == "high"
    # only the pairs the feeds name go to the model; one it wasn't asked about keeps no news,
    # whatever the model says of it (REPORT rates ETH high risk)
    quiet = feeds.gather(fake_fetch, NOW)
    quiet["crypto_headlines"] = [h for h in quiet["crypto_headlines"] if "Ethereum" not in h["title"]]
    chat = FakeChat()
    b = write_brief(SYMS, quiet, chat)
    assert chat.calls[0]["user"].startswith("Pairs: BTC/USDT\n")
    assert [p["symbol"] for p in b["pairs"]] == SYMS  # every pair is in the brief all the same
    assert b["pairs"][1]["risk"] == "normal" and b["pairs"][1]["why"] == "no news found"
    # the model sees no links and is asked for none: a link it gave would be made up
    assert "https://" not in asked["user"] and "url" not in json.dumps(asked["schema"])
    assert "sources" not in json.dumps(asked["schema"])


def test_bybit_events_still_running_are_kept_with_their_dates():
    day = 86_400_000
    rows = {"retCode": 0, "result": {"list": [
        {"title": "Delisting of ZZZUSDT Perpetual Contract", "url": "https://announcements.bybit.com/z",
         "type": {"title": "Delistings"}, "description": "Bybit will be delisting the ZZZUSDT Perpetual Contract.",
         "publishTime": NOW - day, "startDateTimestamp": NOW - day, "endDateTimestamp": NOW - day},
        {"title": "Trading Competition", "url": "https://announcements.bybit.com/c", "type": {"title": "Latest Campaigns"},
         "publishTime": NOW - 10 * day, "startDateTimestamp": NOW - 10 * day, "endDateTimestamp": NOW + 5 * day},
        {"title": "Old campaign", "url": "https://announcements.bybit.com/o", "type": {"title": "Latest Campaigns"},
         "publishTime": NOW - 20 * day, "startDateTimestamp": NOW - 20 * day, "endDateTimestamp": NOW - 9 * day}]}}
    got = feeds.bybit_announcements(lambda u: json.dumps(rows).encode(), NOW)
    assert [g["title"] for g in got] == ["Delisting of ZZZUSDT Perpetual Contract", "Trading Competition"]
    assert got[0]["ends"] is None and got[1]["ends"] == NOW + 5 * day
    from aitrader.news.brief import _prompt, mentioned

    f = {"bybit_announcements": got}
    assert mentioned(["ZZZ/USDT", "BTC/USDT"], f) == ["ZZZ/USDT"]
    prompt = _prompt(["ZZZ/USDT"], f)
    assert "- [Delistings] Delisting of ZZZUSDT Perpetual Contract: Bybit will be delisting" in prompt
    assert "- [Latest Campaigns] Trading Competition (runs " in prompt


def test_brief_pairs_are_the_ones_the_feeds_name():
    from aitrader.news.brief import mentioned

    f = {"crypto_headlines": [{"title": "Bitcoin ETF jumps; Q3 GDP beats, M&A up"}, {"title": "$Q rallies"}],
         "trending": [{"symbol": "PEPE"}], "bybit_announcements": [{"title": "Bybit Will Delist ETHFI/USDT"}]}
    syms = ["BTC/USDT", "ETH/USDT", "Q/USDT", "M/USDT", "1000PEPE/USDT", "SOL/USDT", "ETHFI/USDT"]
    assert mentioned(syms, f) == ["BTC/USDT", "Q/USDT", "1000PEPE/USDT", "ETHFI/USDT"]


def test_update_saves_and_filter_decides(tmp_path):
    from aitrader.ops import EventLog

    events = EventLog(str(tmp_path / "events.jsonl"), service="news")
    out = update(SYMS, str(tmp_path / "news"), fetch=fake_fetch, chat=FakeChat(), now_ms=NOW, events=events)
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


def test_update_without_the_local_model_keeps_feeds(tmp_path, monkeypatch):
    monkeypatch.setenv("AITRADER_LLM_URL", "http://127.0.0.1:9")  # nothing listens there
    out = update(SYMS, str(tmp_path / "news"), fetch=fake_fetch, now_ms=NOW)
    assert out["brief"] is None and "URLError" in out["brief_error"]
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

        def decide(self, *a, **kw):
            return self.d

    t = PaperTrader(CandleStore(str(tmp_path / "c.db")), str(tmp_path / "r.json"), ["bybit"], SYMS,
                    state_path=str(tmp_path / "s.json"), news=Fixed(NewsDecision(True, 0.5)),
                    context=Fixed(NewsDecision(True, 0.5)))
    assert t._context("bybit", "BTC/USDT", NOW) == 0.25
    t.news = Fixed(NewsDecision(False, 0.0))
    assert t._context("bybit", "BTC/USDT", NOW) == 0.0


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner = JobRunner(str(tmp_path / "jobs"), env_file=str(tmp_path / "none.env"))
    monkeypatch.setattr(runner, "command", lambda step: ["sleep", "1"])
    paths = Paths(news=str(tmp_path / "news"))
    app = create_app(Settings(db_path=str(tmp_path / "x.db"), symbols=SYMS), Auth("admin", PW, "k"), runner, paths)
    return TestClient(app), runner, paths


def test_news_page(client, monkeypatch):
    c, runner, paths = client
    monkeypatch.setattr(feeds, "DAY_MS", 10**15)  # the sample Bybit announcements count as recent
    monkeypatch.setattr("aitrader.news.brief.mentioned", lambda syms, _f: syms)  # the sample news is old
    assert c.get("/news", follow_redirects=False).status_code == 303
    c.post("/login", data={"user": "admin", "password": PW})
    assert "No news yet" in c.get("/news").text
    update(SYMS, paths.news, fetch=fake_fetch, chat=FakeChat())
    page = c.get("/news").text
    assert "Border clashes escalate" in page and "ETH/USDT: new buys paused" in page
    assert "<summary>Bybit announcements (3)</summary>" in page and "Bybit: Delistings" in page
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
    # Bybit writes pairs as XYZUSDT: a delisting of a pair we trade must be seen
    assert mentions("Delisting of VELODROMEUSDT Perpetual Contract", ["VELODROME/USDT", "VELO/USDT"]) == \
        ["VELODROME/USDT"]
    assert mentions("Listing of QUSDT; Q3 results", ["Q/USDT"]) == ["Q/USDT"]
    assert mentions("Q3 results, M&A", ["Q/USDT", "M/USDT"]) == []
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
    chat = FakeChat()
    update(SYMS, str(tmp_path / "news"), fetch=fake_fetch, chat=chat, x=r)
    prompt = chat.calls[0]["user"]
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
    chat = FakeChat()
    update(SYMS, paths.news, fetch=fake_fetch, chat=chat, x=None)
    prompt = chat.calls[0]["user"]
    assert "News sites you trust most" in prompt and "decrypt.co/news" in prompt and "coinmarketcap" not in prompt


def test_news_page_shows_the_workspace_pairs(client, monkeypatch):
    """Shawn: in the Futures view, news about the Futures pairs, plus world and economic news."""
    import time

    from aitrader.learn import wallet

    c, _runner, paths = client
    c.post("/login", data={"user": "admin", "password": PW})
    monkeypatch.setattr("aitrader.news.brief.mentioned", lambda syms, _f: syms)  # the sample news is old
    update(SYMS, paths.news, fetch=fake_fetch, chat=FakeChat())
    page = c.get("/news").text
    assert "Spot AI workspace" in page and "<b>BTC/USDT</b>" in page and "<b>ETH/USDT</b>" in page
    wallet.set_market(wallet.market_path("data/learn/paper_state.json"), True)
    page = c.get("/news").text  # no Futures top gainers yet: no Futures pairs
    assert "Futures AI workspace" in page and "<b>BTC/USDT</b>" not in page and "nothing on these pairs" in page
    assert "World and economic events" in page and "Border clashes escalate" in page
    fut = "gainers-test.json"
    with open(fut, "w") as fh:
        json.dump({"on": True, "generated": int(time.time() * 1000), "pairs": [{"symbol": "ETH/USDT"}]}, fh)
    monkeypatch.setenv("AITRADER_GAINERS_FILE", fut)
    page = c.get("/news").text
    assert "Futures AI pairs" in page and "<b>ETH/USDT</b>" in page and "<b>BTC/USDT</b>" not in page
    assert "ETH/USDT: new buys paused" in page
    from aitrader.web.news_pages import _feeds

    heads = {"crypto_headlines": [{"title": "Ethereum upgrade: ETH devs set date", "url": "https://a.example",
                                   "source": "x"},
                                  {"title": "Bitcoin ETF inflows jump", "url": "https://b.example", "source": "x"}],
             "world_headlines": [{"title": "Fed holds rates", "url": "https://c.example", "source": "y"}]}
    out = _feeds(heads, ["ETH/USDT"], "Futures AI")
    assert "Headlines about the Futures AI pairs (1)" in out and "Other crypto headlines (1)" in out
    assert "World and economic headlines (1)" in out
