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

import math
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


# Paper futures only: the leverage choices Bybit offers (each coin also has its own maximum,
# ``market.perp_limits``). The live trader stays capped at MAX_LEVERAGE.
PAPER_LEVERAGE_CHOICES = (1.0, 2.0, 3.0, 5.0, 10.0, 20.0, 25.0, 50.0, 75.0, 100.0)
PAPER_MAX_LEVERAGE = 100.0


def clamp_leverage(value: float) -> float:
    return min(max(float(value), 1.0), MAX_LEVERAGE)


def clamp_paper_leverage(value: float, coin_max: float | None = None) -> float:
    """A paper leverage choice, 1x up to Bybit's 100x and to the coin's own maximum when known."""
    top = min(PAPER_MAX_LEVERAGE, coin_max) if coin_max else PAPER_MAX_LEVERAGE
    return min(max(float(value), 1.0), top)


def perp_symbol(symbol: str) -> str:
    """The ccxt name of the USDT perpetual for a spot pair: BTC/USDT -> BTC/USDT:USDT."""
    return symbol if ":" in symbol else f"{symbol}:{symbol.split('/')[1]}"


def maintenance_rate(coin_max: float | None = None) -> float:
    """The maintenance margin rate of a coin's lowest risk tier. Bybit sets it at half the initial
    margin (1 / the coin's maximum leverage): 0.5% on a 100x coin, 1% on 50x, 2% on 25x. Smaller
    coins are liquidated sooner, so this matters for the top gainers."""
    return max(MAINTENANCE_MARGIN, 0.5 / coin_max) if coin_max and coin_max > 0 else MAINTENANCE_MARGIN


def short_liquidation_price(entry: float, lev: float, mmr: float = MAINTENANCE_MARGIN) -> float:
    """Roughly where Bybit liquidates an isolated short opened at ``entry`` with ``lev``."""
    return entry * (1 + 1 / lev - mmr)


def short_stop_ok(entry: float, stop: float, lev: float, mmr: float = MAINTENANCE_MARGIN) -> bool:
    """True when the stop is above the entry and at most ``STOP_BUFFER`` of the way to the
    liquidation price."""
    if not (entry > 0 and stop > entry):
        return False
    room = short_liquidation_price(entry, lev, mmr) - entry
    return stop - entry <= room * STOP_BUFFER


def long_liquidation_price(entry: float, lev: float, mmr: float = MAINTENANCE_MARGIN) -> float:
    """Roughly where Bybit liquidates an isolated long opened at ``entry`` with ``lev``."""
    return entry * (1 - 1 / lev + mmr)


def long_stop_ok(entry: float, stop: float, lev: float, mmr: float = MAINTENANCE_MARGIN) -> bool:
    """True when the stop is below the entry and at most ``STOP_BUFFER`` of the way down to the
    liquidation price."""
    if not (entry > stop > 0):
        return False
    room = entry - long_liquidation_price(entry, lev, mmr)
    return entry - stop <= room * STOP_BUFFER


def safe_leverage(entry: float, stop: float, short: bool, lev: float, mmr: float = MAINTENANCE_MARGIN) -> float:
    """The highest leverage, up to ``lev``, at which the stop sits at most ``STOP_BUFFER`` of the
    way to the liquidation price (rounded down to 0.1x); 0 when even 1x is too much. A wide stop
    then trades at lower leverage instead of not at all; the risk in USDT doesn't change, only
    the margin set aside."""
    dist = (stop - entry if short else entry - stop) / entry if entry > 0 else 0
    if dist <= 0:
        return 0.0
    most = 1.0 / (dist / STOP_BUFFER + mmr)
    fit = min(lev, math.floor(most * 10 + 1e-9) / 10)
    ok = short_stop_ok if short else long_stop_ok
    while fit >= 1.0 and not ok(entry, stop, fit, mmr):  # rounding at the edge
        fit = round(fit - 0.1, 1)
    return fit if fit >= 1.0 else 0.0


def margin(notional: float, lev: float) -> float:
    return notional / max(lev, 1.0)
