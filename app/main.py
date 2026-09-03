from __future__ import annotations

import asyncio
import colorsys
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import FastAPI, Header, HTTPException, Request, Response

from .calendar_client import CalendarClient
from .config import ConfigurationError, Settings, get_settings
from .cron_client import CronJobClient
from .models import CalendarEvent, CalendarInfo, ParsedEdit, ParsedEvent, ReminderSpec, ScheduledReminder
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
LIST_WORDS = ("list", "show", "what's on", "whats on", "what are my", "upcoming", "plan", "plans", "schedule")
PENDING_TTL_SECONDS = 300


@dataclass
class PendingAction:
    events: list[CalendarEvent]
    expires_at: float
    action: str
    request_text: str


@dataclass
class PendingCalendarDeletion:
    calendar: CalendarInfo
    expires_at: float


@dataclass
class PendingReminderList:
    reminders: list[ScheduledReminder]
    expires_at: float


pending_actions: dict[int, PendingAction] = {}
pending_calendar_deletions: dict[int, PendingCalendarDeletion] = {}
recent_reminder_lists: dict[int, PendingReminderList] = {}
_settings: Settings | None = None
_telegram: TelegramClient | None = None
_calendars: dict[int, CalendarClient] | None = None
_parser: GroqParser | None = None
_cron_client: CronJobClient | None = None


def dependencies() -> tuple[Settings, TelegramClient, dict[int, CalendarClient], GroqParser, CronJobClient | None]:
    global _settings, _telegram, _calendars, _parser, _cron_client
    if _settings is None or _telegram is None or _calendars is None or _parser is None:
        # Construct the complete dependency graph locally. Publishing globals
        # one at a time leaves a poisoned partial state if OAuth or discovery
        # initialization raises, causing later requests to receive None values.
        settings = get_settings()
        telegram = TelegramClient(settings.telegram_bot_token)
        calendars = {
            account.telegram_user_id: CalendarClient(settings, account)
            for account in settings.calendar_accounts
        }
        parser = GroqParser(settings.groq_api_key, settings.groq_model)
        cron_client = None
        if settings.cron_job_api_key and settings.service_base_url:
            cron_client = CronJobClient(
                settings.cron_job_api_key, settings.service_base_url,
                settings.scheduler_secret, settings.user_timezone,
            )
        _settings, _telegram, _calendars, _parser, _cron_client = (
            settings, telegram, calendars, parser, cron_client,
        )
    return _settings, _telegram, _calendars, _parser, _cron_client


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)) -> dict[str, bool]:
    try:
        settings, telegram, calendars, parser, cron = dependencies()
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
    if not chat_id or not text:
        return {"ok": True}
    if chat.get("type") in {"group", "supergroup"} and chat_id != settings.telegram_group_id:
        return {"ok": True}
    if calendar is None:
        await telegram.send_message(chat_id, _unauthorised_message())
        return {"ok": True}
    try:
        if chat.get("type") in {"group", "supergroup"}:
            await handle_group_schedule(chat_id, sender_id, text, settings, telegram, calendars)
            return {"ok": True}
        if chat.get("type") != "private":
            return {"ok": True}
        if re.match(r"^/start(?:@\w+)?(?:\s|$)", text, flags=re.IGNORECASE):
            await telegram.send_message(chat_id, _welcome_message(first_name))
            return {"ok": True}
        await handle_message(chat_id, text, settings, telegram, calendar, parser, cron)
    except Exception:
        logger.exception("Failed handling Telegram message chat_id=%s", chat_id)
        await telegram.send_message(chat_id, "Sorry, I couldn't complete that. Please try again.")
    return {"ok": True}


async def handle_group_schedule(
    chat_id: int,
    sender_id: int,
    text: str,
    settings: Settings,
    telegram: TelegramClient,
    calendars: dict[int, CalendarClient],
) -> None:
    """Serve full-detail, read-only schedules in the single allowed group."""
    lowered = text.lower()
    if not any(word in lowered for word in LIST_WORDS):
        await telegram.send_message(chat_id, "Group access is read-only. Ask me to check a schedule.")
        return
    mentioned = [
        account for account in settings.calendar_accounts
        if account.telegram_username and re.search(rf"(?<!\w)@{re.escape(account.telegram_username)}(?!\w)", lowered)
    ]
    if len(mentioned) > 1:
        await telegram.send_message(chat_id, "Please check one person's schedule at a time.")
        return
    target = mentioned[0] if mentioned else settings.account_for(sender_id)
    if target is None:
        await telegram.send_message(chat_id, _unauthorised_message())
        return

    calendar = calendars[target.telegram_user_id]
    explicit_day = _date_from_text(lowered, settings)
    weekday = _upcoming_weekday_from_text(lowered, settings)
    if "tomorrow" in lowered or "tmr" in lowered or explicit_day or weekday:
        day = explicit_day or weekday or (datetime.now(settings.timezone).date() + timedelta(days=1))
        events = await asyncio.to_thread(calendar.list_events_for_day, day)
        heading = f"@{target.telegram_username or target.telegram_user_id} — {day:%A, %d %B}"
    elif "today" in lowered:
        day = datetime.now(settings.timezone).date()
        events = await asyncio.to_thread(calendar.list_events_for_day, day)
        heading = f"@{target.telegram_username or target.telegram_user_id} — {day:%A, %d %B}"
    else:
        events = await asyncio.to_thread(calendar.list_events, 7)
        heading = f"@{target.telegram_username or target.telegram_user_id} — upcoming events"
    body = "\n\n".join(_format_event_listing(event, index=index) for index, event in enumerate(events, 1))
    await telegram.send_message(chat_id, f"{heading}:\n\n{body or 'No events.'}")


@app.post("/scheduled/reminders")
async def scheduled_reminders(authorization: str | None = Header(default=None)) -> Response:
    settings, telegram, calendars, _, _ = dependencies()
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
    settings, telegram, calendars, _, _ = dependencies()
    if authorization != f"Bearer {settings.scheduler_secret}":
        raise HTTPException(status_code=401, detail="Invalid scheduler secret")
    for telegram_user_id, calendar in calendars.items():
        events = await asyncio.to_thread(calendar.list_events, 1)
        lines = [_format_event_listing(event, bullet=True) for event in events]
        await telegram.send_message(telegram_user_id, "☀️ Today's agenda:\n\n" + ("\n\n".join(lines) if lines else "No upcoming events today."))
    return Response(status_code=204)


@app.post("/scheduled/standalone-reminder")
async def scheduled_standalone_reminder(request: Request, authorization: str | None = Header(default=None)) -> Response:
    settings, telegram, calendars, _, cron = dependencies()
    if authorization != f"Bearer {settings.scheduler_secret}":
        raise HTTPException(status_code=401, detail="Invalid scheduler secret")
    payload = await request.json()
    try:
        telegram_user_id = int(payload.get("telegram_user_id", 0))
        job_id = int(payload.get("job_id", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid standalone reminder")
    message = str(payload.get("message", "")).strip()
    if telegram_user_id not in calendars or not message or not job_id:
        raise HTTPException(status_code=400, detail="Invalid standalone reminder")
    await telegram.send_message(telegram_user_id, f"⏰ {message}")
    if cron:
        try:
            await cron.delete_reminder(job_id)
        except Exception:
            logger.exception("Delivered standalone reminder but could not delete cron job job_id=%s", job_id)
    return Response(status_code=204)


async def handle_message(chat_id: int, text: str, settings: Settings, telegram: TelegramClient, calendar: CalendarClient, parser: GroqParser, cron: CronJobClient | None = None) -> None:
    command = _telegram_command(text)
    lowered = command if command in {"reminders", "calendars", "now"} else text.lower().strip()
    recent_reminders = recent_reminder_lists.get(chat_id)
    if recent_reminders and recent_reminders.expires_at <= time.monotonic():
        recent_reminder_lists.pop(chat_id, None)
        recent_reminders = None
    numbered_removal = re.fullmatch(r"(?:remove|delete|cancel)\s+([1-9]\d*)", lowered)
    if recent_reminders and numbered_removal:
        index = int(numbered_removal.group(1)) - 1
        if index >= len(recent_reminders.reminders):
            await telegram.send_message(chat_id, f"Choose a reminder number from 1 to {len(recent_reminders.reminders)}.")
            return
        selected_reminder = recent_reminders.reminders[index]
        recent_reminder_lists.pop(chat_id, None)
        await _remove_scheduled_reminder(selected_reminder, calendar, cron)
        label = selected_reminder.reminder.message or selected_reminder.event_title or "Reminder"
        await telegram.send_message(chat_id, f"🔕 Reminder removed: {label}")
        return
    if recent_reminders and not any(phrase in lowered for phrase in REMINDER_LIST_PHRASES):
        recent_reminder_lists.pop(chat_id, None)

    pending_calendar = pending_calendar_deletions.get(chat_id)
    if pending_calendar and pending_calendar.expires_at <= time.monotonic():
        pending_calendar_deletions.pop(chat_id, None)
        pending_calendar = None
    if pending_calendar and lowered in {"confirm delete calendar", "/confirm_delete_calendar"}:
        pending_calendar_deletions.pop(chat_id, None)
        await asyncio.to_thread(calendar.delete_calendar, pending_calendar.calendar)
        await telegram.send_message(chat_id, f"✅ Deleted calendar: {pending_calendar.calendar.name}")
        return
    if pending_calendar and lowered in {"cancel", "cancel delete calendar", "/cancel"}:
        pending_calendar_deletions.pop(chat_id, None)
        await telegram.send_message(chat_id, "Calendar deletion cancelled.")
        return

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
                if selected.event_id.startswith("cron:") and cron:
                    await cron.delete_reminder(int(selected.event_id.removeprefix("cron:")))
                elif selected.is_standalone_reminder:
                    await asyncio.to_thread(calendar.delete_event, selected)
                else:
                    await asyncio.to_thread(calendar.clear_reminder, selected)
                await telegram.send_message(chat_id, f"🔕 Reminder removed: {selected.title}")
            else:
                await _set_reminder(chat_id, pending.request_text, selected, settings, telegram, calendar, parser)
            return
        pending_actions.pop(chat_id, None)
    if command == "now":
        now = datetime.now(settings.timezone)
        await telegram.send_message(chat_id, f"🕒 {now:%A}, {now.day} {now:%B %Y} · {_format_clock(now)}\n🌏 {settings.user_timezone}")
        return
    if calendar_name := _calendar_create_name(text):
        existing = await asyncio.to_thread(calendar.list_calendars)
        if any(item.name.casefold() == calendar_name.casefold() for item in existing):
            await telegram.send_message(chat_id, f"A calendar named '{calendar_name}' already exists.")
            return
        created_calendar = await asyncio.to_thread(calendar.create_calendar, calendar_name)
        await telegram.send_message(chat_id, f"✅ Created calendar: {created_calendar.name}")
    elif calendar_name := _calendar_delete_name(text):
        target_calendar = await asyncio.to_thread(calendar.resolve_calendar, calendar_name)
        if target_calendar is None:
            await telegram.send_message(chat_id, f"I couldn't find a calendar named '{calendar_name}'. Send 'calendars' to see available calendars.")
            return
        if target_calendar.primary:
            await telegram.send_message(chat_id, "The primary Google Calendar cannot be deleted.")
            return
        if target_calendar.access_role != "owner":
            await telegram.send_message(chat_id, f"You do not own {target_calendar.name}, so I cannot delete it.")
            return
        pending_calendar_deletions[chat_id] = PendingCalendarDeletion(
            target_calendar, time.monotonic() + PENDING_TTL_SECONDS,
        )
        await telegram.send_message(
            chat_id,
            f"⚠️ Delete the entire calendar '{target_calendar.name}' and all of its events? Reply 'confirm delete calendar' within 5 minutes, or 'cancel'.",
        )
    elif any(phrase in lowered for phrase in FREE_TIME_PHRASES):
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
        if not cron:
            await telegram.send_message(chat_id, "Independent reminders are not configured. Add CRON_JOB_API_KEY and SERVICE_BASE_URL to the service environment.")
            return
        now = datetime.now(settings.timezone)
        standalone = await asyncio.to_thread(parser.parse_standalone_reminder, text, now, settings.user_timezone)
        _apply_standalone_clock_from_text(standalone, text, now)
        if standalone.confidence == "low" or standalone.due_at.tzinfo is None:
            await telegram.send_message(chat_id, "Tell me what to remind you about and when, for example: 'remind me to pay the bill tomorrow at 9am'.")
            return
        await cron.create_reminder(chat_id, standalone.message, standalone.due_at)
        await telegram.send_message(chat_id, f"⏰ Reminder set: {standalone.message}\n📅 {_format_time(standalone.due_at)}")
    elif "reminder" in lowered and any(word in lowered for word in ("remove", "disable", "cancel", "delete")):
        events = await asyncio.to_thread(calendar.list_events, 30)
        events += await asyncio.to_thread(calendar.list_standalone_reminder_events, 30)
        if cron:
            cron_reminders = await cron.list_reminders(chat_id)
            events += [CalendarEvent(
                event_id=reminder.event_id, title=reminder.reminder.message or "Reminder",
                start=reminder.due_at, end=reminder.due_at, is_standalone_reminder=True,
            ) for reminder in cron_reminders]
        match = await asyncio.to_thread(parser.match_event, text, events)
        selected = next((event for event in events if event.event_id == match.matched_event_id), None)
        if selected and not match.ambiguous:
            if selected.event_id.startswith("cron:") and cron:
                await cron.delete_reminder(int(selected.event_id.removeprefix("cron:")))
            elif selected.is_standalone_reminder:
                await asyncio.to_thread(calendar.delete_event, selected)
            else:
                await asyncio.to_thread(calendar.clear_reminder, selected)
            await telegram.send_message(chat_id, f"🔕 Reminder removed: {selected.title}")
            return
        await _ask_to_select(chat_id, "clear_reminder", text, events, match, telegram)
    elif any(phrase in lowered for phrase in REMINDER_LIST_PHRASES):
        now = datetime.now(settings.timezone)
        reminders = []
        unavailable_sources = []
        try:
            reminders.extend(
                reminder for reminder in await asyncio.to_thread(calendar.list_reminders, 30)
                if reminder.due_at >= now
            )
        except Exception:
            unavailable_sources.append("Google Calendar")
            logger.exception("Could not list Google Calendar reminders chat_id=%s", chat_id)
        if cron:
            try:
                reminders.extend(reminder for reminder in await cron.list_reminders(chat_id) if reminder.due_at >= now)
            except Exception:
                unavailable_sources.append("independent reminders")
                logger.exception("Could not list cron-job.org reminders chat_id=%s", chat_id)
        reminders.sort(key=lambda reminder: reminder.due_at)
        if not reminders:
            recent_reminder_lists.pop(chat_id, None)
            if unavailable_sources:
                sources = " and ".join(unavailable_sources)
                await telegram.send_message(chat_id, f"I couldn't retrieve {sources} right now. Please try again shortly.")
            else:
                await telegram.send_message(chat_id, "No upcoming Telegram reminders.")
        else:
            recent_reminder_lists[chat_id] = PendingReminderList(
                reminders, time.monotonic() + PENDING_TTL_SECONDS,
            )
            lines = [_format_reminder_listing(reminder, index) for index, reminder in enumerate(reminders, 1)]
            footer = f"⚠️ Could not retrieve: {', '.join(unavailable_sources)}." if unavailable_sources else None
            for message in _chunk_section_message("Upcoming reminders:", lines, footer):
                await telegram.send_message(chat_id, message)
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
        exact_matches = [event for event in events if event.title.casefold() in lowered.casefold()]
        if len(exact_matches) == 1:
            await _delete_event(chat_id, exact_matches[0], telegram, calendar)
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
        if (
            _is_explicit_add_request(lowered)
            and _date_from_text(lowered, settings)
            and not _has_explicit_event_time(lowered)
            and not re.search(r"\ball[\s-]?day\b", lowered)
        ):
            await telegram.send_message(
                chat_id,
                "What time should I schedule it? Include a time, for example 'at 7pm', or say 'all day'.",
            )
            return
        now = datetime.now(settings.timezone)
        event = _explicit_all_day_event(text, settings) or await asyncio.to_thread(
            parser.parse_event, _event_parser_text(text), now, settings.user_timezone,
        )
        event.reminders = _reminders_from_text(text)
        if not event.reminders and event.reminder_minutes:
            event.reminders = [ReminderSpec(minutes_before=event.reminder_minutes, message=_reminder_message_from_text(text))]
        _apply_recurrence_from_text(event, text)
        _apply_all_day_from_text(event, text, settings)
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
        await telegram.send_message(chat_id, f"✅ Added: {created.title} — {_format_event_range(created)}{calendar_confirmation}{recurrence_confirmation}{reminder_confirmation}")


async def _delete_event(chat_id: int, event: CalendarEvent, telegram: TelegramClient, calendar: CalendarClient) -> None:
    await asyncio.to_thread(calendar.delete_event, event)
    await telegram.send_message(chat_id, f"✅ Deleted: {event.title} — {_format_time(event.start)}")


async def _remove_scheduled_reminder(reminder: ScheduledReminder, calendar: CalendarClient, cron: CronJobClient | None) -> None:
    if reminder.event_id.startswith("cron:"):
        if not cron:
            raise RuntimeError("Independent reminders are not configured")
        await cron.delete_reminder(int(reminder.event_id.removeprefix("cron:")))
    elif reminder.standalone:
        await asyncio.to_thread(calendar.delete_event, CalendarEvent(
            event_id=reminder.event_id,
            title=reminder.reminder.message or "Reminder",
            start=reminder.due_at,
            end=reminder.due_at,
            calendar_id=reminder.calendar_id,
            is_standalone_reminder=True,
        ))
    else:
        await asyncio.to_thread(
            calendar.remove_reminder,
            reminder.event_id, reminder.reminder.reminder_id, reminder.calendar_id,
        )


async def _edit_event(chat_id: int, text: str, existing: CalendarEvent, settings: Settings, telegram: TelegramClient, calendar: CalendarClient, parser: GroqParser) -> None:
    location_match = re.search(r"\b(?:to\s+be\s+)?at\s+(.+?)\s*$", text, flags=re.IGNORECASE)
    if location_match:
        edited = ParsedEdit(
            title=existing.title, start=existing.start, end=existing.end or existing.start,
            location=location_match.group(1).strip(), confidence="high", all_day=existing.all_day,
        )
    else:
        edited = await asyncio.to_thread(parser.parse_edit, text, existing, settings.user_timezone)
    _apply_all_day_from_text(edited, text, settings)
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
    lowered = text.strip().lower()
    if lowered.startswith(("remind me to ", "remind me at ", "remind me in ")):
        return True

    # A lead time links a reminder to an existing event (for example,
    # "set a reminder one day before IPPT"). An absolute/relative clock time
    # instead describes an independent notification.
    if re.search(r"\b(?:minutes?|mins?|hours?|hrs?|days?)\s+before\b", lowered):
        return False
    command = re.match(
        r"^(?:please\s+)?(?:set(?:\s+me)?|add)(?:\s+up)?\s+(?:a\s+)?reminder\b",
        lowered,
    )
    if not command:
        return False
    schedule = lowered[command.end():]
    return bool(re.search(
        r"\b(?:in\s+\d+\s*(?:minutes?|mins?|hours?|hrs?|days?)\b|"
        r"today|tonight|tomorrow|tmr|at\s+\d{1,2}(?::|\.)?\d{0,2}\s*(?:am|pm)?|"
        r"on\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{1,2}\b))",
        schedule,
    ))


def _is_calendar_list_request(text: str) -> bool:
    normalized = re.sub(r"[^a-z]+", " ", text.lower()).strip()
    compact = normalized.replace(" ", "")
    return (
        normalized in {"calendar", "calendars", "calendar list", "calendars list", "calendar types"}
        or compact in {"calendarlist", "calendarslist"}
        or any(phrase in normalized for phrase in ("list calendars", "show calendars", "my calendars", "what calendars"))
    )


def _telegram_command(text: str) -> str | None:
    """Return a normalized Telegram slash command, including @bot suffix forms."""
    match = re.match(r"^/([a-z][a-z0-9_]*)(?:@\w+)?(?:\s|$)", text.strip(), flags=re.IGNORECASE)
    return match.group(1).lower() if match else None


def _calendar_create_name(text: str) -> str | None:
    return _calendar_name_from_command(text, r"create|add|make", r"a(?:\s+new)?|new")


def _calendar_delete_name(text: str) -> str | None:
    return _calendar_name_from_command(text, r"delete|remove", r"the|my")


def _calendar_name_from_command(text: str, verbs: str, determiner: str) -> str | None:
    prefix = re.match(
        rf"^(?:{verbs})\s+(?:(?:{determiner})\s+)?calend[ae]r(?:\s+(?:called|named))?\s+(.+?)\s*$",
        text, flags=re.IGNORECASE,
    )
    if prefix:
        return _clean_calendar_name(prefix.group(1))

    suffix = re.match(
        rf"^(?:{verbs})\s+(?:(?:{determiner})\s+)?(.+?)\s+calend[ae]r\s*$",
        text, flags=re.IGNORECASE,
    )
    if not suffix:
        return None
    candidate = suffix.group(1).strip()
    # Suffix wording is inherently more ambiguous. Do not reinterpret an
    # event request such as "add meeting tomorrow in School calendar" as a
    # request to create a calendar with the entire event text as its name.
    if len(candidate.split()) > 6 or re.search(
        r"\b(?:in|on|at|from|tomorrow|today|tonight|every|"
        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|"
        r"\d{1,2}(?::|\.)\d{2}|\d{1,2}\s*(?:am|pm))\b",
        candidate, flags=re.IGNORECASE,
    ):
        return None
    return _clean_calendar_name(candidate)


def _clean_calendar_name(name: str) -> str | None:
    cleaned = name.strip().strip("\"'").strip()
    return cleaned[:300] or None


def _is_explicit_add_request(text: str) -> bool:
    return bool(re.match(r"^(?:add|create|put)\b", text))


def _has_explicit_event_time(text: str) -> bool:
    """Recognize clock times without mistaking a date or location for one."""
    lowered = text.lower()
    return bool(
        re.search(r"\b\d{1,2}(?::|\.)\d{2}\s*(?:am|pm)?\b", lowered)
        or re.search(r"\b\d{1,2}\s*(?:am|pm)\b", lowered)
        or re.search(r"\b(?:at|from)\s+\d{1,2}\s+(?:to|until|till|-)\s+\d{1,2}\b", lowered)
    )


def _event_parser_text(text: str) -> str:
    """Remove routing controls and normalize all-day synonyms before LLM parsing."""
    normalized = re.sub(r"^\s*add\s+anyway\b", "add", text, flags=re.IGNORECASE)
    return re.sub(r"\bwhole[\s-]+day\b", "all day", normalized, flags=re.IGNORECASE)


def _explicit_all_day_event(text: str, settings: Settings) -> ParsedEvent | None:
    """Parse concise '<day> whole day with <title>' requests without the LLM."""
    normalized = re.sub(r"^\s*add(?:\s+anyway)?\s+", "", text.strip(), flags=re.IGNORECASE)
    match = re.match(
        r"(?P<when>(?:on\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"\d{1,2}(?:st|nd|rd|th)?\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
        r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)(?:\s+\d{4})?))"
        r"\s+(?:all|whole)[\s-]+day\s+(?:with|for)\s+(?P<title>.+?)\s*$",
        normalized, flags=re.IGNORECASE,
    )
    if not match:
        return None
    when = re.sub(r"^on\s+", "", match.group("when"), flags=re.IGNORECASE).strip()
    weekday = _upcoming_weekday_from_text(when, settings)
    day = weekday or _calendar_date_from_text(when, settings)
    if day is None:
        return None
    start = datetime.combine(day, datetime.min.time(), tzinfo=settings.timezone)
    return ParsedEvent(
        title=match.group("title").strip(), start=start, end=start + timedelta(days=1),
        confidence="high", all_day=True,
    )


def _calendar_date_from_text(text: str, settings: Settings):
    match = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+"
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
        r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        r"(?:\s+(\d{4}))?\b",
        text, flags=re.IGNORECASE,
    )
    if not match:
        return None
    year = int(match.group(3) or datetime.now(settings.timezone).year)
    value = f"{match.group(1)} {match.group(2)} {year}"
    for format_string in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(value, format_string).date()
        except ValueError:
            continue
    return None


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
        "Commands: /reminders · /calendars · /now\n"
        "Send ‘list’ to see upcoming events."
    )


def _unauthorised_message() -> str:
    return "Sorry, you do not have permission to use this bot. Please contact @juzteeeen for access."


def _format_event_range(event: CalendarEvent) -> str:
    """Format one event with a compact but unambiguous date/time range."""
    start = event.start
    end = event.end
    if event.all_day:
        if not end or end.date() <= start.date() + timedelta(days=1):
            return f"{start:%a} {start.day} {start:%b} · All day"
        final_day = end.date() - timedelta(days=1)
        return f"{start:%a} {start.day} {start:%b} → {final_day:%a} {final_day.day} {final_day:%b} · All day"
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
    hue, saturation, value = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
    if saturation < 0.18:
        return "⬛" if value < 0.45 else "⬜"
    if hue < 0.04 or hue >= 0.95:
        return "🟥"
    if hue < 0.12:
        return "🟧"
    if hue < 0.20:
        return "🟨"
    if hue < 0.45:
        return "🟩"
    if hue < 0.70:
        return "🟦"
    if hue < 0.90:
        return "🟪"
    return "🟥"


def _format_reminder_listing(reminder: ScheduledReminder, index: int) -> str:
    text = reminder.reminder.message or (f"{reminder.event_title} reminder" if reminder.event_title else "Standalone reminder")
    if reminder.standalone:
        return f"{index}. {text}\n   🔔 Independent reminder\n   ⏰ {_format_time(reminder.due_at)}"
    minutes = reminder.reminder.minutes_before or 0
    unit = "minute" if minutes == 1 else "minutes"
    return f"{index}. {text}\n   🔗 Event reminder\n   ⏰ {_format_time(reminder.due_at)} ({minutes} {unit} before)\n   📅 {reminder.event_title}"


def _chunk_section_message(heading: str, sections: list[str], footer: str | None = None, limit: int = 3900) -> list[str]:
    """Keep structured Telegram replies below the platform message limit."""
    chunks = []
    current = heading
    for section in sections:
        candidate = current + "\n\n" + section
        if len(candidate) <= limit:
            current = candidate
            continue
        chunks.append(current)
        current = heading + " (continued):\n\n" + section
    if footer:
        candidate = current + "\n\n" + footer
        if len(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current)
            current = footer
    chunks.append(current)
    return chunks


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


def _apply_standalone_clock_from_text(reminder, text: str, now: datetime) -> None:
    """Keep dates inside reminder text from overriding an unqualified clock time."""
    command = re.match(
        r"^(?:(?:set|add)(?:\s+me)?\s+(?:a\s+)?reminder|remind\s+me)\b(?P<schedule>.*?)\bto\b",
        text.strip(), flags=re.IGNORECASE,
    )
    if not command:
        return
    schedule = command.group("schedule")
    clock = re.search(r"\b(?:at\s+)?(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)\b", schedule, flags=re.IGNORECASE)
    if not clock or re.search(
        r"\b(?:today|tonight|tomorrow|tmr|on|"
        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
        schedule, flags=re.IGNORECASE,
    ):
        return
    hour = int(clock.group(1))
    minute = int(clock.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return
    if clock.group(3).lower() == "pm" and hour != 12:
        hour += 12
    elif clock.group(3).lower() == "am" and hour == 12:
        hour = 0
    due_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if due_at <= now:
        due_at += timedelta(days=1)
    reminder.due_at = due_at


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


def _apply_all_day_from_text(event, text: str, settings: Settings) -> None:
    """Convert explicit all-day wording to native date-only boundaries."""
    if not re.search(r"\b(?:all|whole)[\s-]?day\b", text, flags=re.IGNORECASE):
        return
    local_start = event.start.astimezone(settings.timezone)
    local_end = event.end.astimezone(settings.timezone)
    start_day = local_start.date()
    end_day = local_end.date()
    if local_end.time() != datetime.min.time():
        end_day += timedelta(days=1)
    if end_day <= start_day:
        end_day = start_day + timedelta(days=1)
    event.start = datetime.combine(start_day, datetime.min.time(), tzinfo=settings.timezone)
    event.end = datetime.combine(end_day, datetime.min.time(), tzinfo=settings.timezone)
    event.all_day = True


def _format_clock(value: datetime) -> str:
    hour = value.hour % 12 or 12
    return f"{hour}:{value:%M} {value:%p}"
