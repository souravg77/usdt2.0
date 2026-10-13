"""The real Bybit account, read only, for the Trading page.

With the real-account key saved (Settings page or ``.env``), the page shows what the account
holds and what it is worth in USDT. Only two reads are made: the wallet balance (with the key)
and the public prices of the coins held. Nothing here can place, change or cancel an order.

Answers are cached for a minute, so Bybit sees at most one balance request a minute however many
pages are open ("Refresh now" asks again at once, at most every ``MIN_GAP_S`` seconds). One Bybit
client is kept for the key and loads only the spot market list, so a refresh is two quick calls
instead of re-loading every perpetual and option market each minute, which could outrun the
timeout and leave the card on old numbers. When a refresh fails the last good numbers stay, marked
with their age and the reason. Errors are shown by their type only: ccxt puts the signed request
in the message.
"""

from __future__ import annotations

import asyncio
import os
import time

CACHE_S = 60.0
MIN_GAP_S = 10.0
STABLE = {"USDT", "USDC", "FDUSD"}
DUST_USDT = 0.01


class RealAccount:
    def __init__(self, adapter_factory=None, clock=time.monotonic, environ=None):
        self.adapter_factory = adapter_factory or _bybit
        self.clock, self.environ = clock, environ if environ is not None else os.environ
        self._snap: dict | None = None
        self._at = 0.0
        self._for = ""
        self._task: asyncio.Task | None = None
        self._adapter = None
        self._adapter_for = ""

    def _keys(self) -> tuple[str, str]:
        return (self.environ.get("BYBIT_API_KEY", "").strip(), self.environ.get("BYBIT_API_SECRET", "").strip())

    async def snapshot(self, wait: float = 4.0) -> dict:
        """The latest view of the account. Refreshes it when older than a minute, waiting at most
        ``wait`` seconds; a slower answer shows up on the next page load."""
        key, secret = self._keys()
        if not (key and secret):
            return {"state": "no_key"}
        fresh = self._snap is not None and self._for == key[-4:] and self.clock() - self._at < CACHE_S
        if not fresh:
            if self._task is None or self._task.done():
                self._task = asyncio.ensure_future(self._refresh(key, secret))
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=wait)
            except asyncio.TimeoutError:
                pass
        if self._snap is None or self._for != key[-4:]:
            return {"state": "loading"}
        return self._snap

    def refresh_now(self) -> bool:
        """Make the next page load ask Bybit again. False when the last answer is under
        ``MIN_GAP_S`` seconds old (it is shown as it is)."""
        if self._snap is not None and self.clock() - self._at < MIN_GAP_S:
            return False
        self._at = 0.0
        return True

    async def _drop_adapter(self) -> None:
        adapter, self._adapter, self._adapter_for = self._adapter, None, ""
        if adapter is not None:
            try:
                await adapter.close()
            except Exception:
                pass

    async def _refresh(self, key: str, secret: str) -> None:
        prev = self._snap if self._snap is not None and self._for == key[-4:] else None
        try:
            if self._adapter is None or self._adapter_for != key:
                await self._drop_adapter()
                self._adapter, self._adapter_for = self.adapter_factory(key, secret), key
            adapter = self._adapter
            bal = await asyncio.wait_for(adapter.balance(), timeout=10)
            assets = sorted(set(bal.free) | set(bal.used))
            pairs = [f"{a}/USDT" for a in assets if a not in STABLE]
            prices, price_note = {}, ""
            if pairs:
                # every spot ticker in one call, then ours picked out: asking for a list makes ccxt
                # refuse the whole call when one coin has no USDT market, and the total then
                # counted the stablecoins only and stood still while prices moved
                try:
                    raw = await asyncio.wait_for(adapter.client.fetch_tickers(), timeout=10)
                    prices = {p.split("/")[0]: float(raw[p]["last"]) for p in pairs
                              if (raw.get(p) or {}).get("last")}
                except Exception as exc:
                    price_note = "coin prices: " + _why(exc)
            coins = []
            for a in assets:
                amount = bal.total(a)
                value = amount if a in STABLE else (amount * prices[a] if a in prices else None)
                if value is not None and value < DUST_USDT:
                    continue
                coins.append({"asset": a, "amount": amount, "free": bal.free.get(a, 0.0), "usdt": value})
            coins.sort(key=lambda c: -(c["usdt"] or 0))
            snap = {"state": "ok", "coins": coins, "total_usdt": sum(c["usdt"] or 0 for c in coins),
                    "at": time.time(), "price_note": price_note}
        except Exception as exc:
            await self._drop_adapter()  # the next try starts with a fresh connection
            if prev is not None and prev.get("state") == "ok":
                snap = {**prev, "stale": _why(exc)}  # keep the last good numbers, with their age
            else:
                snap = {"state": "error", "note": _why(exc), "at": time.time()}
        self._snap, self._at, self._for = snap, self.clock(), key[-4:]


def _bybit(key: str, secret: str):
    from ..config import ExchangeKeys, Settings
    from ..exchanges.ccxt_adapter import BybitAdapter

    # paper Settings: this adapter is only ever asked for the balance and prices; any order
    # would still be refused by the live lock
    adapter = BybitAdapter(Settings(), keys=ExchangeKeys(key, secret))
    # spot markets only: Bybit's full list adds perpetuals and options, several slow calls
    adapter.client.options.setdefault("fetchMarkets", {})["types"] = ["spot"]
    return adapter


def _why(exc: Exception) -> str:
    name = type(exc).__name__
    if name in ("AuthenticationError", "PermissionDenied"):
        return "Bybit refused the key (check it on the Settings page, and that it is bound to this server's IP)"
    if name in ("TimeoutError", "RequestTimeout", "NetworkError", "ExchangeNotAvailable"):
        return "couldn't reach Bybit just now"
    return f"couldn't read the account ({name})"
