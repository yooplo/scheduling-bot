import asyncio
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

import app.main as main
from app.models import CalendarEvent, ParsedEdit, ReminderSpec, ScheduledReminder


@pytest.fixture
def settings():
    yield SimpleNamespace(timezone=ZoneInfo("Asia/Singapore"), user_timezone="Asia/Singapore",
                          telegram_webhook_secret="secret", telegram_group_id=-100)
    main.processed_message_updates.clear()
    main.pending_event_conflicts.pop(123, None)
    main.reminder_delivery_history.clear()


@pytest.mark.asyncio
async def test_duplicate_messages_execute_once_including_concurrent_delivery(monkeypatch, settings):
    telegram, calendar, parser = AsyncMock(), Mock(), Mock()
    handler = AsyncMock()
    monkeypatch.setattr(main, "handle_message", handler)
    monkeypatch.setattr(main, "dependencies", lambda: (settings, telegram, {123:calendar}, parser, None))
    request = AsyncMock()
    request.json.return_value = {"update_id": 42, "message": {"chat": {"id":123,"type":"private"}, "from":{"id":123}, "text":"Dentist tomorrow 2–3pm"}}
    await asyncio.gather(main.webhook(request, "secret"), main.webhook(request, "secret"))
    handler.assert_awaited_once()
    request.json.return_value["update_id"] = 43
    await main.webhook(request, "secret")
    assert handler.await_count == 2


@pytest.mark.asyncio
async def test_failed_message_is_not_replayed_and_expired_id_can_run(monkeypatch, settings):
    telegram, handler = AsyncMock(), AsyncMock(side_effect=RuntimeError("write may have succeeded"))
    monkeypatch.setattr(main, "handle_message", handler)
    monkeypatch.setattr(main, "dependencies", lambda: (settings, telegram, {123:object()}, object(), None))
    request = AsyncMock()
    request.json.return_value = {"update_id":42,"message":{"chat":{"id":123,"type":"private"},"from":{"id":123},"text":"add event"}}
    await main.webhook(request,"secret")
    await main.webhook(request,"secret")
    handler.assert_awaited_once()
    main.processed_message_updates[(123,42)] = 0
    await main.webhook(request,"secret")
    assert handler.await_count == 2


@pytest.mark.asyncio
async def test_unauthorized_update_does_not_poison_deduplication(monkeypatch, settings):
    from fastapi import HTTPException
    monkeypatch.setattr(main,"dependencies",lambda:(settings,AsyncMock(),{123:object()},object(),None))
    request=AsyncMock()
    request.json.return_value={"update_id":42}
    with pytest.raises(HTTPException):
        await main.webhook(request,"wrong")
    assert not main.processed_message_updates


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["add", "cancel"])
async def test_edit_conflict_retains_edit_until_user_decides(settings, action):
    telegram, calendar, parser = AsyncMock(), Mock(), Mock()
    original = CalendarEvent(event_id="e", calendar_id="work", title="Dentist",start="2030-09-13T14:00:00+08:00",end="2030-09-13T15:00:00+08:00")
    edited = ParsedEdit(title="Dentist",start="2031-01-05T16:00:00+08:00",end="2031-01-05T17:00:00+08:00",confidence="high")
    parser.parse_edit.return_value=edited
    other=CalendarEvent(event_id="e",calendar_id="personal",title="Meeting",start=edited.start,end=edited.end)
    calendar._list_events_between.return_value=[original,other]
    calendar.update_event.return_value=other.model_copy(update={"title":"Dentist"})
    await main._edit_event(123,"move Dentist to 5 January 2031 at 4pm",original,settings,telegram,calendar,parser)
    parser.parse_edit.assert_called_once()  # 'at 4pm' is a time, not a location.
    calendar._list_events_between.assert_called_once_with(edited.start,edited.end)
    calendar.update_event.assert_not_called()
    token=main.pending_event_conflicts[123].token
    assert "Apply anyway" in str(telegram.send_message.call_args.args[2])
    await main.handle_conflict_callback(123,"cb",f"conflict:{token}:{action}",settings,telegram,calendar)
    await main.handle_conflict_callback(123,"cb",f"conflict:{token}:{action}",settings,telegram,calendar)
    assert calendar.update_event.call_count == int(action=="add")
    calendar.create_event.assert_not_called()
    assert parser.parse_edit.call_count == 1


@pytest.mark.asyncio
async def test_edit_excludes_itself_but_not_other_calendars(settings):
    telegram,calendar,parser=AsyncMock(),Mock(),Mock()
    original=CalendarEvent(event_id="e",calendar_id="work",title="Dentist",start="2030-09-13T14:00:00+08:00",end="2030-09-13T15:00:00+08:00")
    edited=ParsedEdit(title="Dentist",start="2030-09-13T14:30:00+08:00",end="2030-09-13T15:30:00+08:00",confidence="high")
    parser.parse_edit.return_value=edited
    calendar._list_events_between.return_value=[original]
    calendar.update_event.return_value=original
    await main._edit_event(123,"move Dentist to 2:30pm",original,settings,telegram,calendar,parser)
    calendar.update_event.assert_called_once_with(original,edited)


@pytest.mark.asyncio
async def test_reminder_status_merges_delivered_history_and_labels_source_failure(settings):
    telegram,calendar,cron=AsyncMock(),Mock(),AsyncMock()
    now=datetime.now(settings.timezone)
    remote=ScheduledReminder(event_id="cron:42",standalone=True,reminder=ReminderSpec(message="sleep"),due_at=now,status="Failed scheduler attempt")
    calendar.list_reminders.side_effect=RuntimeError("calendar unavailable")
    cron.list_reminders.return_value=[remote]
    main.reminder_delivery_history[(123,42)]=(time.monotonic()+86400,remote.model_copy(update={"status":"Delivered"}))
    main.reminder_delivery_history[(456,43)]=(time.monotonic()+86400,remote.model_copy(update={"event_id":"cron:43","reminder":ReminderSpec(message="other person's reminder")}))
    await main.handle_message(123,"/reminder_status",settings,telegram,calendar,Mock(),cron)
    reply=telegram.send_message.call_args.args[1]
    assert "Status: Delivered" in reply and "Failed scheduler" not in reply
    assert "Google Calendar" in reply and "lost on restart" in reply
    assert "other person's reminder" not in reply
    cron.list_reminders.assert_awaited_once_with(123,include_history=True)


def test_calendar_status_includes_sent_reminders_without_changing_default(settings):
    from app.calendar_client import CalendarClient
    client=CalendarClient.__new__(CalendarClient)
    now=datetime.now(settings.timezone)
    client._list_events_between=Mock(return_value=[CalendarEvent(event_id="e",title="Dentist",start=now+timedelta(minutes=5),reminders=[ReminderSpec(minutes_before=10,sent=True),ReminderSpec(minutes_before=15)])])
    assert len(client.list_reminders())==1
    assert {r.status for r in client.list_reminders(include_sent=True)}=={"Delivered","Overdue"}
