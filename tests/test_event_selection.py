import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import app.main as main
from app.models import CalendarEvent


@pytest.fixture
def selection():
    telegram, calendar, parser = AsyncMock(), Mock(), Mock()
    events = [CalendarEvent(event_id=str(i), title="Dentist", start=f"2030-09-{12+i}T14:00:00+08:00", calendar_name="Work") for i in (1, 2)]

    async def prompt(action="delete"):
        await main._ask_to_select(123, action, "original request", events, SimpleNamespace(candidates=[]), telegram)
        return telegram.send_message.call_args.args[2]["inline_keyboard"]

    async def click(data):
        await main.handle_selection_callback(123, "cb", data, SimpleNamespace(), telegram, calendar, parser)

    yield prompt, click, telegram, calendar, parser, events
    main.pending_actions.pop(123, None)


@pytest.mark.asyncio
async def test_buttons_identify_events_and_delete_selected_once(selection):
    prompt, click, telegram, calendar, _, events = selection
    buttons = await prompt()
    assert "Dentist" in buttons[1][0]["text"] and "14 Sep" in buttons[1][0]["text"]
    assert "Work" in buttons[1][0]["text"]
    assert buttons[-1][0]["text"] == "Cancel"
    assert all(len(row[0]["callback_data"].encode()) <= 64 for row in buttons)
    data = buttons[1][0]["callback_data"]
    await asyncio.gather(click(data), click(data))
    calendar.delete_event.assert_called_once_with(events[1])
    assert 123 not in main.pending_actions


@pytest.mark.asyncio
@pytest.mark.parametrize("action,helper", [("edit", "_edit_event"), ("remind", "_set_reminder")])
async def test_selection_preserves_original_request(selection, monkeypatch, action, helper):
    prompt, click, _, _, _, events = selection
    handler = AsyncMock()
    monkeypatch.setattr(main, helper, handler)
    buttons = await prompt(action)
    await click(buttons[0][0]["callback_data"])
    args = handler.call_args.args
    assert "original request" in args and events[0] in args
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_remove_reminder_preserves_event(selection):
    prompt, click, _, calendar, _, events = selection
    buttons = await prompt("clear_reminder")
    await click(buttons[1][0]["callback_data"])
    calendar.clear_reminder.assert_called_once_with(events[1])
    calendar.delete_event.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["cancel", "expired", "replaced", "invalid"])
async def test_stale_and_invalid_choices_do_not_execute(selection, mode):
    prompt, click, _, calendar, _, _ = selection
    buttons = await prompt()
    if mode == "cancel":
        await click(buttons[-1][0]["callback_data"])
    elif mode == "expired":
        main.pending_actions[123].expires_at = 0
    elif mode == "replaced":
        await prompt("edit")
        new_token = main.pending_actions[123].token
    else:
        await click(f"select:{main.pending_actions[123].token}:99")
        assert 123 in main.pending_actions
        calendar.delete_event.assert_not_called()
        return
    await click(buttons[0][0]["callback_data"])
    calendar.delete_event.assert_not_called()
    if mode == "replaced":
        assert main.pending_actions[123].token == new_token


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["2", "second"])
async def test_typed_selection_still_works(selection, reply):
    prompt, _, telegram, calendar, parser, events = selection
    await prompt()
    await main.handle_message(123, reply, SimpleNamespace(), telegram, calendar, parser)
    calendar.delete_event.assert_called_once_with(events[1])


def test_new_request_with_number_is_not_a_selection():
    events = [CalendarEvent(event_id="1", title="Dentist", start="2030-09-13T14:00:00+08:00")]
    assert main._selection("add Lunch tomorrow at 1pm", events) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type,sender,allowed", [("private", 123, True), ("private", 456, False), ("group", 123, False)])
async def test_webhook_selection_requires_owners_private_chat(selection, monkeypatch, chat_type, sender, allowed):
    prompt, _, telegram, calendar, parser, _ = selection
    buttons = await prompt()
    settings = SimpleNamespace(telegram_webhook_secret="secret")
    monkeypatch.setattr(main, "dependencies", lambda: (settings, telegram, {123:calendar,456:calendar}, parser, None))
    request = AsyncMock()
    request.json.return_value = {"callback_query": {
        "id": "cb", "data": buttons[0][0]["callback_data"], "from": {"id": sender},
        "message": {"chat": {"id": 123, "type": chat_type}},
    }}
    await main.webhook(request, "secret")
    assert calendar.delete_event.call_count == int(allowed)
