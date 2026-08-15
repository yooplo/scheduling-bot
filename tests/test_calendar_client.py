from datetime import datetime

from app.calendar_client import _to_event


def test_google_event_is_converted():
    event = _to_event({"id": "abc", "summary": "Planning", "start": {"dateTime": "2026-08-16T10:00:00+08:00"}, "end": {"dateTime": "2026-08-16T11:00:00+08:00"}})
    assert event.event_id == "abc"
    assert event.title == "Planning"
    assert event.start == datetime.fromisoformat("2026-08-16T10:00:00+08:00")


def test_all_day_event_uses_calendar_timezone():
    event = _to_event({"id": "abc", "summary": "Holiday", "start": {"date": "2026-08-16"}, "end": {"date": "2026-08-17"}}, "Asia/Singapore")
    assert str(event.end.tzinfo) == "Asia/Singapore"
