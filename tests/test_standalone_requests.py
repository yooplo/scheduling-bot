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
