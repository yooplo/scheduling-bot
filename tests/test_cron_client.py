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
        "expiresAt": 20260820092000,
        "hours": [9], "mdays": [20], "minutes": [15, 16, 17, 18, 19, 20], "months": [8], "wdays": [-1],
    }
    assert job["extendedData"]["headers"]["Authorization"] == "Bearer scheduler-secret"


def test_cron_job_title_is_scoped_to_the_telegram_user():
    client = CronJobClient("api-key", "https://bot.example.com", "secret", "Asia/Singapore")
    job = client._job_payload(987, "pay bill", datetime.fromisoformat("2026-08-20T09:15:00+08:00"))

    assert job["title"] == "SchedulingBot reminder v2:987:pay bill"
    assert job["enabled"] is False


def test_recovery_schedule_covers_midnight_and_year_boundary():
    from datetime import timedelta

    client = CronJobClient("key", "https://bot.example.com", "secret", "Asia/Singapore")
    due = datetime.fromisoformat("2030-12-31T23:58:30+08:00")
    schedule = client._job_payload(987, "sleep", due)["schedule"]
    for offset in range(6):
        at = due + timedelta(minutes=offset)
        assert at.minute in schedule["minutes"] and at.hour in schedule["hours"]
        assert at.day in schedule["mdays"] and at.month in schedule["months"]
    assert schedule["expiresAt"] == 20310101000330


@pytest.mark.asyncio
@pytest.mark.parametrize("patch_status", [200, 500])
async def test_job_is_enabled_only_with_complete_body_and_failure_rolls_back(monkeypatch, patch_status):
    import json
    import httpx

    requests = []
    def handle(request):
        requests.append(request)
        if request.method == "PUT":
            assert json.loads(request.content)["job"]["enabled"] is False
            return httpx.Response(200, json={"jobId": 42})
        if request.method == "PATCH":
            job = json.loads(request.content)["job"]
            assert job["enabled"] is True
            assert json.loads(job["extendedData"]["body"]) == {
                "job_id": 42, "telegram_user_id": 987, "message": "sleep", "due_at": "2030-09-11T21:43:00+08:00",
            }
            return httpx.Response(patch_status, json={})
        return httpx.Response(200, json={})

    original = httpx.AsyncClient
    monkeypatch.setattr("app.cron_client.httpx.AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    client = CronJobClient("key", "https://bot.example.com", "secret", "Asia/Singapore")
    if patch_status == 200:
        assert await client.create_reminder(987, "sleep", datetime.fromisoformat("2030-09-11T21:43:00+08:00")) == 42
        assert [r.method for r in requests] == ["PUT", "PATCH"]
    else:
        with pytest.raises(httpx.HTTPStatusError):
            await client.create_reminder(987, "sleep", datetime.fromisoformat("2030-09-11T21:43:00+08:00"))
        assert [r.method for r in requests] == ["PUT", "PATCH", "DELETE"]


@pytest.mark.asyncio
async def test_recovery_list_shows_due_time_not_next_attempt(monkeypatch):
    import httpx
    from zoneinfo import ZoneInfo

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2030, 9, 11, 21, 44, tzinfo=ZoneInfo("Asia/Singapore"))
    monkeypatch.setattr("app.cron_client.datetime", Clock)
    jobs = [{"enabled": True, "title": "SchedulingBot reminder v2:987:sleep", "jobId": 42,
             "nextExecution": 1, "schedule": {"expiresAt": 20300911214800}},
            {"enabled": True, "title": "SchedulingBot reminder v2:987:expired", "jobId": 43,
             "schedule": {"expiresAt": 20300911214300}}]
    original = httpx.AsyncClient
    monkeypatch.setattr("app.cron_client.httpx.AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"jobs": jobs})), **kwargs))
    reminders = await CronJobClient("key", "https://bot.example.com", "secret", "Asia/Singapore").list_reminders(987)
    assert len(reminders) == 1
    assert reminders[0].due_at.isoformat() == "2030-09-11T21:43:00+08:00"


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


@pytest.mark.asyncio
async def test_status_view_includes_expired_failed_and_disabled_jobs(monkeypatch):
    import httpx
    from zoneinfo import ZoneInfo

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2030, 9, 11, 21, 44, tzinfo=ZoneInfo("Asia/Singapore"))
    monkeypatch.setattr("app.cron_client.datetime", Clock)
    jobs = [
        {"jobId":1,"enabled":True,"title":"SchedulingBot reminder v2:987:expired","schedule":{"expiresAt":20300911214000}},
        {"jobId":2,"enabled":True,"title":"SchedulingBot reminder v2:987:failed","schedule":{"expiresAt":20300911214800},"lastStatus":5},
        {"jobId":3,"enabled":False,"title":"SchedulingBot reminder v2:987:disabled","schedule":{"expiresAt":20300911220000}},
        {"jobId":4,"enabled":True,"title":"SchedulingBot reminder v2:987:scheduled","schedule":{"expiresAt":20300911220000}},
        {"jobId":5,"enabled":True,"title":"SchedulingBot reminder v2:987:retrying","schedule":{"expiresAt":20300911214800}},
        {"jobId":6,"enabled":True,"title":"SchedulingBot reminder v2:456:private","schedule":{"expiresAt":20300911214800}},
    ]
    original=httpx.AsyncClient
    monkeypatch.setattr("app.cron_client.httpx.AsyncClient",lambda **kwargs:original(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={"jobs":jobs})),**kwargs))
    client=CronJobClient("key","https://bot.example.com","secret","Asia/Singapore")
    status={r.reminder.message:r.status for r in await client.list_reminders(987,include_history=True)}
    assert status == {"expired":"Expired (delivery unconfirmed)","failed":"Failed scheduler attempt (retry window open)","disabled":"Disabled","scheduled":"Scheduled","retrying":"Overdue / retrying"}
