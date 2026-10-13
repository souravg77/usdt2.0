"""Dashboard times show in Kolkata time (IST) by default; AITRADER_TZ changes the zone."""

from datetime import datetime, timezone

from aitrader import clock
from aitrader.events.calendar import CalendarFilter, EconomicEvent

TS = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc).timestamp()


def test_kolkata_by_default(monkeypatch):
    monkeypatch.delenv("AITRADER_TZ", raising=False)
    assert clock.local(TS) == "2026-10-13 17:30 IST"
    assert clock.offset_minutes(TS) == 330 and clock.label(TS) == "IST"


def test_other_zone_and_fixed_fallback(monkeypatch):
    monkeypatch.setenv("AITRADER_TZ", "UTC")
    assert clock.local(TS, "%H:%M") == "12:00 UTC"
    import zoneinfo

    monkeypatch.setattr(zoneinfo, "ZoneInfo", lambda name: (_ for _ in ()).throw(LookupError(name)))
    assert clock.local(TS, "%H:%M", tz=clock.zone("Asia/Kolkata")) == "17:30 IST"


def test_dashboard_pages_carry_the_zone(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from aitrader.config import Settings
    from aitrader.web.app import Paths, _when, create_app
    from aitrader.web.auth import Auth
    from aitrader.web.jobs import JobRunner

    from test_web import PW, login

    monkeypatch.delenv("AITRADER_TZ", raising=False)
    monkeypatch.chdir(tmp_path)
    assert _when(TS) == "2026-10-13 17:30 IST"
    client = TestClient(create_app(Settings(db_path=str(tmp_path / "x.db")), Auth("admin", PW, "k"),
                                   JobRunner(str(tmp_path / "jobs")), Paths()))
    login(client)
    page = client.get("/").text
    assert 'data-tz-offset="330"' in page and 'data-tz-label="IST"' in page


def test_calendar_reason_in_local_time(monkeypatch):
    monkeypatch.delenv("AITRADER_TZ", raising=False)
    ev = EconomicEvent(time=datetime.fromtimestamp(TS, timezone.utc), title="CPI", country="US", impact="High")
    f = CalendarFilter(events=[ev], loaded_at=TS)
    d = f.decide(datetime.fromtimestamp(TS - 600, timezone.utc))
    assert "17:30 IST" in d.reason
