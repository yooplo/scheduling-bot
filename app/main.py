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
from .models import CalendarEvent, ParsedEdit, ReminderSpec, ScheduledReminder
from .parser import GroqParser, ParseError
from .telegram_client import TelegramClient, valid_webhook_secret

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = FastAPI(title="Telegram Calendar Bot")

DELETE_WORDS = ("delete", "cancel", "remove")
EDIT_WORDS = ("change", "edit", "move", "reschedule", "update")
REMINDER_PREFIXES = ("set a reminder", "add a reminder", "remind me")
REMINDER_LIST_PHRASES = ("reminders", "upcoming reminders", "all reminders", "show reminders", "my reminders")
FREE_TIME_PHRASES = ("when am i free", "when i'm free", "find free time", "free timing", "free slot", "availability")
LIST_WORDS = ("list", "show", "what's on", "whats on", "what are my", "upcoming", "calendar", "plan", "plans", "schedule")
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
_calendars: dict[int, CalendarClient] | None = None
_parser: GroqParser | None = None


def dependencies() -> tuple[Settings, TelegramClient, dict[int, CalendarClient], GroqParser]:
    global _settings, _telegram, _calendars, _parser
    if _settings is None:
        _settings = get_settings()
        _telegram = TelegramClient(_settings.telegram_bot_token)
        _calendars = {
            account.telegram_user_id: CalendarClient(_settings, account)
            for account in _settings.calendar_accounts
        }
        _parser = GroqParser(_settings.groq_api_key, _settings.groq_model)
    return _settings, _telegram, _calendars, _parser


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)) -> dict[str, bool]:
    try:
        settings, telegram, calendars, parser = dependencies()
    except ConfigurationError:
        logger.exception("Invalid configuration")
        raise HTTPException(status_code=503, detail="Service is not configured")
    if not valid_webhook_secret(x_telegram_bot_api_secret_token, settings.telegram_webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid webhook secret")
    update = await request.json()
    message = update.get("message") or {}
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    sender_id = message.get("from", {}).get("id")
    first_name = (message.get("from", {}).get("first_name") or "").strip()
    text = (message.get("text") or "").strip()
    calendar = calendars.get(sender_id)
    # Private chats prevent a permitted user from exposing their calendar to a
    # group, and ensure each response stays with its paired Telegram account.
    if not chat_id or chat.get("type") != "private" or not text:
        return {"ok": True}
    if calendar is None:
        await telegram.send_message(chat_id, _unauthorised_message())
        return {"ok": True}
    try:
        if re.match(r"^/start(?:@\w+)?(?:\s|$)", text, flags=re.IGNORECASE):
            await telegram.send_message(chat_id, _welcome_message(first_name))
            return {"ok": True}
        await handle_message(chat_id, text, settings, telegram, calendar, parser)
    except Exception:
        logger.exception("Failed handling Telegram message chat_id=%s", chat_id)
        await telegram.send_message(chat_id, "Sorry, I couldn't complete that. Please try again.")
    return {"ok": True}


@app.post("/scheduled/reminders")
async def scheduled_reminders(authorization: str | None = Header(default=None)) -> Response:
    settings, telegram, calendars, _ = dependencies()
    if authorization != f"Bearer {settings.scheduler_secret}":
        raise HTTPException(status_code=401, detail="Invalid scheduler secret")
    for telegram_user_id, calendar in calendars.items():
        reminders = await asyncio.to_thread(calendar.due_reminders)
        for reminder in reminders:
            text = reminder.reminder.message or (f"Reminder: {reminder.event_title} starts at {_format_time(reminder.due_at + timedelta(minutes=reminder.reminder.minutes_before or 0))}" if reminder.event_title else "Reminder")
            await telegram.send_message(telegram_user_id, f"⏰ {text}")
            await asyncio.to_thread(calendar.mark_reminder_sent, reminder.event_id, reminder.reminder.reminder_id, reminder.calendar_id)
    return Response(status_code=204)


@app.post("/scheduled/daily-agenda")
async def scheduled_daily_agenda(authorization: str | None = Header(default=None)) -> Response:
    settings, telegram, calendars, _ = dependencies()
    if authorization != f"Bearer {settings.scheduler_secret}":
        raise HTTPException(status_code=401, detail="Invalid scheduler secret")
    for telegram_user_id, calendar in calendars.items():
        events = await asyncio.to_thread(calendar.list_events, 1)
        lines = [_format_event_listing(event, bullet=True) for event in events]
        await telegram.send_message(telegram_user_id, "☀️ Today's agenda:\n\n" + ("\n\n".join(lines) if lines else "No upcoming events today."))
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
                if selected.is_standalone_reminder:
                    await asyncio.to_thread(calendar.delete_event, selected)
                else:
                    await asyncio.to_thread(calendar.clear_reminder, selected)
                await telegram.send_message(chat_id, f"🔕 Reminder removed: {selected.title}")
            else:
                await _set_reminder(chat_id, pending.request_text, selected, settings, telegram, calendar, parser)
            return
        pending_actions.pop(chat_id, None)
    lowered = text.lower()
    if any(phrase in lowered for phrase in FREE_TIME_PHRASES):
        tomorrow = "tomorrow" in lowered or "tmr" in lowered
        explicit_day = _date_from_text(lowered, settings)
        if tomorrow or explicit_day:
            target_day = explicit_day or (datetime.now(settings.timezone).date() + timedelta(days=1))
            events = await asyncio.to_thread(calendar.list_events_for_day, target_day)
            await telegram.send_message(chat_id, _format_free_slots(events, settings, 1, target_day))
        else:
            events = await asyncio.to_thread(calendar.list_events, 7)
            await telegram.send_message(chat_id, _format_free_slots(events, settings, 7))
    elif _is_calendar_list_request(lowered):
        calendars = await asyncio.to_thread(calendar.list_calendars)
        await telegram.send_message(chat_id, _format_calendar_list(calendars))
    elif _is_standalone_reminder_request(lowered):
        standalone = await asyncio.to_thread(parser.parse_standalone_reminder, text, datetime.now(settings.timezone), settings.user_timezone)
        if standalone.confidence == "low" or standalone.due_at.tzinfo is None:
            await telegram.send_message(chat_id, "Tell me what to remind you about and when, for example: 'remind me to pay the bill tomorrow at 9am'.")
            return
        await asyncio.to_thread(calendar.create_standalone_reminder, standalone.message, standalone.due_at)
        await telegram.send_message(chat_id, f"⏰ Reminder set: {standalone.message}\n📅 {_format_time(standalone.due_at)}")
    elif "reminder" in lowered and any(word in lowered for word in ("remove", "disable", "cancel", "delete")):
        events = await asyncio.to_thread(calendar.list_events, 30)
        events += await asyncio.to_thread(calendar.list_standalone_reminder_events, 30)
        match = await asyncio.to_thread(parser.match_event, text, events)
        selected = next((event for event in events if event.event_id == match.matched_event_id), None)
        if selected and not match.ambiguous:
            if selected.is_standalone_reminder:
                await asyncio.to_thread(calendar.delete_event, selected)
            else:
                await asyncio.to_thread(calendar.clear_reminder, selected)
            await telegram.send_message(chat_id, f"🔕 Reminder removed: {selected.title}")
            return
        await _ask_to_select(chat_id, "clear_reminder", text, events, match, telegram)
    elif any(phrase in lowered for phrase in REMINDER_LIST_PHRASES):
        reminders = [reminder for reminder in await asyncio.to_thread(calendar.list_reminders, 30) if reminder.due_at >= datetime.now(settings.timezone)]
        if not reminders:
            await telegram.send_message(chat_id, "No upcoming Telegram reminders.")
        else:
            lines = [_format_reminder_listing(reminder, index) for index, reminder in enumerate(reminders, 1)]
            await telegram.send_message(chat_id, "Upcoming reminders:\n\n" + "\n\n".join(lines))
    elif any(word in lowered for word in DELETE_WORDS):
        events = await asyncio.to_thread(calendar.list_events, 30)
        if not events:
            await telegram.send_message(chat_id, "There are no events in the next 30 days to delete.")
            return
        if ("weekly" in lowered or "recurring" in lowered) and (series_matches := [event for event in events if event.title.lower() in lowered and event.recurring_event_id]):
            unique_series = {event.recurring_event_id for event in series_matches}
            if len(unique_series) == 1:
                await asyncio.to_thread(calendar.delete_series, series_matches[0])
                await telegram.send_message(chat_id, f"✅ Deleted recurring series: {series_matches[0].title}")
                return
        match = await asyncio.to_thread(parser.match_event, text, events)
        selected = next((e for e in events if e.event_id == match.matched_event_id), None)
        if selected and not match.ambiguous:
            await asyncio.to_thread(calendar.delete_event, selected)
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
    elif any(word in lowered for word in LIST_WORDS) and not _is_explicit_add_request(lowered):
        explicit_day = _date_from_text(lowered, settings)
        weekday = _upcoming_weekday_from_text(lowered, settings)
        if "tomorrow" in lowered or "tmr" in lowered or explicit_day or weekday:
            target = explicit_day or weekday or (datetime.now(settings.timezone).date() + timedelta(days=1))
            events = await asyncio.to_thread(calendar.list_events_for_day, target)
            heading = "Tomorrow's events" if not explicit_day and not weekday else f"Events on {target:%a} {target.day} {target:%b}"
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
        event.reminders = _reminders_from_text(text)
        if not event.reminders and event.reminder_minutes:
            event.reminders = [ReminderSpec(minutes_before=event.reminder_minutes, message=_reminder_message_from_text(text))]
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
        target_calendar = await asyncio.to_thread(calendar.resolve_calendar, event.calendar_name)
        if event.calendar_name and target_calendar is None:
            await telegram.send_message(chat_id, f"I couldn't find a calendar named '{event.calendar_name}'. Send 'calendar types' to see your available calendars.")
            return
        if target_calendar and target_calendar.access_role not in {"owner", "writer"}:
            await telegram.send_message(chat_id, f"I can see {target_calendar.name}, but you do not have permission to add events to it.")
            return
        created = await asyncio.to_thread(calendar.create_event, event, target_calendar.calendar_id if target_calendar else None)
        reminder_confirmation = ""
        if event.reminders:
            reminder_confirmation = "\n⏰ " + _reminder_confirmation(event.reminders)
        recurrence_confirmation = "\n🔁 Repeats weekly" if event.recurrence else ""
        calendar_confirmation = f"\n🗓️ Calendar: {target_calendar.name}" if target_calendar else ""
        await telegram.send_message(chat_id, f"✅ Added: {created.title} — {_format_time(created.start)}–{_format_time(created.end)}{calendar_confirmation}{recurrence_confirmation}{reminder_confirmation}")


async def _delete_event(chat_id: int, event: CalendarEvent, telegram: TelegramClient, calendar: CalendarClient) -> None:
    await asyncio.to_thread(calendar.delete_event, event)
    await telegram.send_message(chat_id, f"✅ Deleted: {event.title} — {_format_time(event.start)}")


async def _edit_event(chat_id: int, text: str, existing: CalendarEvent, settings: Settings, telegram: TelegramClient, calendar: CalendarClient, parser: GroqParser) -> None:
    location_match = re.search(r"\b(?:to\s+be\s+)?at\s+(.+?)\s*$", text, flags=re.IGNORECASE)
    if location_match:
        edited = ParsedEdit(
            title=existing.title, start=existing.start, end=existing.end or existing.start,
            location=location_match.group(1).strip(), confidence="high",
        )
    else:
        edited = await asyncio.to_thread(parser.parse_edit, text, existing, settings.user_timezone)
    if edited.confidence == "low" or edited.start.tzinfo is None or edited.end.tzinfo is None:
        await telegram.send_message(chat_id, "I need a clearer change. For example: 'move IPPT on Saturday to 4pm'.")
        return
    if _is_series_edit(text, existing):
        _apply_recurrence_from_text(edited, text)
        updated = await asyncio.to_thread(calendar.update_series, existing, edited)
        await telegram.send_message(chat_id, _format_series_update_confirmation(updated, edited.recurrence))
        return
    updated = await asyncio.to_thread(calendar.update_event, existing, edited)
    await telegram.send_message(chat_id, _format_update_confirmation(updated))


async def _set_reminder(chat_id: int, text: str, event: CalendarEvent, settings: Settings, telegram: TelegramClient, calendar: CalendarClient, parser: GroqParser) -> None:
    reminder = await asyncio.to_thread(parser.parse_reminder_for_event, text, event, settings.user_timezone)
    if reminder.confidence == "low":
        await telegram.send_message(chat_id, "Tell me when to remind you, for example: 'set a reminder one day before IPPT'.")
        return
    message = _reminder_message_from_text(text)
    append = any(word in text.lower() for word in ("another", "also", "additional"))
    await asyncio.to_thread(calendar.set_reminder, event, reminder.reminder_minutes, message, append)
    unit = "minute" if reminder.reminder_minutes == 1 else "minutes"
    action = "Additional reminder set" if append else "Reminder set"
    message_line = f"\n📝 {message}" if message else ""
    await telegram.send_message(chat_id, f"⏰ {action}: {event.title} — {reminder.reminder_minutes} {unit} before {_format_time(event.start)}{message_line}")


def _is_existing_reminder_request(text: str) -> bool:
    if text.startswith(REMINDER_PREFIXES):
        return True
    return "reminder" in text and (any(word in text for word in EDIT_WORDS) or "another reminder" in text or "additional reminder" in text)


def _is_standalone_reminder_request(text: str) -> bool:
    return text.startswith("remind me to ") or text.startswith("remind me at ")


def _is_calendar_list_request(text: str) -> bool:
    return any(phrase in text for phrase in ("calendar types", "list calendars", "show calendars", "my calendars", "what calendars"))


def _is_explicit_add_request(text: str) -> bool:
    return bool(re.match(r"^(?:add|create|put)\b", text))


def _is_series_edit(text: str, event: CalendarEvent) -> bool:
    """Require explicit series wording so ordinary edits affect one occurrence."""
    if not event.recurring_event_id:
        return False
    lowered = text.lower()
    return (
        any(phrase in lowered for phrase in ("series", "recurring", "weekly", "every "))
        or ("all" in lowered and any(word in lowered for word in ("session", "sessions", "occurrence", "occurrences")))
    )


def _format_free_slots(events: list[CalendarEvent], settings: Settings, days: int, start_day=None) -> str:
    now = datetime.now(settings.timezone)
    first_day = start_day or now.date()
    days_to_check = [first_day + timedelta(days=offset) for offset in range(1 if days == 1 else days)]
    slots: list[str] = []
    for day in days_to_check:
        day_events = [event for event in events if event.start.astimezone(settings.timezone).date() == day]
        cursor = datetime.combine(day, datetime.min.time(), tzinfo=settings.timezone)
        closing = cursor.replace(hour=23, minute=59)
        for event in sorted(day_events, key=lambda item: item.start):
            start = event.start.astimezone(settings.timezone)
            if start - cursor >= timedelta(hours=1):
                slots.append(f"• {cursor:%a} {cursor.day} {cursor:%b}: {_format_clock(cursor)}–{_format_clock(start)}")
            if event.end:
                cursor = max(cursor, event.end.astimezone(settings.timezone))
        if closing - cursor >= timedelta(hours=1):
            slots.append(f"• {cursor:%a} {cursor.day} {cursor:%b}: {_format_clock(cursor)}–{_format_clock(closing)}")
    return "Free time (12:00 AM–11:59 PM):\n" + ("\n".join(slots[:8]) if slots else "No one-hour slots found.")


def _date_from_text(text: str, settings: Settings):
    match = re.search(
        r"\b(\d{1,2})\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
        text,
    )
    if not match:
        return None
    year = datetime.now(settings.timezone).year
    value = f"{match.group(1)} {match.group(2)} {year}"
    for format_string in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(value, format_string).date()
        except ValueError:
            continue
    return None


def _upcoming_weekday_from_text(text: str, settings: Settings):
    match = re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", text, flags=re.IGNORECASE)
    if not match:
        return None
    weekday_names = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    today = datetime.now(settings.timezone).date()
    offset = (weekday_names.index(match.group(1).lower()) - today.weekday()) % 7
    return today + timedelta(days=offset)


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


def _welcome_message(first_name: str) -> str:
    name = first_name or "there"
    return (
        f"Hi {name}! 👋 Welcome to SchedulingBot.\n\n"
        "I can manage your personal Google Calendar. Try:\n"
        "• Dinner tomorrow 7–9pm at La Pasta\n"
        "• What are my plans on 19 Aug?\n"
        "• When am I free tomorrow?\n"
        "• Remind me 30 minutes before IPPT\n\n"
        "Send ‘list’ to see upcoming events."
    )


def _unauthorised_message() -> str:
    return "Sorry, you do not have permission to use this bot. Please contact @juzteeeen for access."


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
    if event.calendar_name:
        lines.append(f"   🗓️ {event.calendar_name}")
    if event.location:
        lines.append(f"   📍 {event.location}")
    return "\n".join(lines)


def _format_update_confirmation(event: CalendarEvent) -> str:
    lines = [f"✅ Updated: {event.title}", f"📅 {_format_event_range(event)}"]
    if event.calendar_name:
        lines.append(f"🗓️ {event.calendar_name}")
    if event.location:
        lines.append(f"📍 {event.location}")
    return "\n".join(lines)


def _format_series_update_confirmation(event: CalendarEvent, recurrence: str | None) -> str:
    lines = [f"✅ Updated recurring series: {event.title}", f"📅 {_format_event_range(event)}"]
    if event.location:
        lines.append(f"📍 {event.location}")
    if recurrence:
        lines.append("🔁 Recurrence rule updated")
    return "\n".join(lines)


def _format_calendar_list(calendars) -> str:
    if not calendars:
        return "I couldn't find any calendars available to this Google account."
    lines = []
    for calendar in calendars:
        default = " (default)" if calendar.primary else ""
        lines.append(f"{_calendar_colour_emoji(calendar.background_color)} {calendar.name}{default}")
    return "Your calendars:\n\n" + "\n".join(lines)


def _calendar_colour_emoji(hex_colour: str | None) -> str:
    if not hex_colour or not re.fullmatch(r"#[0-9a-fA-F]{6}", hex_colour):
        return "⬜"
    red, green, blue = (int(hex_colour[index:index + 2], 16) for index in (1, 3, 5))
    palette = {
        "🟥": (220, 60, 50), "🟧": (245, 145, 45), "🟨": (235, 205, 50),
        "🟩": (80, 165, 85), "🟦": (65, 130, 220), "🟪": (155, 95, 190),
        "🟫": (135, 90, 55), "⬛": (35, 35, 35), "⬜": (235, 235, 235),
    }
    return min(palette, key=lambda emoji: sum((component - reference) ** 2 for component, reference in zip((red, green, blue), palette[emoji])))


def _format_reminder_listing(reminder: ScheduledReminder, index: int) -> str:
    text = reminder.reminder.message or (f"{reminder.event_title} reminder" if reminder.event_title else "Standalone reminder")
    if reminder.standalone:
        return f"{index}. {text}\n   ⏰ {_format_time(reminder.due_at)}"
    minutes = reminder.reminder.minutes_before or 0
    unit = "minute" if minutes == 1 else "minutes"
    return f"{index}. {text}\n   ⏰ {_format_time(reminder.due_at)} ({minutes} {unit} before)\n   📅 {reminder.event_title}"


def _reminder_confirmation(reminders: list[ReminderSpec]) -> str:
    leads = []
    for reminder in reminders:
        minutes = reminder.minutes_before or 0
        unit = "minute" if minutes == 1 else "minutes"
        suffix = f" — {reminder.message}" if reminder.message else ""
        leads.append(f"{minutes} {unit} before{suffix}")
    return "Reminders set: " + "; ".join(leads)


def _reminder_message_from_text(text: str) -> str | None:
    match = re.search(r"\bbefore\s*(?:to\s+|:\s*)(.+)$", text, flags=re.IGNORECASE)
    return match.group(1).strip() if match else None


def _reminders_from_text(text: str) -> list[ReminderSpec]:
    reminders: list[ReminderSpec] = []
    for match in re.finditer(r"\b(\d+)\s*(minutes?|mins?|hours?|hrs?|days?)\s+before\b", text, flags=re.IGNORECASE):
        amount = int(match.group(1))
        unit = match.group(2).lower()
        minutes = amount * (1440 if unit.startswith("day") else 60 if unit.startswith(("hour", "hr")) else 1)
        remainder = text[match.end():]
        message_match = re.match(r"\s*(?:to\s+|:\s*)([^,;.]+)", remainder, flags=re.IGNORECASE)
        message = message_match.group(1).strip() if message_match else None
        if message:
            message = re.split(r"\s+and\s+\d+\s*(?:minutes?|mins?|hours?|hrs?|days?)\s+before\b", message, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        reminders.append(ReminderSpec(minutes_before=minutes, message=message))
    return reminders


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
