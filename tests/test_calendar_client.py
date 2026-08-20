from datetime import datetime

from app.calendar_client import CalendarClient, _serialize_reminders, _to_event
from app.models import CalendarEvent, ParsedEdit, ReminderSpec


def test_google_event_is_converted():
    event = _to_event({"id": "abc", "summary": "Planning", "start": {"dateTime": "2026-08-16T10:00:00+08:00"}, "end": {"dateTime": "2026-08-16T11:00:00+08:00"}})
    assert event.event_id == "abc"
    assert event.title == "Planning"
    assert event.start == datetime.fromisoformat("2026-08-16T10:00:00+08:00")


def test_all_day_event_uses_calendar_timezone():
    event = _to_event({"id": "abc", "summary": "Holiday", "start": {"date": "2026-08-16"}, "end": {"date": "2026-08-17"}}, "Asia/Singapore")
    assert str(event.end.tzinfo) == "Asia/Singapore"
    assert event.all_day is True


def test_all_day_event_uses_google_date_fields_with_exclusive_end():
    client = CalendarClient.__new__(CalendarClient)
    client._timezone = "Asia/Singapore"

    body = client._event_time_body(
        datetime.fromisoformat("2026-08-19T00:00:00+08:00"),
        datetime.fromisoformat("2026-08-20T00:00:00+08:00"),
        all_day=True,
    )

    assert body == {"start": {"date": "2026-08-19"}, "end": {"date": "2026-08-20"}}
    assert "dateTime" not in body["start"]


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


def test_calendar_event_reads_multiple_private_telegram_reminders():
    reminders = [ReminderSpec(reminder_id="first", minutes_before=60, message="Bring ID"), ReminderSpec(reminder_id="second", minutes_before=15)]
    event = _to_event({
        "id": "abc", "summary": "Dentist",
        "start": {"dateTime": "2026-08-18T10:00:00+08:00"},
        "end": {"dateTime": "2026-08-18T11:00:00+08:00"},
        "extendedProperties": {"private": {"telegram_reminders": _serialize_reminders(reminders)}},
    })
    assert [reminder.minutes_before for reminder in event.reminders] == [60, 15]
    assert event.reminders[0].message == "Bring ID"


def test_list_calendars_reads_names_colours_and_default_calendar():
    class FakeCalendarList:
        def list(self, **kwargs):
            return self

        def execute(self):
            return {"items": [
                {"id": "primary-id", "summary": "My calendar", "backgroundColor": "#4285f4", "primary": True, "accessRole": "owner"},
                {"id": "work-id", "summary": "Work", "backgroundColor": "#a4bdfc", "accessRole": "writer"},
            ]}

    class FakeService:
        def calendarList(self):
            return FakeCalendarList()

    client = CalendarClient.__new__(CalendarClient)
    client._service = FakeService()
    calendars = client.list_calendars()

    assert [(calendar.name, calendar.background_color) for calendar in calendars] == [("My calendar", "#4285f4"), ("Work", "#a4bdfc")]
    assert client.resolve_calendar("work").calendar_id == "work-id"
    assert client.resolve_calendar("Work calendar").calendar_id == "work-id"


def test_standalone_reminder_uses_a_hidden_secondary_calendar():
    calls = {"calendar_body": None, "calendar_patch": None, "event_calendar_id": None}

    class Request:
        def __init__(self, result):
            self.result = result

        def execute(self):
            return self.result

    class CalendarList:
        def list(self, **kwargs):
            return Request({"items": []})

        def patch(self, **kwargs):
            calls["calendar_patch"] = kwargs
            return Request({})

    class Calendars:
        def insert(self, **kwargs):
            calls["calendar_body"] = kwargs["body"]
            return Request({"id": "reminder-calendar"})

    class Events:
        def insert(self, **kwargs):
            calls["event_calendar_id"] = kwargs["calendarId"]
            body = kwargs["body"]
            return Request({"id": "reminder-event", **body})

    class FakeService:
        def calendarList(self):
            return CalendarList()

        def calendars(self):
            return Calendars()

        def events(self):
            return Events()

    client = CalendarClient.__new__(CalendarClient)
    client._service = FakeService()
    client._calendar_id = "primary"
    client._timezone = "Asia/Singapore"
    client._reminder_calendar_id = None

    client.create_standalone_reminder("Shower", datetime.fromisoformat("2026-08-20T17:00:00+08:00"))

    assert calls["calendar_body"]["summary"] == "Telegram Reminders"
    assert calls["calendar_patch"]["body"] == {"selected": False, "hidden": True}
    assert calls["event_calendar_id"] == "reminder-calendar"


def test_delivered_standalone_reminder_is_deleted_instead_of_marked_sent():
    calls = {"deleted": None}

    class Request:
        def __init__(self, result=None):
            self.result = result or {}

        def execute(self):
            return self.result

    class Events:
        def get(self, **kwargs):
            return Request({"extendedProperties": {"private": {"telegram_reminder_type": "standalone"}}})

        def delete(self, **kwargs):
            calls["deleted"] = kwargs
            return Request()

    class FakeService:
        def events(self):
            return Events()

    client = CalendarClient.__new__(CalendarClient)
    client._service = FakeService()
    client._calendar_id = "primary"

    client.mark_reminder_sent("reminder-event", "reminder-id", "reminder-calendar")

    assert calls["deleted"] == {"calendarId": "reminder-calendar", "eventId": "reminder-event"}
