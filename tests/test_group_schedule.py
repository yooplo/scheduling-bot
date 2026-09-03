from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.config import CalendarAccount
from app.main import handle_group_schedule, handle_schedule_callback
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

    await handle_schedule_callback(-100123, "callback-1", "schedule:user:222", settings, telegram, {222: object()})
    assert telegram.callbacks == [("callback-1", None)]
    assert telegram.messages[1][1] == "Which day?"
    assert telegram.messages[1][2]["inline_keyboard"][0][1]["callback_data"] == "schedule:day:222:tomorrow"
