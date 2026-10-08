import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

import app.main as main
from app.models import CalendarEvent, CalendarInfo, ParsedEdit, ParsedEvent, ReminderSpec


@pytest.fixture
def flow(monkeypatch):
    for name in ("pending_actions", "pending_event_drafts", "pending_event_conflicts", "processed_message_updates"):
        monkeypatch.setattr(main, name, {})
    settings = SimpleNamespace(timezone=ZoneInfo("Asia/Singapore"), user_timezone="Asia/Singapore",
                               telegram_webhook_secret="secret", telegram_group_id=-100)
    telegram, calendar, parser = AsyncMock(), Mock(), Mock()
    calendar.resolve_calendar.return_value = CalendarInfo(calendar_id="work", name="Work", access_role="writer")
    calendar._list_events_between.return_value = []
    calendar.create_event.side_effect = lambda event, calendar_id: CalendarEvent(
        event_id=f"created-{calendar.create_event.call_count}", calendar_id=calendar_id,
        **event.model_dump(exclude={"action", "recurrence", "calendar_name", "confidence"}),
    )
    calendar.update_event.side_effect = lambda existing, edited: CalendarEvent(
        event_id=existing.event_id, calendar_id=existing.calendar_id,
        **edited.model_dump(exclude={"action", "confidence", "recurrence"}),
    )
    calendar.update_series.side_effect = calendar.update_event.side_effect
    parser.parse_edit.side_effect = lambda message, existing, tz: ParsedEdit(
        title=existing.title, location=existing.location, confidence="high", all_day=existing.all_day,
        start=existing.start.replace(hour=16), end=existing.start.replace(hour=18),
    )
    event = ParsedEvent(title="Drills", location="TSA@JK", calendar_name="Work", confidence="high",
                        start="2030-10-12T20:00:00+08:00", end="2030-10-12T22:00:00+08:00",
                        reminders=[ReminderSpec(minutes_before=15, message="Bring paddle")])

    async def create(recurrence=False):
        await main._create_event(123, event.model_copy(update={"recurrence": "RRULE:FREQ=WEEKLY;BYDAY=MO" if recurrence else None}), telegram, calendar)
        return {button["text"]: button["callback_data"]
                for button in telegram.send_message.call_args.args[2]["inline_keyboard"][0]}

    async def click(data):
        await main.handle_created_callback(123, "cb", data, telegram, calendar)

    monkeypatch.setattr(main, "dependencies", lambda: (settings, telegram, {123: calendar, 456: Mock()}, parser, None))
    return create, click, settings, telegram, calendar, parser


@pytest.mark.asyncio
async def test_created_buttons_delete_exact_calendar_event_once(flow):
    create, click, _, telegram, calendar, parser = flow
    buttons = await create()
    assert set(buttons) == {"Change time", "Delete this event"}
    assert all(len(data.encode()) <= 64 for data in buttons.values())
    saved = main.pending_actions[123].events[0]
    await asyncio.gather(click(buttons["Delete this event"]), click(buttons["Delete this event"]))
    calendar.delete_event.assert_called_once_with(saved)
    assert saved.event_id == "created-1" and saved.calendar_id == "work"
    calendar.create_event.assert_called_once()
    parser.match_event.assert_not_called()
    assert 123 not in main.pending_actions


@pytest.mark.asyncio
async def test_change_time_edits_saved_event_without_matching_or_creating(flow):
    create, click, settings, telegram, calendar, parser = flow
    buttons = await create()
    saved = main.pending_actions[123].events[0]
    await click(buttons["Change time"])
    assert "Drills" in telegram.send_message.call_args.args[1]
    assert "within 5 minutes" in telegram.send_message.call_args.args[1]
    await click(buttons["Change time"])
    assert main.pending_actions[123].action == "created_edit"
    await main.handle_message(123, "4pm", settings, telegram, calendar, parser)
    existing, edited = calendar.update_event.call_args.args
    assert existing == saved
    assert edited.start.hour == 16 and edited.end.hour == 18
    assert edited.title == saved.title and edited.location == saved.location
    assert existing.calendar_id == "work"
    assert existing.reminders[0].message == "Bring paddle"
    assert existing.reminders[0].minutes_before == 15
    calendar._list_events_between.assert_called_once_with(edited.start, edited.end)
    calendar.create_event.assert_called_once()
    calendar.list_events.assert_not_called()
    parser.match_event.assert_not_called()
    assert 123 not in main.pending_actions


@pytest.mark.asyncio
async def test_change_time_retains_edit_conflict_for_apply_anyway(flow):
    create, click, settings, telegram, calendar, _ = flow
    buttons = await create()
    await click(buttons["Change time"])
    saved = main.pending_actions[123].events[0]
    calendar._list_events_between.return_value = [CalendarEvent(
        event_id="busy", title="Meeting", start=saved.start.replace(hour=16), end=saved.end.replace(hour=18),
    )]
    await main.handle_message(123, "4pm", settings, telegram, calendar, flow[5])
    calendar.update_event.assert_not_called()
    assert "Apply anyway" in str(telegram.send_message.call_args.args[2])
    pending = main.pending_event_conflicts[123]
    assert pending.existing == saved
    await main.handle_conflict_callback(123, "cb", f"conflict:{pending.token}:add", settings, telegram, calendar)
    calendar.update_event.assert_called_once()
    calendar.create_event.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "expiry", "new command", "new add", "restart"])
async def test_time_change_prompt_lifecycle_leaves_saved_event_unchanged(flow, action):
    create, click, settings, telegram, calendar, parser = flow
    buttons = await create()
    await click(buttons["Change time"])
    if action == "expiry":
        main.pending_actions[123].expires_at = 0
        await main.handle_message(123, "4pm", settings, telegram, calendar, parser)
        assert "expired" in telegram.send_message.call_args.args[1]
    elif action == "restart":
        main.pending_actions.clear()
    elif action == "new add":
        parser.parse_event.return_value = ParsedEvent(title="Lunch", start="2030-10-13T12:00:00+08:00",
                                                     end="2030-10-13T13:00:00+08:00", confidence="high")
        await main.handle_message(123, "add Lunch 13 October 2030 12-1pm", settings, telegram, calendar, parser)
    else:
        await main.handle_message(123, "/cancel" if action == "cancel" else "/help", settings, telegram, calendar, parser)
    await click(buttons["Delete this event"])
    calendar.update_event.assert_not_called()
    calendar.delete_event.assert_not_called()
    if action != "new add":
        assert 123 not in main.pending_actions


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["expired", "new event", "normal message"])
async def test_stale_created_buttons_cannot_delete_newer_event(flow, action):
    create, click, settings, telegram, calendar, parser = flow
    old = await create()
    if action == "expired":
        main.pending_actions[123].expires_at = 0
    elif action == "new event":
        await create()
    else:
        await main.handle_message(123, "/now", settings, telegram, calendar, parser)
    await click(old["Delete this event"])
    calendar.delete_event.assert_not_called()
    if action == "new event":
        assert main.pending_actions[123].events[0].event_id == "created-2"


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type,chat_id,sender", [("private", 123, 456), ("private", 789, 789), ("group", -100, 123)])
async def test_created_callbacks_require_owners_private_chat(flow, chat_type, chat_id, sender):
    create, _, _, telegram, calendar, _ = flow
    buttons = await create()
    request = AsyncMock()
    request.json.return_value = {"callback_query": {"id": "cb", "data": buttons["Delete this event"],
        "from": {"id": sender}, "message": {"chat": {"id": chat_id, "type": chat_type}}}}
    await main.webhook(request, "secret")
    calendar.delete_event.assert_not_called()
    assert main.pending_actions[123].action == "created"
    telegram.answer_callback_query.assert_awaited_once_with("cb", "Not available here.")


@pytest.mark.asyncio
async def test_created_callback_dispatches_from_authorized_webhook(flow):
    create, _, _, _, calendar, _ = flow
    buttons = await create()
    request = AsyncMock()
    request.json.return_value = {"callback_query": {"id": "cb", "data": buttons["Delete this event"],
        "from": {"id": 123}, "message": {"chat": {"id": 123, "type": "private"}}}}
    await main.webhook(request, "secret")
    calendar.delete_event.assert_called_once()


@pytest.mark.asyncio
async def test_selection_callback_cannot_reinterpret_created_token(flow):
    create, click, settings, telegram, calendar, parser = flow
    buttons = await create()
    token = main.pending_actions[123].token
    await main.handle_selection_callback(123, "cb", f"select:{token}:1", settings, telegram, calendar, parser)
    assert main.pending_actions[123].action == "created"
    calendar.set_reminder.assert_not_called()
    await click(buttons["Delete this event"])
    calendar.delete_event.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["delete", "change"])
async def test_recurring_shortcuts_clearly_target_whole_series(flow, action):
    create, click, settings, telegram, calendar, parser = flow
    buttons = await create(recurrence=True)
    assert set(buttons) == {"Change series time", "Delete series"}
    assert "whole series" in telegram.send_message.call_args.args[1]
    if action == "delete":
        await click(buttons["Delete series"])
        calendar.delete_series.assert_called_once()
        calendar.delete_event.assert_not_called()
    else:
        await click(buttons["Change series time"])
        await main.handle_message(123, "4pm", settings, telegram, calendar, parser)
        calendar.update_series.assert_called_once()
        calendar.update_event.assert_not_called()
    calendar.create_event.assert_called_once()


@pytest.mark.asyncio
async def test_failed_delete_consumes_shortcut_and_requires_new_request(flow):
    create, click, _, _, calendar, _ = flow
    buttons = await create()
    calendar.delete_event.side_effect = RuntimeError("permission denied")
    with pytest.raises(RuntimeError, match="permission denied"):
        await click(buttons["Delete this event"])
    await click(buttons["Delete this event"])
    calendar.delete_event.assert_called_once()
    assert 123 not in main.pending_actions


@pytest.mark.asyncio
async def test_failed_create_does_not_publish_buttons_or_pending_choice(flow):
    create, _, _, telegram, calendar, _ = flow
    calendar.create_event.side_effect = RuntimeError("calendar unavailable")
    with pytest.raises(RuntimeError, match="calendar unavailable"):
        await create()
    telegram.send_message.assert_not_awaited()
    assert not main.pending_actions


@pytest.mark.asyncio
async def test_invalid_time_answer_consumes_prompt_without_calendar_write(flow):
    create, click, settings, telegram, calendar, parser = flow
    buttons = await create()
    await click(buttons["Change time"])
    await main.handle_message(123, "Mon 10 October 2026 8-10pm", settings, telegram, calendar, parser)
    assert "resend the edit" in telegram.send_message.call_args.args[1]
    calendar.update_event.assert_not_called()
    calendar.create_event.assert_called_once()
    assert 123 not in main.pending_actions


@pytest.mark.asyncio
async def test_failed_time_edit_consumes_prompt_and_old_buttons_stay_invalid(flow):
    create, click, settings, telegram, calendar, parser = flow
    buttons = await create()
    await click(buttons["Change time"])
    calendar.update_event.side_effect = RuntimeError("calendar unavailable")
    with pytest.raises(RuntimeError, match="calendar unavailable"):
        await main.handle_message(123, "4pm", settings, telegram, calendar, parser)
    await click(buttons["Change time"])
    calendar.update_event.assert_called_once()
    assert 123 not in main.pending_actions


@pytest.mark.asyncio
async def test_all_day_success_reply_has_controls_and_date_edit_keeps_day_boundaries(flow):
    _, click, settings, telegram, calendar, parser = flow
    event = ParsedEvent(title="Holiday", start="2030-10-12T00:00:00+08:00",
                        end="2030-10-13T00:00:00+08:00", all_day=True, confidence="high")
    await main._create_event(123, event, telegram, calendar)
    assert "All day" in telegram.send_message.call_args.args[1]
    change = telegram.send_message.call_args.args[2]["inline_keyboard"][0][0]["callback_data"]
    await click(change)
    parser.parse_edit.side_effect = None
    parser.parse_edit.return_value = ParsedEdit(title="Holiday", start="2030-10-14T00:00:00+08:00",
                                              end="2030-10-15T00:00:00+08:00", all_day=True, confidence="high")
    await main.handle_message(123, "14 October 2030", settings, telegram, calendar, parser)
    edited = calendar.update_event.call_args.args[1]
    assert edited.all_day and edited.start.hour == edited.end.hour == 0
    assert edited.start.day == 14 and edited.end.day == 15
    calendar.create_event.assert_called_once()
