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
                for row in telegram.send_message.call_args.args[2]["inline_keyboard"] for button in row}

    async def click(data):
        await main.handle_created_callback(123, "cb", data, telegram, calendar)

    monkeypatch.setattr(main, "dependencies", lambda: (settings, telegram, {123: calendar, 456: Mock()}, parser, None))
    return create, click, settings, telegram, calendar, parser


@pytest.mark.asyncio
async def test_created_buttons_delete_exact_calendar_event_once(flow):
    create, click, _, telegram, calendar, parser = flow
    buttons = await create()
    assert set(buttons) == {"Change date/time", "Change calendar", "Delete this event"}
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
    await click(buttons["Change date/time"])
    assert "Drills" in telegram.send_message.call_args.args[1]
    assert "within 5 minutes" in telegram.send_message.call_args.args[1]
    await click(buttons["Change date/time"])
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
    await click(buttons["Change date/time"])
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
@pytest.mark.parametrize("control", ["Change date/time"])
@pytest.mark.parametrize("action", ["cancel", "expiry", "new command", "new add", "restart"])
async def test_time_change_prompt_lifecycle_leaves_saved_event_unchanged(flow, action, control):
    create, click, settings, telegram, calendar, parser = flow
    buttons = await create()
    await click(buttons[control])
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
@pytest.mark.parametrize("control", ["Delete this event", "Change date/time", "Change calendar"])
@pytest.mark.parametrize("chat_type,chat_id,sender", [("private", 123, 456), ("private", 789, 789), ("group", -100, 123)])
async def test_created_callbacks_require_owners_private_chat(flow, chat_type, chat_id, sender, control):
    create, _, _, telegram, calendar, _ = flow
    buttons = await create()
    request = AsyncMock()
    request.json.return_value = {"callback_query": {"id": "cb", "data": buttons[control],
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
@pytest.mark.parametrize("action", ["delete", "change", "date"])
async def test_recurring_shortcuts_clearly_target_whole_series(flow, action):
    create, click, settings, telegram, calendar, parser = flow
    buttons = await create(recurrence=True)
    assert set(buttons) == {"Change series date/time", "Change series calendar", "Delete series"}
    assert "whole series" in telegram.send_message.call_args.args[1]
    if action == "delete":
        await click(buttons["Delete series"])
        calendar.delete_series.assert_called_once()
        calendar.delete_event.assert_not_called()
    else:
        await click(buttons["Change series date/time"])
        await main.handle_message(123, "13 October 2030" if action == "date" else "4pm", settings, telegram, calendar, parser)
        calendar.update_series.assert_called_once()
        calendar.update_event.assert_not_called()
        if action == "date":
            assert calendar.update_series.call_args.args[1].start.isoformat() == "2030-10-13T20:00:00+08:00"
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
    await click(buttons["Change date/time"])
    await main.handle_message(123, "Mon 10 October 2026 8-10pm", settings, telegram, calendar, parser)
    assert "resend the edit" in telegram.send_message.call_args.args[1]
    calendar.update_event.assert_not_called()
    calendar.create_event.assert_called_once()
    assert 123 not in main.pending_actions


@pytest.mark.asyncio
async def test_failed_time_edit_consumes_prompt_and_old_buttons_stay_invalid(flow):
    create, click, settings, telegram, calendar, parser = flow
    buttons = await create()
    await click(buttons["Change date/time"])
    calendar.update_event.side_effect = RuntimeError("calendar unavailable")
    with pytest.raises(RuntimeError, match="calendar unavailable"):
        await main.handle_message(123, "4pm", settings, telegram, calendar, parser)
    await click(buttons["Change date/time"])
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


@pytest.mark.asyncio
@pytest.mark.parametrize("all_day,overnight", [(False, False), (False, True), (True, False)])
async def test_change_date_preserves_saved_clocks_duration_and_all_day_status(flow, all_day, overnight):
    _, click, settings, telegram, calendar, parser = flow
    start = "2030-10-12T00:00:00+08:00" if all_day else "2030-10-12T23:00:00+08:00" if overnight else "2030-10-12T20:00:00+08:00"
    end = "2030-10-13T00:00:00+08:00" if all_day else "2030-10-13T01:00:00+08:00" if overnight else "2030-10-12T22:00:00+08:00"
    await main._create_event(123, ParsedEvent(title="Drills", start=start, end=end, all_day=all_day, confidence="high"), telegram, calendar)
    saved = main.pending_actions[123].events[0]
    buttons = {button["text"]: button["callback_data"] for row in telegram.send_message.call_args.args[2]["inline_keyboard"] for button in row}
    await click(buttons["Change date/time"])
    assert "What date or time should I use" in telegram.send_message.call_args.args[1]
    assert "keep the current time and duration" in telegram.send_message.call_args.args[1]
    await click(buttons["Change date/time"])
    assert main.pending_actions[123].action == "created_edit"
    parser.parse_edit.side_effect = None
    parser.parse_edit.return_value = ParsedEdit(title="Drills", start="2030-10-14T14:00:00+08:00",
                                              end="2030-10-14T15:00:00+08:00", confidence="high", all_day=False)
    await main.handle_message(123, "13 October 2030", settings, telegram, calendar, parser)
    existing, edited = calendar.update_event.call_args.args
    assert existing == saved and edited.start.day == 13
    assert edited.start.time() == saved.start.time() and edited.end.time() == saved.end.time()
    assert edited.end - edited.start == saved.end - saved.start
    assert edited.all_day is all_day
    calendar._list_events_between.assert_called_once_with(edited.start, edited.end)
    calendar.create_event.assert_called_once()
    parser.match_event.assert_not_called()


@pytest.mark.asyncio
async def test_change_date_accepts_explicit_time_range_and_checks_new_interval(flow):
    create, click, settings, telegram, calendar, parser = flow
    buttons = await create()
    await click(buttons["Change date/time"])
    await main.handle_message(123, "13 October 2030 9-11am", settings, telegram, calendar, parser)
    edited = calendar.update_event.call_args.args[1]
    assert edited.start.isoformat() == "2030-10-13T09:00:00+08:00"
    assert edited.end.isoformat() == "2030-10-13T11:00:00+08:00"
    calendar._list_events_between.assert_called_once_with(edited.start, edited.end)


@pytest.mark.asyncio
async def test_change_date_conflict_waits_for_confirmation_without_duplicate_event(flow):
    create, click, settings, telegram, calendar, parser = flow
    buttons = await create()
    await click(buttons["Change date/time"])
    calendar._list_events_between.return_value = [CalendarEvent(event_id="busy", title="Busy",
        start="2030-10-13T20:00:00+08:00", end="2030-10-13T22:00:00+08:00")]
    await main.handle_message(123, "13 October 2030", settings, telegram, calendar, parser)
    calendar.update_event.assert_not_called()
    pending = main.pending_event_conflicts[123]
    assert pending.event.start.isoformat() == "2030-10-13T20:00:00+08:00"
    await main.handle_conflict_callback(123, "cb", f"conflict:{pending.token}:add", settings, telegram, calendar)
    calendar.update_event.assert_called_once()
    calendar.create_event.assert_called_once()

@pytest.mark.asyncio
@pytest.mark.parametrize('recurring', [False, True])
async def test_change_calendar_moves_once_and_renews_correct_target(flow, recurring):
    create, click, _, telegram, calendar, _ = flow
    buttons = await create(recurring)
    saved = main.pending_actions[123].events[0]
    assert 'Calendar: Work' in telegram.send_message.call_args.args[1]
    destination = CalendarInfo(calendar_id='personal', name='Personal', access_role='owner')
    calendar.list_calendars.return_value = [calendar.resolve_calendar.return_value, destination,
        CalendarInfo(calendar_id='read', name='Read only', access_role='reader')]
    calendar.move_event.return_value = saved.model_copy(update={'calendar_id':'personal', 'calendar_name':'Personal'})
    await click(buttons['Change series calendar' if recurring else 'Change calendar'])
    rows = telegram.send_message.call_args.args[2]['inline_keyboard']
    assert [row[0]['text'] for row in rows] == ['Personal', 'Cancel']
    selection = rows[0][0]['callback_data']
    assert len(selection.encode()) <= 64
    await asyncio.gather(click(selection), click(selection))
    calendar.move_event.assert_called_once_with(saved, destination)
    calendar.create_event.assert_called_once()
    assert 'Calendar: Personal' in telegram.send_message.call_args.args[1]
    assert main.pending_actions[123].events[0].calendar_id == 'personal'
    delete = telegram.send_message.call_args.args[2]['inline_keyboard'][-1][-1]['callback_data']
    await click(delete)
    method = calendar.delete_series if recurring else calendar.delete_event
    assert method.call_args.args[0].calendar_id == 'personal'


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['cancel', 'message', 'expiry', 'replacement', 'restart', 'failure', 'partial', 'invalid'])
async def test_calendar_picker_lifecycle_and_failures(flow, ending):
    from app.calendar_client import CalendarMoveError
    create, click, settings, telegram, calendar, parser = flow
    buttons = await create()
    saved = main.pending_actions[123].events[0]
    calendar.list_calendars.return_value = [CalendarInfo(calendar_id='home', name='Home', access_role='owner')]
    await click(buttons['Change calendar'])
    rows = telegram.send_message.call_args.args[2]['inline_keyboard']
    selection = rows[0][0]['callback_data']
    if ending == 'cancel':
        await click(rows[-1][0]['callback_data'])
    elif ending == 'message':
        await main.handle_message(123, '/cancel', settings, telegram, calendar, parser)
    elif ending == 'expiry':
        main.pending_actions[123].expires_at = 0
    elif ending == 'replacement':
        await create()
    elif ending == 'restart':
        main.pending_actions.clear()
    elif ending == 'invalid':
        await click(selection.rsplit(':', 1)[0] + ':99')
    else:
        calendar.move_event.side_effect = (CalendarMoveError(saved.model_copy(update={'calendar_name':'Home'}))
                                           if ending == 'partial' else RuntimeError('unavailable'))
    await click(selection)
    await click(selection)
    if ending in {'failure', 'partial'}:
        calendar.move_event.assert_called_once()
        message = telegram.send_message.call_args.args[1]
        assert 'inspect' in message
        if ending == 'partial':
            assert 'moved to Home' in message and 'reminder preservation failed' in message
    else:
        calendar.move_event.assert_not_called()


@pytest.mark.asyncio
async def test_no_writable_calendar_choices_leave_event_unchanged(flow):
    create, click, _, telegram, calendar, _ = flow
    buttons = await create()
    calendar.list_calendars.return_value = [calendar.resolve_calendar.return_value]
    await click(buttons['Change calendar'])
    assert 'no other writable calendars' in telegram.send_message.call_args.args[1]
    calendar.move_event.assert_not_called()
    assert 123 not in main.pending_actions

@pytest.mark.asyncio
async def test_default_add_displays_resolved_calendar(flow):
    _, _, _, telegram, calendar, _ = flow
    event = ParsedEvent(title='Drills', start='2030-10-12T20:00:00+08:00', end='2030-10-12T22:00:00+08:00', confidence='high')
    await main._create_event(123, event, telegram, calendar)
    calendar.resolve_calendar.assert_called_once_with(None)
    assert 'Calendar: Work' in telegram.send_message.call_args.args[1]
    assert calendar.create_event.call_args.args[1] == 'work'


@pytest.mark.asyncio
async def test_calendar_listing_failure_consumes_picker(flow):
    create, click, _, telegram, calendar, _ = flow
    buttons = await create()
    calendar.list_calendars.side_effect = RuntimeError('unavailable')
    await click(buttons['Change calendar'])
    assert 123 not in main.pending_actions
    assert 'event is unchanged' in telegram.send_message.call_args.args[1]
    calendar.move_event.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['list', 'move'])
async def test_calendar_operation_completion_does_not_replace_newer_request(flow, monkeypatch, stage):
    create, click, _, telegram, calendar, _ = flow
    buttons = await create()
    destination = CalendarInfo(calendar_id='home', name='Home', access_role='owner')
    calendar.list_calendars.return_value = [destination]
    original = main.asyncio.to_thread
    started, finish = asyncio.Event(), asyncio.Event()
    calendar.move_event.side_effect = lambda event, dest: event.model_copy(update={'calendar_id':dest.calendar_id, 'calendar_name':dest.name})
    async def delayed(function, *args):
        if function == (calendar.list_calendars if stage == 'list' else calendar.move_event):
            started.set()
            await finish.wait()
        return await original(function, *args)
    monkeypatch.setattr(main.asyncio, 'to_thread', delayed)
    if stage == 'move':
        await click(buttons['Change calendar'])
        data = telegram.send_message.call_args.args[2]['inline_keyboard'][0][0]['callback_data']
    else:
        data = buttons['Change calendar']
    task = asyncio.create_task(click(data))
    await started.wait()
    await create()
    newer = main.pending_actions[123]
    finish.set()
    await task
    assert main.pending_actions[123] is newer

@pytest.mark.asyncio
@pytest.mark.parametrize('unavailable', ['missing', 'read only'])
async def test_unusable_default_calendar_prevents_creation(flow, unavailable):
    _, _, _, telegram, calendar, _ = flow
    calendar.resolve_calendar.return_value = (None if unavailable == 'missing' else
        CalendarInfo(calendar_id='work', name='Work', access_role='reader'))
    event = ParsedEvent(title='Drills', start='2030-10-12T20:00:00+08:00', end='2030-10-12T22:00:00+08:00', confidence='high')
    await main._create_event(123, event, telegram, calendar)
    calendar.create_event.assert_not_called()
    assert 'Calendar:' not in telegram.send_message.call_args.args[1]
