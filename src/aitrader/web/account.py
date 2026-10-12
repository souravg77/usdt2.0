"""The real Bybit account, read only, for the Trading page.

With the real-account key saved (Settings page or ``.env``), the page shows what the account
holds and what it is worth in USDT. Only two reads are made: the wallet balance (with the key)
and the public prices of the coins held. Nothing here can place, change or cancel an order.

Answers are cached for a minute, so Bybit sees at most one balance request a minute however many
pages are open. Errors are shown by their type only: ccxt puts the signed request in the message.
"""

from __future__ import annotations

import asyncio
import os
import time

CACHE_S = 60.0
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

    async def _refresh(self, key: str, secret: str) -> None:
        adapter = self.adapter_factory(key, secret)
        try:
            bal = await asyncio.wait_for(adapter.balance(), timeout=10)
            assets = sorted(set(bal.free) | set(bal.used))
            pairs = [f"{a}/USDT" for a in assets if a not in STABLE]
            prices = {}
            if pairs:
                try:
                    raw = await asyncio.wait_for(adapter.client.fetch_tickers(pairs), timeout=10)
                    prices = {s.split("/")[0]: float(t["last"]) for s, t in raw.items() if t.get("last")}
                except Exception:
                    pass  # a coin without a USDT market is listed without a value
            coins = []
            for a in assets:
                amount = bal.total(a)
                value = amount if a in STABLE else (amount * prices[a] if a in prices else None)
                if value is not None and value < DUST_USDT:
                    continue
                coins.append({"asset": a, "amount": amount, "free": bal.free.get(a, 0.0), "usdt": value})
            coins.sort(key=lambda c: -(c["usdt"] or 0))
            snap = {"state": "ok", "coins": coins, "total_usdt": sum(c["usdt"] or 0 for c in coins),
                    "at": time.time()}
        except Exception as exc:
            snap = {"state": "error", "note": _why(exc), "at": time.time()}
        finally:
            try:
                await adapter.close()
            except Exception:
                pass
        self._snap, self._at, self._for = snap, self.clock(), key[-4:]


def _bybit(key: str, secret: str):
    from ..config import ExchangeKeys, Settings
    from ..exchanges.ccxt_adapter import BybitAdapter

    # paper Settings: this adapter is only ever asked for the balance and prices; any order
    # would still be refused by the live lock
    return BybitAdapter(Settings(), keys=ExchangeKeys(key, secret))


def _why(exc: Exception) -> str:
    name = type(exc).__name__
    if name in ("AuthenticationError", "PermissionDenied"):
        return "Bybit refused the key (check it on the Settings page, and that it is bound to this server's IP)"
    if name in ("TimeoutError", "RequestTimeout", "NetworkError", "ExchangeNotAvailable"):
        return "couldn't reach Bybit just now"
    return f"couldn't read the account ({name})"
