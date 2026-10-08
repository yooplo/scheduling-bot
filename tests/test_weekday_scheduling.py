from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import app.main as main
from app.models import CalendarInfo, CalendarEvent, ParsedEvent


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict_day,blocked", [(12, False), (14, True)])
async def test_monday_request_checks_monday_despite_wrong_parser_date(monkeypatch, conflict_day, blocked):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 9, 9, 16, 36, tzinfo=ZoneInfo("Asia/Singapore")).astimezone(tz)

    monkeypatch.setattr(main, "datetime", Clock)
    settings = SimpleNamespace(timezone=ZoneInfo("Asia/Singapore"), user_timezone="Asia/Singapore")
    messages, created = [], []

    class Telegram:
        async def send_message(self, chat_id, text, reply_markup=None):
            messages.append(text)

    class Parser:
        def parse_event(self, *args):
            return ParsedEvent(title="Lunch with Seung Yoon", start="2026-09-12T12:00:00+08:00",
                               end="2026-09-12T13:00:00+08:00", location="NUS", confidence="high")

    class Calendar:
        def _list_events_between(self, interval_start, interval_end):
            start = datetime(2026, 9, conflict_day, tzinfo=settings.timezone)
            return [CalendarEvent(event_id="busy", title="All day event", start=start,
                                  end=start + timedelta(days=1), all_day=True)]

        def resolve_calendar(self, name):
            return CalendarInfo(calendar_id="default", name="Personal", access_role="owner")

        def create_event(self, event, calendar_id):
            created.append(event)
            return CalendarEvent(event_id="lunch", **event.model_dump(exclude={"action"}))

    await main.handle_message(91234, "add Lunch with Seung Yoon at 12pm on next monday at NUS",
                              settings, Telegram(), Calendar(), Parser())
    assert ("This overlaps with" in messages[-1]) is blocked
    if not blocked:
        assert created[0].start.isoformat() == "2026-09-14T12:00:00+08:00"
        assert created[0].end.isoformat() == "2026-09-14T13:00:00+08:00"


def test_next_monday_on_monday_moves_to_next_week_preserving_overnight_duration():
    event = ParsedEvent(title="Work", start="2026-09-12T15:00:00Z",
                        end="2026-09-12T18:00:00Z", confidence="high")
    now = datetime(2026, 9, 14, 10, tzinfo=ZoneInfo("Asia/Singapore"))
    main._apply_weekday_from_text(event, "Work next Monday at 11pm", now)
    assert event.start.isoformat() == "2026-09-21T23:00:00+08:00"
    assert event.end.isoformat() == "2026-09-22T02:00:00+08:00"


@pytest.mark.parametrize("text", [
    "Work Monday 21 September at 12pm", "Work Monday to Tuesday", "Work Monday after next",
    "Work Monday 21/9 8-10pm", "Work Monday 21-9 8-10pm",
    "Work Monday 2026-09-21 8-10pm",
])
def test_qualified_dates_and_ranges_are_preserved(text):
    event = ParsedEvent(title="Work", start="2026-09-21T12:00:00+08:00",
                        end="2026-09-21T13:00:00+08:00", confidence="high")
    original = event.model_copy()
    main._apply_weekday_from_text(event, text, datetime(2026, 9, 9, tzinfo=ZoneInfo("Asia/Singapore")))
    assert event == original


@pytest.mark.parametrize("clock_range", ["8-10pm", "8-10 pm", "8:00-10:00pm", "8.00-10.00 PM", "8pm-10pm", "8–10pm"])
def test_compact_clock_ranges_keep_weekday_correction(clock_range):
    event = ParsedEvent(title="Drills", start="2026-10-10T20:00:00+08:00",
                        end="2026-10-10T22:00:00+08:00", confidence="high")
    now = datetime(2026, 10, 8, 17, 4, tzinfo=ZoneInfo("Asia/Singapore"))
    main._apply_weekday_from_text(event, f"Add Drills at TSA@JK on Monday {clock_range}", now)
    assert event.start.isoformat() == "2026-10-12T20:00:00+08:00"
    assert event.end.isoformat() == "2026-10-12T22:00:00+08:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict_day,blocked", [(10, False), (12, True)])
async def test_drills_compact_range_checks_monday_before_creation(monkeypatch, conflict_day, blocked):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 8, 17, 4, tzinfo=ZoneInfo("Asia/Singapore")).astimezone(tz)

    monkeypatch.setattr(main, "datetime", Clock)
    settings = SimpleNamespace(timezone=ZoneInfo("Asia/Singapore"), user_timezone="Asia/Singapore")
    monkeypatch.setattr(main, "pending_event_conflicts", {})
    messages, created = [], []

    class Telegram:
        async def send_message(self, chat_id, text, reply_markup=None):
            messages.append(text)

    class Parser:
        def parse_event(self, *args):
            return ParsedEvent(title="Drills", location="TSA@JK", confidence="high",
                               start="2026-10-10T20:00:00+08:00", end="2026-10-10T22:00:00+08:00")

    class Calendar:
        def _list_events_between(self, interval_start, interval_end):
            start = datetime(2026, 10, conflict_day, 20, tzinfo=settings.timezone)
            return [CalendarEvent(event_id="busy", title="Busy", start=start, end=start + timedelta(hours=1))]

        def resolve_calendar(self, name):
            return CalendarInfo(calendar_id="default", name="Personal", access_role="owner")

        def create_event(self, event, calendar_id):
            created.append(event)
            return CalendarEvent(event_id="drills", **event.model_dump(exclude={"action"}))

    await main.handle_message(91234, "Add Drills at TSA@JK on Monday 8-10pm",
                              settings, Telegram(), Calendar(), Parser())
    assert ("This overlaps with" in messages[-1]) is blocked
    if blocked:
        assert not created
        event = main.pending_event_conflicts[91234].event
    else:
        event = created[0]
        assert "Mon 12 Oct" in messages[-1]
    assert event.start.isoformat() == "2026-10-12T20:00:00+08:00"
    assert event.end.isoformat() == "2026-10-12T22:00:00+08:00"
    assert event.location == "TSA@JK"
