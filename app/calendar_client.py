from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .config import CalendarAccount, Settings
from .models import CalendarEvent, CalendarInfo, ParsedEdit, ParsedEvent, ReminderSpec, ScheduledReminder

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

    def create_event(self, event: ParsedEvent, calendar_id: str | None = None) -> CalendarEvent:
        target_calendar_id = calendar_id or self._calendar_id
        body = {
            "summary": event.title,
            "start": {"dateTime": event.start.isoformat(), "timeZone": self._timezone},
            "end": {"dateTime": event.end.isoformat(), "timeZone": self._timezone},
        }
        if event.location:
            body["location"] = event.location
        reminders = event.reminders or ([ReminderSpec(minutes_before=event.reminder_minutes)] if event.reminder_minutes else [])
        if reminders:
            body["extendedProperties"] = {"private": {"telegram_reminders": _serialize_reminders(reminders)}}
        if event.recurrence:
            body["recurrence"] = [event.recurrence]
        item = self._service.events().insert(calendarId=target_calendar_id, body=body).execute()
        return _to_event(item, self._timezone, target_calendar_id)

    def list_calendars(self) -> list[CalendarInfo]:
        calendars: list[CalendarInfo] = []
        page_token = None
        while True:
            result = self._service.calendarList().list(pageToken=page_token, showHidden=False).execute()
            for item in result.get("items", []):
                calendars.append(CalendarInfo(
                    calendar_id=item["id"], name=item.get("summaryOverride") or item.get("summary") or item["id"],
                    background_color=item.get("backgroundColor"), access_role=item.get("accessRole"), primary=item.get("primary", False),
                ))
            page_token = result.get("nextPageToken")
            if not page_token:
                return calendars

    def resolve_calendar(self, name: str | None) -> CalendarInfo | None:
        if not name:
            return None
        normalized = re.sub(r"\s+calendar\s*$", "", name.strip(), flags=re.IGNORECASE).casefold()
        calendars = self.list_calendars()
        exact = [calendar for calendar in calendars if calendar.name.casefold() == normalized]
        if len(exact) == 1:
            return exact[0]
        partial = [calendar for calendar in calendars if normalized in calendar.name.casefold()]
        return partial[0] if len(partial) == 1 else None

    def list_events(self, days_ahead: int = 7) -> list[CalendarEvent]:
        now = datetime.now(timezone.utc)
        return self._list_events_between(now, now + timedelta(days=days_ahead))

    def list_events_for_day(self, day: date) -> list[CalendarEvent]:
        local_timezone = ZoneInfo(self._timezone)
        start = datetime.combine(day, time.min, tzinfo=local_timezone)
        return self._list_events_between(start, start + timedelta(days=1))

    def _list_events_between(self, start: datetime, end: datetime, include_internal: bool = False) -> list[CalendarEvent]:
        events: list[CalendarEvent] = []
        for calendar in self.list_calendars():
            result = self._service.events().list(
                calendarId=calendar.calendar_id,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            ).execute()
            events.extend(_to_event(item, self._timezone, calendar.calendar_id, calendar.name) for item in result.get("items", []) if item.get("status") != "cancelled")
        # Google may include events that began before timeMin but overlap it. Do
        # not show an event once its end time has passed.
        return sorted([event for event in events if (include_internal or not event.is_standalone_reminder) and (event.end is None or event.end > start)], key=lambda event: event.start)

    def delete_event(self, event: CalendarEvent) -> None:
        self._service.events().delete(calendarId=event.calendar_id or self._calendar_id, eventId=event.event_id).execute()

    def delete_series(self, event: CalendarEvent) -> None:
        self._service.events().delete(calendarId=event.calendar_id or self._calendar_id, eventId=event.recurring_event_id or event.event_id).execute()

    def list_reminders(self, days_ahead: int = 30) -> list[ScheduledReminder]:
        now = datetime.now(timezone.utc)
        reminders: list[ScheduledReminder] = []
        for event in self._list_events_between(now - timedelta(days=8), now + timedelta(days=days_ahead), include_internal=True):
            for reminder in event.reminders:
                if reminder.sent:
                    continue
                due_at = event.start if event.is_standalone_reminder else event.start - timedelta(minutes=reminder.minutes_before or 0)
                reminders.append(ScheduledReminder(event_id=event.event_id, calendar_id=event.calendar_id, reminder=reminder, due_at=due_at, event_title=None if event.is_standalone_reminder else event.title, standalone=event.is_standalone_reminder))
        return sorted(reminders, key=lambda reminder: reminder.due_at)

    def list_standalone_reminder_events(self, days_ahead: int = 30) -> list[CalendarEvent]:
        now = datetime.now(timezone.utc)
        return [event for event in self._list_events_between(now, now + timedelta(days=days_ahead), include_internal=True) if event.is_standalone_reminder]

    def due_reminders(self) -> list[ScheduledReminder]:
        now = datetime.now(timezone.utc)
        return [reminder for reminder in self.list_reminders(8) if reminder.due_at <= now]

    def mark_reminder_sent(self, event_id: str, reminder_id: str, calendar_id: str | None = None) -> None:
        target_calendar_id = calendar_id or self._calendar_id
        item = self._service.events().get(calendarId=target_calendar_id, eventId=event_id).execute()
        private = item.get("extendedProperties", {}).get("private", {})
        reminders = _reminders_from_properties(private)
        for reminder in reminders:
            if reminder.reminder_id == reminder_id:
                reminder.sent = True
        private["telegram_reminders"] = _serialize_reminders(reminders)
        private.pop("telegram_reminder_minutes", None)
        private.pop("telegram_reminder_sent", None)
        self._service.events().patch(calendarId=target_calendar_id, eventId=event_id, body={"extendedProperties": {"private": private}}).execute()

    def set_reminder(self, event: CalendarEvent, reminder_minutes: int, message: str | None = None, append: bool = False) -> None:
        target_calendar_id = event.calendar_id or self._calendar_id
        item = self._service.events().get(calendarId=target_calendar_id, eventId=event.event_id).execute()
        private = item.get("extendedProperties", {}).get("private", {})
        reminders = _reminders_from_properties(private) if append else []
        reminders.append(ReminderSpec(minutes_before=reminder_minutes, message=message))
        private["telegram_reminders"] = _serialize_reminders(reminders)
        private.pop("telegram_reminder_minutes", None)
        private.pop("telegram_reminder_sent", None)
        self._service.events().patch(calendarId=target_calendar_id, eventId=event.event_id, body={"extendedProperties": {"private": private}}).execute()

    def clear_reminder(self, event: CalendarEvent) -> None:
        target_calendar_id = event.calendar_id or self._calendar_id
        item = self._service.events().get(calendarId=target_calendar_id, eventId=event.event_id).execute()
        private = item.get("extendedProperties", {}).get("private", {})
        private.pop("telegram_reminders", None)
        private.pop("telegram_reminder_minutes", None)
        private.pop("telegram_reminder_sent", None)
        self._service.events().patch(calendarId=target_calendar_id, eventId=event.event_id, body={"extendedProperties": {"private": private}}).execute()

    def update_event(self, existing: CalendarEvent, event: ParsedEdit) -> CalendarEvent:
        body = {
            "summary": event.title,
            "start": {"dateTime": event.start.isoformat(), "timeZone": self._timezone},
            "end": {"dateTime": event.end.isoformat(), "timeZone": self._timezone},
            "location": event.location or "",
        }
        item = self._service.events().patch(
            calendarId=existing.calendar_id or self._calendar_id, eventId=existing.event_id, body=body
        ).execute()
        return _to_event(item, self._timezone, existing.calendar_id, existing.calendar_name)

    def create_standalone_reminder(self, message: str, due_at: datetime) -> CalendarEvent:
        reminder = ReminderSpec(message=message)
        body = {
            "summary": "Telegram reminder",
            "start": {"dateTime": due_at.isoformat(), "timeZone": self._timezone},
            "end": {"dateTime": (due_at + timedelta(minutes=1)).isoformat(), "timeZone": self._timezone},
            "transparency": "transparent",
            "visibility": "private",
            "extendedProperties": {"private": {"telegram_reminder_type": "standalone", "telegram_reminders": _serialize_reminders([reminder])}},
        }
        item = self._service.events().insert(calendarId=self._calendar_id, body=body).execute()
        return _to_event(item, self._timezone, self._calendar_id)

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
            calendarId=event.calendar_id or self._calendar_id, eventId=series_id, body=body
        ).execute()
        return _to_event(item, self._timezone, event.calendar_id, event.calendar_name)


def _to_event(item: dict, timezone_name: str = "UTC", calendar_id: str | None = None, calendar_name: str | None = None) -> CalendarEvent:
    start = item.get("start", {}).get("dateTime") or item.get("start", {}).get("date")
    end = item.get("end", {}).get("dateTime") or item.get("end", {}).get("date")
    calendar_timezone = ZoneInfo(timezone_name)
    def parse(value: str | None) -> datetime | None:
        if not value:
            return None
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=calendar_timezone)

    properties = item.get("extendedProperties", {}).get("private", {})
    reminders = _reminders_from_properties(properties)
    reminder = properties.get("telegram_reminder_minutes")
    is_standalone = properties.get("telegram_reminder_type") == "standalone"
    title = reminders[0].message if is_standalone and reminders and reminders[0].message else item.get("summary") or "(untitled)"
    return CalendarEvent(
        event_id=item["id"], title=title,
        start=parse(start),
        end=parse(end),
        location=item.get("location"),
        reminder_minutes=int(reminder) if reminder and reminder.isdigit() else None,
        reminder_sent=properties.get("telegram_reminder_sent") == "true",
        reminders=reminders,
        is_standalone_reminder=is_standalone,
        recurring_event_id=item.get("recurringEventId"),
        calendar_id=calendar_id,
        calendar_name=calendar_name,
    )


def _reminders_from_properties(properties: dict) -> list[ReminderSpec]:
    raw = properties.get("telegram_reminders")
    if raw:
        try:
            return [ReminderSpec.model_validate(value) for value in json.loads(raw)]
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    minutes = properties.get("telegram_reminder_minutes")
    if minutes and minutes.isdigit():
        return [ReminderSpec(reminder_id="legacy", minutes_before=int(minutes), sent=properties.get("telegram_reminder_sent") == "true")]
    return []


def _serialize_reminders(reminders: list[ReminderSpec]) -> str:
    return json.dumps([reminder.model_dump() for reminder in reminders], separators=(",", ":"))
