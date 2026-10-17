"""Pre-trade market checks that don't depend on a strategy.

Binance Academy: a small bid-ask spread means a liquid market, and market orders on a thin book
walk up the book (slippage). The Treasury report adds that reported volume can be inflated by
wash trading, so the live book is checked rather than trusting the volume figure.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import OrderBook


@dataclass(frozen=True)
class LiquidityCheck:
    ok: bool
    spread: float  # (ask - bid) / mid
    bid_depth: float  # quote currency resting within the band below mid
    ask_depth: float
    reason: str


def _depth(levels, lo: float, hi: float) -> float:
    return sum(p * q for p, q in levels if lo <= p <= hi)


def liquidity_check(book: OrderBook, order_quote: float, max_spread: float = 0.001,
                    band: float = 0.005, depth_mult: float = 10.0) -> LiquidityCheck:
    """Allow a trade of ``order_quote`` (e.g. USDT) only if the spread is at most ``max_spread``
    and each side of the book holds at least ``depth_mult`` x the order within ``band`` of mid,
    so both the entry and a stop-market exit can fill without moving the price much."""
    bid, ask = book.best_bid, book.best_ask
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return LiquidityCheck(False, float("nan"), 0.0, 0.0, "order book empty or crossed")
    mid = (bid + ask) / 2
    spread = (ask - bid) / mid
    bid_depth = _depth(book.bids, mid * (1 - band), mid)
    ask_depth = _depth(book.asks, mid, mid * (1 + band))
    need = depth_mult * order_quote
    problems = []
    if spread > max_spread:
        problems.append(f"spread {spread:.3%} > {max_spread:.3%}")
    if bid_depth < need:
        problems.append(f"bid depth {bid_depth:,.0f} < {need:,.0f}")
    if ask_depth < need:
        problems.append(f"ask depth {ask_depth:,.0f} < {need:,.0f}")
    return LiquidityCheck(not problems, spread, bid_depth, ask_depth,
                          "; ".join(problems) or "liquid")
