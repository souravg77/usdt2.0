"""The time zone the dashboard shows times in: ``AITRADER_TZ`` (an IANA name), Kolkata (IST) by
default. Only what people read changes; stored timestamps, candles and schedules stay in UTC."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone, tzinfo

DEFAULT_TZ = "Asia/Kolkata"
# no tz database in the image: Kolkata has had no daylight saving since 1945, so a fixed offset is exact
_FIXED = {"Asia/Kolkata": (timedelta(hours=5, minutes=30), "IST"), "Asia/Calcutta": (timedelta(hours=5, minutes=30), "IST"),
          "UTC": (timedelta(0), "UTC")}


def zone(name: str | None = None) -> tzinfo:
    name = (name or os.environ.get("AITRADER_TZ") or DEFAULT_TZ).strip()
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        off, label = _FIXED.get(name, _FIXED["UTC"])
        return timezone(off, label)


def label(at: float | None = None, tz: tzinfo | None = None) -> str:
    """The zone's short name at ``at`` (IST, UTC, CET, ...)."""
    d = datetime.fromtimestamp(at or 0, tz or zone())
    name = d.tzname() or "UTC"
    return "IST" if name in ("+0530", "UTC+05:30") else name


def local(ts: float, fmt: str = "%Y-%m-%d %H:%M", tz: tzinfo | None = None, suffix: bool = True) -> str:
    """``ts`` (seconds) in the dashboard's zone, followed by the zone's name."""
    tz = tz or zone()
    text = datetime.fromtimestamp(ts, tz).strftime(fmt)
    return f"{text} {label(ts, tz)}" if suffix else text


def offset_minutes(at: float, tz: tzinfo | None = None) -> int:
    """Minutes ahead of UTC at ``at``, for the page scripts' chart labels."""
    off = datetime.fromtimestamp(at, tz or zone()).utcoffset() or timedelta(0)
    return int(off.total_seconds() // 60)
