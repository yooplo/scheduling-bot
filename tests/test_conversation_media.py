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
