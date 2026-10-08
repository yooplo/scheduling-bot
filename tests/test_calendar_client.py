from datetime import datetime

import app.calendar_client as calendar_client_module
import pytest
from app.calendar_client import CalendarClient, _request_builder, _serialize_reminders, _to_event
from app.models import CalendarEvent, CalendarInfo, ParsedEdit, ReminderSpec


def test_google_requests_receive_independent_http_transports(monkeypatch):
    transports = []

    class FakeHttp:
        pass

    class FakeAuthorizedHttp:
        def __init__(self, credentials, http):
            transports.append(http)

    class FakeRequest:
        def __init__(self, http, *args, **kwargs):
            self.http = http

    monkeypatch.setattr(calendar_client_module.httplib2, "Http", FakeHttp)
    monkeypatch.setattr(calendar_client_module.google_auth_httplib2, "AuthorizedHttp", FakeAuthorizedHttp)
    monkeypatch.setattr(calendar_client_module, "HttpRequest", FakeRequest)

    builder = _request_builder(object())
    builder(None)
    builder(None)

    assert len(transports) == 2
    assert transports[0] is not transports[1]


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


def test_create_and_delete_owned_secondary_calendar():
    calls = {}

    class Request:
        def __init__(self, result=None):
            self.result = result or {}

        def execute(self):
            return self.result

    class Calendars:
        def insert(self, **kwargs):
            calls["insert"] = kwargs
            return Request({"id": "school-id", "summary": "School"})

        def delete(self, **kwargs):
            calls["delete"] = kwargs
            return Request()

    class FakeService:
        def calendars(self):
            return Calendars()

    client = CalendarClient.__new__(CalendarClient)
    client._service = FakeService()
    client._timezone = "Asia/Singapore"

    created = client.create_calendar("School")
    client.delete_calendar(created)

    assert calls["insert"] == {"body": {"summary": "School", "timeZone": "Asia/Singapore"}}
    assert calls["delete"] == {"calendarId": "school-id"}


def test_primary_or_unowned_calendar_cannot_be_deleted():
    client = CalendarClient.__new__(CalendarClient)
    with pytest.raises(ValueError, match="primary"):
        client.delete_calendar(CalendarInfo(calendar_id="primary", name="Main", primary=True, access_role="owner"))
    with pytest.raises(PermissionError, match="owned"):
        client.delete_calendar(CalendarInfo(calendar_id="shared", name="Shared", access_role="reader"))


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


def test_remove_reminder_preserves_other_reminders():
    kept = ReminderSpec(reminder_id="keep", minutes_before=60)
    removed = ReminderSpec(reminder_id="remove", minutes_before=15)
    calls = {}

    class Request:
        def __init__(self, result=None):
            self.result = result or {}

        def execute(self):
            return self.result

    class Events:
        def get(self, **kwargs):
            return Request({"extendedProperties": {"private": {"telegram_reminders": _serialize_reminders([kept, removed])}}})

        def patch(self, **kwargs):
            calls.update(kwargs)
            return Request()

    class Service:
        def events(self):
            return Events()

    client = CalendarClient.__new__(CalendarClient)
    client._service = Service()
    client._calendar_id = "primary"
    client.remove_reminder("event", "remove", "calendar")

    private = calls["body"]["extendedProperties"]["private"]
    assert '"reminder_id":"keep"' in private["telegram_reminders"]
    assert '"reminder_id":"remove"' not in private["telegram_reminders"]

@pytest.mark.parametrize('default_id', ['primary', 'account@example.com'])
def test_default_calendar_resolves_to_real_name_and_id(default_id):
    from unittest.mock import Mock
    client = CalendarClient.__new__(CalendarClient)
    client._calendar_id = default_id
    primary = CalendarInfo(calendar_id='account@example.com', name='Personal', primary=True, access_role='owner')
    client.list_calendars = Mock(return_value=[primary])
    assert client.resolve_calendar(None) == primary


@pytest.mark.parametrize('all_day,series,metadata_preserved', [(False, False, False), (True, False, True), (False, True, False)])
def test_native_move_preserves_reminder_state_and_uses_series_master(all_day, series, metadata_preserved):
    from unittest.mock import Mock
    client = CalendarClient.__new__(CalendarClient)
    client._calendar_id, client._timezone = 'work', 'Asia/Singapore'
    destination = CalendarInfo(calendar_id='home', name='Home', access_role='writer')
    client.list_calendars = Mock(return_value=[CalendarInfo(calendar_id='work', name='Work', access_role='owner'), destination])
    reminders = _serialize_reminders([ReminderSpec(minutes_before=15, message='Bring paddle', sent=True)])
    private = {'telegram_reminders': reminders, 'other': 'keep'}
    item = {'id':'moved', 'summary':'Drills', 'location':'TSA@JK',
            'start': {'date':'2030-10-12'} if all_day else {'dateTime':'2030-10-12T20:00:00+08:00'},
            'end': {'date':'2030-10-13'} if all_day else {'dateTime':'2030-10-12T22:00:00+08:00'}}
    source = {**item, 'extendedProperties': {'private': private}}
    events = Mock()
    events.get.return_value.execute.return_value = source
    events.move.return_value.execute.return_value = source if metadata_preserved else item
    events.patch.return_value.execute.return_value = source
    client._service = Mock()
    client._service.events.return_value = events
    event = _to_event(source, client._timezone, 'work')
    if series:
        event = event.model_copy(update={'event_id':'instance', 'recurring_event_id':'master'})
    moved = client.move_event(event, destination)
    events.move.assert_called_once_with(calendarId='work', eventId='master' if series else 'moved', destination='home')
    if metadata_preserved:
        events.patch.assert_not_called()
    else:
        events.patch.assert_called_once_with(calendarId='home', eventId='moved', body={'extendedProperties':{'private':private}})
    assert moved.calendar_id == 'home' and moved.calendar_name == 'Home'
    assert moved.start == event.start and moved.end == event.end and moved.all_day == all_day
    assert moved.location == event.location and moved.reminders[0].sent
    assert moved.recurring_event_id == ('moved' if series else None)
    events.insert.assert_not_called()
    events.delete.assert_not_called()


@pytest.mark.parametrize('source_role,destination_role', [('reader', 'writer'), ('owner', 'reader'), ('owner', None)])
def test_move_rechecks_both_calendar_permissions(source_role, destination_role):
    from unittest.mock import Mock
    client = CalendarClient.__new__(CalendarClient)
    client._calendar_id = 'work'
    destination = CalendarInfo(calendar_id='home', name='Home', access_role='writer')
    client.list_calendars = Mock(return_value=[CalendarInfo(calendar_id='work', name='Work', access_role=source_role),
                                              destination.model_copy(update={'access_role': destination_role})])
    client._service = Mock()
    with pytest.raises(PermissionError):
        client.move_event(CalendarEvent(event_id='event', title='Drills', start='2030-10-12T20:00:00+08:00', calendar_id='work'), destination)
    client._service.events.assert_not_called()


def test_move_reports_known_partial_success_when_metadata_restore_fails():
    from unittest.mock import Mock
    from app.calendar_client import CalendarMoveError
    client = CalendarClient.__new__(CalendarClient)
    client._calendar_id, client._timezone = 'work', 'Asia/Singapore'
    destination = CalendarInfo(calendar_id='home', name='Home', access_role='writer')
    client.list_calendars = Mock(return_value=[CalendarInfo(calendar_id='work', name='Work', access_role='owner'), destination])
    item = {'id':'moved', 'summary':'Drills', 'start':{'dateTime':'2030-10-12T20:00:00+08:00'},
            'end':{'dateTime':'2030-10-12T22:00:00+08:00'}}
    events = Mock()
    events.get.return_value.execute.return_value = {**item, 'extendedProperties':{'private':{'telegram_reminders':'[]'}}}
    events.move.return_value.execute.return_value = item
    events.patch.return_value.execute.side_effect = RuntimeError('unavailable')
    client._service = Mock()
    client._service.events.return_value = events
    with pytest.raises(CalendarMoveError) as error:
        client.move_event(_to_event(item, client._timezone, 'work'), destination)
    assert error.value.event.calendar_id == 'home'
    events.move.assert_called_once()
    events.delete.assert_not_called()
