from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .models import ReminderSpec, ScheduledReminder


class CronJobClient:
    """Persist independent reminders as expiring cron-job.org jobs."""

    API_URL = "https://api.cron-job.org"
    TITLE_PREFIX = "SchedulingBot reminder:"

    def __init__(self, api_key: str, service_base_url: str, scheduler_secret: str, timezone_name: str) -> None:
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        self._callback_url = service_base_url.rstrip("/") + "/scheduled/standalone-reminder"
        self._scheduler_secret = scheduler_secret
        self._timezone = timezone_name

    async def create_reminder(self, telegram_user_id: int, message: str, due_at: datetime) -> int:
        due_at = due_at.astimezone(ZoneInfo(self._timezone))
        job = self._job_payload(telegram_user_id, message, due_at)
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.put(f"{self.API_URL}/jobs", headers=self._headers, json={"job": job})
            response.raise_for_status()
            job_id = int(response.json()["jobId"])
            extended_data = {
                **job["extendedData"],
                "body": json.dumps({"job_id": job_id, "telegram_user_id": telegram_user_id, "message": message}),
            }
            try:
                response = await client.patch(
                    f"{self.API_URL}/jobs/{job_id}", headers=self._headers,
                    json={"job": {"extendedData": extended_data}},
                )
                response.raise_for_status()
            except Exception:
                await client.delete(f"{self.API_URL}/jobs/{job_id}", headers=self._headers)
                raise
        return job_id

    def _job_payload(self, telegram_user_id: int, message: str, due_at: datetime) -> dict:
        schedule = {
            "timezone": self._timezone,
            "expiresAt": int((due_at + timedelta(days=1)).strftime("%Y%m%d%H%M%S")),
            "hours": [due_at.hour],
            "mdays": [due_at.day],
            "minutes": [due_at.minute],
            "months": [due_at.month],
            "wdays": [-1],
        }
        title = f"{self.TITLE_PREFIX}{telegram_user_id}:{message}"[:128]
        job = {
            "url": self._callback_url,
            "enabled": True,
            "title": title,
            "saveResponses": False,
            "requestMethod": 1,
            "requestTimeout": 60,
            "schedule": schedule,
            "extendedData": {
                "headers": {"Authorization": f"Bearer {self._scheduler_secret}", "Content-Type": "application/json"},
                "body": "{}",
            },
        }
        return job

    async def list_reminders(self, telegram_user_id: int) -> list[ScheduledReminder]:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(f"{self.API_URL}/jobs", headers=self._headers)
            response.raise_for_status()
            jobs = response.json().get("jobs", [])
        prefix = f"{self.TITLE_PREFIX}{telegram_user_id}:"
        reminders: list[ScheduledReminder] = []
        for job in jobs:
            if not job.get("enabled") or not job.get("title", "").startswith(prefix):
                continue
            schedule = job.get("schedule", {})
            try:
                if job.get("nextExecution"):
                    due_at = datetime.fromtimestamp(job["nextExecution"], tz=ZoneInfo(self._timezone))
                else:
                    due_at = datetime(
                        datetime.now(ZoneInfo(self._timezone)).year,
                        int(schedule["months"][0]), int(schedule["mdays"][0]),
                        int(schedule["hours"][0]), int(schedule["minutes"][0]),
                        tzinfo=ZoneInfo(self._timezone),
                    )
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            message = job["title"][len(prefix):] or "Reminder"
            reminders.append(ScheduledReminder(
                event_id=f"cron:{job['jobId']}", reminder=ReminderSpec(message=message),
                due_at=due_at, standalone=True,
            ))
        return sorted(reminders, key=lambda item: item.due_at)

    async def delete_reminder(self, job_id: int) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.delete(f"{self.API_URL}/jobs/{job_id}", headers=self._headers)
            response.raise_for_status()
