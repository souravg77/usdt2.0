"""Bybit USDT perpetual futures: the leverage setting and the liquidation check.

The agent sells perpetuals short (the short twins in ``strategies.mirror``); longs stay on spot.
Positions use **isolated** margin, so a position can lose at most the margin put into it, never
the rest of the account. Size always comes from the risk engine (the stop distance): leverage
doesn't make a trade bigger, it only means less margin is set aside for it, which also brings the
liquidation price closer. So:

- ``AITRADER_LEVERAGE`` (default 1, at most ``MAX_LEVERAGE``) sets it;
- a short is only taken when its stop sits at most halfway to the liquidation price, so the stop
  (resting on Bybit) always closes the position long before Bybit would liquidate it.
"""

from __future__ import annotations

import os

MAX_LEVERAGE = 3.0
MAINTENANCE_MARGIN = 0.005  # Bybit's lowest tier for the big USDT perpetuals (0.5%)
STOP_BUFFER = 0.5  # the stop may use at most this share of the distance to liquidation


def leverage(environ=None) -> float:
    """The configured leverage, clamped to 1..MAX_LEVERAGE (a bad value means 1)."""
    raw = (environ if environ is not None else os.environ).get("AITRADER_LEVERAGE", "1")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 1.0
    return min(max(value, 1.0), MAX_LEVERAGE)


def perp_symbol(symbol: str) -> str:
    """The ccxt name of the USDT perpetual for a spot pair: BTC/USDT -> BTC/USDT:USDT."""
    return symbol if ":" in symbol else f"{symbol}:{symbol.split('/')[1]}"


def short_liquidation_price(entry: float, lev: float) -> float:
    """Roughly where Bybit liquidates an isolated short opened at ``entry`` with ``lev``."""
    return entry * (1 + 1 / lev - MAINTENANCE_MARGIN)


def short_stop_ok(entry: float, stop: float, lev: float) -> bool:
    """True when the stop is above the entry and at most ``STOP_BUFFER`` of the way to the
    liquidation price."""
    if not (entry > 0 and stop > entry):
        return False
    room = short_liquidation_price(entry, lev) - entry
    return stop - entry <= room * STOP_BUFFER


def long_liquidation_price(entry: float, lev: float) -> float:
    """Roughly where Bybit liquidates an isolated long opened at ``entry`` with ``lev``."""
    return entry * (1 - 1 / lev + MAINTENANCE_MARGIN)


def long_stop_ok(entry: float, stop: float, lev: float) -> bool:
    """True when the stop is below the entry and at most ``STOP_BUFFER`` of the way down to the
    liquidation price."""
    if not (entry > stop > 0):
        return False
    room = entry - long_liquidation_price(entry, lev)
    return entry - stop <= room * STOP_BUFFER


def margin(notional: float, lev: float) -> float:
    return notional / max(lev, 1.0)
