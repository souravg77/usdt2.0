"""Derivatives readings for one Bybit USDT perpetual (Agent F's inputs): last, mark and index
price, basis, funding and open interest, and how far a short is from liquidation.

- **Last price** is the perpetual's own last trade. **Index price** is Bybit's average of spot
  prices on several exchanges. **Mark price** is what Bybit uses for unrealised P&L and
  liquidation; it follows the index, so a wick on one exchange doesn't liquidate anyone.
- **Basis** = (last - index) / index: how far the perpetual trades above (+) or below (-) spot.
  **Mark premium** = (mark - index) / index.
- **Funding** is paid every 8 hours (``fundingIntervalHour``) between longs and shorts: a positive
  rate means longs pay shorts. ``funding_annualized`` = rate x payments per year.

Sources: the REST ticker (``/v5/market/tickers?category=linear``, through ccxt) and the public
linear WebSocket ``tickers`` topic (``BybitSpotStream(category="linear")``). Both carry the same
fields; a WebSocket delta only carries the fields that changed. Public data: no key needed.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

from .context import perp_symbol

FUNDING_HOURS = 8.0


def _f(value) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


@dataclass
class DerivSnapshot:
    symbol: str  # the spot pair, e.g. BTC/USDT; the perpetual is BTC/USDT:USDT
    last: float | None = None
    mark: float | None = None
    index: float | None = None
    funding_rate: float | None = None
    next_funding_ts: int | None = None  # ms
    funding_hours: float = FUNDING_HOURS
    open_interest: float | None = None  # coins
    ts: int = 0  # ms, Bybit's time of the reading
    notes: list[str] = field(default_factory=list)

    @property
    def basis(self) -> float | None:
        if self.last is None or not self.index:
            return None
        return (self.last - self.index) / self.index

    @property
    def mark_premium(self) -> float | None:
        if self.mark is None or not self.index:
            return None
        return (self.mark - self.index) / self.index

    @property
    def funding_annualized(self) -> float | None:
        if self.funding_rate is None:
            return None
        return self.funding_rate * (24 / self.funding_hours) * 365

    def funding_in_s(self, now_ms: int | None = None) -> float | None:
        """Seconds until the next funding payment."""
        if not self.next_funding_ts:
            return None
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        return max(0.0, (self.next_funding_ts - now_ms) / 1000)

    def as_dict(self) -> dict:
        return {"symbol": self.symbol, "last": self.last, "mark": self.mark, "index": self.index,
                "basis": self.basis, "mark_premium": self.mark_premium, "funding_rate": self.funding_rate,
                "funding_annualized": self.funding_annualized, "next_funding_ts": self.next_funding_ts,
                "open_interest": self.open_interest, "ts": self.ts}


def parse_ticker(info: dict, symbol: str, base: DerivSnapshot | None = None) -> DerivSnapshot:
    """A Bybit V5 linear ticker (REST row or WebSocket snapshot/delta) applied on top of ``base``:
    fields missing from ``info`` keep their previous value."""
    snap = base or DerivSnapshot(symbol)
    pairs = {"lastPrice": "last", "markPrice": "mark", "indexPrice": "index", "fundingRate": "funding_rate",
             "openInterest": "open_interest"}
    for key, attr in pairs.items():
        if key in info and info[key] not in (None, ""):
            setattr(snap, attr, _f(info[key]))
    if info.get("nextFundingTime") not in (None, "", "0"):
        snap.next_funding_ts = int(float(info["nextFundingTime"]))
    if info.get("fundingIntervalHour") not in (None, ""):
        snap.funding_hours = _f(info["fundingIntervalHour"]) or FUNDING_HOURS
    return snap


async def fetch_snapshot(client, symbol: str) -> DerivSnapshot:
    """REST reading for the perpetual of ``symbol`` (a ccxt Bybit client with swap markets)."""
    t = await client.fetch_ticker(perp_symbol(symbol))
    snap = parse_ticker(t.get("info") or {}, symbol)
    if snap.last is None:
        snap.last = _f(t.get("last"))
    snap.ts = int(t.get("timestamp") or time.time() * 1000)
    return snap


def liquidation_distance(mark: float | None, liq_price: float | None, short: bool = True) -> float | None:
    """How far the price must move against the position to liquidate it, as a fraction of the
    mark price (0.25 = 25%). None when either price is unknown (Bybit sends liqPrice "" when
    there's no position)."""
    if not mark or not liq_price:
        return None
    return (liq_price - mark) / mark if short else (mark - liq_price) / mark


def stop_inside_liquidation(entry: float, stop: float, liq_price: float | None, short: bool = True,
                            max_fraction: float = 0.5) -> bool:
    """True when the stop sits at most ``max_fraction`` of the way from entry to Bybit's real
    liquidation price (the rule ``trade.futures.short_stop_ok`` applies with an estimate)."""
    if not liq_price:
        return True  # unknown: the entry-time estimate stands
    if short:
        return stop <= entry + max_fraction * (liq_price - entry)
    return stop >= entry - max_fraction * (entry - liq_price)
