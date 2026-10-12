"""Bybit V5 public WebSocket: best bid, best ask and last price per pair, with their age.
With ``category="linear"`` it streams the USDT perpetuals instead, whose tickers also carry the
mark price, index price, funding rate, next funding time and open interest (``derivs``).

One connection to ``wss://stream.bybit.com/v5/public/spot`` subscribes to ``orderbook.1.<SYMBOL>``
(level-1 book: best bid and ask) and ``tickers.<SYMBOL>`` (last price) for every pair. As Bybit's
docs ask, it sends ``{"op": "ping"}`` every 20 seconds. If nothing arrives for ``read_timeout``
seconds (pongs count), or the socket closes or errors, it reconnects with exponential backoff and
subscribes again. Prices are never cleared on a disconnect: their ``age`` keeps growing, so
``stale(symbol, max_age)`` turns true and callers stop trusting them.

Read side (for stop checks and stale-data detection; plain methods, safe to call from any task
on the loop that runs the stream, or from another thread, since each quote is replaced whole):

    stream = BybitSpotStream(["BTC/USDT", "ETH/USDT"])
    task = asyncio.create_task(stream.run())
    stream.best_bid("BTC/USDT"), stream.best_ask("BTC/USDT"), stream.last_price("BTC/USDT")
    stream.age("BTC/USDT")          # seconds since the last update (inf if none yet)
    stream.stale("BTC/USDT", 5.0)   # True if no update in the last 5 seconds
    stream.stop(); await task

Public market data only: no keys, no orders. Pairs are given as ``BTC/USDT`` or ``BTCUSDT``.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Protocol

log = logging.getLogger(__name__)

PUBLIC_SPOT_URL = "wss://stream.bybit.com/v5/public/spot"
PUBLIC_LINEAR_URL = "wss://stream.bybit.com/v5/public/linear"
URLS = {"spot": PUBLIC_SPOT_URL, "linear": PUBLIC_LINEAR_URL}
PING_S = 20.0  # Bybit: "send the ping heartbeat packet every 20 seconds"
ARGS_PER_SUBSCRIBE = 10  # spot accepts at most 10 topics per subscribe request


@dataclass(frozen=True)
class Quote:
    """The latest top of book and last trade price of one pair.

    ``received`` is ``time.monotonic()`` (or the stream's clock) when the last update arrived;
    ``ts`` is Bybit's own timestamp of that update in milliseconds.
    """

    symbol: str
    bid: float | None = None
    bid_size: float | None = None
    ask: float | None = None
    ask_size: float | None = None
    last: float | None = None
    ts: int = 0
    received: float = -math.inf
    # perpetuals only (linear tickers)
    mark: float | None = None
    index: float | None = None
    funding_rate: float | None = None
    next_funding_ts: int | None = None
    open_interest: float | None = None


class Socket(Protocol):
    """What the stream needs from a WebSocket connection (tests pass a fake)."""

    async def send(self, text: str) -> None: ...

    async def recv(self) -> str:
        """The next text message; raises ``ConnectionError`` when the socket is closed."""
        ...

    async def close(self) -> None: ...


Connect = Callable[[str], Awaitable[Socket]]


def to_bybit(symbol: str) -> str:
    """``BTC/USDT`` -> ``BTCUSDT`` (Bybit's spot symbol)."""
    return symbol.replace("/", "").replace("-", "").upper().strip()


class BybitSpotStream:
    def __init__(
        self,
        symbols: list[str],
        url: str | None = None,
        connect: Connect | None = None,
        ping_s: float = PING_S,
        read_timeout: float | None = None,
        max_backoff: float = 60.0,
        min_backoff: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        on_update: Callable[[Quote], None] | None = None,
        category: str = "spot",
    ):
        if category not in URLS:
            raise ValueError(f"category must be one of {sorted(URLS)}")
        self.category = category
        self.url = url or URLS[category]
        self._connect = connect or aiohttp_connect
        self.ping_s = ping_s
        self.read_timeout = read_timeout or 2 * ping_s
        self.max_backoff, self.min_backoff = max_backoff, min_backoff
        self._clock = clock
        self.on_update = on_update
        self._names: dict[str, str] = {}  # BTCUSDT -> the name the caller used (BTC/USDT)
        for s in symbols:
            self._names.setdefault(to_bybit(s), s)
        self._quotes: dict[str, Quote] = {}
        self._stop = asyncio.Event()
        self._req = itertools.count(1)
        self.connected, self._fresh = False, False
        self.connects = 0  # successful connections so far (1 + reconnects)
        self.last_error = ""

    # ---- read side -------------------------------------------------------------------------------

    def quote(self, symbol: str) -> Quote | None:
        return self._quotes.get(to_bybit(symbol))

    def best_bid(self, symbol: str) -> float | None:
        q = self.quote(symbol)
        return q.bid if q else None

    def best_ask(self, symbol: str) -> float | None:
        q = self.quote(symbol)
        return q.ask if q else None

    def last_price(self, symbol: str) -> float | None:
        q = self.quote(symbol)
        return q.last if q else None

    def mark_price(self, symbol: str) -> float | None:
        q = self.quote(symbol)
        return q.mark if q else None

    def index_price(self, symbol: str) -> float | None:
        q = self.quote(symbol)
        return q.index if q else None

    def derivs(self, symbol: str):
        """The perpetual's readings as a ``data.derivs.DerivSnapshot`` (linear streams)."""
        from .derivs import DerivSnapshot

        q = self.quote(symbol)
        if q is None:
            return None
        return DerivSnapshot(q.symbol, last=q.last, mark=q.mark, index=q.index, funding_rate=q.funding_rate,
                             next_funding_ts=q.next_funding_ts, open_interest=q.open_interest, ts=q.ts)

    def age(self, symbol: str) -> float:
        """Seconds since the last update for ``symbol``; ``inf`` when none has arrived yet."""
        q = self.quote(symbol)
        return max(0.0, self._clock() - q.received) if q else math.inf

    def stale(self, symbol: str, max_age: float) -> bool:
        """True when ``symbol`` has had no update for more than ``max_age`` seconds (or ever)."""
        return self.age(symbol) > max_age

    @property
    def symbols(self) -> list[str]:
        return list(self._names.values())

    # ---- connection ------------------------------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    def topics(self) -> list[str]:
        return [t for s in self._names for t in (f"orderbook.1.{s}", f"tickers.{s}")]

    async def run(self) -> None:
        """Stream until ``stop()``; reconnects with backoff (1s, 2s, 4s ... ``max_backoff``)."""
        backoff = self.min_backoff
        while not self._stop.is_set():
            ws, self._fresh = None, False
            try:
                ws = await self._connect(self.url)
                self.connected, self.connects = True, self.connects + 1
                await self._subscribe(ws)
                if await self._session(ws):
                    backoff = self.min_backoff
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network errors, timeouts, closed sockets
                if self._fresh:  # the connection worked for a while: retry quickly
                    backoff = self.min_backoff
                self.last_error = f"{type(exc).__name__}: {exc}"[:300]
                log.warning("bybit %s stream: %s", self.category, self.last_error)
            finally:
                self.connected = False
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception:
                        pass
            if self._stop.is_set():
                break
            log.info("bybit %s stream: reconnecting in %.0fs", self.category, backoff)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, self.max_backoff)

    async def _subscribe(self, ws: Socket) -> None:
        topics = self.topics()
        for i in range(0, len(topics), ARGS_PER_SUBSCRIBE):
            await ws.send(json.dumps({"op": "subscribe", "req_id": str(next(self._req)),
                                      "args": topics[i:i + ARGS_PER_SUBSCRIBE]}))

    async def _session(self, ws: Socket) -> bool:
        """Read until the socket fails, goes quiet or ``stop()``; True if any price arrived."""
        got_data = False
        pinger = asyncio.create_task(self._pinger(ws))
        stopper = asyncio.create_task(self._stop.wait())
        try:
            while not self._stop.is_set():
                reader = asyncio.ensure_future(ws.recv())
                done, _ = await asyncio.wait({reader, stopper, pinger}, timeout=self.read_timeout,
                                             return_when=asyncio.FIRST_COMPLETED)
                if reader not in done:
                    reader.cancel()
                    if stopper in done:
                        return got_data
                    if pinger in done:
                        pinger.result()  # raises the ping failure
                    raise TimeoutError(f"no message for {self.read_timeout:.0f}s")
                if self._handle(reader.result()):
                    got_data = self._fresh = True
            return got_data
        finally:
            pinger.cancel()
            stopper.cancel()

    async def _pinger(self, ws: Socket) -> None:
        while True:
            await asyncio.sleep(self.ping_s)
            await ws.send(json.dumps({"op": "ping", "req_id": str(next(self._req))}))

    # ---- messages --------------------------------------------------------------------------------

    def _handle(self, text: str) -> bool:
        """Apply one message; True if it updated a price. Malformed messages are skipped."""
        try:
            msg = json.loads(text)
        except ValueError:
            return False
        if not isinstance(msg, dict):
            return False
        if msg.get("op") == "subscribe" and msg.get("success") is False:
            self.last_error = f"subscribe refused: {msg.get('ret_msg', '')}"[:300]
            log.warning("bybit %s stream: %s", self.category, self.last_error)
            return False
        topic, data = str(msg.get("topic") or ""), msg.get("data")
        if not isinstance(data, dict):
            return False
        try:
            ts = int(msg.get("ts") or 0)
            if topic.startswith("orderbook.1."):
                return self._book(topic[len("orderbook.1."):], msg.get("type"), data, ts)
            if topic.startswith("tickers."):
                return self._ticker(topic[len("tickers."):], data, ts)
        except (TypeError, ValueError, IndexError):
            return False
        return False

    def _book(self, sym: str, kind, data: dict, ts: int) -> bool:
        if sym not in self._names:
            return False
        q = self._quotes.get(sym) or Quote(self._names[sym])
        bid, bid_size = _level(data.get("b"), q.bid, q.bid_size, snapshot=kind == "snapshot")
        ask, ask_size = _level(data.get("a"), q.ask, q.ask_size, snapshot=kind == "snapshot")
        self._put(sym, replace(q, bid=bid, bid_size=bid_size, ask=ask, ask_size=ask_size, ts=ts or q.ts))
        return True

    def _ticker(self, sym: str, data: dict, ts: int) -> bool:
        """A ticker snapshot or delta: only the fields it carries change."""
        if sym not in self._names:
            return False
        fields = {"lastPrice": "last", "markPrice": "mark", "indexPrice": "index",
                  "fundingRate": "funding_rate", "openInterest": "open_interest"}
        changes = {attr: float(data[key]) for key, attr in fields.items() if data.get(key) not in (None, "")}
        if data.get("nextFundingTime") not in (None, "", "0"):
            changes["next_funding_ts"] = int(float(data["nextFundingTime"]))
        if not changes:
            return False
        q = self._quotes.get(sym) or Quote(self._names[sym])
        self._put(sym, replace(q, ts=ts or q.ts, **changes))
        return True

    def _put(self, sym: str, q: Quote) -> None:
        q = replace(q, received=self._clock())
        self._quotes[sym] = q
        if self.on_update is not None:
            try:
                self.on_update(q)
            except Exception:
                log.exception("bybit %s stream: on_update failed", self.category)


def _level(levels, price, size, snapshot: bool) -> tuple[float | None, float | None]:
    """Best level from a level-1 ``b``/``a`` list. A snapshot replaces it; in a delta a size of
    0 removes the level, and an empty list leaves it as it was."""
    if not levels:
        return (None, None) if snapshot else (price, size)
    p, s = float(levels[0][0]), float(levels[0][1])
    if s <= 0:
        return None, None
    return p, s


# ---- the real socket (aiohttp ships with ccxt) ---------------------------------------------------


class _AiohttpSocket:
    def __init__(self, session, ws):
        self._session, self._ws = session, ws

    async def send(self, text: str) -> None:
        await self._ws.send_str(text)

    async def recv(self) -> str:
        import aiohttp

        while True:
            msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                return msg.data
            if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                raise ConnectionError(f"socket closed ({msg.type.name})")

    async def close(self) -> None:
        try:
            await self._ws.close()
        finally:
            await self._session.close()


async def aiohttp_connect(url: str) -> Socket:
    import aiohttp

    session = aiohttp.ClientSession(trust_env=True)  # honours HTTPS_PROXY like the REST clients
    try:
        ws = await session.ws_connect(url, autoping=True, heartbeat=None)
    except BaseException:
        await session.close()
        raise
    return _AiohttpSocket(session, ws)


def BybitLinearStream(symbols: list[str], **kw) -> BybitSpotStream:
    """The USDT perpetuals of ``symbols`` (``BTC/USDT`` streams ``BTCUSDT`` linear): book, last,
    mark and index price, funding and open interest."""
    return BybitSpotStream(symbols, category="linear", **kw)
