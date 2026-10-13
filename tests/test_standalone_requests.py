from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

import app.main as main
from app.models import ParsedStandaloneReminder


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["remind me", "set a reminder"])
@pytest.mark.parametrize("day", ["tmr", "tomorrow", "today", "tonight"])
async def test_relative_day_reminder_routes_and_resolves_local_date(monkeypatch, command, day):
    now = datetime.fromisoformat("2026-09-13T22:20:00+08:00")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr(main, "datetime", Clock)
    settings = SimpleNamespace(timezone=ZoneInfo("Asia/Singapore"), user_timezone="Asia/Singapore")
    telegram, calendar, parser, cron = AsyncMock(), Mock(), Mock(), AsyncMock()
    parser.parse_standalone_reminder.return_value = ParsedStandaloneReminder(
        message="collect shopee parcel at j9", due_at="2026-09-13T12:00:00+08:00", confidence="high",
    )
    await main.handle_message(
        123, f"{command} {day} at around 12pm to collect shopee parcel at j9",
        settings, telegram, calendar, parser, cron,
    )
    parser.parse_standalone_reminder.assert_called_once()
    parser.match_event.assert_not_called()
    assert not calendar.mock_calls
    assert 123 not in main.pending_actions
    if day in {"tmr", "tomorrow"}:
        cron.create_reminder.assert_awaited_once_with(
            123, "collect shopee parcel at j9", datetime.fromisoformat("2026-09-14T12:00:00+08:00"),
        )
        assert "Mon 14 Sep 12:00 PM" in telegram.send_message.call_args.args[1]
    else:
        cron.create_reminder.assert_not_awaited()
        assert "past" in telegram.send_message.call_args.args[1]


def test_event_lead_time_still_routes_to_event_selection():
    assert not main._is_standalone_reminder_request("remind me 15 minutes before Dental tomorrow")


@pytest.mark.asyncio
@pytest.mark.parametrize('text,now_text,expected', [
    ("remind me to Bring Darren’s Paddle on Friday 8pm", '2026-09-30T23:47:00+08:00', '2026-10-02T20:00:00+08:00'),
    ("remind me on Friday at 8pm to Bring Darren’s Paddle", '2026-09-30T23:47:00+08:00', '2026-10-02T20:00:00+08:00'),
    ("remind me to bring paddle next Friday 8pm", '2026-10-02T10:00:00+08:00', '2026-10-09T20:00:00+08:00'),
    ("remind me to bring paddle this Friday 8pm", '2026-10-02T10:00:00+08:00', '2026-10-02T20:00:00+08:00'),
    ("remind me to bring paddle on Friday 8pm", '2026-10-02T23:00:00+08:00', None),
])
async def test_weekday_reminder_corrects_wrong_parser_date(monkeypatch, text, now_text, expected):
    now = datetime.fromisoformat(now_text)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr(main, 'datetime', Clock)
    settings = SimpleNamespace(timezone=ZoneInfo('Asia/Singapore'), user_timezone='Asia/Singapore')
    telegram, calendar, parser, cron = AsyncMock(), Mock(), Mock(), AsyncMock()
    parser.parse_standalone_reminder.return_value = ParsedStandaloneReminder(
        message='Bring Darren’s Paddle', due_at='2026-10-01T12:00:00Z', confidence='high',
    )
    await main.handle_message(123, text, settings, telegram, calendar, parser, cron)
    assert not calendar.mock_calls
    parser.match_event.assert_not_called()
    if expected:
        cron.create_reminder.assert_awaited_once_with(123, 'Bring Darren’s Paddle', datetime.fromisoformat(expected))
        assert 'Fri' in telegram.send_message.call_args.args[1]
    else:
        cron.create_reminder.assert_not_awaited()
        assert 'past' in telegram.send_message.call_args.args[1]


@pytest.mark.parametrize("text", [
    "remind me to bring paddle last Friday 8pm",
    "remind me to bring paddle on 9 October Friday 8pm",
    "remind me at 8pm on Friday 9 October to bring paddle",
])
def test_qualified_weekday_reminder_preserves_parser_date(text):
    reminder = ParsedStandaloneReminder(message="bring paddle", due_at="2026-10-09T20:00:00+08:00", confidence="high")
    original = reminder.due_at
    main._apply_standalone_clock_from_text(reminder, text, datetime.fromisoformat("2026-09-30T23:47:00+08:00"))
    assert reminder.due_at == original

@pytest.mark.asyncio
@pytest.mark.parametrize('context', ['none', 'draft', 'expired draft', 'saved edit', 'expired saved edit'])
@pytest.mark.parametrize('now_text', ['2026-10-12T23:15:00+08:00', '2026-10-13T11:35:00+08:00'])
async def test_new_independent_reminder_replaces_pending_event_flow(monkeypatch, context, now_text):
    import time
    from app.models import CalendarEvent, ParsedEventDraft
    now = datetime.fromisoformat(now_text)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)
    monkeypatch.setattr(main, 'datetime', Clock)
    for name in ('pending_actions', 'pending_event_drafts', 'pending_event_conflicts', 'recent_reminder_lists', 'pending_calendar_deletions'):
        monkeypatch.setattr(main, name, {})
    if 'draft' in context:
        main.pending_event_drafts[123] = main.PendingEventDraft('Dentist tomorrow at 2pm', now,
            0 if context == 'expired draft' else time.monotonic() + 300)
    elif 'saved edit' in context:
        event = CalendarEvent(event_id='saved', title='Drills', start='2026-10-13T20:00:00+08:00', end='2026-10-13T22:00:00+08:00')
        main.pending_actions[123] = main.PendingAction([event], 0 if context == 'expired saved edit' else time.monotonic()+300, 'created_edit', '', 'old')
    settings = SimpleNamespace(timezone=ZoneInfo('Asia/Singapore'), user_timezone='Asia/Singapore')
    telegram, calendar, parser, cron = AsyncMock(), Mock(), Mock(), AsyncMock()
    text = 'remind me tmr at 9am to do the pf1101a group thing'
    parser.parse_standalone_reminder.return_value = ParsedStandaloneReminder(
        message='do the pf1101a group thing', due_at='2026-10-13T09:00:00+08:00', confidence='high')
    parser.parse_event.return_value = ParsedEventDraft(title='Task', start='2026-10-14T09:00:00+08:00', end=None,
        missing_fields=['duration'], confidence='low')
    await main.handle_message(123, text, settings, telegram, calendar, parser, cron)
    parser.parse_standalone_reminder.assert_called_once_with(text, now, 'Asia/Singapore')
    parser.parse_event.assert_not_called()
    parser.parse_edit.assert_not_called()
    parser.match_event.assert_not_called()
    assert not calendar.mock_calls
    cron.create_reminder.assert_awaited_once_with(123, 'do the pf1101a group thing',
        (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0))
    assert ('Tue 13 Oct 9:00 AM' if now.day == 12 else 'Wed 14 Oct 9:00 AM') in telegram.send_message.call_args.args[1]
    assert 123 not in main.pending_event_drafts and 123 not in main.pending_actions

@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['unconfigured', 'unclear', 'scheduler'])
async def test_failed_new_reminder_does_not_resume_event_draft(monkeypatch, failure):
    import time
    monkeypatch.setattr(main, 'pending_event_drafts', {})
    monkeypatch.setattr(main, 'pending_actions', {})
    now = datetime.now(ZoneInfo('Asia/Singapore'))
    main.pending_event_drafts[123] = main.PendingEventDraft('Dentist tomorrow at 2pm', now, time.monotonic()+300)
    settings = SimpleNamespace(timezone=ZoneInfo('Asia/Singapore'), user_timezone='Asia/Singapore')
    telegram, calendar, parser, cron = AsyncMock(), Mock(), Mock(), AsyncMock()
    parser.parse_standalone_reminder.return_value = ParsedStandaloneReminder(
        message='do the pf1101a group thing', due_at=now+timedelta(days=1),
        confidence='low' if failure == 'unclear' else 'high')
    if failure == 'scheduler':
        cron.create_reminder.side_effect = RuntimeError('unavailable')
        with pytest.raises(RuntimeError, match='unavailable'):
            await main.handle_message(123, 'remind me tmr at 9am to do the pf1101a group thing', settings, telegram, calendar, parser, cron)
    else:
        await main.handle_message(123, 'remind me tmr at 9am to do the pf1101a group thing',
            settings, telegram, calendar, parser, None if failure == 'unconfigured' else cron)
        cron.create_reminder.assert_not_awaited()
        assert 'How long?' not in telegram.send_message.call_args.args[1]
    assert 123 not in main.pending_event_drafts
    parser.parse_event.assert_not_called()
    assert not calendar.mock_calls
