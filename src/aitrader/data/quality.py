"""Data-quality checks: gaps, duplicates, misaligned timestamps and impossible bars."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import TIMEFRAME_MS, Candle


@dataclass
class QualityReport:
    count: int = 0
    gaps: list[tuple[int, int]] = field(default_factory=list)  # (from_ts, missing_bars)
    duplicates: list[int] = field(default_factory=list)
    misaligned: list[int] = field(default_factory=list)
    out_of_order: list[int] = field(default_factory=list)
    bad_bars: list[int] = field(default_factory=list)  # high<low, close outside range, negatives

    @property
    def ok(self) -> bool:
        return not (self.gaps or self.duplicates or self.misaligned or self.out_of_order or self.bad_bars)

    def as_text(self) -> str:
        missing = sum(n for _, n in self.gaps)
        return (
            f"bars={self.count} gaps={len(self.gaps)} (missing={missing}) "
            f"duplicates={len(self.duplicates)} misaligned={len(self.misaligned)} "
            f"out_of_order={len(self.out_of_order)} bad_bars={len(self.bad_bars)}"
        )


def check_candles(candles: list[Candle], timeframe: str) -> QualityReport:
    """Bars must be UTC-aligned to the timeframe (e.g. 4h bars open at 00,04,08... UTC)."""
    step = TIMEFRAME_MS[timeframe]
    rep = QualityReport(count=len(candles))
    seen: set[int] = set()
    prev: int | None = None
    for c in candles:
        if c.ts in seen:
            rep.duplicates.append(c.ts)
            continue
        seen.add(c.ts)
        if c.ts % step != 0:
            rep.misaligned.append(c.ts)
        if (
            c.high < c.low
            or not (c.low <= c.open <= c.high)
            or not (c.low <= c.close <= c.high)
            or min(c.open, c.high, c.low, c.close, c.volume) < 0
        ):
            rep.bad_bars.append(c.ts)
        if prev is not None:
            if c.ts < prev:
                rep.out_of_order.append(c.ts)
            elif c.ts - prev > step:
                rep.gaps.append((prev, (c.ts - prev) // step - 1))
        prev = c.ts if prev is None else max(prev, c.ts)
    return rep
