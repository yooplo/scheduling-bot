from datetime import datetime

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
