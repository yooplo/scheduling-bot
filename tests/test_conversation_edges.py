import asyncio
import threading
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

import app.main as main
from app.models import CalendarEvent, MediaEventSuggestion, ParsedEdit, ParsedEventDraft
from test_conversation_media import flow


@pytest.mark.asyncio
async def test_media_recurrence_correction_uses_latest_weekday(flow):
    send, media, _, _, calendar, parser = flow
    parser.suggest_media_event.return_value = MediaEventSuggestion(event_text="Dinner every Monday 7–9pm")
    await media()
    initial = main.media_previews[123].event
    parser.parse_event.return_value = ParsedEventDraft(**initial.model_dump()).model_copy(update={
        "start": initial.start + timedelta(days=1), "end": initial.end + timedelta(days=1),
        "recurrence": "RRULE:FREQ=WEEKLY;BYDAY=TU"})
    await send("actually every Tuesday")
    proposed = main.media_previews[123].event
    assert proposed.recurrence == "RRULE:FREQ=WEEKLY;BYDAY=TU"
    assert proposed.start.weekday() == 1
    calendar.create_event.assert_not_called()
    await send("add event")
    assert calendar.create_event.call_args.args[0].recurrence == "RRULE:FREQ=WEEKLY;BYDAY=TU"


@pytest.mark.parametrize("text,rule", [
    ("Gym every Monday and Wednesday", "RRULE:FREQ=WEEKLY;BYDAY=MO,WE"),
    ("Gym every Monday for 3 weeks", "RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=3"),
    ("Gym every Monday\nUser follow-up: actually daily", "RRULE:FREQ=DAILY"),
    ("Gym every Monday\nUser follow-up: actually just once", None),
])
def test_recurrence_override_preserves_corrected_or_qualified_rule(text, rule):
    event = ParsedEventDraft(title="Gym", start="2030-10-14T19:00:00+08:00", end="2030-10-14T20:00:00+08:00",
        confidence="high", recurrence=rule)
    main._apply_recurrence_from_text(event, text)
    assert event.recurrence == rule


@pytest.mark.asyncio
@pytest.mark.parametrize("correction,expected", [
    ("actually remind me 15 minutes before", [15]),
    ("actually remind me an hour before", [60]),
    ("also remind me 15 minutes before", [60, 15]),
    ("actually no reminders", []),
    ("actually remind me 15m before", [15]),
])
async def test_media_reminder_correction_replaces_or_extends_explicitly(flow, correction, expected):
    send, media, _, _, calendar, parser = flow
    parser.suggest_media_event.return_value = MediaEventSuggestion(event_text="Dinner on 12 October 2030 7–9pm, remind me 60 minutes before")
    parser.parse_event.return_value = parser.parse_event.return_value.model_copy(update={"reminder_minutes": 60})
    await media()
    await send(correction)
    assert [r.minutes_before for r in main.media_previews[123].event.reminders] == expected
    calendar.create_event.assert_not_called()
    await send("add event")
    saved = calendar.create_event.call_args.args[0]
    assert [r.minutes_before for r in saved.reminders] == expected
    if not expected:
        assert saved.reminder_minutes is None


@pytest.mark.parametrize("text,expected", [
    ("remind me 60 minutes before\nUser follow-up: actually 15 minutes before\nUser follow-up: 4pm", [15]),
    ("remind me an hour before and 15 minutes before", [60, 15]),
    ("remind me 60 minutes before\nUser follow-up: also 60 minutes before", [60]),
    ("remind me 60 minutes before\nUser follow-up: also 15 minutes before\nUser follow-up: remove the 60 minutes before reminder", [15]),
    ("remind me 0 minutes before\nUser follow-up: actually 15 minutes before", [15]),
])
def test_reminder_merge_handles_history_words_duplicates_and_removal(text, expected):
    assert [r.minutes_before for r in main._reminders_from_text(text)] == expected


@pytest.mark.asyncio
async def test_reminder_message_dates_do_not_move_the_event(flow):
    send, media, _, _, _, parser = flow
    parser.suggest_media_event.return_value = MediaEventSuggestion(event_text="Dinner on 12 October 2030 7–9pm")
    await media()
    original = main.media_previews[123].event
    parser.parse_event.return_value = ParsedEventDraft(**original.model_dump())
    await send("remind me 15 minutes before to book next Wednesday at noon")
    proposed = main.media_previews[123].event
    assert proposed.start == original.start and proposed.end == original.end
    assert proposed.reminders[0].message == "book next Wednesday at noon"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["add_parse", "edit_parse", "reminder_parse", "conflict_read", "calendar_resolution"])
async def test_cancel_prevents_work_that_has_not_started_writing(flow, stage):
    send, _, _, _, calendar, parser = flow
    if stage in {"edit_parse", "reminder_parse"}:
        await send("Dinner on 12 October 2030 7–9pm")
    target = {"add_parse": parser.parse_event, "edit_parse": parser.parse_edit,
        "reminder_parse": parser.parse_reminder_for_event, "conflict_read": calendar._list_events_between,
        "calendar_resolution": calendar.resolve_calendar}[stage]
    original = target.side_effect
    result = target.return_value
    started, finish = threading.Event(), threading.Event()

    def slow(*args):
        started.set()
        finish.wait(timeout=5)
        return original(*args) if callable(original) else result

    target.side_effect = slow
    message = "move it to 4pm" if stage == "edit_parse" else "remind me an hour before" if stage == "reminder_parse" else "Dinner on 12 October 2030 7–9pm"
    task = asyncio.create_task(send(message))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        await send("/cancel")
    finally:
        finish.set()
        await task
    assert 123 not in main.pending_event_conflicts
    assert 123 not in main.pending_event_drafts
    assert 123 not in main.pending_actions
    assert 123 not in main.recent_events
    calendar.update_event.assert_not_called()
    calendar.set_reminder.assert_not_called()
    assert calendar.create_event.call_count == (1 if stage in {"edit_parse", "reminder_parse"} else 0)


@pytest.mark.asyncio
async def test_cancel_during_interrupted_draft_lookup_does_not_restore_draft(flow):
    send, _, _, _, calendar, parser = flow
    parser.parse_event.return_value = parser.parse_event.return_value.model_copy(update={"end": None, "confidence": "low", "missing_fields": ["duration"]})
    await send("Dinner tomorrow 7pm")
    assert 123 in main.pending_event_drafts
    started, finish = threading.Event(), threading.Event()

    def lookup(*args):
        started.set()
        finish.wait(timeout=5)
        return []

    calendar.list_events_for_day.side_effect = lookup
    task = asyncio.create_task(send("what are my plans tomorrow?"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        await send("/cancel")
    finally:
        finish.set()
        await task
    assert 123 not in main.pending_event_drafts
    assert 123 not in main.recent_event_lists


@pytest.mark.asyncio
async def test_submitted_create_can_finish_without_overwriting_newer_context(flow):
    send, _, _, _, calendar, _ = flow
    original_create = calendar.create_event.side_effect
    started, finish = threading.Event(), threading.Event()

    def create(*args):
        started.set()
        finish.wait(timeout=5)
        return original_create(*args)

    calendar.create_event.side_effect = create
    newer = CalendarEvent(event_id="newer", title="Newer", start="2030-10-13T12:00:00+08:00", end="2030-10-13T13:00:00+08:00")
    calendar.list_events.return_value = [newer]
    task = asyncio.create_task(send("Dinner on 12 October 2030 7–9pm"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        await send("list")
    finally:
        finish.set()
        await task
    calendar.create_event.assert_called_once()
    assert 123 not in main.pending_actions
    assert 123 not in main.recent_events
    assert main.recent_event_lists[123].events == [newer]


@pytest.mark.asyncio
async def test_stale_media_callback_does_not_cancel_new_media_interpretation(flow):
    _, media, _, _, calendar, parser = flow
    await media()
    old_token = main.media_previews[123].token
    started, finish = threading.Event(), threading.Event()

    def suggest(*args):
        started.set()
        finish.wait(timeout=5)
        return MediaEventSuggestion(event_text="New appointment on 12 October 2030 7–9pm")

    parser.suggest_media_event.side_effect = suggest
    task = asyncio.create_task(media())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        from unittest.mock import Mock
        request = Mock()
        request.json = AsyncMock(return_value={"callback_query": {"id": "old", "from": {"id": 123},
            "message": {"chat": {"id": 123, "type": "private"}}, "data": f"media:{old_token}:add"}})
        await main.webhook(request, "secret")
    finally:
        finish.set()
        await task
    assert main.media_previews[123].token != old_token
    calendar.create_event.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["Cancel Culture", "Change Management", "Reminder Rehearsal", "Showtime", "My Reminders Review"])
async def test_explicit_event_titles_do_not_become_commands(flow, title):
    send, _, _, _, calendar, parser = flow
    parser.parse_event.return_value = parser.parse_event.return_value.model_copy(update={"title": title})
    event_name = f"'{title}'" if title == "Reminder Rehearsal" else title
    await send(f"add {event_name} on 12 October 2030 7–9pm")
    calendar.create_event.assert_called_once()
    assert calendar.create_event.call_args.args[0].title == title
    calendar.delete_event_with_undo.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["Cancel Culture", "Reminder Rehearsal", "Availability Session", "My Calendars Review"])
async def test_named_edit_titles_do_not_select_delete_or_reminder_intents(flow, title):
    send, _, _, _, calendar, parser = flow
    target = CalendarEvent(event_id="named", title=title, start="2030-10-12T19:00:00+08:00", end="2030-10-12T21:00:00+08:00")
    calendar.list_events.return_value = [target]
    from app.models import DeleteMatch
    parser.match_event.return_value = DeleteMatch(matched_event_id="named", ambiguous=False)
    await send(f"move {title} to 4pm")
    calendar.update_event.assert_called_once()
    calendar.set_reminder.assert_not_called()
    calendar.delete_event_with_undo.assert_not_called()


@pytest.mark.asyncio
async def test_named_edit_replaces_incomplete_text_creation(flow):
    send, _, _, _, calendar, parser = flow
    complete = parser.parse_event.return_value
    parser.parse_event.return_value = complete.model_copy(update={"end": None, "confidence": "low", "missing_fields": ["duration"]})
    await send("Dinner tomorrow 7pm")
    dental = CalendarEvent(event_id="dental", title="Dental", start=complete.start, end=complete.end)
    calendar.list_events.return_value = [dental]
    from app.models import DeleteMatch
    parser.match_event.return_value = DeleteMatch(matched_event_id="dental", ambiguous=False)
    parser.parse_event.return_value = complete
    await send("move Dental to 4pm")
    assert calendar.update_event.call_args.args[0].event_id == "dental"
    calendar.create_event.assert_not_called()
    assert 123 not in main.pending_event_drafts


@pytest.mark.asyncio
async def test_lookup_that_outlives_draft_reports_expiry_instead_of_restoring_it(flow):
    send, _, _, _, calendar, parser = flow
    parser.parse_event.return_value = parser.parse_event.return_value.model_copy(update={"end": None, "confidence": "low", "missing_fields": ["duration"]})
    await send("Dinner tomorrow 7pm")
    draft = main.pending_event_drafts[123]
    started, finish = threading.Event(), threading.Event()

    def lookup(*args):
        started.set()
        finish.wait(timeout=5)
        return []

    calendar.list_events_for_day.side_effect = lookup
    task = asyncio.create_task(send("what are my plans tomorrow?"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        draft.expires_at = 0
    finally:
        finish.set()
        answer = await task
    assert 123 not in main.pending_event_drafts
    assert "expired" in answer


@pytest.mark.asyncio
async def test_all_day_edit_with_naive_model_times_is_not_silently_normalized(flow):
    send, _, _, _, calendar, parser = flow
    await send("Dinner on 12 October 2030 7–9pm")
    event = main.recent_events[123].events[0]
    parser.parse_edit.side_effect = None
    parser.parse_edit.return_value = ParsedEdit(title=event.title, start=event.start.replace(tzinfo=None),
        end=event.end.replace(tzinfo=None), confidence="high", all_day=True)
    await send("change it to all day")
    calendar.update_event.assert_not_called()


@pytest.mark.asyncio
async def test_additional_reminder_can_be_added_to_incomplete_media_draft(flow):
    send, media, _, _, calendar, parser = flow
    complete = parser.parse_event.return_value
    parser.suggest_media_event.return_value = MediaEventSuggestion(event_text="Dinner tomorrow 7pm, remind me 60 minutes before")
    parser.parse_event.return_value = complete.model_copy(update={"end": None, "confidence": "low", "missing_fields": ["duration"]})
    await media()
    await send("add another reminder 15 minutes before")
    assert 123 in main.media_requests and 123 in main.pending_event_drafts
    parser.parse_event.return_value = complete
    await send("2 hours")
    assert [r.minutes_before for r in main.media_previews[123].event.reminders] == [60, 15]
    calendar.create_event.assert_not_called()


@pytest.mark.asyncio
async def test_other_users_input_does_not_cancel_current_chat(flow):
    send, _, _, _, calendar, parser = flow
    complete = parser.parse_event.return_value
    started, finish = threading.Event(), threading.Event()

    def parse(*args):
        started.set()
        finish.wait(timeout=5)
        return complete

    parser.parse_event.side_effect = parse
    task = asyncio.create_task(send("Dinner on 12 October 2030 7–9pm"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        await send("/cancel", chat_id=456)
    finally:
        finish.set()
        await task
    calendar.create_event.assert_called_once()
    assert main.recent_events[123].events[0].title == "Dinner"
    assert not main.conversation_turns


@pytest.mark.asyncio
async def test_help_category_browsing_does_not_cancel_nonmedia_work(flow):
    send, _, _, _, calendar, parser = flow
    complete = parser.parse_event.return_value
    started, finish = threading.Event(), threading.Event()

    def parse(*args):
        started.set()
        finish.wait(timeout=5)
        return complete

    parser.parse_event.side_effect = parse
    task = asyncio.create_task(send("Dinner on 12 October 2030 7–9pm"))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        from unittest.mock import Mock
        request = Mock()
        request.json = AsyncMock(return_value={"callback_query": {"id": "help", "from": {"id": 123},
            "message": {"chat": {"id": 123, "type": "private"}}, "data": "help:events"}})
        await main.webhook(request, "secret")
    finally:
        finish.set()
        await task
    calendar.create_event.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["media", "conflict", "delete"])
async def test_cancel_during_write_confirmation_acknowledgement_prevents_submission(flow, kind):
    send, media, settings, telegram, calendar, parser = flow
    if kind == "media":
        await media()
        token = main.media_previews[123].token
    elif kind == "conflict":
        complete = parser.parse_event.return_value
        calendar._list_events_between.return_value = [CalendarEvent(event_id="busy", title="Busy", start=complete.start, end=complete.end)]
        await send("Dinner on 12 October 2030 7–9pm")
        token = main.pending_event_conflicts[123].token
    else:
        await send("Dinner on 12 October 2030 7–9pm")
        await send("delete it")
        token = main.pending_actions[123].token
    started, finish = asyncio.Event(), asyncio.Event()

    async def acknowledge(*args):
        started.set()
        await finish.wait()

    telegram.answer_callback_query.side_effect = acknowledge
    if kind == "media":
        callback = main.handle_media_callback(123, "cb", f"media:{token}:add", settings, telegram, calendar)
    elif kind == "conflict":
        callback = main.handle_conflict_callback(123, "cb", f"conflict:{token}:add", settings, telegram, calendar)
    else:
        callback = main.handle_delete_callback(123, "cb", f"delete:{token}:confirm", telegram, calendar)
    task = asyncio.create_task(callback)
    await started.wait()
    try:
        await send("/cancel")
    finally:
        finish.set()
        await task
    assert calendar.create_event.call_count == (1 if kind == "delete" else 0)
    calendar.delete_event_with_undo.assert_not_called()
    assert 123 not in main.pending_actions
    assert 123 not in main.pending_event_conflicts


@pytest.mark.asyncio
@pytest.mark.parametrize("correction,expected", [("actually remind me fifteen minutes before", [15]), ("also remind me fifteen minutes before", [60, 15])])
async def test_nonlocal_reminder_phrase_still_supersedes_old_instruction(flow, correction, expected):
    send, media, _, _, calendar, parser = flow
    parser.suggest_media_event.return_value = MediaEventSuggestion(event_text="Dinner on 12 October 2030 7–9pm, remind me 60 minutes before")
    await media()
    parser.parse_event.return_value = parser.parse_event.return_value.model_copy(update={"reminder_minutes": 15})
    await send(correction)
    assert [r.minutes_before for r in main.media_previews[123].event.reminders] == expected
    calendar.create_event.assert_not_called()


def test_quoted_reminder_message_lead_times_are_not_extra_alerts():
    reminders = main._reminders_from_text("remind me 15 minutes before to say 'leave 60 minutes before'")
    assert [(r.minutes_before, r.message) for r in reminders] == [(15, "say 'leave 60 minutes before'")]


@pytest.mark.asyncio
async def test_quoted_reminder_words_in_event_title_are_not_reminder_instructions(flow):
    send, _, _, _, calendar, parser = flow
    parser.parse_event.return_value = parser.parse_event.return_value.model_copy(update={"title": "Remind Me Before Dawn"})
    await send("add 'Remind Me Before Dawn' on 12 October 2030 7–9pm")
    calendar.create_event.assert_called_once()
    assert calendar.create_event.call_args.args[0].reminders == []


@pytest.mark.asyncio
@pytest.mark.parametrize("correction,changes", [("at Clinic Monday", {"location": "Clinic Monday"}), ("call it Monday", {"title": "Monday"})])
async def test_literal_name_corrections_do_not_reschedule_media_event(flow, correction, changes):
    send, media, _, _, _, parser = flow
    parser.suggest_media_event.return_value = MediaEventSuggestion(event_text="Dinner on 12 October 2030 7–9pm")
    await media()
    original = main.media_previews[123].event
    parser.parse_event.return_value = ParsedEventDraft(**original.model_dump()).model_copy(update=changes)
    await send(correction)
    result = main.media_previews[123].event
    assert result.start == original.start and result.end == original.end
    for key, value in changes.items():
        assert getattr(result, key) == value


@pytest.mark.asyncio
async def test_quoted_weekday_title_is_not_a_date_or_recurrence_instruction(flow):
    send, _, _, _, calendar, parser = flow
    parser.parse_event.return_value = parser.parse_event.return_value.model_copy(update={"title": "Every Monday", "recurrence": None})
    await send("add 'Every Monday' on 12 October 2030 7–9pm")
    calendar.create_event.assert_called_once()
    event = calendar.create_event.call_args.args[0]
    assert event.start.day == 12 and event.recurrence is None


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix,append", [("actually", False), ("also", True)])
async def test_reminder_correction_prefix_after_saved_event_uses_reminder_flow(flow, prefix, append):
    send, _, _, _, calendar, parser = flow
    await send("Dinner on 12 October 2030 7–9pm")
    from app.models import ParsedReminder
    parser.parse_reminder_for_event.return_value = ParsedReminder(reminder_minutes=15, confidence="high")
    await send(f"{prefix} remind me 15 minutes before")
    assert calendar.set_reminder.call_args.args[0].event_id == "dinner"
    assert calendar.set_reminder.call_args.args[3] == append
    calendar.update_event.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("pending", [False, True])
async def test_clear_reminders_targets_saved_event_without_creating_an_event(flow, pending):
    send, _, _, _, calendar, parser = flow
    await send("Dinner on 12 October 2030 7–9pm")
    if pending:
        from app.models import ParsedReminder
        parser.parse_reminder_for_event.return_value = ParsedReminder(reminder_minutes=15, confidence="low")
        await send("remind me before it")
    await send("no reminders")
    assert calendar.clear_reminder.call_args.args[0].event_id == "dinner"
    calendar.create_event.assert_called_once()
    calendar.update_event.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_media", [False, True])
async def test_named_management_with_correction_prefix_still_targets_its_named_event(flow, pending_media):
    send, media, _, _, calendar, parser = flow
    if pending_media:
        await media()
    else:
        await send("Dinner on 12 October 2030 7–9pm")
    complete = parser.parse_event.return_value
    target = CalendarEvent(event_id="dental", title="Dental", start=complete.start, end=complete.end)
    calendar.list_events.return_value = [target]
    from app.models import DeleteMatch
    parser.match_event.return_value = DeleteMatch(matched_event_id="dental", ambiguous=False)
    await send("actually move Dental to Friday at 4pm")
    assert calendar.update_event.call_args.args[0].event_id == "dental"
    assert 123 not in main.media_previews


@pytest.mark.parametrize("location", ["Once Studio", '\"No Repeats Studio\"'])
def test_recurrence_does_not_treat_location_names_as_one_off_corrections(location):
    event = ParsedEventDraft(title="Gym", start="2030-10-14T19:00:00+08:00", end="2030-10-14T20:00:00+08:00",
        confidence="high", recurrence="RRULE:FREQ=WEEKLY;BYDAY=MO")
    main._apply_recurrence_from_text(event, f"Gym every Monday at {location}")
    assert event.recurrence == "RRULE:FREQ=WEEKLY;BYDAY=MO"


@pytest.mark.asyncio
@pytest.mark.parametrize("rule,label", [("RRULE:FREQ=DAILY", "Repeats daily"), ("RRULE:FREQ=WEEKLY;INTERVAL=2", "Recurring event")])
async def test_nonweekly_series_does_not_claim_to_be_weekly_or_force_weekly_edit_context(flow, rule, label):
    send, _, _, _, _, parser = flow
    parser.parse_event.return_value = parser.parse_event.return_value.model_copy(update={"recurrence": rule})
    answer = await send("Dinner on 12 October 2030 7–9pm")
    assert label in answer
    assert main.pending_actions[123].request_text == "recurring series"
