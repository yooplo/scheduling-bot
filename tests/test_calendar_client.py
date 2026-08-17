from datetime import datetime

from app.calendar_client import CalendarClient, _to_event
from app.models import CalendarEvent, ParsedEdit


def test_google_event_is_converted():
    event = _to_event({"id": "abc", "summary": "Planning", "start": {"dateTime": "2026-08-16T10:00:00+08:00"}, "end": {"dateTime": "2026-08-16T11:00:00+08:00"}})
    assert event.event_id == "abc"
    assert event.title == "Planning"
    assert event.start == datetime.fromisoformat("2026-08-16T10:00:00+08:00")


def test_all_day_event_uses_calendar_timezone():
    event = _to_event({"id": "abc", "summary": "Holiday", "start": {"date": "2026-08-16"}, "end": {"date": "2026-08-17"}}, "Asia/Singapore")
    assert str(event.end.tzinfo) == "Asia/Singapore"


def test_update_series_patches_the_recurring_event_master():
    captured = {}

    class FakeEvents:
        def patch(self, **kwargs):
            captured.update(kwargs)
            return self

        def execute(self):
            return {
                "id": "series-master", "summary": "Gym",
                "start": {"dateTime": "2026-08-18T19:00:00+08:00"},
                "end": {"dateTime": "2026-08-18T20:00:00+08:00"},
            }

    class FakeService:
        def events(self):
            return FakeEvents()

    client = CalendarClient.__new__(CalendarClient)
    client._service = FakeService()
    client._calendar_id = "primary"
    client._timezone = "Asia/Singapore"
    source = CalendarEvent(
        event_id="instance", recurring_event_id="series-master", title="Gym",
        start=datetime.fromisoformat("2026-08-17T20:00:00+08:00"),
        end=datetime.fromisoformat("2026-08-17T21:00:00+08:00"),
    )
    edited = ParsedEdit(
        title="Gym", start=datetime.fromisoformat("2026-08-18T19:00:00+08:00"),
        end=datetime.fromisoformat("2026-08-18T20:00:00+08:00"),
        location="Studio", confidence="high", recurrence="RRULE:FREQ=WEEKLY;BYDAY=TU",
    )

    client.update_series(source, edited)

    assert captured["eventId"] == "series-master"
    assert captured["body"]["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=TU"]
