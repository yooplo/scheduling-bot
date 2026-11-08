import pytest

import app.main as main
from app.models import CalendarEvent, DeleteMatch, ParsedReminder
from test_conversation_media import flow


async def open_saved_time_prompt(flow):
    send, _, _, telegram, calendar, _ = flow
    await send("Dinner on 12 October 2030 7–9pm")
    buttons = telegram.send_message.call_args.args[2]["inline_keyboard"]
    change = next(button["callback_data"] for row in buttons for button in row if button["text"] == "Change date/time")
    await main.handle_created_callback(123, "callback", change, telegram, calendar)
    assert main.pending_actions[123].action == "created_edit"


@pytest.mark.asyncio
@pytest.mark.parametrize("message,operation", [
    ("move Dental to Friday at 4pm", "edit"),
    ("actually move Dental to Friday at 4pm", "edit"),
    ("change Dental's reminder to 30 minutes before", "remind"),
    ("delete Dental", "delete"),
    ("remove Dental reminder", "clear_reminder"),
])
async def test_saved_time_prompt_routes_replacement_to_named_event(flow, message, operation):
    send, _, _, _, calendar, parser = flow
    await open_saved_time_prompt(flow)
    draft = parser.parse_event.return_value
    target = CalendarEvent(event_id="dental", title="Dental", start=draft.start, end=draft.end)
    calendar.list_events.return_value = [target]
    calendar.list_standalone_reminder_events.return_value = []
    parser.match_event.return_value = DeleteMatch(matched_event_id="dental", ambiguous=False)
    await send(message)
    if operation == "edit":
        assert calendar.update_event.call_args.args[0].event_id == "dental"
    else:
        calendar.update_event.assert_not_called()
        if operation == "remind":
            assert calendar.set_reminder.call_args.args[0].event_id == "dental"
        elif operation == "clear_reminder":
            calendar.clear_reminder.assert_called_once_with(target)
        else:
            assert main.pending_actions[123].action == "delete_confirm"
            assert main.pending_actions[123].events[0].event_id == "dental"
            calendar.delete_event_with_undo.assert_not_called()
    assert calendar.create_event.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["remind me 30 minutes before", "change its reminder to 30 minutes before"])
async def test_saved_time_prompt_allows_reminder_action_switch(flow, message):
    send, _, _, _, calendar, parser = flow
    await open_saved_time_prompt(flow)
    parser.parse_reminder_for_event.return_value = ParsedReminder(reminder_minutes=30, confidence="high")
    await send(message)
    assert calendar.set_reminder.call_args.args[0].event_id == "dinner"
    calendar.update_event.assert_not_called()
    parser.match_event.assert_not_called()


@pytest.mark.asyncio
async def test_saved_time_prompt_survives_read_only_lookup_without_renewing_expiry(flow):
    send, _, _, _, calendar, _ = flow
    await open_saved_time_prompt(flow)
    pending = main.pending_actions[123]
    expiry = pending.expires_at
    await send("list")
    assert main.pending_actions[123] is pending
    assert pending.expires_at == expiry
    calendar.update_event.assert_not_called()
    await send("4pm")
    assert calendar.update_event.call_args.args[0].event_id == "dinner"


@pytest.mark.asyncio
async def test_saved_time_prompt_replacement_invalidates_original_shortcut(flow):
    send, _, _, telegram, calendar, parser = flow
    await open_saved_time_prompt(flow)
    old_token = main.pending_actions[123].token
    calendar.list_events.return_value = []
    await send("delete Dental")
    await main.handle_created_callback(123, "old", f"created:{old_token}:delete", telegram, calendar)
    calendar.update_event.assert_not_called()
    calendar.delete_event_with_undo.assert_not_called()


@pytest.mark.asyncio
async def test_saved_time_prompt_expiry_does_not_restore_old_context(flow):
    send, _, _, _, calendar, _ = flow
    await open_saved_time_prompt(flow)
    main.pending_actions[123].expires_at = 0
    main.recent_events[123].expires_at = 0
    reply = await send("move it to 4pm")
    assert "Which event" in reply or "expired" in reply
    calendar.update_event.assert_not_called()
