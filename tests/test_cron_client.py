from datetime import datetime

import pytest

from app.cron_client import CronJobClient


def test_standalone_reminder_job_calls_secured_callback_at_due_minute():
    client = CronJobClient(
        "api-key", "https://bot.example.com/", "scheduler-secret", "Asia/Singapore"
    )
    due_at = datetime.fromisoformat("2026-08-20T09:15:00+08:00")

    job = client._job_payload(12345, "wash my hands", due_at)

    assert job["url"] == "https://bot.example.com/scheduled/standalone-reminder"
    assert job["requestMethod"] == 1
    assert job["schedule"] == {
        "timezone": "Asia/Singapore",
        "expiresAt": 20260821091500,
        "hours": [9], "mdays": [20], "minutes": [15], "months": [8], "wdays": [-1],
    }
    assert job["extendedData"]["headers"]["Authorization"] == "Bearer scheduler-secret"


def test_cron_job_title_is_scoped_to_the_telegram_user():
    client = CronJobClient("api-key", "https://bot.example.com", "secret", "Asia/Singapore")
    job = client._job_payload(987, "pay bill", datetime.fromisoformat("2026-08-20T09:15:00+08:00"))

    assert job["title"] == "SchedulingBot reminder:987:pay bill"


@pytest.mark.asyncio
async def test_malformed_cron_jobs_are_ignored(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"jobs": [
                {"enabled": True, "title": None, "jobId": 1},
                {"enabled": True, "title": "SchedulingBot reminder:987:valid", "jobId": None,
                 "nextExecution": 1787620500},
            ]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr("app.cron_client.httpx.AsyncClient", lambda **kwargs: Client())
    client = CronJobClient("api-key", "https://bot.example.com", "secret", "Asia/Singapore")

    assert await client.list_reminders(987) == []
