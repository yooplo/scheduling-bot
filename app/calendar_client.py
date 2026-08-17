from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .config import CalendarAccount, Settings
from .models import CalendarEvent, ParsedEdit, ParsedEvent

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"


class CalendarClient:
    def __init__(self, settings: Settings, account: CalendarAccount) -> None:
        credentials = Credentials(
            token=None,
            refresh_token=account.google_refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            scopes=[CALENDAR_SCOPE],
        )
        credentials.refresh(Request())
        self._service = build("calendar", "v3", credentials=credentials, cache_discovery=False)
        self._calendar_id = account.google_calendar_id
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
        if event.recurrence:
            body["recurrence"] = [event.recurrence]
        item = self._service.events().insert(calendarId=self._calendar_id, body=body).execute()
        return _to_event(item, self._timezone)

    def list_events(self, days_ahead: int = 7) -> list[CalendarEvent]:
        now = datetime.now(timezone.utc)
        return self._list_events_between(now, now + timedelta(days=days_ahead))

    def list_events_for_day(self, day: date) -> list[CalendarEvent]:
        local_timezone = ZoneInfo(self._timezone)
        start = datetime.combine(day, time.min, tzinfo=local_timezone)
        return self._list_events_between(start, start + timedelta(days=1))

    def _list_events_between(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        result = self._service.events().list(
            calendarId=self._calendar_id,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = [_to_event(item, self._timezone) for item in result.get("items", []) if item.get("status") != "cancelled"]
        # Google may include events that began before timeMin but overlap it. Do
        # not show an event once its end time has passed.
        return [event for event in events if event.end is None or event.end > start]

    def delete_event(self, event_id: str) -> None:
        self._service.events().delete(calendarId=self._calendar_id, eventId=event_id).execute()

    def delete_series(self, event: CalendarEvent) -> None:
        self.delete_event(event.recurring_event_id or event.event_id)

    def due_reminders(self) -> list[CalendarEvent]:
        now = datetime.now(timezone.utc)
        due = []
        # Use the same Calendar query proven by normal list requests. Some
        # Calendar configurations reject the privateExtendedProperty filter.
        for event in self.list_events(8):
            if event.reminder_minutes and not event.reminder_sent and event.start - timedelta(minutes=event.reminder_minutes) <= now < event.start:
                due.append(event)
        return due

    def mark_reminder_sent(self, event_id: str) -> None:
        item = self._service.events().get(calendarId=self._calendar_id, eventId=event_id).execute()
        private = item.get("extendedProperties", {}).get("private", {})
        private["telegram_reminder_sent"] = "true"
        self._service.events().patch(calendarId=self._calendar_id, eventId=event_id, body={"extendedProperties": {"private": private}}).execute()

    def set_reminder(self, event_id: str, reminder_minutes: int) -> None:
        item = self._service.events().get(calendarId=self._calendar_id, eventId=event_id).execute()
        private = item.get("extendedProperties", {}).get("private", {})
        private["telegram_reminder_minutes"] = str(reminder_minutes)
        private.pop("telegram_reminder_sent", None)
        self._service.events().patch(calendarId=self._calendar_id, eventId=event_id, body={"extendedProperties": {"private": private}}).execute()

    def clear_reminder(self, event_id: str) -> None:
        item = self._service.events().get(calendarId=self._calendar_id, eventId=event_id).execute()
        private = item.get("extendedProperties", {}).get("private", {})
        private.pop("telegram_reminder_minutes", None)
        private.pop("telegram_reminder_sent", None)
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

    def update_series(self, event: CalendarEvent, edited: ParsedEdit) -> CalendarEvent:
        """Patch a recurring-event master so the change applies to every occurrence."""
        series_id = event.recurring_event_id or event.event_id
        body = {
            "summary": edited.title,
            "start": {"dateTime": edited.start.isoformat(), "timeZone": self._timezone},
            "end": {"dateTime": edited.end.isoformat(), "timeZone": self._timezone},
            "location": edited.location or "",
        }
        # Omit recurrence to preserve the current rule; supply it to replace it.
        if edited.recurrence:
            body["recurrence"] = [edited.recurrence]
        item = self._service.events().patch(
            calendarId=self._calendar_id, eventId=series_id, body=body
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
        recurring_event_id=item.get("recurringEventId"),
    )
