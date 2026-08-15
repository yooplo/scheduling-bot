from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .config import Settings
from .models import CalendarEvent, ParsedEdit, ParsedEvent

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"


class CalendarClient:
    def __init__(self, settings: Settings) -> None:
        credentials = Credentials(
            token=None,
            refresh_token=settings.google_refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            scopes=[CALENDAR_SCOPE],
        )
        credentials.refresh(Request())
        self._service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        self._calendar_id = settings.google_calendar_id
        self._timezone = settings.user_timezone

    def create_event(self, event: ParsedEvent) -> CalendarEvent:
        body = {
            "summary": event.title,
            "start": {"dateTime": event.start.isoformat(), "timeZone": self._timezone},
            "end": {"dateTime": event.end.isoformat(), "timeZone": self._timezone},
        }
        if event.location:
            body["location"] = event.location
        if event.reminder_minutes:
            body["extendedProperties"] = {"private": {"telegram_reminder_minutes": str(event.reminder_minutes)}}
        item = self._service.events().insert(calendarId=self._calendar_id, body=body).execute()
        return _to_event(item, self._timezone)

    def list_events(self, days_ahead: int = 7) -> list[CalendarEvent]:
        now = datetime.now(timezone.utc)
        result = self._service.events().list(
            calendarId=self._calendar_id,
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=days_ahead)).isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = [_to_event(item, self._timezone) for item in result.get("items", []) if item.get("status") != "cancelled"]
        # Google may include events that began before timeMin but overlap it. Do
        # not show an event once its end time has passed.
        return [event for event in events if event.end is None or event.end > now]

    def delete_event(self, event_id: str) -> None:
        self._service.events().delete(calendarId=self._calendar_id, eventId=event_id).execute()

    def due_reminders(self) -> list[CalendarEvent]:
        now = datetime.now(timezone.utc)
        result = self._service.events().list(calendarId=self._calendar_id, timeMin=now.isoformat(), timeMax=(now + timedelta(days=8)).isoformat(), singleEvents=True, orderBy="startTime", privateExtendedProperty="telegram_reminder_minutes").execute()
        due = []
        for item in result.get("items", []):
            event = _to_event(item, self._timezone)
            if event.reminder_minutes and not event.reminder_sent and event.start - timedelta(minutes=event.reminder_minutes) <= now < event.start:
                due.append(event)
        return due

    def mark_reminder_sent(self, event_id: str) -> None:
        item = self._service.events().get(calendarId=self._calendar_id, eventId=event_id).execute()
        private = item.get("extendedProperties", {}).get("private", {})
        private["telegram_reminder_sent"] = "true"
        self._service.events().patch(calendarId=self._calendar_id, eventId=event_id, body={"extendedProperties": {"private": private}}).execute()

    def update_event(self, event_id: str, event: ParsedEdit) -> CalendarEvent:
        body = {
            "summary": event.title,
            "start": {"dateTime": event.start.isoformat(), "timeZone": self._timezone},
            "end": {"dateTime": event.end.isoformat(), "timeZone": self._timezone},
            "location": event.location or "",
        }
        item = self._service.events().patch(
            calendarId=self._calendar_id, eventId=event_id, body=body
        ).execute()
        return _to_event(item, self._timezone)


def _to_event(item: dict, timezone_name: str = "UTC") -> CalendarEvent:
    start = item.get("start", {}).get("dateTime") or item.get("start", {}).get("date")
    end = item.get("end", {}).get("dateTime") or item.get("end", {}).get("date")
    calendar_timezone = ZoneInfo(timezone_name)
    def parse(value: str | None) -> datetime | None:
        if not value:
            return None
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=calendar_timezone)

    properties = item.get("extendedProperties", {}).get("private", {})
    reminder = properties.get("telegram_reminder_minutes")
    return CalendarEvent(
        event_id=item["id"], title=item.get("summary") or "(untitled)",
        start=parse(start),
        end=parse(end),
        location=item.get("location"),
        reminder_minutes=int(reminder) if reminder and reminder.isdigit() else None,
        reminder_sent=properties.get("telegram_reminder_sent") == "true",
    )
