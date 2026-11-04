from unittest.mock import AsyncMock

import pytest

import app.main as main
from app.models import CalendarEvent, DeleteMatch, ReminderSpec, ScheduledReminder
from test_conversation_media import flow


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "change Dental's reminder to 30 minutes before",
    "update Dental’s reminder to 30 minutes before",
    "edit James' reminder to 30 minutes before",
    "change Dental reminder to 30 minutes before",
    "edit the reminder about Dental to 30 minutes before",
    "set 'Dental' reminder to 30 minutes before",
])
@pytest.mark.parametrize("context", ["none", "saved", "followup", "media"])
async def test_explicit_reminder_edit_updates_only_the_named_reminder(flow, message, context):
    send, media, _, _, calendar, parser = flow
    if context in {"saved", "followup"}:
        await send("Dinner on 12 October 2030 7–9pm")
    if context == "followup":
        parsed = parser.parse_reminder_for_event.return_value
        parser.parse_reminder_for_event.return_value = parsed.model_copy(update={"confidence": "low"})
        await send("remind me before it")
        parser.parse_reminder_for_event.return_value = parsed
    if context == "media":
        await media()
    complete = parser.parse_event.return_value
    target = CalendarEvent(event_id="dental", title="James" if "James" in message else "Dental", start=complete.start, end=complete.end)
    calendar.list_events.return_value = [target]
    parser.match_event.return_value = DeleteMatch(matched_event_id="dental", ambiguous=False)
    await send(message)
    calendar.set_reminder.assert_called_once()
    assert calendar.set_reminder.call_args.args[0].event_id == "dental"
    calendar.update_event.assert_not_called()
    calendar.update_series.assert_not_called()
    calendar.delete_event_with_undo.assert_not_called()
    assert 123 not in main.media_previews


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "remove Dental reminder", "delete the reminder about Dental", "disable Dental's reminder",
    "cancel Dental’s reminder", "remove 'Dental' reminder",
])
async def test_explicit_reminder_removal_never_requests_event_deletion(flow, message):
    send, _, _, _, calendar, parser = flow
    complete = parser.parse_event.return_value
    target = CalendarEvent(event_id="dental", title="Dental", start=complete.start, end=complete.end)
    calendar.list_events.return_value = [target]
    calendar.list_standalone_reminder_events.return_value = []
    parser.match_event.return_value = DeleteMatch(matched_event_id="dental", ambiguous=False)
    await send(message)
    calendar.clear_reminder.assert_called_once_with(target)
    calendar.delete_event_with_undo.assert_not_called()
    calendar.delete_event.assert_not_called()
    assert 123 not in main.pending_actions


@pytest.mark.asyncio
async def test_suffix_reminder_removal_can_match_an_independent_reminder(flow):
    _, _, settings, telegram, calendar, parser = flow
    calendar.list_events.return_value = []
    calendar.list_standalone_reminder_events.return_value = []
    cron = AsyncMock()
    cron.list_reminders.return_value = [ScheduledReminder(event_id="cron:55", due_at=parser.parse_event.return_value.start,
        reminder=ReminderSpec(message="pay the bill"), standalone=True)]
    parser.match_event.return_value = DeleteMatch(matched_event_id="cron:55", ambiguous=False)
    await main.handle_message(123, "remove bill reminder", settings, telegram, calendar, parser, cron)
    cron.delete_reminder.assert_awaited_once_with(55)
    calendar.delete_event.assert_not_called()
    calendar.delete_event_with_undo.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["change its reminder to 30 minutes before", "update that event's reminder to 30 minutes before"])
async def test_possessive_pronoun_reminder_keeps_exact_recent_target(flow, message):
    send, _, _, _, calendar, parser = flow
    await send("Dinner on 12 October 2030 7–9pm")
    await send(message)
    assert calendar.set_reminder.call_args.args[0].event_id == "dinner"
    parser.match_event.assert_not_called()
    calendar.update_event.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["Reminder Rehearsal", "Dental Reminder Workshop", "Dental's Reminder Rehearsal", "'Dental Reminder'"])
@pytest.mark.parametrize("verb", ["move", "delete"])
async def test_reminder_word_in_literal_event_title_keeps_event_routing(flow, title, verb):
    send, _, _, _, calendar, parser = flow
    complete = parser.parse_event.return_value
    target = CalendarEvent(event_id="named", title=title.strip("'"), start=complete.start, end=complete.end)
    calendar.list_events.return_value = [target]
    parser.match_event.return_value = DeleteMatch(matched_event_id="named", ambiguous=False)
    await send(f"{verb} {title}" + (" to Friday at 4pm" if verb == "move" else ""))
    if verb == "move":
        assert calendar.update_event.call_args.args[0].event_id == "named"
    else:
        assert main.pending_actions[123].action == "delete_confirm"
        assert main.pending_actions[123].events[0].event_id == "named"
    calendar.clear_reminder.assert_not_called()
    calendar.set_reminder.assert_not_called()
