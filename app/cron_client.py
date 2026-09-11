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
    RECOVERY_TITLE_PREFIX = "SchedulingBot reminder v2:"
    RECOVERY_MINUTES = 5

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
                "body": json.dumps({"job_id": job_id, "telegram_user_id": telegram_user_id, "message": message, "due_at": due_at.isoformat()}),
            }
            try:
                response = await client.patch(
                    f"{self.API_URL}/jobs/{job_id}", headers=self._headers,
                    json={"job": {"extendedData": extended_data, "enabled": True}},
                )
                response.raise_for_status()
            except Exception:
                await client.delete(f"{self.API_URL}/jobs/{job_id}", headers=self._headers)
                raise
        return job_id

    def _job_payload(self, telegram_user_id: int, message: str, due_at: datetime) -> dict:
        due_at = due_at.astimezone(ZoneInfo(self._timezone))
        attempts = [due_at + timedelta(minutes=offset) for offset in range(self.RECOVERY_MINUTES + 1)]
        schedule = {
            "timezone": self._timezone,
            "expiresAt": int(attempts[-1].strftime("%Y%m%d%H%M%S")),
            "hours": sorted({at.hour for at in attempts}),
            "mdays": sorted({at.day for at in attempts}),
            "minutes": sorted({at.minute for at in attempts}),
            "months": sorted({at.month for at in attempts}),
            "wdays": [-1],
        }
        title = f"{self.RECOVERY_TITLE_PREFIX}{telegram_user_id}:{message}"[:128]
        job = {
            "url": self._callback_url,
            "enabled": False,
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

    async def list_reminders(self, telegram_user_id: int, include_history: bool = False) -> list[ScheduledReminder]:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(f"{self.API_URL}/jobs", headers=self._headers)
            response.raise_for_status()
            jobs = response.json().get("jobs", [])
        prefixes = (f"{self.RECOVERY_TITLE_PREFIX}{telegram_user_id}:", f"{self.TITLE_PREFIX}{telegram_user_id}:")
        reminders: list[ScheduledReminder] = []
        for job in jobs:
            title = job.get("title")
            prefix = next((prefix for prefix in prefixes if isinstance(title, str) and title.startswith(prefix)), None)
            if prefix is None or (not job.get("enabled") and not include_history):
                continue
            schedule = job.get("schedule", {})
            try:
                if prefix == prefixes[0]:
                    due_at = datetime.strptime(str(schedule["expiresAt"]), "%Y%m%d%H%M%S").replace(tzinfo=ZoneInfo(self._timezone)) - timedelta(minutes=self.RECOVERY_MINUTES)
                    if not include_history and datetime.now(ZoneInfo(self._timezone)) > due_at + timedelta(minutes=self.RECOVERY_MINUTES):
                        continue
                elif include_history and schedule.get("expiresAt"):
                    due_at = datetime.strptime(str(schedule["expiresAt"]), "%Y%m%d%H%M%S").replace(tzinfo=ZoneInfo(self._timezone)) - timedelta(days=1)
                elif job.get("nextExecution"):
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
            try:
                now = datetime.now(ZoneInfo(self._timezone))
                if include_history and due_at < now - timedelta(days=1):
                    continue
                deadline = due_at + timedelta(minutes=self.RECOVERY_MINUTES) if prefix == prefixes[0] else due_at
                status = "Scheduled"
                if job.get("lastStatus", 0) not in (0, 1):
                    status = "Failed scheduler attempt" + (" (retry window open)" if now <= deadline and job.get("enabled") else "")
                elif now > deadline:
                    status = "Expired (delivery unconfirmed)"
                elif not job.get("enabled"):
                    status = "Disabled"
                elif now >= due_at:
                    status = "Overdue / retrying"
                reminders.append(ScheduledReminder(
                    event_id=f"cron:{int(job['jobId'])}", reminder=ReminderSpec(message=title[len(prefix):] or "Reminder"),
                    due_at=due_at, standalone=True, status=status,
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(reminders, key=lambda item: item.due_at)

    async def delete_reminder(self, job_id: int) -> None:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.delete(f"{self.API_URL}/jobs/{job_id}", headers=self._headers)
            response.raise_for_status()
