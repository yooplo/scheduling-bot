from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

import app.main as main
from app.models import CalendarInfo, CalendarEvent, ParsedEdit, ParsedEventDraft


@pytest.fixture
def flow(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 8, 17, 4, tzinfo=ZoneInfo("Asia/Singapore")).astimezone(tz)

    monkeypatch.setattr(main, "datetime", Clock)
    monkeypatch.setattr(main, "pending_event_drafts", {})
    monkeypatch.setattr(main, "pending_event_conflicts", {})
    settings = SimpleNamespace(timezone=ZoneInfo("Asia/Singapore"), user_timezone="Asia/Singapore")
    telegram, calendar, parser = AsyncMock(), Mock(), Mock()
    calendar._list_events_between.return_value = []
    calendar.resolve_calendar.return_value = CalendarInfo(calendar_id="default", name="Personal", access_role="owner")
    calendar.create_event.side_effect = lambda event, _: CalendarEvent(event_id="new", **event.model_dump(exclude={"action"}))
    calendar.update_event.side_effect = lambda existing, edited: CalendarEvent(event_id=existing.event_id, **edited.model_dump(exclude={"action"}))
    parser.parse_event.side_effect = lambda *args: ParsedEventDraft(
        title="Drills", location="TSA@JK", start="2026-10-10T14:00:00+08:00",
        end="2026-10-10T15:00:00+08:00", confidence="high",
    )
    parser.parse_edit.side_effect = lambda *args: ParsedEdit(
        title="Drills", location="TSA@JK", start="2026-10-10T14:00:00+08:00",
        end="2026-10-10T15:00:00+08:00", confidence="high",
    )
    return settings, telegram, calendar, parser


@pytest.mark.asyncio
@pytest.mark.parametrize("clock", ["8-10pm", "8–10pm", "8pm-10pm", "8.00-10.00 PM", "20:00–22:00", "20.00-22.00"])
async def test_add_corrects_abbreviated_weekday_and_clocks_before_conflicts(flow, clock):
    settings, telegram, calendar, parser = flow
    await main.handle_message(123, f"Add Drills at TSA@JK on Mon {clock}", settings, telegram, calendar, parser)
    event = calendar.create_event.call_args.args[0]
    assert event.start.isoformat() == "2026-10-12T20:00:00+08:00"
    assert event.end.isoformat() == "2026-10-12T22:00:00+08:00"
    assert event.title == "Drills" and event.location == "TSA@JK"
    calendar._list_events_between.assert_called_once_with(event.start, event.end)
    calendar.list_events.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("weekday,day", [("Mon", 12), ("TUE", 13), ("Wed", 14), ("Thu", 8), ("Fri", 9), ("Sat", 10), ("Sun", 11)])
async def test_each_weekday_abbreviation_includes_today(flow, weekday, day):
    settings, telegram, calendar, parser = flow
    await main.handle_message(123, f"Add Drills {weekday} 20:00-22:00", settings, telegram, calendar, parser)
    assert calendar.create_event.call_args.args[0].start.day == day


@pytest.mark.asyncio
@pytest.mark.parametrize("clock", ["11pm-1am", "23:00–01:00"])
async def test_explicit_overnight_range_ends_next_local_day(flow, clock):
    settings, telegram, calendar, parser = flow
    await main.handle_message(123, f"Add Drills Mon {clock}", settings, telegram, calendar, parser)
    event = calendar.create_event.call_args.args[0]
    assert event.start.isoformat() == "2026-10-12T23:00:00+08:00"
    assert event.end.isoformat() == "2026-10-13T01:00:00+08:00"


@pytest.mark.asyncio
async def test_known_weekday_and_range_complete_missing_parser_dates_and_times(flow):
    settings, telegram, calendar, parser = flow
    parser.parse_event.side_effect = None
    parser.parse_event.return_value = ParsedEventDraft(title="Drills", confidence="low", missing_fields=["date", "time", "duration"])
    await main.handle_message(123, "Add Drills Mon 20:00-22:00", settings, telegram, calendar, parser)
    assert calendar.create_event.call_args.args[0].start.isoformat() == "2026-10-12T20:00:00+08:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("date", ["10 October 2026", "10th Oct 2026", "10/10/2026", "10-10-2026", "2026-10-10", "10 Oct"])
@pytest.mark.parametrize("answer", ["Mon", "12 October 2026", "Mon 20:00-22:00"])
async def test_date_weekday_disagreement_waits_and_later_answer_wins(flow, date, answer):
    settings, telegram, calendar, parser = flow
    original = f"Add Drills at TSA@JK on Mon {date} 8-10pm"
    await main.handle_message(123, original, settings, telegram, calendar, parser)
    assert "don't match" in telegram.send_message.call_args.args[1]
    assert "Saturday" in telegram.send_message.call_args.args[1]
    assert main.pending_event_drafts[123].request_text == original
    parser.parse_event.assert_not_called()
    calendar._list_events_between.assert_not_called()
    calendar.create_event.assert_not_called()
    await main.handle_message(123, answer, settings, telegram, calendar, parser)
    event = calendar.create_event.call_args.args[0]
    assert event.start.isoformat() == "2026-10-12T20:00:00+08:00"
    assert event.end.isoformat() == "2026-10-12T22:00:00+08:00"
    assert original in parser.parse_event.call_args.args[0]
    assert 123 not in main.pending_event_drafts


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "expiry", "replacement"])
async def test_date_clarification_obeys_existing_draft_lifecycle(flow, action):
    settings, telegram, calendar, parser = flow
    await main.handle_message(123, "Add Drills Mon 10 October 2026 8-10pm", settings, telegram, calendar, parser)
    if action == "expiry":
        main.pending_event_drafts[123].expires_at = 0
        await main.handle_message(123, "Mon", settings, telegram, calendar, parser)
        assert "expired" in telegram.send_message.call_args.args[1]
    elif action == "replacement":
        await main.handle_message(123, "add New event Tue 20:00-22:00", settings, telegram, calendar, parser)
        assert "10 October" not in parser.parse_event.call_args.args[0]
        assert calendar.create_event.call_args.args[0].start.day == 13
    else:
        await main.handle_message(123, "/cancel", settings, telegram, calendar, parser)
        assert "cancelled" in telegram.send_message.call_args.args[1]
    if action != "replacement":
        calendar.create_event.assert_not_called()
    assert not main.pending_event_drafts


@pytest.mark.asyncio
@pytest.mark.parametrize("clock", ["25:00-26:00", "20:60-22:00", "13pm-2pm", "11-1am", "8pm-8pm", "8-10pm and 11pm-1am"])
async def test_invalid_or_ambiguous_ranges_wait_without_calendar_operations(flow, clock):
    settings, telegram, calendar, parser = flow
    await main.handle_message(123, f"Add Drills Mon {clock}", settings, telegram, calendar, parser)
    parser.parse_event.assert_not_called()
    assert not calendar.mock_calls
    assert 123 in main.pending_event_drafts
    await main.handle_message(123, "11pm-1am", settings, telegram, calendar, parser)
    assert calendar.create_event.call_args.args[0].end.isoformat() == "2026-10-13T01:00:00+08:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("destination,expected", [("Tue 20:00–22:00", "2026-10-13"), ("20:00–22:00", "2026-10-19")])
async def test_edits_correct_destination_clocks_and_preserve_date_without_new_date(flow, destination, expected):
    settings, telegram, calendar, parser = flow
    existing = CalendarEvent(event_id="drills", title="Drills", start="2026-10-19T23:00:00+08:00", end="2026-10-20T01:00:00+08:00")
    await main._edit_event(123, f"move Monday Drills to {destination}", existing, settings, telegram, calendar, parser)
    edited = calendar.update_event.call_args.args[1]
    assert edited.start.isoformat() == f"{expected}T20:00:00+08:00"
    assert edited.end.isoformat() == f"{expected}T22:00:00+08:00"
    calendar._list_events_between.assert_called_once_with(edited.start, edited.end)


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", ["Mon 10 October 2026 8-10pm", "Mon 20:70–22:00"])
async def test_invalid_edit_destination_asks_for_revised_request_without_write(flow, destination):
    settings, telegram, calendar, parser = flow
    existing = CalendarEvent(event_id="drills", title="Drills", start="2026-10-19T20:00:00+08:00", end="2026-10-19T22:00:00+08:00")
    await main._edit_event(123, f"move Drills to {destination}", existing, settings, telegram, calendar, parser)
    assert "resend the edit" in telegram.send_message.call_args.args[1]
    parser.parse_edit.assert_not_called()
    assert not calendar.mock_calls
    assert not main.pending_event_drafts and not main.pending_event_conflicts


@pytest.mark.asyncio
@pytest.mark.parametrize("all_day,overnight", [(False, False), (True, False), (False, True)])
async def test_future_event_conflicts_use_exact_interval_and_saved_confirmation(flow, all_day, overnight):
    settings, telegram, calendar, parser = flow
    start = datetime(2027, 3, 15, 0 if all_day else 23 if overnight else 20, tzinfo=settings.timezone)
    end = start + (timedelta(days=1) if all_day else timedelta(hours=2))
    parser.parse_event.side_effect = None
    parser.parse_event.return_value = ParsedEventDraft(title="Drills", start=start, end=end, all_day=all_day, confidence="high")
    calendar._list_events_between.return_value = [CalendarEvent(event_id="busy", title="Busy", start=start, end=end)]
    await main.handle_message(123, "Add Drills 15 March 2027 " + ("all day" if all_day else "23:00-01:00" if overnight else "20:00-22:00"), settings, telegram, calendar, parser)
    calendar._list_events_between.assert_called_once_with(start, end)
    calendar.list_events.assert_not_called()
    calendar.create_event.assert_not_called()
    assert "overlaps" in telegram.send_message.call_args.args[1]
    token = main.pending_event_conflicts[123].token
    await main.handle_conflict_callback(123, "cb", f"conflict:{token}:add", settings, telegram, calendar)
    calendar.create_event.assert_called_once()


@pytest.mark.asyncio
async def test_failed_interval_query_prevents_creation(flow):
    settings, telegram, calendar, parser = flow
    calendar._list_events_between.side_effect = RuntimeError("calendar unavailable")
    with pytest.raises(RuntimeError, match="calendar unavailable"):
        await main.handle_message(123, "Add Drills Mon 20:00-22:00", settings, telegram, calendar, parser)
    calendar.create_event.assert_not_called()
    assert not main.pending_event_conflicts


@pytest.mark.asyncio
async def test_matching_weekday_and_date_do_not_ask_for_clarification(flow):
    settings, telegram, calendar, parser = flow
    await main.handle_message(123, "Add Drills Mon 12/10/2026 20:00-22:00", settings, telegram, calendar, parser)
    assert calendar.create_event.call_args.args[0].start.isoformat() == "2026-10-12T20:00:00+08:00"
    assert not main.pending_event_drafts


@pytest.mark.asyncio
@pytest.mark.parametrize("date", ["31 February 2026", "31/02/2026", "2026-02-31"])
async def test_invalid_explicit_dates_ask_without_calendar_writes(flow, date):
    settings, telegram, calendar, parser = flow
    await main.handle_message(123, f"Add Drills Mon {date} 20:00-22:00", settings, telegram, calendar, parser)
    assert "date is invalid" in telegram.send_message.call_args.args[1]
    assert not calendar.mock_calls
    parser.parse_event.assert_not_called()


@pytest.mark.asyncio
async def test_date_answer_uses_original_reference_year_after_year_rollover(flow, monkeypatch):
    settings, telegram, calendar, parser = flow
    await main.handle_message(123, "Add Drills Tue 5 January 8-10pm", settings, telegram, calendar, parser)

    class NewYearClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2027, 1, 1, tzinfo=settings.timezone).astimezone(tz)

    monkeypatch.setattr(main, "datetime", NewYearClock)
    await main.handle_message(123, "5 January", settings, telegram, calendar, parser)
    assert calendar.create_event.call_args.args[0].start.isoformat() == "2026-01-05T20:00:00+08:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["4pm", "two hours"])
async def test_later_clock_or_duration_does_not_restore_original_range(flow, answer):
    settings, telegram, calendar, parser = flow
    await main.handle_message(123, "Add Drills Mon 10 October 2026 8-10pm", settings, telegram, calendar, parser)
    parser.parse_event.side_effect = None
    parser.parse_event.return_value = ParsedEventDraft(title="Drills", start="2026-10-12T16:00:00+08:00",
                                                       end="2026-10-12T18:00:00+08:00", confidence="high")
    await main.handle_message(123, "Mon " + answer, settings, telegram, calendar, parser)
    event = calendar.create_event.call_args.args[0]
    assert event.start.hour == 16 and event.end.hour == 18


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["Add Drills every Mon 8-10pm", "add Mon whole day with Drills"])
async def test_abbreviations_work_for_recurrence_and_concise_all_day_requests(flow, message):
    settings, telegram, calendar, parser = flow
    await main.handle_message(123, message, settings, telegram, calendar, parser)
    event = calendar.create_event.call_args.args[0]
    assert event.start.date().isoformat() == "2026-10-12"
    if "every" in message:
        assert event.recurrence == "RRULE:FREQ=WEEKLY;BYDAY=MO"
    else:
        assert event.all_day and event.start.hour == 0
        assert event.end.date().isoformat() == "2026-10-13"
        parser.parse_event.assert_not_called()


@pytest.mark.asyncio
async def test_qualified_weekday_keeps_parser_date_and_corrects_only_clocks(flow):
    settings, telegram, calendar, parser = flow
    parser.parse_event.side_effect = None
    parser.parse_event.return_value = ParsedEventDraft(title="Drills", start="2026-10-19T14:00:00+08:00",
                                                       end="2026-10-19T15:00:00+08:00", confidence="high")
    await main.handle_message(123, "Add Drills Mon after next 20:00-22:00", settings, telegram, calendar, parser)
    assert calendar.create_event.call_args.args[0].start.isoformat() == "2026-10-19T20:00:00+08:00"


@pytest.mark.asyncio
async def test_multiple_weekday_span_retains_parser_date_boundaries(flow):
    settings, telegram, calendar, parser = flow
    parser.parse_event.side_effect = None
    parser.parse_event.return_value = ParsedEventDraft(title="Drills", start="2026-10-12T14:00:00+08:00",
                                                       end="2026-10-13T15:00:00+08:00", confidence="high")
    await main.handle_message(123, "Add Drills Mon to Tue 20:00-22:00", settings, telegram, calendar, parser)
    event = calendar.create_event.call_args.args[0]
    assert event.start.isoformat() == "2026-10-12T20:00:00+08:00"
    assert event.end.isoformat() == "2026-10-13T22:00:00+08:00"


@pytest.mark.asyncio
async def test_back_to_back_future_events_do_not_conflict(flow):
    settings, telegram, calendar, parser = flow
    start = datetime(2027, 3, 15, 20, tzinfo=settings.timezone)
    end = start + timedelta(hours=2)
    calendar._list_events_between.return_value = [
        CalendarEvent(event_id="before", title="Before", start=start - timedelta(hours=1), end=start),
        CalendarEvent(event_id="after", title="After", start=end, end=end + timedelta(hours=1)),
    ]
    await main.handle_message(123, "Add Drills 15 March 2027 20:00-22:00", settings, telegram, calendar, parser)
    calendar._list_events_between.assert_called_once_with(start, end)
    calendar.create_event.assert_called_once()
    assert not main.pending_event_conflicts
