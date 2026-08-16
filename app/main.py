from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import FastAPI, Header, HTTPException, Request, Response

from .calendar_client import CalendarClient
from .config import ConfigurationError, Settings, get_settings
from .models import CalendarEvent
from .parser import GroqParser, ParseError
from .telegram_client import TelegramClient, valid_webhook_secret

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = FastAPI(title="Telegram Calendar Bot")

DELETE_WORDS = ("delete", "cancel", "remove")
EDIT_WORDS = ("change", "edit", "move", "reschedule", "update")
REMINDER_PREFIXES = ("set a reminder", "add a reminder", "remind me")
REMINDER_LIST_PHRASES = ("reminders", "upcoming reminders", "all reminders", "show reminders", "my reminders")
FREE_TIME_PHRASES = ("when am i free", "when i'm free", "find free time", "free slot", "availability")
LIST_WORDS = ("list", "show", "what's on", "whats on", "what are my", "upcoming", "calendar", "plans", "schedule")
PENDING_TTL_SECONDS = 300


@dataclass
class PendingAction:
    events: list[CalendarEvent]
    expires_at: float
    action: str
    request_text: str


pending_actions: dict[int, PendingAction] = {}
_settings: Settings | None = None
_telegram: TelegramClient | None = None
_calendar: CalendarClient | None = None
_parser: GroqParser | None = None


def dependencies() -> tuple[Settings, TelegramClient, CalendarClient, GroqParser]:
    global _settings, _telegram, _calendar, _parser
    if _settings is None:
        _settings = get_settings()
        _telegram = TelegramClient(_settings.telegram_bot_token)
        _calendar = CalendarClient(_settings)
        _parser = GroqParser(_settings.groq_api_key, _settings.groq_model)
    return _settings, _telegram, _calendar, _parser


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)) -> dict[str, bool]:
    try:
        settings, telegram, calendar, parser = dependencies()
    except ConfigurationError:
        logger.exception("Invalid configuration")
        raise HTTPException(status_code=503, detail="Service is not configured")
    if not valid_webhook_secret(x_telegram_bot_api_secret_token, settings.telegram_webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")
    update = await request.json()
    message = update.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    sender_id = message.get("from", {}).get("id")
    text = (message.get("text") or "").strip()
    if not chat_id or not text or sender_id != settings.allowed_telegram_user_id:
        return {"ok": True}
    try:
        await handle_message(chat_id, text, settings, telegram, calendar, parser)
    except Exception:
        logger.exception("Failed handling Telegram message chat_id=%s", chat_id)
        await telegram.send_message(chat_id, "Sorry, I couldn't complete that. Please try again.")
    return {"ok": True}


@app.post("/scheduled/reminders")
async def scheduled_reminders(authorization: str | None = Header(default=None)) -> Response:
    settings, telegram, calendar, _ = dependencies()
    if authorization != f"Bearer {settings.scheduler_secret}":
        raise HTTPException(status_code=401, detail="Invalid scheduler secret")
    events = await asyncio.to_thread(calendar.due_reminders)
    for event in events:
        await telegram.send_message(settings.allowed_telegram_user_id, f"⏰ Reminder: {event.title} starts at {_format_time(event.start)}")
        await asyncio.to_thread(calendar.mark_reminder_sent, event.event_id)
    return Response(status_code=204)


@app.post("/scheduled/daily-agenda")
async def scheduled_daily_agenda(authorization: str | None = Header(default=None)) -> Response:
    settings, telegram, calendar, _ = dependencies()
    if authorization != f"Bearer {settings.scheduler_secret}":
        raise HTTPException(status_code=401, detail="Invalid scheduler secret")
    events = await asyncio.to_thread(calendar.list_events, 1)
    lines = [_format_event_listing(event, bullet=True) for event in events]
    await telegram.send_message(settings.allowed_telegram_user_id, "☀️ Today's agenda:\n\n" + ("\n\n".join(lines) if lines else "No upcoming events today."))
    return Response(status_code=204)


async def handle_message(chat_id: int, text: str, settings: Settings, telegram: TelegramClient, calendar: CalendarClient, parser: GroqParser) -> None:
    pending = pending_actions.get(chat_id)
    if pending and pending.expires_at > time.monotonic():
        selected = _selection(text, pending.events)
        if selected:
            pending_actions.pop(chat_id, None)
            if pending.action == "delete":
                await _delete_event(chat_id, selected, telegram, calendar)
            elif pending.action == "edit":
                await _edit_event(chat_id, pending.request_text, selected, settings, telegram, calendar, parser)
            elif pending.action == "clear_reminder":
                await asyncio.to_thread(calendar.clear_reminder, selected.event_id)
                await telegram.send_message(chat_id, f"🔕 Reminder removed: {selected.title}")
            else:
                await _set_reminder(chat_id, pending.request_text, selected, settings, telegram, calendar, parser)
            return
        pending_actions.pop(chat_id, None)
    lowered = text.lower()
    if any(phrase in lowered for phrase in FREE_TIME_PHRASES):
        target_days = 1 if ("tomorrow" in lowered or "tmr" in lowered) else 7
        events = await asyncio.to_thread(calendar.list_events, target_days)
        await telegram.send_message(chat_id, _format_free_slots(events, settings, target_days))
    elif "reminder" in lowered and any(word in lowered for word in ("remove", "disable", "cancel", "delete")):
        events = await asyncio.to_thread(calendar.list_events, 30)
        match = await asyncio.to_thread(parser.match_event, text, events)
        selected = next((event for event in events if event.event_id == match.matched_event_id), None)
        if selected and not match.ambiguous:
            await asyncio.to_thread(calendar.clear_reminder, selected.event_id)
            await telegram.send_message(chat_id, f"🔕 Reminder removed: {selected.title}")
            return
        await _ask_to_select(chat_id, "clear_reminder", text, events, match, telegram)
    elif any(phrase in lowered for phrase in REMINDER_LIST_PHRASES):
        events = await asyncio.to_thread(calendar.list_events, 30)
        reminders = [event for event in events if event.reminder_minutes and not event.reminder_sent]
        if not reminders:
            await telegram.send_message(chat_id, "No upcoming Telegram reminders.")
        else:
            lines = [_format_reminder_listing(event, index) for index, event in enumerate(reminders, 1)]
            await telegram.send_message(chat_id, "Upcoming reminders:\n\n" + "\n\n".join(lines))
    elif any(word in lowered for word in DELETE_WORDS):
        events = await asyncio.to_thread(calendar.list_events, 30)
        if not events:
            await telegram.send_message(chat_id, "There are no events in the next 30 days to delete.")
            return
        match = await asyncio.to_thread(parser.match_event, text, events)
        selected = next((e for e in events if e.event_id == match.matched_event_id), None)
        if selected and not match.ambiguous:
            await asyncio.to_thread(calendar.delete_event, selected.event_id)
            await telegram.send_message(chat_id, f"✅ Deleted: {selected.title} — {_format_time(selected.start)}")
            return
        await _ask_to_select(chat_id, "delete", text, events, match, telegram)
    elif _is_existing_reminder_request(lowered):
        events = await asyncio.to_thread(calendar.list_events, 30)
        if not events:
            await telegram.send_message(chat_id, "There are no events in the next 30 days to remind you about.")
            return
        match = await asyncio.to_thread(parser.match_event, text, events)
        selected = next((event for event in events if event.event_id == match.matched_event_id), None)
        if selected and not match.ambiguous:
            await _set_reminder(chat_id, text, selected, settings, telegram, calendar, parser)
            return
        await _ask_to_select(chat_id, "remind", text, events, match, telegram)
    elif any(word in lowered for word in EDIT_WORDS):
        events = await asyncio.to_thread(calendar.list_events, 30)
        if not events:
            await telegram.send_message(chat_id, "There are no events in the next 30 days to edit.")
            return
        match = await asyncio.to_thread(parser.match_event, text, events)
        selected = next((event for event in events if event.event_id == match.matched_event_id), None)
        if selected and not match.ambiguous:
            await _edit_event(chat_id, text, selected, settings, telegram, calendar, parser)
            return
        await _ask_to_select(chat_id, "edit", text, events, match, telegram)
    elif any(word in lowered for word in LIST_WORDS):
        if "tomorrow" in lowered or "tmr" in lowered:
            target = datetime.now(settings.timezone).date() + timedelta(days=1)
            events = await asyncio.to_thread(calendar.list_events_for_day, target)
            heading = "Tomorrow's events"
        elif "today" in lowered:
            target = datetime.now(settings.timezone).date()
            events = await asyncio.to_thread(calendar.list_events_for_day, target)
            heading = "Today's events"
        else:
            events = await asyncio.to_thread(calendar.list_events, 7)
            heading = "Upcoming events"
        if not events:
            await telegram.send_message(chat_id, f"No events for {heading.lower().replace(' events', '')}.")
        else:
            lines = [_format_event_listing(event, index=index) for index, event in enumerate(events, 1)]
            await telegram.send_message(chat_id, heading + ":\n\n" + "\n\n".join(lines))
    else:
        now = datetime.now(settings.timezone)
        event = await asyncio.to_thread(parser.parse_event, text, now, settings.user_timezone)
        event.reminder_minutes = event.reminder_minutes or _reminder_minutes_from_text(text)
        _apply_recurrence_from_text(event, text)
        if event.confidence == "low":
            await telegram.send_message(chat_id, "I need a clearer date and time. For example: 'dentist tomorrow 2–3pm'.")
            return
        if event.start.tzinfo is None or event.end.tzinfo is None:
            await telegram.send_message(chat_id, "Please include a date and time with enough detail for me to schedule it.")
            return
        conflicts = [existing for existing in await asyncio.to_thread(calendar.list_events, 30) if existing.start < event.end and (existing.end or existing.start) > event.start]
        if conflicts and "add anyway" not in lowered:
            details = "\n".join(f"• {existing.title} — {_format_event_range(existing)}" for existing in conflicts[:3])
            await telegram.send_message(chat_id, "⚠️ This overlaps with:\n" + details + "\n\nReply with 'add anyway' plus your event details to continue.")
            return
        created = await asyncio.to_thread(calendar.create_event, event)
        reminder_confirmation = ""
        if event.reminder_minutes:
            unit = "minute" if event.reminder_minutes == 1 else "minutes"
            reminder_confirmation = f"\n⏰ Reminder set: {event.reminder_minutes} {unit} before"
        recurrence_confirmation = "\n🔁 Repeats weekly" if event.recurrence else ""
        await telegram.send_message(chat_id, f"✅ Added: {created.title} — {_format_time(created.start)}–{_format_time(created.end)}{recurrence_confirmation}{reminder_confirmation}")


async def _delete_event(chat_id: int, event: CalendarEvent, telegram: TelegramClient, calendar: CalendarClient) -> None:
    await asyncio.to_thread(calendar.delete_event, event.event_id)
    await telegram.send_message(chat_id, f"✅ Deleted: {event.title} — {_format_time(event.start)}")


async def _edit_event(chat_id: int, text: str, existing: CalendarEvent, settings: Settings, telegram: TelegramClient, calendar: CalendarClient, parser: GroqParser) -> None:
    edited = await asyncio.to_thread(parser.parse_edit, text, existing, settings.user_timezone)
    if edited.confidence == "low" or edited.start.tzinfo is None or edited.end.tzinfo is None:
        await telegram.send_message(chat_id, "I need a clearer change. For example: 'move IPPT on Saturday to 4pm'.")
        return
    updated = await asyncio.to_thread(calendar.update_event, existing.event_id, edited)
    await telegram.send_message(chat_id, f"✅ Updated: {updated.title} — {_format_time(updated.start)}–{_format_time(updated.end)}")


async def _set_reminder(chat_id: int, text: str, event: CalendarEvent, settings: Settings, telegram: TelegramClient, calendar: CalendarClient, parser: GroqParser) -> None:
    reminder = await asyncio.to_thread(parser.parse_reminder_for_event, text, event, settings.user_timezone)
    if reminder.confidence == "low":
        await telegram.send_message(chat_id, "Tell me when to remind you, for example: 'set a reminder one day before IPPT'.")
        return
    await asyncio.to_thread(calendar.set_reminder, event.event_id, reminder.reminder_minutes)
    unit = "minute" if reminder.reminder_minutes == 1 else "minutes"
    await telegram.send_message(chat_id, f"⏰ Reminder set: {event.title} — {reminder.reminder_minutes} {unit} before {_format_time(event.start)}")


def _is_existing_reminder_request(text: str) -> bool:
    if text.startswith(REMINDER_PREFIXES):
        return True
    return "reminder" in text and any(word in text for word in EDIT_WORDS)


def _format_free_slots(events: list[CalendarEvent], settings: Settings, days: int) -> str:
    now = datetime.now(settings.timezone)
    days_to_check = [now.date() + timedelta(days=offset) for offset in range(1 if days == 1 else days)]
    slots: list[str] = []
    for day in days_to_check:
        day_events = [event for event in events if event.start.astimezone(settings.timezone).date() == day]
        cursor = datetime.combine(day, datetime.min.time(), tzinfo=settings.timezone).replace(hour=9)
        closing = cursor.replace(hour=18)
        for event in sorted(day_events, key=lambda item: item.start):
            start = event.start.astimezone(settings.timezone)
            if start - cursor >= timedelta(hours=1):
                slots.append(f"• {cursor:%a} {cursor.day} {cursor:%b}: {_format_clock(cursor)}–{_format_clock(start)}")
            if event.end:
                cursor = max(cursor, event.end.astimezone(settings.timezone))
        if closing - cursor >= timedelta(hours=1):
            slots.append(f"• {cursor:%a} {cursor.day} {cursor:%b}: {_format_clock(cursor)}–{_format_clock(closing)}")
    return "Free time (9 AM–6 PM):\n" + ("\n".join(slots[:8]) if slots else "No one-hour slots found.")


async def _ask_to_select(chat_id: int, action: str, request_text: str, events: list[CalendarEvent], match, telegram: TelegramClient) -> None:
    choices = [event for event in events if event.event_id in {candidate.event_id for candidate in match.candidates}] or events[:5]
    pending_actions[chat_id] = PendingAction(choices, time.monotonic() + PENDING_TTL_SECONDS, action, request_text)
    lines = [f"{i}. {event.title} — {_format_time(event.start)}" for i, event in enumerate(choices, 1)]
    verb = {"edit": "edit", "delete": "delete", "remind": "set a reminder for", "clear_reminder": "remove the reminder for"}[action]
    await telegram.send_message(chat_id, f"Which event should I {verb}? Reply with a number:\n" + "\n".join(lines))


def _selection(text: str, events: list[CalendarEvent]) -> CalendarEvent | None:
    match = re.search(r"\b([1-9]\d*)\b", text)
    if match and (index := int(match.group(1))) <= len(events):
        return events[index - 1]
    ordinals = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4}
    index = ordinals.get(text.lower().strip())
    return events[index] if index is not None and index < len(events) else None


def _format_time(value: datetime | None) -> str:
    if not value:
        return "time unavailable"
    hour = value.hour % 12 or 12
    return f"{value:%a} {value.day} {value:%b} {hour}:{value:%M} {value:%p}"


def _format_event_range(event: CalendarEvent) -> str:
    """Format one event with a compact but unambiguous date/time range."""
    start = event.start
    end = event.end
    if not end:
        return _format_time(start)
    start_date = f"{start:%a} {start.day} {start:%b}"
    start_time = _format_clock(start)
    end_time = _format_clock(end)
    if start.date() == end.date():
        return f"{start_date} · {start_time}–{end_time}"
    end_date = f"{end:%a} {end.day} {end:%b}"
    return f"{start_date} {start_time} → {end_date} {end_time}"


def _format_event_listing(event: CalendarEvent, index: int | None = None, bullet: bool = False) -> str:
    prefix = "•" if bullet else f"{index}."
    lines = [f"{prefix} {event.title}", f"   {_format_event_range(event)}"]
    if event.location:
        lines.append(f"   📍 {event.location}")
    return "\n".join(lines)


def _format_reminder_listing(event: CalendarEvent, index: int) -> str:
    assert event.reminder_minutes is not None
    reminder_at = event.start - timedelta(minutes=event.reminder_minutes)
    unit = "minute" if event.reminder_minutes == 1 else "minutes"
    return f"{index}. {event.title}\n   ⏰ {_format_time(reminder_at)} ({event.reminder_minutes} {unit} before)\n   📅 {_format_event_range(event)}"


def _reminder_minutes_from_text(text: str) -> int | None:
    """Handle common reminder expressions even if the LLM omits the optional field."""
    lowered = text.lower()
    match = re.search(r"\b(\d+)\s*(minutes?|mins?|hours?|hrs?|days?)\s+before\b", lowered)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        multiplier = 1440 if unit.startswith("day") else 60 if unit.startswith(("hour", "hr")) else 1
        return amount * multiplier
    words = {"one": 1, "a": 1, "an": 1, "two": 2, "three": 3}
    match = re.search(r"\b(one|a|an|two|three)\s+(minutes?|hours?|days?)\s+before\b", lowered)
    if match:
        multiplier = 1440 if match.group(2).startswith("day") else 60 if match.group(2).startswith("hour") else 1
        return words[match.group(1)] * multiplier
    return None


def _apply_recurrence_from_text(event, text: str) -> None:
    """Ensure common weekly recurrence wording never relies on optional LLM output."""
    match = re.search(r"\bevery\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", text.lower())
    if not match:
        return
    weekdays = {"monday": (0, "MO"), "tuesday": (1, "TU"), "wednesday": (2, "WE"), "thursday": (3, "TH"), "friday": (4, "FR"), "saturday": (5, "SA"), "sunday": (6, "SU")}
    target_day, rrule_day = weekdays[match.group(1)]
    offset = (target_day - event.start.weekday()) % 7
    if offset:
        event.start += timedelta(days=offset)
        event.end += timedelta(days=offset)
    event.recurrence = f"RRULE:FREQ=WEEKLY;BYDAY={rrule_day}"


def _format_clock(value: datetime) -> str:
    hour = value.hour % 12 or 12
    return f"{hour}:{value:%M} {value:%p}"
