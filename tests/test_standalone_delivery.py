import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

import app.main as main


@pytest.fixture
def delivery(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2030, 9, 11, 21, 44, tzinfo=ZoneInfo("Asia/Singapore"))
    monkeypatch.setattr(main, "datetime", Clock)
    telegram, cron = AsyncMock(), AsyncMock()
    cron.is_current_reminder.return_value = True
    monkeypatch.setattr(main, "dependencies", lambda: (SimpleNamespace(scheduler_secret="secret"), telegram, {123: object()}, None, cron))
    payload = {"job_id": 42, "telegram_user_id": 123, "message": "sleep", "due_at": "2030-09-11T21:43:00+08:00"}
    async def call(secret="Bearer secret"):
        request = AsyncMock()
        request.json.return_value = payload
        return await main.scheduled_standalone_reminder(request, secret)
    yield call, payload, telegram, cron
    main.delivered_standalone_reminders.clear()
    main.reminder_delivery_history.clear()
    main.standalone_deliveries_in_flight.clear()
    main.recent_reminder_lists.pop(123, None)


@pytest.mark.asyncio
async def test_missed_due_minute_delivers_on_recovery_attempt_once(delivery):
    call, _, telegram, cron = delivery
    await asyncio.gather(call(), call())
    telegram.send_message.assert_awaited_once_with(123, "⏰ sleep")
    assert main.reminder_delivery_history[(123, 42)][1].status == "Delivered"
    cron.delete_reminder.assert_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("due", ["2030-09-11T21:45:00+08:00", "2030-09-11T21:38:00+08:00"])
async def test_early_and_expired_callbacks_do_not_deliver(delivery, due):
    call, payload, telegram, cron = delivery
    payload["due_at"] = due
    assert (await call()).status_code == 204
    telegram.send_message.assert_not_awaited()
    cron.delete_reminder.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_telegram_send_can_retry(delivery):
    call, _, telegram, _ = delivery
    telegram.send_message.side_effect = [RuntimeError("network failure"), None]
    with pytest.raises(RuntimeError):
        await call()
    assert main.reminder_delivery_history[(123, 42)][1].status == "Failed (delivery not confirmed)"
    assert (await call()).status_code == 204
    assert telegram.send_message.await_count == 2


@pytest.mark.asyncio
async def test_failed_deletion_retries_without_resending(delivery):
    call, _, telegram, cron = delivery
    cron.delete_reminder.side_effect = [RuntimeError("network failure"), None]
    await call()
    await call()
    telegram.send_message.assert_awaited_once()
    assert cron.delete_reminder.await_count == 2


@pytest.mark.asyncio
async def test_legacy_callback_remains_supported(delivery):
    call, payload, telegram, _ = delivery
    payload.pop("due_at")
    await call()
    telegram.send_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [None, "invalid", "2030-09-11T21:43:00"])
async def test_invalid_due_timestamp_is_rejected(delivery, bad):
    call, payload, telegram, _ = delivery
    payload["due_at"] = bad
    with pytest.raises(HTTPException) as exc:
        await call()
    assert exc.value.status_code == 400
    telegram.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_unauthorized_callback_is_rejected(delivery):
    call, _, telegram, _ = delivery
    with pytest.raises(HTTPException) as exc:
        await call("Bearer wrong")
    assert exc.value.status_code == 401
    telegram.send_message.assert_not_awaited()


async def reschedule(payload, telegram, cron):
    import time
    from app.models import ReminderSpec, ScheduledReminder

    reminder = ScheduledReminder(event_id="cron:42", standalone=True,
        due_at=datetime.fromisoformat(payload["due_at"]), reminder=ReminderSpec(message="sleep"))
    main.recent_reminder_lists[123] = main.PendingReminderList([reminder], time.monotonic() + 300)
    await main.handle_message(123, "update 1 to 9.45pm",
        SimpleNamespace(timezone=ZoneInfo("Asia/Singapore")), telegram, object(), object(), cron)


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_update_response", [False, True])
async def test_reschedule_after_failed_delete_rejects_old_callback_and_delivers_new(delivery, monkeypatch, lost_update_response):
    call, payload, telegram, cron = delivery
    current = payload.copy()
    async def is_current(job_id, user_id, message, due_at):
        return (job_id, user_id, message, due_at) == (
            current["job_id"], current["telegram_user_id"], current["message"], current["due_at"])
    async def update(job_id, user_id, due_at):
        current["due_at"] = due_at.isoformat()
        if lost_update_response:
            raise RuntimeError("response lost")
    cron.is_current_reminder.side_effect = is_current
    cron.update_reminder.side_effect = update
    cron.delete_reminder.side_effect = RuntimeError("delete failed")
    await call()
    assert (123, 42) in main.delivered_standalone_reminders
    other_key = (999, 42)
    main.delivered_standalone_reminders[other_key] = main.delivered_standalone_reminders[(123, 42)]
    main.reminder_delivery_history[other_key] = main.reminder_delivery_history[(123, 42)]
    # The list may have been displayed just before the original due time.
    if lost_update_response:
        with pytest.raises(RuntimeError, match="response lost"):
            await reschedule(payload, telegram, cron)
        assert (123, 42) in main.delivered_standalone_reminders
    else:
        await reschedule(payload, telegram, cron)
        assert (123, 42) not in main.delivered_standalone_reminders
        assert (123, 42) not in main.reminder_delivery_history
    assert other_key in main.delivered_standalone_reminders
    assert other_key in main.reminder_delivery_history
    telegram.send_message.reset_mock()
    cron.delete_reminder.reset_mock(side_effect=True)
    await call()
    telegram.send_message.assert_not_awaited()
    cron.delete_reminder.assert_not_awaited()
    # No local schedule cache is needed to reject an old callback after restart.
    main.standalone_deliveries_in_flight.clear()
    await call()
    telegram.send_message.assert_not_awaited()
    cron.delete_reminder.assert_not_awaited()
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2030, 9, 11, 21, 45, tzinfo=ZoneInfo("Asia/Singapore"))
    monkeypatch.setattr(main, "datetime", Clock)
    payload.update(current)
    await call()
    await call()
    telegram.send_message.assert_awaited_once_with(123, "⏰ sleep")
    cron.delete_reminder.assert_awaited()


@pytest.mark.asyncio
async def test_failed_update_preserves_delivery_tracking_and_releases_guard(delivery):
    call, payload, telegram, cron = delivery
    cron.delete_reminder.side_effect = RuntimeError("delete failed")
    await call()
    marker = main.delivered_standalone_reminders[(123, 42)]
    history = main.reminder_delivery_history[(123, 42)]
    cron.update_reminder.side_effect = RuntimeError("update failed")
    with pytest.raises(RuntimeError, match="update failed"):
        await reschedule(payload, telegram, cron)
    assert main.delivered_standalone_reminders[(123, 42)] == marker
    assert main.reminder_delivery_history[(123, 42)] == history
    assert (123, 42) not in main.standalone_deliveries_in_flight
    await call()
    telegram.send_message.assert_awaited_once()
    assert cron.delete_reminder.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["delivery", "update"])
async def test_delivery_and_update_cannot_overlap(delivery, operation):
    call, payload, telegram, cron = delivery
    entered, release = asyncio.Event(), asyncio.Event()
    async def hold(*args):
        entered.set()
        await release.wait()
    if operation == "delivery":
        cron.delete_reminder.side_effect = hold
        task = asyncio.create_task(call())
    else:
        cron.update_reminder.side_effect = hold
        task = asyncio.create_task(reschedule(payload, telegram, cron))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if operation == "delivery":
            await reschedule(payload, telegram, cron)
            cron.update_reminder.assert_not_awaited()
            assert "try again shortly" in telegram.send_message.call_args.args[1]
        else:
            await call()
            cron.is_current_reminder.assert_not_awaited()
            telegram.send_message.assert_not_awaited()
            cron.delete_reminder.assert_not_awaited()
    finally:
        release.set()
        await task
    assert (123, 42) not in main.standalone_deliveries_in_flight


@pytest.mark.asyncio
async def test_schedule_lookup_failure_can_retry_without_send_or_delete(delivery):
    call, _, telegram, cron = delivery
    cron.is_current_reminder.side_effect = [RuntimeError("lookup failed"), True]
    with pytest.raises(RuntimeError, match="lookup failed"):
        await call()
    telegram.send_message.assert_not_awaited()
    cron.delete_reminder.assert_not_awaited()
    assert (123, 42) not in main.standalone_deliveries_in_flight
    await call()
    telegram.send_message.assert_awaited_once()
