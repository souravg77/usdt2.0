"""Bybit V5 public spot WebSocket: subscribe, level-1 book and tickers, ping, age and staleness,
reconnect with resubscribe. A fake socket stands in for the network."""

import asyncio
import json
import math

from aitrader.data.bybit_ws import BybitSpotStream, to_bybit


class FakeSocket:
    def __init__(self, messages=(), fail_after=False):
        self.inbox: asyncio.Queue = asyncio.Queue()
        for m in messages:
            self.inbox.put_nowait(m)
        if fail_after:
            self.inbox.put_nowait(ConnectionError("dropped"))
        self.sent, self.closed = [], False

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def recv(self):
        item = await self.inbox.get()
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, str) else json.dumps(item)

    async def close(self):
        self.closed = True


class Connector:
    def __init__(self, sockets):
        self.sockets, self.urls = list(sockets), []

    async def __call__(self, url):
        self.urls.append(url)
        if not self.sockets:
            await asyncio.sleep(3600)
        s = self.sockets.pop(0)
        if isinstance(s, Exception):
            raise s
        return s


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def book(sym, bid, ask, kind="snapshot", ts=1_700_000_000_000):
    return {"topic": f"orderbook.1.{sym}", "type": kind, "ts": ts,
            "data": {"s": sym, "b": [[str(bid), "1.5"]] if bid else [], "a": [[str(ask), "2"]] if ask else [],
                     "u": 1, "seq": 1}}


def ticker(sym, last, ts=1_700_000_000_001):
    return {"topic": f"tickers.{sym}", "type": "snapshot", "ts": ts,
            "data": {"symbol": sym, "lastPrice": str(last), "highPrice24h": "1", "lowPrice24h": "1"}}


async def until(cond, timeout=2.0):
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        assert asyncio.get_running_loop().time() < end, "condition not met in time"
        await asyncio.sleep(0.005)


def test_symbol_names():
    assert to_bybit("BTC/USDT") == "BTCUSDT" and to_bybit("ethusdt") == "ETHUSDT"


async def test_subscribes_and_tracks_quotes():
    clock = Clock()
    sock = FakeSocket([{"success": True, "ret_msg": "subscribe", "op": "subscribe"},
                       book("BTCUSDT", 60000.5, 60001), ticker("BTCUSDT", 60000.7),
                       book("SOLUSDT", 1, 2),  # not ours: ignored
                       "not json", {"topic": "tickers.ETHUSDT", "data": "bad"}])
    conn = Connector([sock])
    seen = []
    s = BybitSpotStream(["BTC/USDT", "ETH/USDT"], connect=conn, clock=clock, on_update=seen.append)
    assert s.age("BTC/USDT") == math.inf and s.stale("BTC/USDT", 60) and s.best_bid("BTC/USDT") is None
    task = asyncio.create_task(s.run())
    await until(lambda: s.last_price("BTC/USDT") is not None)
    assert conn.urls == ["wss://stream.bybit.com/v5/public/spot"]
    assert sock.sent[0]["op"] == "subscribe"
    assert sock.sent[0]["args"] == ["orderbook.1.BTCUSDT", "tickers.BTCUSDT", "orderbook.1.ETHUSDT", "tickers.ETHUSDT"]
    assert s.best_bid("BTC/USDT") == 60000.5 and s.best_ask("BTCUSDT") == 60001.0
    assert s.last_price("BTC/USDT") == 60000.7
    q = s.quote("BTC/USDT")
    assert q.symbol == "BTC/USDT" and q.bid_size == 1.5 and q.ts == 1_700_000_000_001
    assert s.quote("SOL/USDT") is None and s.quote("ETH/USDT") is None
    assert [u.symbol for u in seen] == ["BTC/USDT", "BTC/USDT"]
    assert s.connected and s.age("BTC/USDT") == 0 and not s.stale("BTC/USDT", 5)
    clock.t += 6
    assert s.age("BTC/USDT") == 6 and s.stale("BTC/USDT", 5) and not s.stale("BTC/USDT", 10)
    s.stop()
    await asyncio.wait_for(task, 1)
    assert sock.closed and not s.connected


async def test_book_delta_and_empty_side():
    s = BybitSpotStream(["BTC/USDT"], connect=Connector([]))
    assert s._handle(json.dumps(book("BTCUSDT", 100, 101)))
    s._handle(json.dumps(book("BTCUSDT", 99, None, kind="delta")))  # delta without asks keeps the ask
    assert (s.best_bid("BTC/USDT"), s.best_ask("BTC/USDT")) == (99.0, 101.0)
    s._handle(json.dumps({"topic": "orderbook.1.BTCUSDT", "type": "delta", "ts": 5,
                          "data": {"s": "BTCUSDT", "b": [["99", "0"]], "a": []}}))  # size 0 removes
    assert s.best_bid("BTC/USDT") is None and s.best_ask("BTC/USDT") == 101.0
    s._handle(json.dumps(book("BTCUSDT", 98, None)))  # a snapshot replaces both sides
    assert (s.best_bid("BTC/USDT"), s.best_ask("BTC/USDT")) == (98.0, None)


async def test_pings_every_interval():
    sock = FakeSocket()
    s = BybitSpotStream(["BTC/USDT"], connect=Connector([sock]), ping_s=0.01, read_timeout=5)
    task = asyncio.create_task(s.run())
    await until(lambda: sum(m["op"] == "ping" for m in sock.sent) >= 3)
    s.stop()
    await asyncio.wait_for(task, 1)


async def test_reconnects_and_resubscribes():
    first = FakeSocket([book("BTCUSDT", 100, 101)], fail_after=True)
    second = FakeSocket([book("BTCUSDT", 102, 103)])
    conn = Connector([OSError("dns"), first, second])
    s = BybitSpotStream(["BTC/USDT"], connect=conn, min_backoff=0.01, max_backoff=0.02)
    task = asyncio.create_task(s.run())
    await until(lambda: s.best_bid("BTC/USDT") == 102.0)
    assert len(conn.urls) == 3 and s.connects == 2 and first.closed
    assert [m["args"] for m in second.sent if m["op"] == "subscribe"] == [["orderbook.1.BTCUSDT", "tickers.BTCUSDT"]]
    assert "dropped" in s.last_error or "dns" in s.last_error
    s.stop()
    await asyncio.wait_for(task, 1)


async def test_silent_socket_times_out_and_reconnects():
    quiet, live = FakeSocket(), FakeSocket([ticker("BTCUSDT", 5)])
    s = BybitSpotStream(["BTC/USDT"], connect=Connector([quiet, live]), ping_s=10, read_timeout=0.05,
                        min_backoff=0.01)
    task = asyncio.create_task(s.run())
    await until(lambda: s.last_price("BTC/USDT") == 5.0)
    assert quiet.closed and "no message" in s.last_error
    s.stop()
    await asyncio.wait_for(task, 1)


async def test_many_pairs_split_subscribe_requests():
    syms = [f"C{i}/USDT" for i in range(7)]
    sock = FakeSocket()
    s = BybitSpotStream(syms, connect=Connector([sock]))
    task = asyncio.create_task(s.run())
    await until(lambda: len(sock.sent) >= 2)
    subs = [m["args"] for m in sock.sent if m["op"] == "subscribe"]
    assert [len(a) for a in subs] == [10, 4] and len({m["req_id"] for m in sock.sent}) == len(sock.sent)
    s.stop()
    await asyncio.wait_for(task, 1)


async def test_refused_subscription_is_reported():
    s = BybitSpotStream(["BTC/USDT"], connect=Connector([]))
    assert not s._handle(json.dumps({"success": False, "ret_msg": "error:handler not found", "op": "subscribe"}))
    assert "subscribe refused" in s.last_error


async def test_real_aiohttp_socket_against_local_server(monkeypatch):
    """The default connector, against a local aiohttp WebSocket server (no internet needed)."""
    from aiohttp import web

    for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    got = []

    async def handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        got.append(json.loads((await ws.receive()).data))
        await ws.send_str(json.dumps(book("BTCUSDT", 10, 11)))
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/ws", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        s = BybitSpotStream(["BTC/USDT"], url=f"ws://127.0.0.1:{port}/ws", min_backoff=5)
        task = asyncio.create_task(s.run())
        await until(lambda: s.best_ask("BTC/USDT") == 11.0)
        await until(lambda: not s.connected)  # the server closed: the stream noticed
        assert got[0]["op"] == "subscribe" and "closed" in s.last_error
        s.stop()
        await asyncio.wait_for(task, 2)
    finally:
        await runner.cleanup()
