from __future__ import annotations

from datetime import datetime, timedelta, timezone

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .config import Settings
from .models import CalendarEvent, ParsedEvent

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
        item = self._service.events().insert(calendarId=self._calendar_id, body=body).execute()
        return _to_event(item)

    def list_events(self, days_ahead: int = 7) -> list[CalendarEvent]:
        now = datetime.now(timezone.utc)
        result = self._service.events().list(
            calendarId=self._calendar_id,
            timeMin=now.isoformat(),
            timeMax=(now + timedelta(days=days_ahead)).isoformat(),
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        return [_to_event(item) for item in result.get("items", []) if item.get("status") != "cancelled"]

    def delete_event(self, event_id: str) -> None:
        self._service.events().delete(calendarId=self._calendar_id, eventId=event_id).execute()


def _to_event(item: dict) -> CalendarEvent:
    start = item.get("start", {}).get("dateTime") or item.get("start", {}).get("date")
    end = item.get("end", {}).get("dateTime") or item.get("end", {}).get("date")
    return CalendarEvent(
        event_id=item["id"], title=item.get("summary") or "(untitled)",
        start=datetime.fromisoformat(start.replace("Z", "+00:00")),
        end=datetime.fromisoformat(end.replace("Z", "+00:00")) if end else None,
        location=item.get("location"),
    )

