from types import SimpleNamespace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.main import handle_message, pending_calendar_deletions
from app.models import CalendarInfo, ReminderSpec, ScheduledReminder


class FakeTelegram:
    def __init__(self):
        self.messages = []

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))


@pytest.mark.asyncio
async def test_calendar_deletion_requires_confirmation():
    target = CalendarInfo(calendar_id="school-id", name="School", access_role="owner")

    class FakeCalendar:
        def __init__(self):
            self.deleted = []

        def resolve_calendar(self, name):
            return target if name.casefold() == "school" else None

        def delete_calendar(self, calendar):
            self.deleted.append(calendar.calendar_id)

    chat_id = 984321
    telegram = FakeTelegram()
    calendar = FakeCalendar()
    settings = SimpleNamespace()
    try:
        await handle_message(chat_id, "delete calendar School", settings, telegram, calendar, object())
        assert calendar.deleted == []
        assert "confirm delete calendar" in telegram.messages[-1][1]

        await handle_message(chat_id, "confirm delete calendar", settings, telegram, calendar, object())
        assert calendar.deleted == ["school-id"]
        assert telegram.messages[-1][1] == "✅ Deleted calendar: School"
    finally:
        pending_calendar_deletions.pop(chat_id, None)


@pytest.mark.asyncio
async def test_reminders_still_list_when_one_source_is_unavailable():
    class FailingCalendar:
        def list_reminders(self, _days):
            raise RuntimeError("Google unavailable")

    class WorkingCron:
        async def list_reminders(self, _chat_id):
            return [ScheduledReminder(
                event_id="cron:1", reminder=ReminderSpec(message="Pay bill"),
                due_at=datetime.now(ZoneInfo("Asia/Singapore")) + timedelta(hours=1), standalone=True,
            )]

    telegram = FakeTelegram()
    settings = SimpleNamespace(timezone=ZoneInfo("Asia/Singapore"))

    await handle_message(765432, "/reminders", settings, telegram, FailingCalendar(), object(), WorkingCron())

    reply = telegram.messages[-1][1]
    assert "Pay bill" in reply
    assert "Could not retrieve: Google Calendar" in reply


@pytest.mark.asyncio
async def test_now_slash_command_uses_configured_timezone():
    telegram = FakeTelegram()
    settings = SimpleNamespace(timezone=ZoneInfo("Asia/Singapore"), user_timezone="Asia/Singapore")

    await handle_message(123456, "/now@SchedulingBot", settings, telegram, object(), object())

    assert telegram.messages[-1][1].startswith("🕒 ")
    assert "Asia/Singapore" in telegram.messages[-1][1]
