from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

import app.main as main
from app.models import CalendarEvent, CalendarInfo, ParsedEvent


@pytest.fixture
def flow():
    main.pending_event_conflicts.clear()
    main.pending_event_drafts.clear()
    settings = SimpleNamespace(timezone=ZoneInfo("Asia/Singapore"), user_timezone="Asia/Singapore")
    telegram, calendar, parser = AsyncMock(), Mock(), Mock()
    event = ParsedEvent(title="Dentist", start="2030-09-13T14:00:00+08:00", end="2030-09-13T15:00:00+08:00",
                        location="Clinic", calendar_name="Work", reminder_minutes=15, confidence="high")
    parser.parse_event.return_value = event
    calendar.list_events.return_value = [CalendarEvent(event_id="busy", title="Meeting", start=event.start, end=event.end)]
    calendar.resolve_calendar.return_value = CalendarInfo(calendar_id="work", name="Work", access_role="writer")
    calendar.create_event.side_effect = lambda event, _: CalendarEvent(event_id="new", **event.model_dump(exclude={"action"}))

    async def warn():
        await main.handle_message(123, "Dentist on 13 Sept 2030 2–3pm at Clinic in Work calendar", settings, telegram, calendar, parser)
        buttons = telegram.send_message.call_args.args[2]["inline_keyboard"][0]
        return {button["text"]: button["callback_data"] for button in buttons}

    async def click(data):
        await main.handle_conflict_callback(123, "callback", data, settings, telegram, calendar)

    yield warn, click, settings, telegram, calendar, parser
    main.pending_event_conflicts.clear()
    main.pending_event_drafts.clear()


@pytest.mark.asyncio
async def test_add_anyway_creates_exact_saved_event_once(flow):
    warn, click, _, telegram, calendar, parser = flow
    buttons = await warn()
    assert set(buttons) == {"Add anyway", "Change time", "Cancel"}
    saved = main.pending_event_conflicts[123].event.model_copy(deep=True)
    calendar.create_event.assert_not_called()
    await click(buttons["Add anyway"])
    calendar.create_event.assert_called_once_with(saved, "work")
    assert parser.parse_event.call_count == 1
    await click(buttons["Add anyway"])
    calendar.create_event.assert_called_once()
    assert "already used" in telegram.answer_callback_query.call_args.args[1]


@pytest.mark.asyncio
async def test_change_time_preserves_context_and_checks_conflicts_again(flow):
    warn, click, settings, telegram, calendar, parser = flow
    buttons = await warn()
    await click(buttons["Change time"])
    assert "What date or time" in telegram.send_message.call_args.args[1]
    calendar.create_event.assert_not_called()
    parser.parse_event.return_value = parser.parse_event.return_value.model_copy(update={
        "start": parser.parse_event.return_value.start.replace(hour=16),
        "end": parser.parse_event.return_value.end.replace(hour=17),
    })
    await main.handle_message(123, "4pm", settings, telegram, calendar, parser)
    text = parser.parse_event.call_args.args[0]
    assert "Clinic in Work calendar" in text and "duration of 60 minutes" in text
    assert "User follow-up: 4pm" in text
    assert calendar.list_events.call_count == 2
    created = calendar.create_event.call_args.args[0]
    assert created.start.hour == 16 and created.end.hour == 17 and not created.all_day
    assert created.location == "Clinic" and created.reminders[0].minutes_before == 15


@pytest.mark.asyncio
async def test_changed_time_still_conflicting_offers_new_choices(flow):
    warn, click, settings, telegram, calendar, parser = flow
    old = await warn()
    await click(old["Change time"])
    await main.handle_message(123, "2pm", settings, telegram, calendar, parser)
    assert "overlaps" in telegram.send_message.call_args.args[1]
    new_token = main.pending_event_conflicts[123].token
    await click(old["Add anyway"])
    assert main.pending_event_conflicts[123].token == new_token
    calendar.create_event.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["Cancel", "expired", "new command"])
async def test_cancel_expiry_and_new_commands_invalidate_buttons(flow, action):
    warn, click, settings, _, calendar, parser = flow
    buttons = await warn()
    if action == "Cancel":
        await click(buttons["Cancel"])
    elif action == "expired":
        main.pending_event_conflicts[123].expires_at = 0
    else:
        await main.handle_message(123, "/now", settings, flow[3], calendar, parser)
    await click(buttons["Add anyway"])
    assert 123 not in main.pending_event_conflicts
    calendar.create_event.assert_not_called()


@pytest.mark.asyncio
async def test_add_anyway_still_checks_calendar_write_permission(flow):
    warn, click, _, telegram, calendar, _ = flow
    buttons = await warn()
    calendar.resolve_calendar.return_value.access_role = "reader"
    await click(buttons["Add anyway"])
    calendar.create_event.assert_not_called()
    assert "do not have permission" in telegram.send_message.call_args.args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type,sender,chat_id,allowed", [("private",123,123,True), ("private",456,123,False), ("group",123,-100,False)])
async def test_conflict_webhook_only_accepts_owners_private_chat(flow, monkeypatch, chat_type, sender, chat_id, allowed):
    warn, _, settings, telegram, calendar, parser = flow
    buttons = await warn()
    settings.telegram_webhook_secret = "secret"
    monkeypatch.setattr(main, "dependencies", lambda: (settings, telegram, {123: calendar,456:calendar}, parser, None))
    request = AsyncMock()
    request.json.return_value = {"callback_query": {
        "id": "cb", "data": buttons["Add anyway"], "from": {"id": sender},
        "message": {"chat": {"id": chat_id, "type": chat_type}},
    }}
    assert await main.webhook(request, "secret") == {"ok": True}
    assert calendar.create_event.call_count == int(allowed)


@pytest.mark.asyncio
async def test_typed_add_anyway_needs_no_repeated_details(flow):
    warn, _, settings, telegram, calendar, parser = flow
    await warn()
    await main.handle_message(123, "add anyway", settings, telegram, calendar, parser)
    calendar.create_event.assert_called_once()
    assert parser.parse_event.call_count == 1


@pytest.mark.asyncio
async def test_concurrent_taps_consume_choice_once(flow):
    import asyncio

    warn, click, _, _, calendar, _ = flow
    buttons = await warn()
    await asyncio.gather(click(buttons["Add anyway"]), click(buttons["Add anyway"]))
    calendar.create_event.assert_called_once()


@pytest.mark.asyncio
async def test_change_from_all_day_to_timed_uses_latest_answer(flow):
    warn, click, settings, telegram, calendar, parser = flow
    event = parser.parse_event.return_value
    event.all_day = True
    event.start = event.start.replace(hour=0)
    event.end = event.end.replace(day=14, hour=0)
    await main.handle_message(123, "Dentist on 13 Sept 2030 all day", settings, telegram, calendar, parser)
    button = telegram.send_message.call_args.args[2]["inline_keyboard"][0][1]["callback_data"]
    await click(button)
    parser.parse_event.return_value = event.model_copy(update={
        "start": event.start.replace(hour=16), "end": event.start.replace(hour=17), "all_day": False,
    })
    await main.handle_message(123, "make it 4–5pm instead", settings, telegram, calendar, parser)
    created = calendar.create_event.call_args.args[0]
    assert not created.all_day and created.start.hour == 16 and created.end.hour == 17
