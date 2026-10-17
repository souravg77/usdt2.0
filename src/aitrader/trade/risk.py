"""The risk engine (roadmap "Risk, security and compliance"). Every order the executor sends is
sized and checked here; strategies cannot switch it off.

- Risk per trade is capped (default 0.5% of equity, never above 1%); the position size comes
  from the stop distance.
- Daily and weekly loss caps. Hitting either pauses trading until a person resumes it.
- A maximum number of open positions, a maximum exposure per coin, and total exposure capped
  at the account's cash (spot, no leverage).
- Orders below the exchange minimum are skipped rather than rounded up.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

HARD_MAX_RISK = 0.01  # no setting can raise risk per trade above 1%


@dataclass(frozen=True)
class RiskLimits:
    risk_per_trade: float = 0.005
    daily_loss_pct: float = 0.02
    weekly_loss_pct: float = 0.05
    max_open_positions: int = 3
    max_coin_exposure_pct: float = 0.25
    max_total_exposure_pct: float = 0.9
    min_order_usdt: float = 10.0

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
        )


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
             signal_risk: float, size_mult: float, open_positions: list[dict]) -> Sizing:
        """Units to buy. ``open_positions`` are dicts with ``symbol`` and ``notional``."""
        lim = self.limits
        why = []
        if entry <= stop or entry <= 0:
            return Sizing(0, 0, 0, ["stop is not below the entry"])
        if len(open_positions) >= lim.max_open_positions:
            return Sizing(0, 0, 0, [f"{len(open_positions)} positions open (max {lim.max_open_positions})"])
        risk_pct = min(signal_risk, lim.risk_per_trade, HARD_MAX_RISK) * max(0.0, min(size_mult, 1.0))
        units = equity * risk_pct / (entry - stop)
        caps = {
            "coin exposure": self.coin_room(equity, symbol, open_positions),
            "total exposure": lim.max_total_exposure_pct * equity - sum(p["notional"] for p in open_positions),
            "free cash": free_cash * 0.995,  # leave room for the fee
        }
        for name, room in caps.items():
            if units * entry > room:
                why.append(f"size cut to the {name} limit")
                units = max(0.0, room) / entry
        notional = units * entry
        if notional < lim.min_order_usdt:
            return Sizing(0, notional, 0, why + [f"order of {notional:.2f} USDT is below the "
                                                  f"{lim.min_order_usdt:g} USDT minimum"])
        return Sizing(units, notional, units * (entry - stop), why)

    def coin_room(self, equity: float, symbol: str, open_positions: list[dict]) -> float:
        base = symbol.split("/")[0]
        used = sum(p["notional"] for p in open_positions if p["symbol"].split("/")[0] == base)
        return self.limits.max_coin_exposure_pct * equity - used

    def loss_breach(self, equity_now: float, closed: list[dict], now: datetime | None = None) -> str | None:
        """A reason string if today's or this week's realised loss passed its cap. ``closed``
        are trades with ``exit_ts`` (ms) and ``pnl_usdt``."""
        now = now or datetime.now(timezone.utc)
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week0 = day0 - timedelta(days=day0.weekday())
        for start, cap, label in ((day0, self.limits.daily_loss_pct, "daily"),
                                  (week0, self.limits.weekly_loss_pct, "weekly")):
            ms = start.timestamp() * 1000
            pnl = sum(t["pnl_usdt"] for t in closed if t["exit_ts"] >= ms)
            start_equity = equity_now - pnl
            if start_equity > 0 and pnl < 0 and -pnl / start_equity >= cap:
                return (f"{label} loss cap hit: {pnl:+.2f} USDT ({pnl / start_equity:+.1%} of "
                        f"{start_equity:,.2f}; cap {cap:.1%})")
        return None

