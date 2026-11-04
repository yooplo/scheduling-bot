import asyncio
from types import SimpleNamespace
from datetime import timedelta
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

import app.main as main
from app.models import CalendarEvent, CalendarInfo, MediaEventSuggestion, ParsedEdit, ParsedEventDraft, ParsedReminder


@pytest.fixture
def flow(monkeypatch):
    for name in ("pending_actions", "pending_event_drafts", "pending_event_conflicts",
                 "recent_reminder_lists", "pending_calendar_deletions", "processed_message_updates", "pending_media"):
        monkeypatch.setattr(main, name, {})
    settings = SimpleNamespace(timezone=ZoneInfo("Asia/Singapore"), user_timezone="Asia/Singapore",
        groq_vision_model="vision-model", groq_transcription_model="whisper-test",
        telegram_webhook_secret="secret", telegram_group_id=-100)
    telegram, calendar, parser = AsyncMock(), Mock(), Mock()
    calendar._list_events_between.return_value = []
    calendar.list_events.return_value = []
    calendar.list_events_for_day.return_value = []
    calendar.resolve_calendar.return_value = CalendarInfo(calendar_id="personal", name="Personal", access_role="owner")
    draft = ParsedEventDraft(title="Dinner", start="2030-10-12T19:00:00+08:00",
        end="2030-10-12T21:00:00+08:00", location="La Pasta", confidence="high")
    parser.parse_event.return_value = draft
    calendar.create_event.side_effect = lambda event, cid: CalendarEvent(
        event_id="dinner", calendar_id=cid, **event.model_dump(exclude={"action", "calendar_name"}))
    parser.parse_edit.side_effect = lambda text, event, tz: ParsedEdit(title=event.title,
        location=event.location, start=event.start.replace(hour=16), end=event.start.replace(hour=18), confidence="high")
    calendar.update_event.side_effect = lambda event, edit: CalendarEvent(event_id=event.event_id,
        calendar_id=event.calendar_id, **edit.model_dump(exclude={"action"}))
    parser.parse_reminder_for_event.return_value = ParsedReminder(reminder_minutes=60, confidence="high")
    telegram.download_media.return_value = b"media"
    telegram.download_file.return_value = b"media"
    monkeypatch.setattr(main, "interpret_media", AsyncMock(return_value="Dinner Saturday 7–9pm at La Pasta"))
    parser.transcribe.return_value = "Dinner Saturday 7–9pm at La Pasta"
    parser.suggest_media_event.return_value = MediaEventSuggestion(event_text="Dinner Saturday 7–9pm at La Pasta")
    monkeypatch.setattr(main, "dependencies", lambda: (settings, telegram, {123: calendar}, parser, None))

    async def send(text, chat_id=123):
        await main.handle_message(chat_id, text, settings, telegram, calendar, parser)
        return telegram.send_message.call_args.args[1]

    async def media(message=None):
        await main.handle_media_message(123, message or {"voice": {"file_id": "voice", "duration": 10}}, settings, telegram, calendar, parser)
        return telegram.send_message.call_args.args[1]

    return send, media, settings, telegram, calendar, parser


@pytest.mark.asyncio
async def test_followups_edit_exact_saved_event_and_attach_reminder(flow):
    send, _, _, _, calendar, parser = flow
    await send("Dinner Saturday 7–9pm at La Pasta")
    await send("Make it 4pm")
    assert calendar.update_event.call_args.args[0].event_id == "dinner"
    await send("remind me an hour before")
    assert calendar.set_reminder.call_args.args[0].start.hour == 16
    calendar.create_event.assert_called_once()
    parser.match_event.assert_not_called()


@pytest.mark.asyncio
async def test_date_only_correction_preserves_time_duration_and_location(flow):
    send, _, _, _, calendar, _ = flow
    await send("Dinner Saturday 7–9pm at La Pasta")
    await send("Actually 13 October 2030")
    event, edit = calendar.update_event.call_args.args
    assert edit.start.day == 13 and edit.start.hour == 19
    assert edit.end.hour == 21 and edit.location == "La Pasta"


@pytest.mark.asyncio
async def test_numbered_list_reference_and_ambiguous_pronoun(flow):
    send, _, _, _, calendar, parser = flow
    events = [CalendarEvent(event_id=str(i), title=f"Event {i}", calendar_id="personal",
        start=f"2030-10-12T{12+i}:00:00+08:00", end=f"2030-10-12T{13+i}:00:00+08:00") for i in (1, 2)]
    calendar.list_events.return_value = events
    await send("list")
    await send("move it to 4pm")
    calendar.update_event.assert_not_called()
    assert "Which event" in telegram_text(flow)
    await send("second")
    assert calendar.update_event.call_args.args[0].event_id == "2"
    parser.match_event.assert_not_called()
    await send("list")
    await send("move the second one to Friday")
    assert calendar.update_event.call_args.args[0].event_id == "2"


def telegram_text(flow):
    return flow[3].send_message.call_args.args[1]


@pytest.mark.asyncio
async def test_missing_or_expired_context_and_cross_user_do_not_write(flow):
    send, _, _, _, calendar, parser = flow
    assert "Which event" in await send("make it 4pm")
    await send("Dinner Saturday 7–9pm")
    assert "Which event" in await send("make it 4pm", 456)
    main.recent_events[123].expires_at = 0
    assert "Which event" in await send("make it 4pm")
    calendar.update_event.assert_not_called()
    parser.match_event.assert_not_called()


@pytest.mark.asyncio
async def test_deletion_of_reference_still_requires_confirmation(flow):
    send, _, _, _, calendar, _ = flow
    await send("Dinner Saturday 7–9pm")
    assert "Delete" in await send("delete it")
    calendar.delete_event_with_undo.assert_not_called()
    assert main.pending_actions[123].action == "delete_confirm"


@pytest.mark.asyncio
async def test_draft_survives_schedule_interruption_without_extending_expiry(flow):
    send, _, _, _, calendar, parser = flow
    complete = parser.parse_event.return_value
    parser.parse_event.return_value = complete.model_copy(update={"end": None, "missing_fields": ["duration"], "confidence": "low"})
    assert "For: Dinner" in await send("Dinner tomorrow 7pm")
    expiry = main.pending_event_drafts[123].expires_at
    assert "draft is still open" in await send("what are my plans tomorrow?")
    assert main.pending_event_drafts[123].expires_at == expiry
    calendar.create_event.assert_not_called()
    parser.parse_event.return_value = complete
    await send("2 hours")
    assert "User follow-up: 2 hours" in parser.parse_event.call_args.args[0]
    calendar.create_event.assert_called_once()


@pytest.mark.asyncio
async def test_voice_requires_confirmation_and_duplicate_taps_create_once(flow):
    _, media, settings, telegram, calendar, parser = flow
    assert "Suggested event" in await media()
    main.interpret_media.assert_awaited_once()
    calendar.create_event.assert_not_called()
    token = main.media_previews[123].token
    await asyncio.gather(*(main.handle_media_callback(123, "cb", f"media:{token}:add", settings, telegram, calendar) for _ in range(2)))
    calendar.create_event.assert_called_once()


@pytest.mark.asyncio
async def test_image_caption_and_corrections_keep_preview_gate(flow):
    send, media, _, _, calendar, parser = flow
    await media({"photo": [{"file_id": "small"}, {"file_id": "large"}], "caption": "My appointment"})
    assert "Caption: My appointment" in parser.suggest_media_event.call_args.args[0]
    assert parser.suggest_media_event.call_args.args[3:] == (None, "image/jpeg", "vision-model")
    old = main.media_previews[123].token
    await send("Actually Sunday")
    assert main.media_previews[123].token != old
    calendar.create_event.assert_not_called()
    await send("add event")
    calendar.create_event.assert_called_once()


@pytest.mark.asyncio
async def test_incomplete_media_answers_still_require_confirmation(flow):
    send, media, _, _, calendar, parser = flow
    complete = parser.parse_event.return_value
    parser.parse_event.return_value = complete.model_copy(update={"end": None, "missing_fields": ["duration"], "confidence": "low"})
    parser.suggest_media_event.return_value = MediaEventSuggestion(event_text="Dinner tomorrow 7pm")
    assert "How long" in await media()
    parser.parse_event.return_value = complete
    assert "Suggested event" in await send("2 hours")
    calendar.create_event.assert_not_called()


@pytest.mark.asyncio
async def test_media_confirm_rechecks_conflicts_despite_source_override(flow):
    _, media, settings, telegram, calendar, parser = flow
    parser.suggest_media_event.return_value = MediaEventSuggestion(event_text="add anyway Dinner Saturday 7–9pm")
    await media()
    calendar._list_events_between.return_value = [CalendarEvent(event_id="busy", title="Busy",
        start=main.media_previews[123].event.start, end=main.media_previews[123].event.end)]
    token = main.media_previews[123].token
    await main.handle_media_callback(123, "cb", f"media:{token}:add", settings, telegram, calendar)
    assert "overlaps" in telegram.send_message.call_args.args[1]
    calendar.create_event.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["expired", "cancel", "replacement", "stale"])
async def test_media_expiry_cancel_replacement_and_stale_buttons(flow, mode):
    send, media, settings, telegram, calendar, _ = flow
    await media()
    token = main.media_previews[123].token
    if mode == "expired":
        main.media_previews[123].expires_at = 0
    elif mode == "cancel":
        await send("/cancel")
    elif mode == "replacement":
        await send("list")
    else:
        await media()
    await main.handle_media_callback(123, "cb", f"media:{token}:add", settings, telegram, calendar)
    calendar.create_event.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [{"document": {"mime_type": "application/pdf"}},
    {"voice": {"file_id": "x", "duration": 301}},
    {"photo": [{"file_id": "x", "file_size": 11 * 1024 * 1024}]}])
async def test_unsupported_and_oversized_media_do_not_download(flow, message):
    _, media, _, telegram, calendar, _ = flow
    await media(message)
    telegram.download_media.assert_not_called()
    calendar.create_event.assert_not_called()


@pytest.mark.asyncio
async def test_no_event_and_provider_failure_do_not_write(flow):
    _, media, _, telegram, calendar, parser = flow
    parser.suggest_media_event.return_value = MediaEventSuggestion()
    assert "couldn't identify" in await media()
    parser.suggest_media_event.side_effect = RuntimeError("provider unavailable")
    assert "No event was added" in await media()
    assert not main.media_requests
    calendar.create_event.assert_not_called()


@pytest.mark.asyncio
async def test_image_without_config_does_not_download(flow):
    _, media, settings, telegram, _, _ = flow
    settings.groq_vision_model = None
    assert "GROQ_VISION_MODEL" in await media({"photo": [{"file_id": "x"}]})
    telegram.download_media.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("sender,kind,chat_id", [(999, "private", 999), (123, "group", -100), (123, "group", -999)])
async def test_media_webhook_authorization_and_group_read_only(flow, sender, kind, chat_id):
    _, _, _, telegram, calendar, parser = flow
    request = Mock()
    request.json = AsyncMock(return_value={"update_id": 100, "message": {
        "chat": {"id": chat_id, "type": kind}, "from": {"id": sender}, "voice": {"file_id": "x"}}})
    await main.webhook(request, "secret")
    telegram.download_media.assert_not_called()
    parser.transcribe.assert_not_called()
    calendar.create_event.assert_not_called()


@pytest.mark.asyncio
async def test_authorized_media_webhook_and_unauthorized_callback(flow):
    _, _, _, telegram, calendar, _ = flow
    request = Mock()
    update = {"update_id": 101, "message": {"chat": {"id": 123, "type": "private"},
        "from": {"id": 123}, "voice": {"file_id": "x"}}}
    request.json = AsyncMock(return_value=update)
    await main.webhook(request, "secret")
    token = main.media_previews[123].token
    request.json.return_value = {"callback_query": {"id": "cb", "from": {"id": 999},
        "message": {"chat": {"id": 123, "type": "private"}}, "data": f"media:{token}:add"}}
    await main.webhook(request, "secret")
    calendar.create_event.assert_not_called()
    assert telegram.answer_callback_query.call_args.args[1] == "Not available here."


@pytest.mark.asyncio
async def test_uncertain_contextual_edit_retains_target_for_answer_and_cancel(flow):
    send, _, _, _, calendar, parser = flow
    await send("Dinner Saturday 7–9pm")
    valid = parser.parse_edit.side_effect
    parser.parse_edit.side_effect = lambda *args: valid(*args).model_copy(update={"confidence": "low"})
    assert "What date or time" in await send("move it to Friday evening")
    assert main.pending_actions[123].action == "followup_edit"
    calendar.update_event.assert_not_called()
    parser.parse_edit.side_effect = valid
    await send("after 7pm for 2 hours")
    assert calendar.update_event.call_args.args[0].event_id == "dinner"
    assert "User follow-up: after 7pm" in parser.parse_edit.call_args.args[0]
    parser.parse_edit.side_effect = lambda *args: valid(*args).model_copy(update={"confidence": "low"})
    await send("move it to Friday evening")
    assert "cancelled" in await send("/cancel")
    assert 123 not in main.pending_actions


@pytest.mark.asyncio
async def test_remove_its_reminder_does_not_delete_event(flow):
    send, _, _, _, calendar, _ = flow
    await send("Dinner Saturday 7–9pm")
    await send("remove reminder for it")
    calendar.clear_reminder.assert_called_once()
    calendar.delete_event_with_undo.assert_not_called()
    assert 123 not in main.pending_actions


@pytest.mark.asyncio
async def test_parse_failure_retains_draft_for_safe_retry(flow):
    send, _, _, _, calendar, parser = flow
    parser.parse_event.side_effect = RuntimeError("temporarily unavailable")
    assert "draft is retained" in await send("Dinner Saturday 7–9pm")
    calendar.create_event.assert_not_called()
    parser.parse_event.side_effect = None
    await send("retry")
    calendar.create_event.assert_called_once()


@pytest.mark.asyncio
async def test_suggested_slots_preserve_duration_and_recheck_occupancy(flow):
    send, _, settings, telegram, calendar, _ = flow
    busy = CalendarEvent(event_id="busy", title="Busy", start="2030-10-12T19:00:00+08:00", end="2030-10-12T22:00:00+08:00")
    calendar._list_events_between.return_value = [busy]
    await send("Dinner on 12 October 2030 7–9pm")
    token = main.pending_event_conflicts[123].token
    await main.handle_conflict_callback(123, "cb", f"conflict:{token}:suggest", settings, telegram, calendar)
    pending = main.pending_event_conflicts[123]
    assert len(pending.suggestions) == 2
    assert pending.suggestions[0].start.hour == 22
    assert pending.suggestions[0].end - pending.suggestions[0].start == timedelta(hours=2)
    calendar.create_event.assert_not_called()
    selected = pending.suggestions[0]
    calendar._list_events_between.return_value = [busy.model_copy(update={"start": selected.start, "end": selected.end})]
    await main.handle_conflict_callback(123, "cb", f"conflict:{pending.token}:slot:0", settings, telegram, calendar)
    calendar.create_event.assert_not_called()
    assert "overlaps" in telegram.send_message.call_args.args[1]


@pytest.mark.asyncio
async def test_suggested_slot_can_be_saved_once(flow):
    send, _, settings, telegram, calendar, _ = flow
    calendar._list_events_between.return_value = [CalendarEvent(event_id="busy", title="Busy",
        start="2030-10-12T19:00:00+08:00", end="2030-10-12T22:00:00+08:00")]
    await send("Dinner on 12 October 2030 7–9pm")
    token = main.pending_event_conflicts[123].token
    await main.handle_conflict_callback(123, "cb", f"conflict:{token}:suggest", settings, telegram, calendar)
    token = main.pending_event_conflicts[123].token
    calendar._list_events_between.return_value = []
    await asyncio.gather(*(main.handle_conflict_callback(123, "cb", f"conflict:{token}:slot:1", settings, telegram, calendar) for _ in range(2)))
    calendar.create_event.assert_called_once()


@pytest.mark.asyncio
async def test_list_ordinals_remain_stable_after_edit_and_it_means_latest_event(flow):
    send, _, _, _, calendar, _ = flow
    calendar.list_events.return_value = [CalendarEvent(event_id=str(i), title=f"Event {i}", calendar_id="personal",
        start=f"2030-10-12T{12+i}:00:00+08:00", end=f"2030-10-12T{13+i}:00:00+08:00") for i in (1, 2)]
    await send("list")
    await send("move the second one to 4pm")
    await send("make it 5pm")
    assert calendar.update_event.call_args.args[0].event_id == "2"
    await send("delete the first one")
    assert main.pending_actions[123].events[0].event_id == "1"


@pytest.mark.asyncio
async def test_recurring_master_followup_requires_explicit_series_scope(flow):
    send, _, _, _, calendar, _ = flow
    await send("Dinner Saturday 7–9pm")
    event = main.recent_events[123].events[0]
    main._remember_event(123, event.model_copy(update={"recurring_event_id": event.event_id}))
    assert "whole recurring series" in await send("make it 4pm")
    calendar.update_event.assert_not_called()
    await send("delete the series")
    assert main.pending_actions[123].series
    calendar.delete_series.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_cancel_during_media_event_parsing_cannot_restore_or_create(flow, fail):
    send, media, _, _, calendar, parser = flow
    import threading
    started, finish = threading.Event(), threading.Event()
    complete = parser.parse_event.return_value

    def slow_parse(*args):
        started.set()
        finish.wait(timeout=5)
        if fail:
            raise RuntimeError("provider failed after cancellation")
        return complete

    parser.parse_event.side_effect = slow_parse
    task = asyncio.create_task(media())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        await send("/cancel")
    finally:
        finish.set()
        await task
    assert 123 not in main.media_previews
    assert 123 not in main.pending_event_drafts
    calendar.create_event.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_kind", ["edit", "remind"])
@pytest.mark.parametrize("replacement", ["edit", "remind", "delete"])
async def test_named_request_replaces_pending_followup_without_touching_old_event(flow, pending_kind, replacement):
    send, _, _, _, calendar, parser = flow
    await send("Dinner on 12 October 2030 7–9pm")
    dinner = main.recent_events[123].events[0]
    valid_edit = parser.parse_edit.side_effect
    if pending_kind == "edit":
        parser.parse_edit.side_effect = lambda *args: valid_edit(*args).model_copy(update={"confidence": "low"})
        await send("move it to Friday evening")
    else:
        parser.parse_reminder_for_event.return_value = ParsedReminder(reminder_minutes=60, confidence="low")
        await send("remind me before it")
    assert main.pending_actions[123].action == f"followup_{pending_kind}"
    dental = dinner.model_copy(update={"event_id": "dental", "title": "Dental"})
    calendar.list_events.return_value = [dinner, dental]
    from app.models import DeleteMatch
    parser.match_event.return_value = DeleteMatch(matched_event_id="dental", ambiguous=False)
    parser.parse_edit.side_effect = valid_edit
    parser.parse_reminder_for_event.return_value = ParsedReminder(reminder_minutes=30, confidence="high")
    parser.parse_edit.reset_mock()
    parser.parse_reminder_for_event.reset_mock()

    request = {"edit": "move Dental to Friday at 4pm", "remind": "set a reminder 30 minutes before Dental",
               "delete": "delete Dental"}[replacement]
    await send(request)
    if replacement == "edit":
        assert calendar.update_event.call_args.args[0].event_id == "dental"
        assert parser.parse_edit.call_args.args[0] == request
        calendar.set_reminder.assert_not_called()
    elif replacement == "remind":
        assert calendar.set_reminder.call_args.args[0].event_id == "dental"
        assert parser.parse_reminder_for_event.call_args.args[0] == request
        calendar.update_event.assert_not_called()
    else:
        assert main.pending_actions[123].action == "delete_confirm"
        assert main.pending_actions[123].events[0].event_id == "dental"
        calendar.update_event.assert_not_called()
        calendar.set_reminder.assert_not_called()
    calendar.delete_event_with_undo.assert_not_called()


@pytest.mark.asyncio
async def test_reminder_clarification_answer_keeps_pending_event(flow):
    send, _, _, _, calendar, parser = flow
    await send("Dinner on 12 October 2030 7–9pm")
    parser.parse_reminder_for_event.return_value = ParsedReminder(reminder_minutes=60, confidence="low")
    await send("remind me before it")
    parser.parse_reminder_for_event.return_value = ParsedReminder(reminder_minutes=30, confidence="high")
    await send("30 minutes before")
    assert calendar.set_reminder.call_args.args[0].event_id == "dinner"
    parser.match_event.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("lead_text,lead_minutes", [("15 minutes", 15), ("one hour", 60)])
async def test_media_conflict_change_time_retains_details_without_overlap_override(flow, lead_text, lead_minutes):
    send, media, settings, telegram, calendar, parser = flow
    source = f"Dinner every Saturday on 12 October 2030 7–9pm at La Pasta in Work calendar, remind me {lead_text} before to bring ID"
    parser.suggest_media_event.return_value = MediaEventSuggestion(event_text=source)
    parser.parse_event.return_value = parser.parse_event.return_value.model_copy(update={"calendar_name": "Work", "reminder_minutes": lead_minutes})
    await media()
    preview = main.media_previews[123]
    # Even a control phrase in the retained source must never skip confirmation's conflict check.
    preview.request_text += "\nUser follow-up: add anyway"
    original = preview.event
    calendar._list_events_between.return_value = [CalendarEvent(event_id="busy", title="Busy", start=original.start, end=original.end)]
    await main.handle_media_callback(123, "cb", f"media:{preview.token}:add", settings, telegram, calendar)
    calendar.create_event.assert_not_called()
    conflict = main.pending_event_conflicts[123]
    await main.handle_conflict_callback(123, "cb", f"conflict:{conflict.token}:change", settings, telegram, calendar)

    def parse_changed(text, *args):
        for detail in ("Dinner", "La Pasta", "Work", "every Saturday", f"{lead_text} before", "bring ID"):
            assert detail in text
        assert "add anyway" not in text.lower()
        assert "13 October 2030" in text
        return ParsedEventDraft(**original.model_dump(exclude={"action"})).model_copy(update={
            "start": original.start + timedelta(days=1), "end": original.end + timedelta(days=1)})

    parser.parse_event.side_effect = parse_changed
    await send("13 October 2030 7–9pm")
    calendar.create_event.assert_called_once()
    saved = calendar.create_event.call_args.args[0]
    assert (saved.title, saved.location, saved.calendar_name, saved.recurrence) == (
        original.title, original.location, original.calendar_name, original.recurrence)
    assert [(reminder.minutes_before, reminder.message) for reminder in saved.reminders] == [
        (reminder.minutes_before, reminder.message) for reminder in original.reminders]


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [False, True])
@pytest.mark.parametrize("duration_text,minutes", [("last 3 hours", 180), ("last 45 minutes", 45), ("last one hour", 60), ("last 90min", 90), ("last 1.5hours", 90), ("last 3h", 180)])
async def test_explicit_duration_edit_survives_context_and_selection(flow, selection, duration_text, minutes):
    send, _, _, _, calendar, parser = flow
    await send("Dinner on 12 October 2030 7–9pm")
    original = main.recent_events[123].events[0]
    if selection:
        calendar.list_events.return_value = [original, original.model_copy(update={"event_id": "other", "title": "Other"})]
        await send("list")
    parser.parse_edit.side_effect = lambda text, event, tz: ParsedEdit(title=event.title, location=event.location,
        start=event.start, end=event.start + timedelta(minutes=minutes), confidence="high")
    await send(f"change it to {duration_text}")
    if selection:
        await send("first")
    target, edited = calendar.update_event.call_args.args
    assert target.event_id == "dinner"
    assert edited.start == original.start
    assert edited.end - edited.start == timedelta(minutes=minutes)


@pytest.mark.asyncio
@pytest.mark.parametrize("title,new_request", [
    ("Dental", "remind me Dental 30 minutes before"),
    ("An Evening with Amelia", "set a reminder 30 minutes before An Evening with Amelia"),
    ("123 Workshop", "set a reminder 30 minutes before 123 Workshop"),
])
async def test_named_reminders_on_either_side_of_lead_time_replace_followup(flow, title, new_request):
    send, _, _, _, calendar, parser = flow
    await send("Dinner on 12 October 2030 7–9pm")
    dinner = main.recent_events[123].events[0]
    parser.parse_reminder_for_event.return_value = ParsedReminder(reminder_minutes=60, confidence="low")
    await send("remind me before it")
    target = dinner.model_copy(update={"event_id": "target", "title": title})
    calendar.list_events.return_value = [dinner, target]
    from app.models import DeleteMatch
    parser.match_event.return_value = DeleteMatch(matched_event_id="target", ambiguous=False)
    parser.parse_reminder_for_event.return_value = ParsedReminder(reminder_minutes=30, confidence="high")
    await send(new_request)
    assert calendar.set_reminder.call_args.args[0].event_id == "target"
    assert parser.parse_reminder_for_event.call_args.args[0] == new_request


@pytest.mark.asyncio
@pytest.mark.parametrize("context", ["none", "saved", "followup"])
@pytest.mark.parametrize("action", ["edit", "remind", "delete"])
@pytest.mark.parametrize("title", ["Bring It On", "It Takes Two", "That Show", "The Second One Rehearsal"])
async def test_titles_containing_references_use_named_event_matching(flow, context, action, title):
    send, _, _, _, calendar, parser = flow
    dinner = CalendarEvent(event_id="dinner", calendar_id="personal", title="Dinner",
        start="2030-10-12T19:00:00+08:00", end="2030-10-12T21:00:00+08:00")
    if context != "none":
        await send("Dinner on 12 October 2030 7–9pm")
        dinner = main.recent_events[123].events[0]
    if context == "followup":
        parser.parse_reminder_for_event.return_value = ParsedReminder(reminder_minutes=60, confidence="low")
        await send("remind me before it")
        assert main.pending_actions[123].action == "followup_remind"
    target = dinner.model_copy(update={"event_id": "named", "title": title})
    calendar.list_events.return_value = [dinner, target]
    from app.models import DeleteMatch
    parser.match_event.return_value = DeleteMatch(matched_event_id="named", ambiguous=False)
    parser.parse_reminder_for_event.return_value = ParsedReminder(reminder_minutes=30, confidence="high")
    message = {"edit": f"move {title} to Friday at 4pm", "remind": f"set a reminder 30 minutes before {title}",
               "delete": f"delete {title}"}[action]
    await send(message)
    if action == "edit":
        assert calendar.update_event.call_args.args[0].event_id == "named"
        assert parser.match_event.call_args.args[0] == message
        calendar.set_reminder.assert_not_called()
    elif action == "remind":
        assert calendar.set_reminder.call_args.args[0].event_id == "named"
        assert parser.match_event.call_args.args[0] == message
        calendar.update_event.assert_not_called()
    else:
        assert main.pending_actions[123].action == "delete_confirm"
        assert main.pending_actions[123].events[0].event_id == "named"
        calendar.update_event.assert_not_called()
        calendar.set_reminder.assert_not_called()
    calendar.delete_event_with_undo.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("word,hour", [("noon", 12), ("midnight", 0), ("midday", 12)])
@pytest.mark.parametrize("selection", [False, True])
async def test_word_clock_corrections_keep_parsed_time(flow, word, hour, selection):
    send, _, _, _, calendar, parser = flow
    await send("Dinner on 12 October 2030 7–9pm")
    dinner = main.recent_events[123].events[0]
    if selection:
        calendar.list_events.return_value = [dinner, dinner.model_copy(update={"event_id": "other", "title": "Other"})]
        await send("list")
    start = dinner.start.replace(hour=hour)
    parser.parse_edit.side_effect = lambda *args: ParsedEdit(title=dinner.title, location=dinner.location,
        start=start, end=start + (dinner.end - dinner.start), confidence="high")
    await send(f"make it {word}")
    if selection:
        await send("first")
    target, edited = calendar.update_event.call_args.args
    assert target.event_id == "dinner"
    assert edited.start == start
    assert edited.end - edited.start == dinner.end - dinner.start
    assert edited.location == dinner.location


@pytest.mark.asyncio
async def test_ordinal_in_new_details_does_not_replace_pronoun_target(flow):
    send, _, _, _, calendar, _ = flow
    await send("Dinner on 12 October 2030 7–9pm")
    dinner = main.recent_events[123].events[0]
    calendar.list_events.return_value = [dinner, dinner.model_copy(update={"event_id": "other", "title": "Other"})]
    await send("list")
    await send("move it to 4pm at The Second One")
    assert main.pending_actions[123].action == "edit"
    await send("first")
    assert calendar.update_event.call_args.args[0].event_id == "dinner"


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["set a reminder for it 30 minutes before", "remind me 30 minutes before it"])
async def test_exact_reminder_pronoun_retains_saved_target(flow, message):
    send, _, _, _, calendar, parser = flow
    await send("Dinner on 12 October 2030 7–9pm")
    await send(message)
    assert calendar.set_reminder.call_args.args[0].event_id == "dinner"
    parser.match_event.assert_not_called()


@pytest.mark.asyncio
async def test_media_word_clock_correction_supersedes_original_numeric_range(flow):
    send, media, _, _, calendar, parser = flow
    await media()
    original = main.media_previews[123].event
    start = original.start.replace(hour=12)
    parser.parse_event.return_value = ParsedEventDraft(**original.model_dump(exclude={"action"})).model_copy(
        update={"start": start, "end": start + timedelta(hours=2)})
    await send("actually noon")
    assert main.media_previews[123].event.start == start
    calendar.create_event.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_kind,message", [("remind", "move it to 4pm"), ("edit", "remind me an hour before")])
async def test_expired_followup_action_switch_never_restores_target(flow, monkeypatch, pending_kind, message):
    send, _, _, _, calendar, parser = flow
    clock = [1000.0]
    monkeypatch.setattr(main, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    await send("Dinner on 12 October 2030 7–9pm")
    dinner = main.recent_events[123].events[0]
    calendar.list_events.return_value = [dinner]
    valid_edit = parser.parse_edit.side_effect
    if pending_kind == "remind":
        parser.parse_reminder_for_event.return_value = ParsedReminder(reminder_minutes=60, confidence="low")
        await send("remind me before it")
    else:
        parser.parse_edit.side_effect = lambda *args: valid_edit(*args).model_copy(update={"confidence": "low"})
        await send("move it to Friday evening")
    assert main.pending_actions[123].action == f"followup_{pending_kind}"
    clock[0] += main.PENDING_TTL_SECONDS + 1
    parser.parse_edit.side_effect = valid_edit
    parser.parse_reminder_for_event.return_value = ParsedReminder(reminder_minutes=60, confidence="high")
    answer = await send(message)
    assert "Which event" in answer or "expired" in answer
    assert 123 not in main.recent_events
    assert 123 not in main.pending_actions
    calendar.update_event.assert_not_called()
    calendar.set_reminder.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_kind", ["edit", "remind"])
@pytest.mark.parametrize("action", ["edit", "remind", "delete"])
async def test_expired_followup_allows_fresh_named_request(flow, monkeypatch, pending_kind, action):
    send, _, _, _, calendar, parser = flow
    clock = [1000.0]
    monkeypatch.setattr(main, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    await send("Dinner on 12 October 2030 7–9pm")
    dinner = main.recent_events[123].events[0]
    valid_edit = parser.parse_edit.side_effect
    if pending_kind == "remind":
        parser.parse_reminder_for_event.return_value = ParsedReminder(reminder_minutes=60, confidence="low")
        await send("remind me before it")
    else:
        parser.parse_edit.side_effect = lambda *args: valid_edit(*args).model_copy(update={"confidence": "low"})
        await send("move it to Friday evening")
    clock[0] += main.PENDING_TTL_SECONDS + 1
    dental = dinner.model_copy(update={"event_id": "dental", "title": "Dental"})
    calendar.list_events.return_value = [dental]
    from app.models import DeleteMatch
    parser.match_event.return_value = DeleteMatch(matched_event_id="dental", ambiguous=False)
    parser.parse_edit.side_effect = valid_edit
    parser.parse_reminder_for_event.return_value = ParsedReminder(reminder_minutes=30, confidence="high")
    await send({"edit": "move Dental to Friday at 4pm", "remind": "set a reminder 30 minutes before Dental",
                "delete": "delete Dental"}[action])
    if action == "edit":
        assert calendar.update_event.call_args.args[0].event_id == "dental"
    elif action == "remind":
        assert calendar.set_reminder.call_args.args[0].event_id == "dental"
    else:
        assert main.pending_actions[123].action == "delete_confirm"
        assert main.pending_actions[123].events[0].event_id == "dental"
    calendar.delete_event_with_undo.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,hour,end_hour,day", [
    ("actually noon", 12, 14, 12),
    ("actually 4pm", 16, 18, 12),
    ("actually 8–10pm", 20, 22, 12),
    ("actually 13 October 2030 8–10pm", 20, 22, 13),
    ("actually 3h", 19, 22, 12),
])
async def test_latest_edit_clarification_replaces_retained_range(flow, answer, hour, end_hour, day):
    send, _, _, _, calendar, parser = flow
    await send("Dinner on 12 October 2030 7–9pm")
    original = main.recent_events[123].events[0]
    parser.parse_edit.side_effect = None
    parser.parse_edit.return_value = ParsedEdit(title=original.title, location=original.location,
        start=original.start, end=original.end, confidence="low")
    await send("move it to 12 October 2030 7–9pm")
    assert main.pending_actions[123].action == "followup_edit"
    parser.parse_edit.return_value = ParsedEdit(title=original.title, location=original.location,
        start=original.start.replace(day=day, hour=hour), end=original.end.replace(day=day, hour=end_hour), confidence="high")
    await send(answer)
    calendar.update_event.assert_called_once()
    target, edited = calendar.update_event.call_args.args
    assert target.event_id == original.event_id
    assert (edited.start.day, edited.start.hour, edited.end.day, edited.end.hour) == (day, hour, day, end_hour)
    assert edited.location == original.location
    assert answer in parser.parse_edit.call_args.args[0]
