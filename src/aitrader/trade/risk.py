"""The risk engine (roadmap "Risk, security and compliance"). Every order the executor sends is
sized and checked here; strategies cannot switch it off.

- Risk per trade is capped (default 0.25% of equity, never above 1%); the position size comes
  from the stop distance.
- Daily and weekly loss caps (default 1% and 5%). Hitting either pauses trading until a person
  resumes it.
- A maximum number of open positions, a maximum exposure per coin, and total exposure capped
  at the account's equity. A spot buy is paid in full from free cash; a perpetual short needs
  only its margin (notional / leverage) from free cash, but its full notional counts as exposure,
  so leverage never makes the book bigger.
- Coins that move together (default BTC, ETH and SOL) form a correlation group: the group as a
  whole has a position limit and an exposure cap, so three of them don't count as three
  independent bets.
- Orders below the exchange minimum are skipped rather than rounded up.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

HARD_MAX_RISK = 0.01  # no setting can raise risk per trade above 1%


@dataclass(frozen=True)
class RiskLimits:
    risk_per_trade: float = 0.0025
    daily_loss_pct: float = 0.01
    weekly_loss_pct: float = 0.05
    max_open_positions: int = 3
    max_coin_exposure_pct: float = 0.25
    max_total_exposure_pct: float = 0.9
    min_order_usdt: float = 10.0
    correlation_groups: tuple[tuple[str, ...], ...] = (("BTC", "ETH", "SOL"),)
    max_group_positions: int = 2
    max_group_exposure_pct: float = 0.4

    @classmethod
    def from_env(cls) -> "RiskLimits":
        e = os.environ.get
        d = cls()
        return cls(
            risk_per_trade=min(float(e("AITRADER_RISK_PER_TRADE", d.risk_per_trade)), HARD_MAX_RISK),
            daily_loss_pct=float(e("AITRADER_DAILY_LOSS_PCT", d.daily_loss_pct)),
            weekly_loss_pct=float(e("AITRADER_WEEKLY_LOSS_PCT", d.weekly_loss_pct)),
            max_open_positions=int(e("AITRADER_MAX_OPEN_POSITIONS", d.max_open_positions)),
            max_coin_exposure_pct=float(e("AITRADER_MAX_COIN_EXPOSURE_PCT", d.max_coin_exposure_pct)),
            max_total_exposure_pct=min(float(e("AITRADER_MAX_TOTAL_EXPOSURE_PCT", d.max_total_exposure_pct)), 1.0),
            min_order_usdt=float(e("AITRADER_MIN_ORDER_USDT", d.min_order_usdt)),
            correlation_groups=_groups(e("AITRADER_CORRELATION_GROUPS")) or d.correlation_groups,
            max_group_positions=int(e("AITRADER_MAX_GROUP_POSITIONS", d.max_group_positions)),
            max_group_exposure_pct=min(float(e("AITRADER_MAX_GROUP_EXPOSURE_PCT", d.max_group_exposure_pct)), 1.0),
        )


def _groups(text: str | None) -> tuple[tuple[str, ...], ...]:
    """``"BTC,ETH,SOL;DOGE,SHIB"`` -> two groups."""
    return tuple(tuple(c.strip().upper() for c in g.split(",") if c.strip())
                 for g in (text or "").split(";") if g.strip())


@dataclass
class Sizing:
    units: float
    notional: float
    risk_usdt: float
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.units > 0


class RiskEngine:
    def __init__(self, limits: RiskLimits | None = None):
        self.limits = limits or RiskLimits()

    def size(self, symbol: str, equity: float, free_cash: float, entry: float, stop: float,
             signal_risk: float, size_mult: float, open_positions: list[dict], short: bool = False,
             leverage: float = 1.0) -> Sizing:
        """Units to buy (or, ``short``, to sell on a perpetual at ``leverage``). ``open_positions``
        are dicts with ``symbol`` and ``notional``."""
        lim = self.limits
        why = []
        if entry <= 0:
            return Sizing(0, 0, 0, ["no price"])
        if short and stop <= entry:
            return Sizing(0, 0, 0, ["stop is not above the entry"])
        if not short and entry <= stop:
            return Sizing(0, 0, 0, ["stop is not below the entry"])
        lev = max(1.0, leverage) if short else 1.0
        if len(open_positions) >= lim.max_open_positions:
            return Sizing(0, 0, 0, [f"{len(open_positions)} positions open (max {lim.max_open_positions})"])
        group = self.group(symbol)
        in_group = [p for p in open_positions if group and self.group(p["symbol"]) == group]
        if group and len(in_group) >= lim.max_group_positions:
            return Sizing(0, 0, 0, [f"{len(in_group)} positions open in the correlated group "
                                    f"{'/'.join(group)} (max {lim.max_group_positions})"])
        risk_pct = min(signal_risk, lim.risk_per_trade, HARD_MAX_RISK) * max(0.0, min(size_mult, 1.0))
        units = equity * risk_pct / abs(entry - stop)
        caps = {
            "coin exposure": self.coin_room(equity, symbol, open_positions),
            "correlated group exposure": (lim.max_group_exposure_pct * equity - sum(p["notional"] for p in in_group)
                                          if group else float("inf")),
            "total exposure": lim.max_total_exposure_pct * equity - sum(p["notional"] for p in open_positions),
            "free cash": free_cash * 0.995 * lev,  # leave room for the fee; a short needs only margin
        }
        for name, room in caps.items():
            if units * entry > room:
                why.append(f"size cut to the {name} limit")
                units = max(0.0, room) / entry
        notional = units * entry
        if notional < lim.min_order_usdt:
            return Sizing(0, notional, 0, why + [f"order of {notional:.2f} USDT is below the "
                                                  f"{lim.min_order_usdt:g} USDT minimum"])
        return Sizing(units, notional, units * abs(entry - stop), why)

    def group(self, symbol: str) -> tuple[str, ...] | None:
        base = symbol.split("/")[0]
        return next((g for g in self.limits.correlation_groups if base in g), None)

    def coin_room(self, equity: float, symbol: str, open_positions: list[dict]) -> float:
        base = symbol.split("/")[0]
        used = sum(p["notional"] for p in open_positions if p["symbol"].split("/")[0] == base)
        return self.limits.max_coin_exposure_pct * equity - used

    def loss_breach(self, equity_now: float, closed: list[dict], now: datetime | None = None,
                    open_loss: float = 0.0) -> str | None:
        """A reason string if today's or this week's loss passed its cap. ``closed`` are trades
        with ``exit_ts`` (ms) and ``pnl_usdt``; ``open_loss`` (zero or negative) is what the open
        positions are down right now, so a big loss counts before the trade is closed.
        ``equity_now`` values open positions at cost."""
        now = now or datetime.now(timezone.utc)
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week0 = day0 - timedelta(days=day0.weekday())
        for start, cap, label in ((day0, self.limits.daily_loss_pct, "daily"),
                                  (week0, self.limits.weekly_loss_pct, "weekly")):
            ms = start.timestamp() * 1000
            realised = sum(t["pnl_usdt"] for t in closed if t["exit_ts"] >= ms)
            start_equity = equity_now - realised
            pnl = realised + min(0.0, open_loss)
            if start_equity > 0 and pnl < 0 and -pnl / start_equity >= cap:
                held = f", {open_loss:+.2f} of it in open positions" if open_loss < 0 else ""
                return (f"{label} loss cap hit: {pnl:+.2f} USDT ({pnl / start_equity:+.1%} of "
                        f"{start_equity:,.2f}; cap {cap:.1%}){held}")
        return None

