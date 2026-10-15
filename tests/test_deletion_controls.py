from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

import app.main as main
from app.models import CalendarEvent


@pytest.fixture
def delete_flow(monkeypatch):
    monkeypatch.setattr(main, "pending_actions", {})
    telegram, calendar, parser = AsyncMock(), Mock(), Mock()
    event = CalendarEvent(
        event_id="event-1", calendar_id="work", calendar_name="Work",
        title="Dentist", start=datetime.fromisoformat("2030-09-13T14:00:00+08:00"),
        end=datetime.fromisoformat("2030-09-13T15:00:00+08:00"),
    )
    calendar.list_events.return_value = [event]
    calendar.delete_event_with_undo.return_value = {
        "id": "event-1", "summary": "Dentist",
        "start": {"dateTime": "2030-09-13T14:00:00+08:00"},
        "end": {"dateTime": "2030-09-13T15:00:00+08:00"},
    }
    calendar.restore_deleted_event.return_value = event.model_copy(update={"event_id": "restored-1"})
    settings = SimpleNamespace(timezone=ZoneInfo("Asia/Singapore"), user_timezone="Asia/Singapore")
    return settings, telegram, calendar, parser, event


@pytest.mark.asyncio
async def test_direct_delete_requires_confirmation_and_undo_is_one_use(delete_flow):
    settings, telegram, calendar, parser, event = delete_flow
    await main.handle_message(123, "delete Dentist tomorrow", settings, telegram, calendar, parser)

    assert calendar.delete_event_with_undo.call_count == 0
    pending = main.pending_actions[123]
    assert pending.action == "delete_confirm"
    message, markup = telegram.send_message.call_args.args[1:3]
    assert "Dentist" in message and "You can undo this deletion for 5 minutes." in message
    confirm = markup["inline_keyboard"][0][0]["callback_data"]

    await main.handle_delete_callback(123, "confirm-callback", confirm, telegram, calendar)
    calendar.delete_event_with_undo.assert_called_once_with(event)
    assert main.pending_actions[123].action == "undo_delete"
    undo = telegram.send_message.call_args.args[2]["inline_keyboard"][0][0]["callback_data"]

    await main.handle_delete_callback(123, "undo-callback", undo, telegram, calendar)
    calendar.restore_deleted_event.assert_called_once_with(
        calendar.delete_event_with_undo.return_value, "work", "Work",
    )
    assert "Restored: Dentist" in telegram.send_message.call_args.args[1]
    assert 123 not in main.pending_actions

    await main.handle_delete_callback(123, "stale-undo", undo, telegram, calendar)
    calendar.restore_deleted_event.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["cancel", "expiry", "replacement"])
async def test_unconfirmed_delete_never_writes(delete_flow, choice):
    settings, telegram, calendar, parser, _ = delete_flow
    await main.handle_message(123, "delete Dentist", settings, telegram, calendar, parser)
    confirm = telegram.send_message.call_args.args[2]["inline_keyboard"][0][0]["callback_data"]

    if choice == "cancel":
        cancel = telegram.send_message.call_args.args[2]["inline_keyboard"][0][1]["callback_data"]
        await main.handle_delete_callback(123, "cancel", cancel, telegram, calendar)
    elif choice == "expiry":
        main.pending_actions[123].expires_at = 0
        await main.handle_delete_callback(123, "expired", confirm, telegram, calendar)
    else:
        await main.handle_message(123, "/now", settings, telegram, calendar, parser)
        await main.handle_delete_callback(123, "replaced", confirm, telegram, calendar)

    calendar.delete_event_with_undo.assert_not_called()
    calendar.delete_event.assert_not_called()
    calendar.delete_series.assert_not_called()


@pytest.mark.asyncio
async def test_typed_confirmation_executes_delete(delete_flow):
    settings, telegram, calendar, parser, event = delete_flow
    await main.handle_message(123, "delete Dentist", settings, telegram, calendar, parser)
    await main.handle_message(123, "confirm delete", settings, telegram, calendar, parser)

    calendar.delete_event_with_undo.assert_called_once_with(event)
    assert main.pending_actions[123].action == "undo_delete"


@pytest.mark.asyncio
async def test_typed_undo_restores_event(delete_flow):
    settings, telegram, calendar, parser, _ = delete_flow
    await main.handle_message(123, "delete Dentist", settings, telegram, calendar, parser)
    await main.handle_message(123, "confirm delete", settings, telegram, calendar, parser)
    await main.handle_message(123, "undo", settings, telegram, calendar, parser)

    calendar.restore_deleted_event.assert_called_once()
    assert 123 not in main.pending_actions


@pytest.mark.asyncio
async def test_expired_undo_does_not_restore_event(delete_flow):
    settings, telegram, calendar, parser, _ = delete_flow
    await main.handle_message(123, "delete Dentist", settings, telegram, calendar, parser)
    confirm = telegram.send_message.call_args.args[2]["inline_keyboard"][0][0]["callback_data"]
    await main.handle_delete_callback(123, "confirm", confirm, telegram, calendar)
    undo = telegram.send_message.call_args.args[2]["inline_keyboard"][0][0]["callback_data"]
    main.pending_actions[123].expires_at = 0

    await main.handle_delete_callback(123, "expired-undo", undo, telegram, calendar)

    calendar.restore_deleted_event.assert_not_called()
    assert 123 not in main.pending_actions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,reply,expected",
    [("delete_confirm", "confirm delete", "confirmation expired"),
     ("undo_delete", "undo", "Undo option expired")],
)
async def test_expired_typed_delete_controls_do_not_fall_through_to_event_creation(delete_flow, state, reply, expected):
    settings, telegram, calendar, parser, _ = delete_flow
    await main.handle_message(123, "delete Dentist", settings, telegram, calendar, parser)
    if state == "undo_delete":
        confirm = telegram.send_message.call_args.args[2]["inline_keyboard"][0][0]["callback_data"]
        await main.handle_delete_callback(123, "confirm", confirm, telegram, calendar)
    main.pending_actions[123].expires_at = 0

    await main.handle_message(123, reply, settings, telegram, calendar, parser)

    assert expected in telegram.send_message.call_args.args[1]
    parser.parse_event.assert_not_called()
    if state == "undo_delete":
        calendar.delete_event_with_undo.assert_called_once()
    else:
        calendar.delete_event_with_undo.assert_not_called()


@pytest.mark.asyncio
async def test_recurring_series_requires_confirmation_and_has_no_undo(delete_flow):
    settings, telegram, calendar, parser, event = delete_flow
    series_event = event.model_copy(update={"recurring_event_id": "series-1"})
    calendar.list_events.return_value = [series_event]

    await main.handle_message(123, "delete weekly Dentist series", settings, telegram, calendar, parser)
    message, markup = telegram.send_message.call_args.args[1:3]
    assert "entire recurring series" in message
    assert "cannot be undone" in message
    calendar.delete_series.assert_not_called()

    confirm = markup["inline_keyboard"][0][0]["callback_data"]
    await main.handle_delete_callback(123, "confirm", confirm, telegram, calendar)
    calendar.delete_series.assert_called_once_with(series_event)
    calendar.delete_event_with_undo.assert_not_called()
    assert main.pending_actions == {}


@pytest.mark.asyncio
async def test_delete_confirmation_callback_requires_authorized_private_chat(delete_flow, monkeypatch):
    _, telegram, calendar, parser, _ = delete_flow
    await main._ask_delete_confirmation(123, delete_flow[4], telegram)
    callback_data = telegram.send_message.call_args.args[2]["inline_keyboard"][0][0]["callback_data"]
    settings = SimpleNamespace(telegram_webhook_secret="secret")
    monkeypatch.setattr(main, "dependencies", lambda: (settings, telegram, {123: calendar}, parser, None))
    request = AsyncMock()
    request.json.return_value = {"callback_query": {
        "id": "cb", "data": callback_data, "from": {"id": 123},
        "message": {"chat": {"id": 456, "type": "private"}},
    }}

    await main.webhook(request, "secret")
    calendar.delete_event_with_undo.assert_not_called()
    telegram.answer_callback_query.assert_awaited_once_with("cb", "Not available here.")
