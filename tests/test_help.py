from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import app.main as main
from app.config import CalendarAccount


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/help", "/help@the_scheduling_bot"])
async def test_private_help_menu_clears_pending_choices_without_external_operations(command):
    telegram, calendar, parser = AsyncMock(), Mock(), Mock()
    stores = [main.pending_event_drafts, main.pending_event_conflicts, main.pending_actions,
              main.pending_calendar_deletions, main.recent_reminder_lists]
    try:
        for store in stores:
            store[123] = object()
        await main.handle_message(123, command, SimpleNamespace(), telegram, calendar, parser)
        buttons = telegram.send_message.call_args.args[2]["inline_keyboard"]
        assert [row[0]["text"] for row in buttons] == ["Events", "Reminders", "Availability", "Calendars"]
        assert all(123 not in store for store in stores)
        assert not calendar.mock_calls and not parser.mock_calls
    finally:
        for store in stores:
            store.pop(123, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("topic", ["menu", "events", "reminders", "availability", "calendars", "unknown"])
async def test_help_callback_shows_examples_and_back_without_changing_state(monkeypatch, topic):
    telegram, calendar, parser = AsyncMock(), Mock(), Mock()
    settings = SimpleNamespace(telegram_webhook_secret="secret")
    monkeypatch.setattr(main, "dependencies", lambda: (settings, telegram, {123:calendar}, parser, None))
    request = AsyncMock()
    request.json.return_value = {"callback_query": {"id":"cb", "data":f"help:{topic}",
        "from":{"id":123}, "message":{"chat":{"id":123,"type":"private"}}}}
    pending = object()
    main.pending_event_drafts[123] = pending
    try:
        await main.webhook(request, "secret")
        telegram.answer_callback_query.assert_awaited_once_with("cb")
        assert main.pending_event_drafts[123] is pending
        assert not calendar.mock_calls and not parser.mock_calls
        if topic == "unknown":
            telegram.send_message.assert_not_awaited()
        else:
            text, markup = telegram.send_message.call_args.args[1:]
            assert len(text) < 3900
            if topic != "menu":
                assert markup == {"inline_keyboard":[[{"text":"Back","callback_data":"help:menu"}]]}
                assert "\n\n" in text
    finally:
        main.pending_event_drafts.pop(123, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type,chat_id,sender", [("group",-100,123), ("private",123,456), ("private",789,789)])
async def test_private_help_callbacks_reject_other_contexts(monkeypatch, chat_type, chat_id, sender):
    telegram = AsyncMock()
    monkeypatch.setattr(main,"dependencies",lambda:(SimpleNamespace(telegram_webhook_secret="secret"),telegram,{123:object(),456:object()},object(),None))
    request=AsyncMock()
    request.json.return_value={"callback_query":{"id":"cb","data":"help:events","from":{"id":sender},"message":{"chat":{"id":chat_id,"type":chat_type}}}}
    await main.webhook(request,"secret")
    telegram.send_message.assert_not_awaited()
    telegram.answer_callback_query.assert_awaited_once_with("cb","Not available here.")


@pytest.mark.asyncio
async def test_group_help_shows_only_schedules_and_preserves_other_users_prompt():
    telegram=AsyncMock()
    settings=SimpleNamespace(calendar_accounts=(CalendarAccount(123,"token","primary","alice"),))
    main.pending_group_schedule_dates[(-100,123)]=(123,float("inf"))
    main.pending_group_schedule_dates[(-100,456)]=(123,float("inf"))
    try:
        await main.handle_group_schedule(-100,123,"/help@bot",settings,telegram,{})
        text,markup=telegram.send_message.call_args.args[1:]
        assert "read-only" in text and "Yesterday / Tomorrow" in text
        assert "/reminders" not in text and "delete" not in text
        assert markup["inline_keyboard"][0][0]["callback_data"]=="schedule:user:123"
        assert (-100,123) not in main.pending_group_schedule_dates
        assert (-100,456) in main.pending_group_schedule_dates
    finally:
        main.pending_group_schedule_dates.pop((-100,123),None)
        main.pending_group_schedule_dates.pop((-100,456),None)


def test_welcome_advertises_help():
    assert "/help" in main._welcome_message("Alice")
