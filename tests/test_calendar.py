from datetime import datetime, timedelta, timezone

from aitrader.events import CalendarFilter, parse_calendar

FEED = [
    {"title": "CPI m/m", "country": "USD", "date": "2026-10-14T08:30:00-04:00", "impact": "High",
     "forecast": "0.3%", "previous": "0.4%"},
    {"title": "Retail Sales", "country": "USD", "date": "2026-10-15T08:30:00-04:00", "impact": "Medium"},
    {"title": "Bad", "country": "USD", "date": "not a date", "impact": "High"},
    {"title": "Naive", "country": "USD", "date": "2026-10-16T08:30:00", "impact": "High"},
]
CPI_UTC = datetime(2026, 10, 14, 12, 30, tzinfo=timezone.utc)


def test_parse_converts_new_york_to_utc_and_skips_bad_rows():
    evs = parse_calendar(FEED)
    assert [e.title for e in evs] == ["CPI m/m", "Retail Sales"]
    assert evs[0].time == CPI_UTC


def make_filter(now):
    cal = CalendarFilter(fetcher=lambda url: parse_calendar(FEED) if "thisweek" in url else
                         (_ for _ in ()).throw(IOError("404")))
    assert cal.refresh(now=now)
    return cal


def test_blocks_before_and_after_high_impact():
    cal = make_filter(CPI_UTC.timestamp() - 3600)
    before = cal.decide(CPI_UTC - timedelta(minutes=10))
    assert not before.allow_entries and before.tighten_stops and "CPI" in before.reason
    after = cal.decide(CPI_UTC + timedelta(minutes=20))
    assert not after.allow_entries and not after.tighten_stops
    clear = cal.decide(CPI_UTC + timedelta(minutes=31))
    assert clear.allow_entries and clear.size_multiplier == 1.0
    early = cal.decide(CPI_UTC - timedelta(minutes=31))
    assert early.allow_entries


def test_medium_impact_does_not_block():
    cal = make_filter(CPI_UTC.timestamp())
    t = datetime(2026, 10, 15, 12, 25, tzinfo=timezone.utc)
    assert cal.decide(t).allow_entries


def test_fails_safe_when_feed_unavailable():
    def broken(url):
        raise IOError("down")

    cal = CalendarFilter(fetcher=broken)
    assert not cal.refresh()
    d = cal.decide(datetime.now(timezone.utc))
    assert d.allow_entries and d.size_multiplier == 0.5 and "unavailable" in d.reason


def test_stale_feed_counts_as_failed():
    cal = make_filter(CPI_UTC.timestamp() - 86_400 * 2)
    d = cal.decide(CPI_UTC + timedelta(hours=5))
    assert d.size_multiplier == 0.5
