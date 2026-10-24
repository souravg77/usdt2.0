"""Market-context filter (roadmap Phase 2, step 3): vetoes or shrinks new entries when the
derivatives market or the whole crypto market looks stretched. It never creates an entry.

Rules (defaults are conservative: they only cut risk, and only on clear extremes):

- funding above +0.05% per 8h: longs are crowded, size x0.5; above +0.10%: no new longs.
- open interest up more than 15% in 24h while price fell: new leverage is piling into a falling
  market, size x0.5.
- funding above +0.05% per 8h and open interest up more than 10% in 24h: crowded longs still
  piling in (Shawn's gate), no new longs.
- total crypto market cap down more than 8% in 24h: market-wide sell-off, no new longs.

A short (``side="sell"``, Futures AI) gets the mirror: funding below -0.05% means shorts are
crowded (size x0.5) and below -0.10% means a squeeze is likely (no new shorts); open interest up
15% while price rose halves it. A market-wide sell-off doesn't block shorts.
- a series older than twice its refresh interval (``data.context.REFRESH_MS``) is ignored and
  that rule is skipped. Missing data never blocks a trade; the calendar filter already covers the
  "feed down" case for macro risk.

Size multipliers from several rules multiply (two x0.5 rules give x0.25).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..data.context import DAY_MS, GLOBAL, SOURCE_EXCHANGE, ContextStore, stale_after
from ..models import TIMEFRAME_MS

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContextDecision:
    allow_entries: bool
    size_multiplier: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class ContextFilter:
    """Decides, for a market and a moment, whether new entries are allowed and at what size."""

    store: ContextStore
    funding_reduce: float = 0.0005  # +0.05% per 8h
    funding_block: float = 0.001  # +0.10% per 8h
    funding_size: float = 0.5
    oi_jump: float = 0.15  # open interest +15% in 24h ...
    oi_size: float = 0.5  # ... while price fell
    crowd_oi: float = 0.10  # open interest +10% in 24h while funding is above ``funding_reduce``
    mcap_drop: float = 0.08  # total market cap -8% in 24h
    source_exchange: str = SOURCE_EXCHANGE
    price_timeframes: tuple[str, ...] = ("1h", "15m", "4h")

    def _fresh(self, metric: str, exchange: str, symbol: str, at: int) -> tuple[int, float] | None:
        row = self.store.value_at(metric, exchange, symbol, at)
        if row is None or at - row[0] > stale_after(metric):
            return None
        return row

    def _derivs(self, metric: str, exchange: str, symbol: str, at: int) -> tuple[int, float] | None:
        row = self._fresh(metric, exchange, symbol, at)
        if row is None and exchange != self.source_exchange:
            row = self._fresh(metric, self.source_exchange, symbol, at)
        return row

    def _price_change(self, exchange: str, symbol: str, now: int) -> float | None:
        """24h change of the last closed bar's close, from stored candles."""
        for tf in self.price_timeframes:
            step = TIMEFRAME_MS[tf]
            bars = self.store.candles.load(exchange, symbol, tf, now - DAY_MS - 2 * step, now - step + 1)
            if len(bars) < 2:
                continue
            last, first = bars[-1], bars[0]
            if now - (last.ts + step) > 2 * step or last.ts - first.ts < DAY_MS:
                continue
            ago = next((b for b in reversed(bars) if b.ts <= last.ts - DAY_MS), None)
            if ago is not None and ago.close > 0:
                return last.close / ago.close - 1
        return None

    def decide(self, exchange: str, symbol: str, now_ms: int | None = None,
               price_change_24h: float | None = None, side: str = "buy") -> ContextDecision:
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        allow, size, reasons = True, 1.0, []
        short = side in ("sell", "short")
        sign = -1.0 if short else 1.0  # a short reads every rule upside down

        fr = self._derivs("funding_rate", exchange, symbol, now)
        if fr is None:
            reasons.append("funding rate missing or stale: rule skipped")
        elif sign * fr[1] > self.funding_block:
            allow = False
            reasons.append(f"funding {fr[1]:+.4%}/8h {'below' if short else 'above'} "
                           f"{sign * self.funding_block:+.2%}: no new {'shorts' if short else 'longs'}")
        elif sign * fr[1] > self.funding_reduce:
            size *= self.funding_size
            reasons.append(f"funding {fr[1]:+.4%}/8h {'below' if short else 'above'} "
                           f"{sign * self.funding_reduce:+.2%}: size x{self.funding_size:g}")

        oi_now = self._derivs("open_interest", exchange, symbol, now)
        oi_ago = self._derivs("open_interest", exchange, symbol, now - DAY_MS) if oi_now else None
        oi_chg = oi_now[1] / oi_ago[1] - 1 if oi_now and oi_ago and oi_ago[1] > 0 else None
        if oi_chg is not None and fr is not None and allow and sign * fr[1] > self.funding_reduce \
                and oi_chg > self.crowd_oi:
            allow = False
            side_word = "shorts" if short else "longs"
            reasons.append(f"funding {fr[1]:+.4%}/8h and open interest {oi_chg:+.1%} in 24h: crowded "
                           f"{side_word} still piling in, no new {side_word}")
        move = price_change_24h if price_change_24h is not None else self._price_change(exchange, symbol, now)
        if oi_chg is None or move is None:
            reasons.append("open interest or price history missing or stale: rule skipped")
        elif oi_chg > self.oi_jump and sign * move < 0:
            size *= self.oi_size
            reasons.append(f"open interest {oi_chg:+.1%} in 24h while price {move:+.1%}: "
                           f"size x{self.oi_size:g}")

        chg = self._fresh("total_mcap_change_24h", *GLOBAL, now)
        if chg is None:
            reasons.append("total market cap change missing or stale: rule skipped")
        elif chg[1] < -self.mcap_drop and not short:
            allow = False
            reasons.append(f"total market cap {chg[1]:+.1%} in 24h (below -{self.mcap_drop:.0%}): "
                           "no new longs")

        return ContextDecision(allow, size if allow else 0.0, reasons)
