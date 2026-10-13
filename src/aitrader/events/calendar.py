"""Economic-calendar no-trade windows (roadmap Phase 2, step 4).

Reads the Crypto Craft weekly JSON feed. Each item looks like
``{"title": "CPI m/m", "country": "USD", "date": "2026-10-14T08:30:00-04:00", "impact": "High",
"forecast": "0.3%", "previous": "0.4%"}``. Times carry a New York offset; everything is converted
to UTC here. High-impact events block new entries before the release and for a settling period
after it. If the feed cannot be read, the filter fails safe and cuts position size.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..clock import local

log = logging.getLogger(__name__)

FEED_THIS_WEEK = "https://nfs.faireconomy.media/cc_calendar_thisweek.json"
FEED_NEXT_WEEK = "https://nfs.faireconomy.media/cc_calendar_nextweek.json"


@dataclass(frozen=True)
class EconomicEvent:
    time: datetime  # UTC
    title: str
    country: str
    impact: str
    forecast: str = ""
    previous: str = ""
    actual: str = ""
    source: str = "cryptocraft"
    asset: str = "*"  # macro events apply to every coin


@dataclass(frozen=True)
class FilterDecision:
    allow_entries: bool
    size_multiplier: float
    tighten_stops: bool
    reason: str
    event: EconomicEvent | None = None


def parse_calendar(items: list[dict]) -> list[EconomicEvent]:
    out = []
    for it in items:
        try:
            dt = datetime.fromisoformat(str(it["date"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            log.warning("skipping calendar item with bad date: %r", it.get("date"))
            continue
        if dt.tzinfo is None:  # the feed should always carry an offset; refuse to guess
            log.warning("skipping calendar item without time zone: %r", it.get("title"))
            continue
        out.append(EconomicEvent(
            time=dt.astimezone(timezone.utc),
            title=str(it.get("title", "")),
            country=str(it.get("country", "")),
            impact=str(it.get("impact", "")).strip().title(),
            forecast=str(it.get("forecast", "") or ""),
            previous=str(it.get("previous", "") or ""),
            actual=str(it.get("actual", "") or ""),
        ))
    return sorted(out, key=lambda e: e.time)


def fetch_calendar(url: str = FEED_THIS_WEEK, timeout: float = 15.0) -> list[EconomicEvent]:
    req = urllib.request.Request(url, headers={"User-Agent": "aitrader/0.1 (calendar filter)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return parse_calendar(json.loads(resp.read().decode("utf-8")))


@dataclass
class CalendarFilter:
    """Decides, for a moment in time, whether new entries are allowed and at what size."""

    block_before: timedelta = timedelta(minutes=30)
    settle_after: timedelta = timedelta(minutes=30)
    impacts: tuple[str, ...] = ("High",)
    failsafe_size: float = 0.5
    refresh_every: float = 3 * 3600  # seconds
    max_age: float = 12 * 3600  # older than this and we treat the feed as failed
    urls: tuple[str, ...] = (FEED_THIS_WEEK, FEED_NEXT_WEEK)
    events: list[EconomicEvent] = field(default_factory=list)
    loaded_at: float | None = None
    fetcher: object = None

    def refresh(self, force: bool = False, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        if not force and self.loaded_at and now - self.loaded_at < self.refresh_every:
            return True
        fetch = self.fetcher or fetch_calendar
        events: list[EconomicEvent] = []
        ok_this_week = False
        for i, url in enumerate(self.urls):
            try:
                events += fetch(url)
                ok_this_week |= i == 0
            except Exception as exc:  # next week's file often doesn't exist yet: that's fine
                log.warning("calendar fetch failed for %s: %s", url, exc)
        if not ok_this_week:
            return False
        uniq = {(e.time, e.title, e.country): e for e in events}
        self.events = sorted(uniq.values(), key=lambda e: e.time)
        self.loaded_at = now
        return True

    def healthy(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return self.loaded_at is not None and now - self.loaded_at <= self.max_age

    def decide(self, at: datetime, asset: str = "*") -> FilterDecision:
        at = at.astimezone(timezone.utc)
        for ev in self.events:
            if ev.impact not in self.impacts or ev.asset not in ("*", asset):
                continue
            if ev.time - self.block_before <= at < ev.time:
                return FilterDecision(False, 0.0, True, f"{ev.title} ({ev.country}) at "
                                      f"{local(ev.time.timestamp(), '%H:%M')}: no new entries before release", ev)
            if ev.time <= at < ev.time + self.settle_after:
                return FilterDecision(False, 0.0, False, f"{ev.title} ({ev.country}) released "
                                      f"{local(ev.time.timestamp(), '%H:%M')}: waiting for markets to settle", ev)
        if not self.healthy(at.timestamp()):
            return FilterDecision(True, self.failsafe_size, False,
                                  "calendar feed unavailable: trading at reduced size")
        return FilterDecision(True, 1.0, False, "no high-impact event nearby")

    def upcoming(self, at: datetime, hours: float = 48) -> list[EconomicEvent]:
        end = at + timedelta(hours=hours)
        return [e for e in self.events if at <= e.time <= end and e.impact in self.impacts]
