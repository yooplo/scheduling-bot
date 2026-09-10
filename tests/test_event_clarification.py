from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

import app.main as main
from app.models import CalendarEvent, CalendarInfo, ParsedEventDraft


@pytest.fixture
def flow():
    main.pending_event_drafts.clear()
    settings = SimpleNamespace(timezone=ZoneInfo("Asia/Singapore"), user_timezone="Asia/Singapore")
    telegram, calendar, parser = AsyncMock(), Mock(), Mock()
    calendar.list_events.return_value = []
    calendar.resolve_calendar.return_value = None
    calendar.create_event.side_effect = lambda event, _: CalendarEvent(event_id="new", **event.model_dump(exclude={"action"}))

    async def send(text, chat_id=123):
        await main.handle_message(chat_id, text, settings, telegram, calendar, parser)
        return telegram.send_message.call_args.args[1]

    yield send, telegram, calendar, parser
    main.pending_event_drafts.clear()


def draft(**changes):
    return ParsedEventDraft(**dict(
        dict(title="Dentist", start="2030-09-13T14:00:00+08:00",
             end="2030-09-13T15:00:00+08:00", confidence="high"), **changes,
    ))


@pytest.mark.asyncio
async def test_missing_time_then_duration_preserves_request_and_reference_time(flow):
    send, _, calendar, parser = flow
    parser.parse_event.side_effect = [
        draft(start=None, end=None, confidence="low", missing_fields=["time", "duration"]),
        draft(end=None, confidence="low", missing_fields=["duration"]),
        draft(location="Clinic", calendar_name="Work", reminder_minutes=15),
    ]
    original = "Dentist tomorrow at Clinic in Work calendar, remind me 15 minutes before"
    calendar.resolve_calendar.return_value = CalendarInfo(calendar_id="work", name="Work", access_role="writer")
    assert (await send(original)).startswith("What time?")
    reference = main.pending_event_drafts[123].now
    assert (await send("2pm")).startswith("How long?")
    calendar.create_event.assert_not_called()
    assert (await send("1 hour")).startswith("✅ Added:")
    parsed_text, parsed_now, _ = parser.parse_event.call_args.args
    assert original in parsed_text and "User follow-up: 2pm" in parsed_text
    assert "User follow-up: 1 hour" in parsed_text
    assert parsed_now == reference
    event = calendar.create_event.call_args.args[0]
    assert event.title == "Dentist" and event.location == "Clinic"
    assert event.calendar_name == "Work" and event.reminders[0].minutes_before == 15
    assert 123 not in main.pending_event_drafts


@pytest.mark.asyncio
async def test_missing_date_can_be_answered_with_multiple_details(flow):
    send, _, calendar, parser = flow
    parser.parse_event.side_effect = [
        draft(start=None, end=None, confidence="low", missing_fields=["date", "time", "duration"]),
        draft(),
    ]
    assert (await send("Dentist")).startswith("Which date?")
    assert (await send("13 September 2030, 2–3pm")).startswith("✅ Added:")
    calendar.create_event.assert_called_once()


@pytest.mark.asyncio
async def test_explicit_date_prompt_can_be_completed_as_all_day(flow):
    send, _, calendar, parser = flow
    assert (await send("add Dentist on 13 Sept 2030")).startswith("What time?")
    parser.parse_event.assert_not_called()
    parser.parse_event.return_value = draft(
        start="2030-09-13T00:00:00+08:00", end="2030-09-14T00:00:00+08:00", all_day=True,
    )
    assert "All day" in await send("all day")
    assert calendar.create_event.call_args.args[0].all_day


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["cancel", "/cancel", "/now"])
async def test_cancel_and_commands_leave_draft_without_creating(flow, command):
    send, _, calendar, parser = flow
    parser.parse_event.return_value = draft(end=None, confidence="low", missing_fields=["duration"])
    await send("Dentist tomorrow 2pm")
    reply = await send(command)
    assert 123 not in main.pending_event_drafts
    calendar.create_event.assert_not_called()
    assert "cancelled" in reply if command != "/now" else "Asia/Singapore" in reply


@pytest.mark.asyncio
async def test_expired_answer_does_not_create_and_other_chats_are_isolated(flow):
    send, _, calendar, parser = flow
    parser.parse_event.return_value = draft(end=None, confidence="low", missing_fields=["duration"])
    await send("Dentist tomorrow 2pm")
    await send("Lunch tomorrow 1pm", chat_id=456)
    main.pending_event_drafts[123].expires_at = 0
    assert "expired" in await send("1 hour")
    assert 123 not in main.pending_event_drafts and 456 in main.pending_event_drafts
    calendar.create_event.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_answer_reprompts_and_complete_event_still_checks_conflicts(flow):
    send, _, calendar, parser = flow
    incomplete = draft(end=None, confidence="low", missing_fields=["duration"])
    parser.parse_event.side_effect = [incomplete, incomplete, draft()]
    await send("Dentist tomorrow 2pm")
    assert (await send("not sure")).startswith("How long?")
    calendar.create_event.assert_not_called()
    calendar.list_events.return_value = [CalendarEvent(
        event_id="busy", title="Busy", start=datetime.fromisoformat("2030-09-13T14:00:00+08:00"),
        end=datetime.fromisoformat("2030-09-13T16:00:00+08:00"),
    )]
    assert "overlaps" in await send("1 hour")
    calendar.create_event.assert_not_called()


@pytest.mark.asyncio
async def test_new_explicit_add_replaces_draft(flow):
    send, _, calendar, parser = flow
    parser.parse_event.side_effect = [draft(end=None, confidence="low", missing_fields=["duration"]), draft(title="Lunch")]
    await send("Dentist tomorrow 2pm")
    await send("add Lunch on 13 Sept 2030 2–3pm")
    assert "Dentist" not in parser.parse_event.call_args.args[0]
    assert calendar.create_event.call_args.args[0].title == "Lunch"


@pytest.mark.asyncio
async def test_natural_time_answer_is_parsed_without_repeating_initial_time_prompt(flow):
    send, _, calendar, parser = flow
    await send("add Dentist on 13 Sept 2030")
    parser.parse_event.return_value = draft(start="2030-09-13T12:00:00+08:00", end="2030-09-13T13:00:00+08:00")
    assert (await send("noon for an hour")).startswith("✅ Added:")
    calendar.create_event.assert_called_once()
