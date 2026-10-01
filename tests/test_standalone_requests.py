from datetime import datetime
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
