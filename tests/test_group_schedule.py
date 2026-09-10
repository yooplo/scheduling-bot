from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.config import CalendarAccount
from app.main import _calendar_date_from_text, handle_group_schedule, handle_schedule_callback, pending_group_schedule_dates
from app.models import CalendarEvent


@pytest.mark.asyncio
async def test_group_schedule_reads_mentioned_users_calendar_with_details():
    class Telegram:
        messages = []
        callbacks = []

        async def send_message(self, chat_id, text, reply_markup=None):
            self.messages.append((chat_id, text, reply_markup))

        async def answer_callback_query(self, callback_id, text=None):
            self.callbacks.append((callback_id, text))

    class Calendar:
        def list_events_for_day(self, _day):
            return [CalendarEvent(
                event_id="1", title="Project meeting",
                start=datetime.fromisoformat("2026-09-05T13:00:00+08:00"),
                end=datetime.fromisoformat("2026-09-05T14:30:00+08:00"),
                location="Office", calendar_name="Work",
            )]

    accounts = (
        CalendarAccount(111, "token-1", "primary", "alice"),
        CalendarAccount(222, "token-2", "primary", "juzteeeen"),
    )
    settings = SimpleNamespace(
        calendar_accounts=accounts,
        timezone=ZoneInfo("Asia/Singapore"),
        account_for=lambda user_id: next((a for a in accounts if a.telegram_user_id == user_id), None),
    )
    telegram = Telegram()

    await handle_group_schedule(
        -100123, 111, "check @juzteeeen schedule tmr @bot",
        settings, telegram, {111: Calendar(), 222: Calendar()},
    )

    reply = telegram.messages[0][1]
    assert "@juzteeeen" in reply
    assert "Project meeting" in reply
    assert "Office" in reply
    assert "Work" in reply


@pytest.mark.asyncio
async def test_bare_schedule_command_offers_user_then_day_buttons():
    class Telegram:
        def __init__(self):
            self.messages = []
            self.callbacks = []

        async def send_message(self, chat_id, text, reply_markup=None):
            self.messages.append((chat_id, text, reply_markup))

        async def answer_callback_query(self, callback_id, text=None):
            self.callbacks.append((callback_id, text))

    accounts = (
        CalendarAccount(111, "token-1", "primary", "alice"),
        CalendarAccount(222, "token-2", "primary", "amemefoo"),
    )
    settings = SimpleNamespace(
        calendar_accounts=accounts,
        timezone=ZoneInfo("Asia/Singapore"),
        account_for=lambda user_id: next((a for a in accounts if a.telegram_user_id == user_id), None),
    )
    telegram = Telegram()

    await handle_group_schedule(-100123, 111, "/schedule@the_scheduling_bot", settings, telegram, {})
    assert telegram.messages[0][1] == "Whose schedule?"
    assert telegram.messages[0][2]["inline_keyboard"][1][0]["callback_data"] == "schedule:user:222"

    await handle_schedule_callback(-100123, 111, "callback-1", "schedule:user:222", settings, telegram, {222: object()})
    assert telegram.callbacks == [("callback-1", None)]
    assert telegram.messages[1][1] == "Which day?"
    assert telegram.messages[1][2]["inline_keyboard"][0][1]["callback_data"] == "schedule:day:222:tomorrow"
    assert all(button["text"] != "Next 7 days" for row in telegram.messages[1][2]["inline_keyboard"] for button in row)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["13 Sept", "13 sept", "13 SEPT", "13 Sep", "13 September", "13th Sept 2026"])
async def test_specific_date_button_prompts_for_and_reads_a_date(reply):
    class Telegram:
        def __init__(self):
            self.messages = []

        async def send_message(self, chat_id, text, reply_markup=None):
            self.messages.append((chat_id, text, reply_markup))

        async def answer_callback_query(self, _callback_id, text=None):
            pass

    class Calendar:
        requested_day = None

        def list_events_for_day(self, day):
            self.requested_day = day
            return []

    account = CalendarAccount(222, "token", "primary", "amemefoo")
    settings = SimpleNamespace(
        calendar_accounts=(account,), timezone=ZoneInfo("Asia/Singapore"),
        account_for=lambda user_id: account if user_id == 222 else None,
    )
    telegram, calendar = Telegram(), Calendar()
    try:
        await handle_schedule_callback(-100123, 111, "callback-2", "schedule:day:222:specific", settings, telegram, {222: calendar})
        assert telegram.messages[0][2] == {"force_reply": True}

        await handle_group_schedule(-100123, 111, reply, settings, telegram, {222: calendar})
        year = 2026 if "2026" in reply else datetime.now(settings.timezone).year
        assert calendar.requested_day == date(year, 9, 13)
        assert f"@amemefoo — {calendar.requested_day:%A, %d %B}" in telegram.messages[1][1]
        assert (-100123, 111) not in pending_group_schedule_dates
    finally:
        pending_group_schedule_dates.pop((-100123, 111), None)


def test_sept_abbreviation_still_rejects_invalid_dates():
    settings = SimpleNamespace(timezone=ZoneInfo("Asia/Singapore"))
    assert _calendar_date_from_text("31 Sept 2026", settings) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/schedule", "/schedule@the_scheduling_bot", "/schedule @alice 13 Sept 2030", "/help"])
async def test_command_cancels_pending_specific_date(command):
    from unittest.mock import AsyncMock, Mock

    accounts = (
        CalendarAccount(111, "token-1", "primary", "alice"),
        CalendarAccount(222, "token-2", "primary", "bob"),
    )
    settings = SimpleNamespace(
        calendar_accounts=accounts, timezone=ZoneInfo("Asia/Singapore"),
        account_for=lambda user_id: next((a for a in accounts if a.telegram_user_id == user_id), None),
    )
    telegram = AsyncMock()
    calendar = Mock()
    calendar.list_events_for_day.return_value = []
    try:
        await handle_schedule_callback(-100123, 111, "cb", "schedule:day:222:specific", settings, telegram, {222: calendar})
        other_pending = (111, float("inf"))
        pending_group_schedule_dates[(-100123, 222)] = other_pending
        await handle_group_schedule(-100123, 111, command, settings, telegram, {111: calendar, 222: calendar})
        assert (-100123, 111) not in pending_group_schedule_dates
        assert pending_group_schedule_dates[(-100123, 222)] == other_pending
        reply = telegram.send_message.call_args.args[1]
        if "2030" in command:
            calendar.list_events_for_day.assert_called_once_with(date(2030, 9, 13))
            assert reply.startswith("@alice")
        else:
            calendar.list_events_for_day.assert_not_called()
            assert reply == ("Group access is read-only. Ask me to check a schedule." if command == "/help" else "Whose schedule?")
    finally:
        pending_group_schedule_dates.pop((-100123, 111), None)
        pending_group_schedule_dates.pop((-100123, 222), None)
