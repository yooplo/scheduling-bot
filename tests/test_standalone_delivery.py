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
    monkeypatch.setattr(main, "dependencies", lambda: (SimpleNamespace(scheduler_secret="secret"), telegram, {123: object()}, None, cron))
    payload = {"job_id": 42, "telegram_user_id": 123, "message": "sleep", "due_at": "2030-09-11T21:43:00+08:00"}
    async def call(secret="Bearer secret"):
        request = AsyncMock()
        request.json.return_value = payload
        return await main.scheduled_standalone_reminder(request, secret)
    yield call, payload, telegram, cron
    main.delivered_standalone_reminders.clear()
    main.standalone_deliveries_in_flight.clear()


@pytest.mark.asyncio
async def test_missed_due_minute_delivers_on_recovery_attempt_once(delivery):
    call, _, telegram, cron = delivery
    await asyncio.gather(call(), call())
    telegram.send_message.assert_awaited_once_with(123, "⏰ sleep")
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
