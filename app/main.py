from __future__ import annotations

import asyncio
import colorsys
import logging
import os
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import wraps
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request, Response

from .calendar_client import CalendarClient, CalendarMoveError
from .config import ConfigurationError, Settings, get_settings
from .cron_client import CronJobClient
from .models import DeleteMatch, CalendarEvent, CalendarInfo, ParsedEdit, ParsedEvent, ParsedStandaloneReminder, ReminderSpec, ScheduledReminder
from .media import PHOTO_LIMIT, VOICE_LIMIT, interpret_media
from .message_formatting import format_media_preview
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
WEEKDAY_NAMES = r"mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?"
MONTH_NAMES = r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
NAMED_DATE_PATTERN = rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_NAMES})(?:\s+(\d{{4}}))?\b"
NUMERIC_DATE_PATTERN = r"\b(?:(\d{4})-(\d{1,2})-(\d{1,2})|(\d{1,2})[-/](\d{1,2})(?:[-/](\d{4}))?)\b(?![-/]\d)"
DATE_HINT_PATTERN = rf"\b(?:{WEEKDAY_NAMES}|{MONTH_NAMES}|today|tonight|tomorrow|tmr|next|this|last|following|after|before|weeks?|months?)\b|{NUMERIC_DATE_PATTERN}"
REMINDER_LEAD_PATTERN = r"\b(\d+(?:\.\d+)?|(?:one|two|three) and a half|one|an?|two|three|half(?: an?)?)\s*(minutes?|mins?|m|hours?|hrs?|h|days?|d)\s+before\b"
RECURRENCE_HINT_PATTERN = r"\b(?:every|repeat(?:s|ing)?|recurr(?:ing|ence)|daily|weekly|monthly|yearly|once|one[- ]off)\b"
WORD_CLOCK_PATTERN = r"\b(?:noon|midnight|midday)\b"
DURATION_HINT_PATTERN = r"\b(?:hours?|hrs?|minutes?|mins?|days?|weeks?|duration)\b|\b\d+(?:\.\d+)?\s*(?:hours?|hrs?|minutes?|mins?|days?|weeks?|[hm])\b"
TIME_HINT_PATTERN = r"\b\d{1,2}[:.]\d{2}|\b\d{1,2}\s*(?:am|pm)\b|\b(?:all|whole)[\s-]+day\b|\b(?:hours?|minutes?|hrs?|mins?|until|till|duration)\b" + "|" + WORD_CLOCK_PATTERN + "|" + DURATION_HINT_PATTERN
CLOCK_RANGE_PATTERN = re.compile(
    r"\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?\s*[-–—]\s*(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?\b",
    flags=re.IGNORECASE,
)


@dataclass
class PendingAction:
    events: list[CalendarEvent]
    expires_at: float
    action: str
    request_text: str
    token: str = ""
    calendars: list[CalendarInfo] = field(default_factory=list)
    series: bool = False
    deleted_resource: dict[str, object] | None = None


@dataclass
class PendingCalendarDeletion:
    calendar: CalendarInfo
    expires_at: float


@dataclass
class PendingReminderList:
    reminders: list[ScheduledReminder]
    expires_at: float


@dataclass
class PendingEventDraft:
    request_text: str
    now: datetime
    expires_at: float


pending_event_drafts: dict[int, PendingEventDraft] = {}
pending_media: dict[int, tuple[str, float]] = {}
standalone_deliveries_in_flight: set[tuple[int, int]] = set()
delivered_standalone_reminders: dict[tuple[int, int], float] = {}
reminder_delivery_history: dict[tuple[int, int], tuple[float, ScheduledReminder]] = {}
processed_message_updates: dict[tuple[int, int], float] = {}


@dataclass
class PendingEventConflict:
    event: ParsedEvent | ParsedEdit
    request_text: str
    now: datetime
    token: str
    expires_at: float
    existing: CalendarEvent | None = None
    series: bool = False
    suggestions: list[ParsedEvent | ParsedEdit] = field(default_factory=list)


@dataclass
class ConversationContext:
    events: list[CalendarEvent]
    expires_at: float


recent_events: dict[int, ConversationContext] = {}
recent_event_lists: dict[int, ConversationContext] = {}
media_requests: dict[int, float] = {}
media_previews: dict[int, PendingEventConflict] = {}
pending_event_conflicts: dict[int, PendingEventConflict] = {}
pending_actions: dict[int, PendingAction] = {}
pending_calendar_deletions: dict[int, PendingCalendarDeletion] = {}
recent_reminder_lists: dict[int, PendingReminderList] = {}
pending_group_schedule_dates: dict[tuple[int, int], tuple[int, float]] = {}
_settings: Settings | None = None
_telegram: TelegramClient | None = None
_calendars: dict[int, CalendarClient] | None = None
_parser: GroqParser | None = None
_cron_client: CronJobClient | None = None


# Each private input owns its async continuations until a newer valid input arrives.
conversation_turns: dict[int, object] = {}
active_conversation_turn: ContextVar[tuple[int, object] | None] = ContextVar("active_conversation_turn", default=None)


def _turn_is_current(chat_id: int) -> bool:
    active = active_conversation_turn.get()
    return active is None or active[0] != chat_id or conversation_turns.get(chat_id) is active[1]


def _private_callback_is_current(chat_id: int, data: str) -> bool:
    parts = data.split(":", 2)
    if len(parts) != 3:
        return False
    store = {"media": media_previews, "conflict": pending_event_conflicts,
             "select": pending_actions, "created": pending_actions, "delete": pending_actions}.get(parts[0])
    pending = store.get(chat_id) if store is not None else None
    return bool(pending and pending.token == parts[1] and pending.expires_at > time.monotonic())


def _conversation_handler(*, callback=False):
    def decorate(handler):
        @wraps(handler)
        async def run(chat_id, *args, **kwargs):
            if callback:
                data = args[1] if len(args) > 1 else kwargs.get("data", "")
                if not _private_callback_is_current(chat_id, data):
                    return await handler(chat_id, *args, **kwargs)
            active = active_conversation_turn.get()
            if active and active[0] == chat_id:
                if _turn_is_current(chat_id):
                    return await handler(chat_id, *args, **kwargs)
                return
            marker = object()
            conversation_turns[chat_id] = marker
            reset = active_conversation_turn.set((chat_id, marker))
            try:
                return await handler(chat_id, *args, **kwargs)
            finally:
                active_conversation_turn.reset(reset)
                if conversation_turns.get(chat_id) is marker:
                    conversation_turns.pop(chat_id, None)
        return run
    return decorate


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
    return {"status": "ok", "commit": os.getenv("RENDER_GIT_COMMIT", "unknown")}


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
    callback = update.get("callback_query") or {}
    if callback:
        chat_id = callback.get("message", {}).get("chat", {}).get("id")
        sender_id = callback.get("from", {}).get("id")
        if callback.get("data", "").startswith(("conflict:", "select:", "help:", "created:", "delete:", "media:")):
            if callback.get("message", {}).get("chat", {}).get("type") != "private" or chat_id != sender_id or sender_id not in calendars:
                await telegram.answer_callback_query(callback.get("id", ""), "Not available here.")
                return {"ok": True}
            try:
                current_callback = _private_callback_is_current(chat_id, callback["data"])
                if current_callback or callback["data"].startswith("help:"):
                    if callback["data"].startswith("help:") and (chat_id in pending_media or chat_id in media_requests):
                        conversation_turns.pop(chat_id, None)
                    pending_media.pop(chat_id, None)
                    if not callback["data"].startswith("media:"):
                        if chat_id in media_requests:
                            pending_event_drafts.pop(chat_id, None)
                        media_requests.pop(chat_id, None)
                        media_previews.pop(chat_id, None)
                if callback["data"].startswith("media:"):
                    await handle_media_callback(chat_id, callback.get("id", ""), callback["data"], settings, telegram, calendars[sender_id])
                elif callback["data"].startswith("help:"):
                    topic = callback["data"].removeprefix("help:")
                    await telegram.answer_callback_query(callback.get("id", ""))
                    if topic == "menu" or topic in HELP_TOPICS:
                        await _send_help(chat_id, telegram, topic)
                elif callback["data"].startswith("select:"):
                    await handle_selection_callback(chat_id, callback.get("id", ""), callback["data"], settings, telegram, calendars[sender_id], parser, cron)
                elif callback["data"].startswith("created:"):
                    await handle_created_callback(chat_id, callback.get("id", ""), callback["data"], telegram, calendars[sender_id])
                elif callback["data"].startswith("delete:"):
                    await handle_delete_callback(chat_id, callback.get("id", ""), callback["data"], telegram, calendars[sender_id])
                else:
                    await handle_conflict_callback(chat_id, callback.get("id", ""), callback["data"], settings, telegram, calendars[sender_id])
            except Exception:
                logger.exception("Failed handling private callback chat_id=%s", chat_id)
                await telegram.send_message(chat_id, "Sorry, I couldn't complete that. Please check your calendar before trying again.")
            return {"ok": True}
        if chat_id != settings.telegram_group_id or sender_id not in calendars:
            await telegram.answer_callback_query(callback.get("id", ""), "Not available here.")
            return {"ok": True}
        try:
            await handle_schedule_callback(
                chat_id, sender_id, callback.get("id", ""), callback.get("data", ""),
                settings, telegram, calendars,
            )
        except Exception:
            logger.exception("Failed handling Telegram callback chat_id=%s", chat_id)
            await telegram.send_message(chat_id, "Sorry, I couldn't complete that. Please try again.")
        return {"ok": True}
    message = update.get("message") or {}
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    sender_id = message.get("from", {}).get("id")
    first_name = (message.get("from", {}).get("first_name") or "").strip()
    text = (message.get("text") or "").strip()
    calendar = calendars.get(sender_id)
    has_media = any(message.get(key) for key in ("voice", "audio", "photo", "document", "video", "video_note", "animation", "sticker"))
    if not chat_id or (not text and not has_media):
        return {"ok": True}
    if chat.get("type") in {"group", "supergroup"} and chat_id != settings.telegram_group_id:
        return {"ok": True}
    if calendar is None:
        await telegram.send_message(chat_id, _unauthorised_message())
        return {"ok": True}
    update_id = update.get("update_id")
    if isinstance(update_id, int):
        now = time.monotonic()
        for key, expiry in list(processed_message_updates.items()):
            if expiry <= now:
                processed_message_updates.pop(key, None)
        key = (chat_id, update_id)
        if key in processed_message_updates:
            return {"ok": True}
        if len(processed_message_updates) >= 10000:
            processed_message_updates.pop(next(iter(processed_message_updates)))
        # Record before awaiting: a failed response may follow a successful write.
        processed_message_updates[key] = now + 86400
    try:
        if chat.get("type") in {"group", "supergroup"}:
            if text:
                await handle_group_schedule(chat_id, sender_id, text, settings, telegram, calendars)
            return {"ok": True}
        if chat.get("type") != "private" or chat_id != sender_id:
            return {"ok": True}
        if has_media:
            await handle_media_message(chat_id, message, settings, telegram, calendar, parser)
            return {"ok": True}
        pending_media.pop(chat_id, None)
        if text.lower() == "use media":
            if not media_previews.get(chat_id) and not (chat_id in media_requests and chat_id in pending_event_drafts):
                await telegram.send_message(chat_id, "No active event suggestion. Send a photo or voice note with event details, or type your request.")
                return {"ok": True}
            text = "add event" if chat_id in media_previews else "retry"
        if re.match(r"^/start(?:@\w+)?(?:\s|$)", text, flags=re.IGNORECASE):
            conversation_turns.pop(chat_id, None)
            pending_event_drafts.pop(chat_id, None)
            pending_event_conflicts.pop(chat_id, None)
            pending_actions.pop(chat_id, None)
            _clear_conversation(chat_id)
            await telegram.send_message(chat_id, _welcome_message(first_name))
            return {"ok": True}
        await handle_message(chat_id, text, settings, telegram, calendar, parser, cron)
    except Exception:
        logger.exception("Failed handling Telegram message chat_id=%s", chat_id)
        await telegram.send_message(chat_id, "Sorry, I couldn't complete that. Please try again.")
    return {"ok": True}


async def handle_media(chat_id: int, message: dict, settings: Settings, telegram: TelegramClient) -> tuple[str, float] | None:
    # A new input invalidates both previous previews and unrelated pending actions.
    for store in (pending_media, pending_event_drafts, pending_event_conflicts,
                  pending_actions, pending_calendar_deletions, recent_reminder_lists):
        store.pop(chat_id, None)
    for key, value in list(pending_media.items()):
        if value[1] <= time.monotonic():
            pending_media.pop(key, None)
    marker = ("", time.monotonic() + PENDING_TTL_SECONDS)
    pending_media[chat_id] = marker
    try:
        voice = message.get("voice")
        kind = "voice" if voice else "photo"
        info = voice or max(message["photo"], key=lambda photo: photo.get("width", 0) * photo.get("height", 0))
        limit = VOICE_LIMIT if voice else PHOTO_LIMIT
        if info.get("file_size", 0) > limit or (voice and voice.get("duration", 0) > 120):
            pending_media.pop(chat_id, None)
            await telegram.send_message(chat_id, "Please send a photo at most 4 MiB or a voice note at most 10 MiB and 2 minutes.")
            return
        data = await telegram.download_file(info["file_id"], limit)
        text = await interpret_media(data, kind, message.get("caption") or "", settings)
        # A newer message or callback can supersede a slow model response.
        if pending_media.get(chat_id) is not marker:
            return
        marker = (text, time.monotonic() + PENDING_TTL_SECONDS)
        pending_media[chat_id] = marker
        await telegram.send_message(chat_id, format_media_preview(text, photo=kind == "photo"), parse_mode="HTML")
        if pending_media.get(chat_id) is marker:
            return marker
    except Exception:
        # HTTP errors may include the bot token in the URL; do not log exceptions.
        if pending_media.get(chat_id) is marker:
            pending_media.pop(chat_id, None)
            await telegram.send_message(chat_id, "I couldn't read that media. Try a clearer photo or shorter voice note, or send the request as text.")


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
    pending_key = (chat_id, sender_id)
    if _telegram_command(text) == "help":
        pending_group_schedule_dates.pop(pending_key, None)
        buttons = [[{"text": f"@{account.telegram_username or account.telegram_user_id}",
                     "callback_data": f"schedule:user:{account.telegram_user_id}"}]
                   for account in settings.calendar_accounts]
        await telegram.send_message(chat_id,
            "Group help — read-only schedules\n\n"
            "/schedule — choose a person and date\n"
            "/schedule @username tomorrow — replace @username with a person below\n"
            "check my schedule on 19 September\n\n"
            "Use Yesterday / Tomorrow to step from the displayed date, Pick date to enter another date, "
            "or Change person to compare schedules. Send /help anytime.", {"inline_keyboard": buttons})
        return
    pending = pending_group_schedule_dates.get(pending_key)
    if pending and (pending[1] <= time.monotonic() or lowered.startswith("/")):
        pending_group_schedule_dates.pop(pending_key, None)
        pending = None
    if pending:
        day = (
            datetime.now(settings.timezone).date() + timedelta(days=1)
            if "tomorrow" in lowered or "tmr" in lowered
            else datetime.now(settings.timezone).date() if "today" in lowered
            else _calendar_date_from_text(lowered, settings) or _upcoming_weekday_from_text(lowered, settings)
        )
        if day is None:
            await telegram.send_message(chat_id, "I couldn't read that date. Reply with something like 19 September.", {"force_reply": True})
            return
        pending_group_schedule_dates.pop(pending_key, None)
        target = settings.account_for(pending[0])
        if target:
            await _send_group_schedule(chat_id, target, calendars[target.telegram_user_id], telegram, settings, day)
        return
    if re.fullmatch(r"/schedule(?:@\w+)?", lowered.strip()):
        buttons = [[{
            "text": f"@{account.telegram_username or account.telegram_user_id}",
            "callback_data": f"schedule:user:{account.telegram_user_id}",
        }] for account in settings.calendar_accounts]
        await telegram.send_message(chat_id, "Whose schedule?", {"inline_keyboard": buttons})
        return
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
    explicit_day = _calendar_date_from_text(lowered, settings)
    weekday = _upcoming_weekday_from_text(lowered, settings)
    if "tomorrow" in lowered or "tmr" in lowered or explicit_day or weekday:
        day = explicit_day or weekday or (datetime.now(settings.timezone).date() + timedelta(days=1))
    elif "today" in lowered:
        day = datetime.now(settings.timezone).date()
    else:
        day = None
    await _send_group_schedule(chat_id, target, calendar, telegram, settings, day)


async def _send_group_schedule(chat_id, target, calendar, telegram, settings, day=None) -> None:
    if day:
        events = await asyncio.to_thread(calendar.list_events_for_day, day)
        heading = f"@{target.telegram_username or target.telegram_user_id} — {day:%A, %d %B}"
    else:
        events = await asyncio.to_thread(calendar.list_events, 7)
        heading = f"@{target.telegram_username or target.telegram_user_id} — upcoming events"
    body = "\n\n".join(_format_event_listing(event, index=index) for index, event in enumerate(events, 1))
    anchor = day or datetime.now(settings.timezone).date()
    target_id = target.telegram_user_id
    buttons = [[
        {"text": label, "callback_data": f"schedule:day:{target_id}:{date.fromordinal(anchor.toordinal() + offset).isoformat()}"}
        for label, offset in (("Yesterday", -1), ("Tomorrow", 1))
        if date.min.toordinal() <= anchor.toordinal() + offset <= date.max.toordinal()
    ], [
        {"text": "Pick date", "callback_data": f"schedule:day:{target_id}:specific"},
        {"text": "Change person", "callback_data": f"schedule:people:{day.isoformat() if day else 'week'}"},
    ]]
    await telegram.send_message(chat_id, f"{heading}:\n\n{body or 'No events.'}", {"inline_keyboard": buttons})


async def handle_schedule_callback(
    chat_id: int,
    sender_id: int,
    callback_id: str,
    data: str,
    settings: Settings,
    telegram: TelegramClient,
    calendars: dict[int, CalendarClient],
) -> None:
    await telegram.answer_callback_query(callback_id)
    people_match = re.fullmatch(r"schedule:people:(\d{4}-\d{2}-\d{2}|week)", data)
    if people_match:
        value = people_match.group(1)
        if value != "week":
            try:
                date.fromisoformat(value)
            except ValueError:
                return
        pending_group_schedule_dates.pop((chat_id, sender_id), None)
        buttons = [[{
            "text": f"@{account.telegram_username or account.telegram_user_id}",
            "callback_data": f"schedule:user:{account.telegram_user_id}:{value}",
        }] for account in settings.calendar_accounts if account.telegram_user_id in calendars]
        await telegram.send_message(chat_id, "Whose schedule?", {"inline_keyboard": buttons})
        return
    user_match = re.fullmatch(r"schedule:user:(\d+)(?::(\d{4}-\d{2}-\d{2}|week))?", data)
    if user_match:
        target_id = int(user_match.group(1))
        if target_id not in calendars:
            return
        if user_match.group(2):
            await _show_selected_schedule(chat_id, sender_id, target_id, user_match.group(2), settings, telegram, calendars)
            return
        pending_group_schedule_dates.pop((chat_id, sender_id), None)
        buttons = [[
            {"text": "Today", "callback_data": f"schedule:day:{target_id}:today"},
            {"text": "Tomorrow", "callback_data": f"schedule:day:{target_id}:tomorrow"},
        ], [
            {"text": "Specific date", "callback_data": f"schedule:day:{target_id}:specific"},
        ]]
        await telegram.send_message(chat_id, "Which day?", {"inline_keyboard": buttons})
        return
    day_match = re.fullmatch(r"schedule:day:(\d+):(today|tomorrow|week|specific|\d{4}-\d{2}-\d{2})", data)
    if not day_match or int(day_match.group(1)) not in calendars:
        return
    target = settings.account_for(int(day_match.group(1)))
    choice = day_match.group(2)
    if target is None:
        return
    if choice == "specific":
        pending_group_schedule_dates[(chat_id, sender_id)] = (target.telegram_user_id, time.monotonic() + PENDING_TTL_SECONDS)
        await telegram.send_message(chat_id, "Reply with a date, for example: 19 September", {"force_reply": True})
        return
    if choice not in {"today", "tomorrow"}:
        await _show_selected_schedule(chat_id, sender_id, target.telegram_user_id, choice, settings, telegram, calendars)
        return
    pending_group_schedule_dates.pop((chat_id, sender_id), None)
    day = datetime.now(settings.timezone).date() + timedelta(days=1 if choice == "tomorrow" else 0)
    await _send_group_schedule(chat_id, target, calendars[target.telegram_user_id], telegram, settings, day)


async def _show_selected_schedule(chat_id, sender_id, target_id, value, settings, telegram, calendars):
    try:
        day = None if value == "week" else date.fromisoformat(value)
    except ValueError:
        return
    target = settings.account_for(target_id)
    if target is None:
        return
    pending_group_schedule_dates.pop((chat_id, sender_id), None)
    await _send_group_schedule(chat_id, target, calendars[target_id], telegram, settings, day)


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
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid standalone reminder")
    try:
        telegram_user_id = int(payload.get("telegram_user_id", 0))
        job_id = int(payload.get("job_id", 0))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid standalone reminder")
    message = str(payload.get("message", "")).strip()
    if telegram_user_id not in calendars or not message or not job_id:
        raise HTTPException(status_code=400, detail="Invalid standalone reminder")
    retention_seconds = 86400
    if "due_at" in payload:
        try:
            due_at = datetime.fromisoformat(payload["due_at"])
            if due_at.tzinfo is None:
                raise ValueError("Missing timezone")
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid reminder due time")
        now = datetime.now(due_at.tzinfo)
        deadline = due_at + timedelta(minutes=CronJobClient.RECOVERY_MINUTES)
        if now < due_at or now > deadline:
            return Response(status_code=204)
        retention_seconds = (deadline - now).total_seconds() + 60
    for key, expiry in list(delivered_standalone_reminders.items()):
        if expiry <= time.monotonic():
            delivered_standalone_reminders.pop(key, None)
    key = (telegram_user_id, job_id)
    if key in standalone_deliveries_in_flight:
        return Response(status_code=204)
    standalone_deliveries_in_flight.add(key)
    try:
        if not cron:
            raise HTTPException(status_code=503, detail="Independent reminders are not configured")
        if not await cron.is_current_reminder(job_id, telegram_user_id, message, payload.get("due_at")):
            return Response(status_code=204)
        observed_delivery = reminder_delivery_history.get(key)
        if "due_at" in payload and observed_delivery and observed_delivery[1].due_at != due_at:
            # The remote update may have succeeded even if its response was lost.
            delivered_standalone_reminders.pop(key, None)
            reminder_delivery_history.pop(key, None)
        if key not in delivered_standalone_reminders:
            for history_key, (expiry, _) in list(reminder_delivery_history.items()):
                if expiry <= time.monotonic():
                    reminder_delivery_history.pop(history_key, None)
            observed = ScheduledReminder(event_id=f"cron:{job_id}", reminder=ReminderSpec(message=message),
                due_at=due_at if "due_at" in payload else datetime.now().astimezone(), standalone=True)
            try:
                await telegram.send_message(telegram_user_id, f"⏰ {message}")
            except Exception:
                observed.status = "Failed (delivery not confirmed)"
                reminder_delivery_history[key] = (time.monotonic() + 86400, observed)
                raise
            observed.status = "Delivered"
            reminder_delivery_history[key] = (time.monotonic() + 86400, observed)
            delivered_standalone_reminders[key] = time.monotonic() + retention_seconds
        if cron:
            try:
                await cron.delete_reminder(job_id)
            except Exception:
                logger.exception("Delivered standalone reminder but could not delete cron job job_id=%s", job_id)
    finally:
        standalone_deliveries_in_flight.discard(key)
    return Response(status_code=204)


@_conversation_handler()
async def handle_media_message(chat_id, message, settings, telegram, calendar, parser):
    _clear_conversation(chat_id)
    for store in (pending_event_drafts, pending_event_conflicts, pending_actions, recent_reminder_lists, pending_calendar_deletions):
        store.pop(chat_id, None)
    media = message.get("voice") or message.get("audio")
    image = False
    mime_type = None
    if not media and message.get("photo"):
        media = message["photo"][-1]
        image, mime_type = True, "image/jpeg"
    if not media and message.get("document"):
        document = message["document"]
        mime_type = document.get("mime_type")
        if mime_type in {"image/jpeg", "image/png", "image/webp"}:
            media, image = document, True
    if not media:
        await telegram.send_message(chat_id, "Send a voice note, audio file, or JPEG/PNG/WebP image of an appointment or invitation. PDFs and videos aren't supported yet.")
        return
    if media.get("file_size", 0) > 10 * 1024 * 1024 or media.get("duration", 0) > 300:
        await telegram.send_message(chat_id, "Please send a file under 10 MiB and audio no longer than 5 minutes.")
        return
    vision_model = getattr(settings, "groq_vision_model", None)
    if image and not vision_model:
        await telegram.send_message(chat_id, "Image suggestions need GROQ_VISION_MODEL configured with a vision-capable Groq model. You can type the event details instead.")
        return
    original_preview = None
    try:
        if message.get("photo") or message.get("voice"):
            original_preview = await handle_media(chat_id, message, settings, telegram)
            if not original_preview or pending_media.get(chat_id) is not original_preview or not original_preview[0]:
                return
            content = original_preview[0] + "\nCaption: " + (message.get("caption") or "")
        else:
            original_preview = ("", time.monotonic() + PENDING_TTL_SECONDS)
            pending_media[chat_id] = original_preview
            data = await telegram.download_media(media["file_id"])
            caption = (message.get("caption") or "").strip()
            content = caption
            if not image:
                transcript = await asyncio.to_thread(parser.transcribe, data, media.get("file_name", "audio.mp3"),
                    getattr(settings, "groq_transcription_model", "whisper-large-v3-turbo"))
                content = f"Transcript: {transcript}\nCaption: {caption}"
            if not image:
                await telegram.send_message(chat_id, format_media_preview(content, photo=False), parse_mode="HTML")
        now = datetime.now(settings.timezone)
        suggestion = await asyncio.to_thread(parser.suggest_media_event, content, now, settings.user_timezone,
            data if image and not message.get("photo") else None, mime_type, vision_model)
        if original_preview is not None and pending_media.get(chat_id) is not original_preview:
            return
        if not suggestion.event_text or not suggestion.event_text.strip():
            await telegram.send_message(chat_id, "I couldn't identify a calendar event in that. Tell me what you'd like to schedule and when.")
            return
        media_requests[chat_id] = time.monotonic() + PENDING_TTL_SECONDS
        # Provider-generated content cannot opt out of the preview or conflict checks.
        request_text = re.sub(r"\badd\s+anyway\b", "add", suggestion.event_text, flags=re.I)
        await _add_event(chat_id, request_text, settings, telegram, calendar, parser, now)
    except Exception:
        if original_preview is not None and pending_media.get(chat_id) is not original_preview:
            return
        media_requests.pop(chat_id, None)
        media_previews.pop(chat_id, None)
        pending_event_drafts.pop(chat_id, None)
        # Do not log provider exceptions: their URLs or payloads may contain credentials/media.
        logger.warning("Media interpretation failed chat_id=%s", chat_id)
        await telegram.send_message(chat_id, "I couldn't read that media. Please resend it or type the event details. No event was added.")


@_conversation_handler(callback=True)
async def handle_media_callback(chat_id, callback_id, data, settings, telegram, calendar):
    match = re.fullmatch(r"media:([a-f0-9]{32}):(add|cancel)", data)
    preview = media_previews.get(chat_id)
    if not match or not preview or preview.token != match.group(1) or preview.expires_at <= time.monotonic():
        if preview and preview.expires_at <= time.monotonic():
            media_previews.pop(chat_id, None)
            media_requests.pop(chat_id, None)
        message = "This suggestion expired or was already used. Please send the media again."
        if callback_id:
            await telegram.answer_callback_query(callback_id, message)
        else:
            await telegram.send_message(chat_id, message)
        return
    media_previews.pop(chat_id, None)
    media_requests.pop(chat_id, None)
    if callback_id:
        await telegram.answer_callback_query(callback_id)
    if match.group(2) == "cancel":
        await telegram.send_message(chat_id, "Event suggestion cancelled.")
        return
    # Recheck the current calendar only after confirmation; source text cannot bypass overlaps.
    request_text = re.sub(r"\badd\s+anyway\b", "add", preview.request_text, flags=re.I)
    await _check_and_create_event(chat_id, preview.event, request_text, preview.now, telegram, calendar)


def _clear_conversation(chat_id):
    for store in (recent_events, recent_event_lists, media_requests, media_previews, pending_media):
        store.pop(chat_id, None)


def _remember_event(chat_id, event):
    recent_events[chat_id] = ConversationContext([event], time.monotonic() + PENDING_TTL_SECONDS)
    listing = recent_event_lists.get(chat_id)
    if listing:
        listing.events = [event if (item.event_id, item.calendar_id) == (event.event_id, event.calendar_id) else item
                          for item in listing.events]


def _correction(text):
    return bool(re.match(r"^(?:actually|sorry[, ]|make (?:it|that)|instead\b)", text, re.I))


def _command_text(text: str) -> str:
    return re.sub(r"^(?:(?:please|can you|could you|would you|i want to|i'd like to|i would like to)\s+|(?:actually|sorry|instead|also)[,\s]+)+", "", text.strip(), flags=re.I)


def _management_action(text: str) -> str | None:
    match = re.match(r"^(delete|remove|cancel|disable|change|edit|move|reschedule|update)\b", _command_text(text), flags=re.I)
    if not match:
        return None
    return "delete" if match[1].lower() in {*DELETE_WORDS, "disable"} else "edit"


def _is_reminder_removal_request(text: str) -> bool:
    return bool(re.match(r"^(?:delete|remove|cancel|disable)\s+(?:(?:the|a|all)\s+)?reminders?(?:\s+(?:for|on|from)\b|$)", _command_text(text), flags=re.I))


def _clear_reminder_command(text: str) -> bool:
    return bool(re.fullmatch(r"(?:no reminders?|without reminders?|don't remind me|do not remind me|(?:remove|delete|disable|cancel) (?:(?:the|all) )?reminders?)", _command_text(text), flags=re.I))


def _is_reminder_list_request(text: str) -> bool:
    return bool(re.fullmatch(r"(?:(?:show|list)(?: me)? )?(?:(?:all|my|the|upcoming) )*reminders", _command_text(text).strip(), flags=re.I))


def _read_only_request(text):
    lower = _command_text(text).lower().strip()
    return (_telegram_command(text) in {"now", "calendars"}
            or _is_calendar_list_request(lower)
            or any(phrase in lower for phrase in FREE_TIME_PHRASES)
            or bool(re.match(r"^(?:list|upcoming|schedule|plans|what(?:'s|s| are)|show (?:my |the )?(?:schedule|events))\b", lower)))


def _event_reference(text: str) -> str | None:
    """Return a contextual target only when the entire target is a reference."""
    lowered = _command_text(text).lower().strip()
    if _is_existing_reminder_request(lowered) or _is_reminder_removal_request(lowered):
        target_match = re.search(r"\b(?:before|for|about)\s+(.+)$", lowered)
        if target_match:
            target = target_match.group(1)
        else:
            command = re.match(r"^(?:remind me|(?:set|add|change|update|edit)(?: (?:a|the|another))? reminder)\s+(.+)$", lowered)
            if not command:
                return None
            target = command.group(1)
        # A target may precede its lead time: 'remind me about it one hour before'.
        target = re.split(r"\s+(?:\d+(?:\.\d+)?|an?|one|two|three|half|quarter|the)\s*(?:minutes?|mins?|hours?|hrs?|days?) before\b", target, maxsplit=1)[0]
    else:
        command = re.match(r"^(change|edit|move|reschedule|update|delete|remove|cancel|disable)\s+(.+)$", lowered)
        if not command:
            return None
        target = command.group(2)
        if command.group(1) in EDIT_WORDS:
            target = re.split(r"\s+to\s+", target, maxsplit=1)[0]
    target = target.strip()
    pattern = r"(?:it|that|this event|that event|this series|that series|the series|the (?:first|second|third|fourth|fifth|last|\d+(?:st|nd|rd|th)?) (?:one|event))"
    return target if re.fullmatch(pattern, target) else None


def _unqualified_reminder_request(text: str) -> bool:
    text = _command_text(text)
    prefix = r"(?:remind me|(?:set|add|change|update|edit)(?: (?:a|the|another|additional))? reminder)"
    clock = r"\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)"
    number_words = r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[- ](?:one|two|three|four|five|six|seven|eight|nine))?)"
    word_lead = rf"{number_words}\s+(?:minutes?|hours?|days?|weeks?) before"
    return bool(re.fullmatch(rf"{prefix}\s+(?:(?:to|for|at)\s+)?(?:{REMINDER_LEAD_PATTERN}|{word_lead}|{clock})(?:\s*(?:to\s+|:\s*).+)?", text.strip(), flags=re.I))


def _media_field_correction(text: str) -> bool:
    text = _command_text(text)
    return (_unqualified_reminder_request(text)
            or bool(re.fullmatch(rf"(?:remove|disable|delete|cancel)\s+(?:(?:the|all)\s+)?(?:{REMINDER_LEAD_PATTERN}\s+)?reminders?", text.strip(), flags=re.I))
            or bool(re.fullmatch(r"(?:no reminders?|without reminders?|don't remind me|do not remind me|just once|one[- ]off|don't repeat|do not repeat|stop repeating|no recurrence)", text.strip(), flags=re.I)))


def _replaces_pending_followup(text: str, action: str) -> bool:
    """Separate a new event command from details answering the open question."""
    lowered = _command_text(text).lower().strip()
    if _clear_reminder_command(text) or re.match(r"^(?:delete|remove|cancel|disable)\s+\S", lowered):
        return True
    if _is_existing_reminder_request(lowered):
        if action != "followup_remind":
            return True
        if _event_reference(text):
            return False
        return not _unqualified_reminder_request(text)
    command = re.match(r"^(?:change|edit|move|reschedule|update)\s+(.+)$", lowered)
    if not command:
        return False
    if action != "followup_edit":
        return True
    target = re.split(r"\b(?:to|at|on|for|by|until)\b", command.group(1), maxsplit=1)[0].strip()
    return bool(target) and not re.fullmatch(
        r"(?:the )?(?:it|that|this(?: event)?|event|date|time|duration|length|location|calendar|title|name)", target)


@_conversation_handler()
async def handle_message(chat_id, text, settings, telegram, calendar, parser, cron=None):
    pending_media.pop(chat_id, None)
    lower = text.lower().strip()
    command = _telegram_command(text)
    if command == "cancel" or lower == "cancel":
        had_pending = any(chat_id in store for store in (pending_actions, pending_event_drafts, pending_event_conflicts, pending_calendar_deletions, recent_reminder_lists))
        _clear_conversation(chat_id)
        if not had_pending:
            await telegram.send_message(chat_id, "Conversation cancelled.")
            return
    elif command in {"help", "start"}:
        _clear_conversation(chat_id)
        await _handle_message(chat_id, text, settings, telegram, calendar, parser, cron)
        return
    for store in (recent_events, recent_event_lists):
        context = store.get(chat_id)
        if context and context.expires_at <= time.monotonic():
            store.pop(chat_id, None)
    if chat_id in media_requests or chat_id in pending_event_drafts:
        management_action = "followup_remind" if _is_existing_reminder_request(lower) else "followup_edit"
        if _replaces_pending_followup(text, management_action) and not _media_field_correction(text):
            for store in (media_requests, media_previews, pending_event_drafts):
                store.pop(chat_id, None)
    if media_requests.get(chat_id, float("inf")) <= time.monotonic():
        media_requests.pop(chat_id, None)
        media_previews.pop(chat_id, None)
        pending_event_drafts.pop(chat_id, None)
        if not _is_explicit_add_request(lower) and not _read_only_request(text):
            await telegram.send_message(chat_id, "That event suggestion expired. Please send the media again.")
            return
    preview = media_previews.get(chat_id)
    if preview and lower in {"add event", "confirm", "yes"}:
        await handle_media_callback(chat_id, "", f"media:{preview.token}:add", settings, telegram, calendar)
        return
    if preview:
        media_previews.pop(chat_id, None)
        if (_correction(text) or _media_field_correction(text) or re.search(DATE_HINT_PATTERN + "|" + TIME_HINT_PATTERN, text, re.I)
                or re.match(r"^(?:at|in|rename|call it|change|move|remind|set .*reminder)\b", lower)) and (not _is_explicit_add_request(lower) or _media_field_correction(text)) and not _read_only_request(text) and not _is_standalone_reminder_request(lower):
            await _add_event(chat_id, preview.request_text + "\nUser follow-up: " + text, settings, telegram, calendar, parser, preview.now)
            return
        media_requests.pop(chat_id, None)
    draft = pending_event_drafts.get(chat_id)
    if draft and draft.expires_at > time.monotonic() and _read_only_request(text):
        pending_event_drafts.pop(chat_id, None)
        try:
            await _handle_message(chat_id, text, settings, telegram, calendar, parser, cron)
        finally:
            if _turn_is_current(chat_id) and draft.expires_at > time.monotonic():
                pending_event_drafts.setdefault(chat_id, draft)
        if not _turn_is_current(chat_id):
            return
        if draft.expires_at <= time.monotonic():
            await telegram.send_message(chat_id, "That event draft expired during the lookup. Please send its full details again.")
            return
        await telegram.send_message(chat_id, "Your event draft is still open: " + draft.request_text.split("\nUser follow-up:")[0][:300] + "\nReply with the missing details, or /cancel.")
        return
    if (_is_explicit_add_request(lower) and not (chat_id in media_requests and _media_field_correction(text))) or _is_standalone_reminder_request(lower):
        media_requests.pop(chat_id, None)
    # Existing pending selections/confirmations retain priority over contextual references.
    pending = pending_actions.get(chat_id)
    if pending and pending.action in {"followup_edit", "followup_remind"}:
        replacement = ((text.startswith("/") and not _read_only_request(text)) or _is_explicit_add_request(lower)
                       or _is_standalone_reminder_request(lower) or _replaces_pending_followup(text, pending.action))
        if lower in {"cancel", "/cancel"}:
            pending_actions.pop(chat_id, None)
            await telegram.send_message(chat_id, "Follow-up cancelled. The saved event is unchanged.")
            return
        elif pending.expires_at <= time.monotonic():
            pending_actions.pop(chat_id, None)
            pending = None
            if not replacement and not _read_only_request(text):
                await telegram.send_message(chat_id, "That follow-up expired. Please send a new request with the event name.")
                return
        elif replacement:
            _remember_event(chat_id, pending.events[0])
            pending_actions.pop(chat_id, None)
            pending = None
        elif _read_only_request(text):
            pending_actions.pop(chat_id, None)
            try:
                await _handle_message(chat_id, text, settings, telegram, calendar, parser, cron)
            finally:
                if _turn_is_current(chat_id) and pending.expires_at > time.monotonic():
                    pending_actions.setdefault(chat_id, pending)
            if not _turn_is_current(chat_id):
                return
            if pending.expires_at <= time.monotonic():
                await telegram.send_message(chat_id, "That follow-up expired during the lookup. Please send a new request with the event name.")
                return
            await telegram.send_message(chat_id, f"Your question about {pending.events[0].title} is still open. Reply with the missing details, or /cancel.")
            return
        else:
            pending_actions.pop(chat_id, None)
            combined = pending.request_text + "\nUser follow-up: " + text
            if pending.action == "followup_edit":
                await _edit_event(chat_id, combined, pending.events[0], settings, telegram, calendar, parser, preserve_clock=True)
            else:
                await _set_reminder(chat_id, combined, pending.events[0], settings, telegram, calendar, parser)
            return
    actionable_pending = pending and pending.action not in {"created", "undo_delete"} and pending.expires_at > time.monotonic()
    if not draft and not actionable_pending and not pending_event_conflicts.get(chat_id):
        reference = _event_reference(text)
        reminder = _is_existing_reminder_request(lower) and not _is_standalone_reminder_request(lower)
        implicit_reminder = reminder and _unqualified_reminder_request(text)
        clear_reminder = _is_reminder_removal_request(lower) or _clear_reminder_command(lower)
        action = "clear_reminder" if clear_reminder else "remind" if reminder else "delete" if _management_action(lower) == "delete" else "edit"
        has_action = bool(re.match(r"^(?:move|change|edit|update|reschedule|delete|remove|disable|cancel|remind|set|add)\b", _command_text(lower)))
        correction = _correction(text) and not _replaces_pending_followup(text, "followup_remind" if reminder else "followup_edit")
        contextual = ((correction or implicit_reminder or _clear_reminder_command(text)) or (reference and has_action)) and not text.startswith("/") and not _read_only_request(text) and (not _is_explicit_add_request(lower) or reminder) and not _is_standalone_reminder_request(lower)
        if contextual:
            contexts = [item for item in (recent_events.get(chat_id), recent_event_lists.get(chat_id)) if item]
            context = max(contexts, key=lambda item: item.expires_at, default=None)
            choices = list(context.events) if context else []
            ordinal = re.fullmatch(r"the (first|second|third|fourth|fifth|last|\d+)(?:st|nd|rd|th)? (?:one|event)", reference or "")
            if ordinal:
                context = recent_event_lists.get(chat_id)
                choices = list(context.events) if context else []
            if ordinal and not context:
                choices = []
            elif ordinal:
                value = ordinal.group(1)
                number = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "last": len(choices)}.get(value)
                number = number if number is not None else int(value)
                choices = choices[number-1:number] if number > 0 else []
            request = text
            if action == "edit" and _correction(text) and _management_action(text) is None:
                destination = re.sub(r"^(?:actually|sorry|make (?:it|that)|instead)[, ]*", "", text, flags=re.I)
                request = "move this event to " + destination
            if not choices:
                await telegram.send_message(chat_id, "Which event do you mean? Send its name or list your schedule again.")
                return
            pending_actions.pop(chat_id, None)
            recent_reminder_lists.pop(chat_id, None)
            if len(choices) > 1:
                await _ask_to_select(chat_id, action, request, choices, DeleteMatch(ambiguous=True), telegram)
            else:
                selected = choices[0]
                if selected.recurring_event_id == selected.event_id and not re.search(r"\b(?:series|recurring|weekly|every)\b", request, re.I):
                    await telegram.send_message(chat_id, "Do you mean the whole recurring series? Include 'series' in your request to change all sessions, or list your schedule and choose one occurrence.")
                    return
                _remember_event(chat_id, selected)
                if action == "edit":
                    await _edit_event(chat_id, request, choices[0], settings, telegram, calendar, parser, preserve_clock=True)
                else:
                    await _execute_selection(chat_id, PendingAction(choices, 0, action, request), choices[0], settings, telegram, calendar, parser, cron)
            return
    await _handle_message(chat_id, text, settings, telegram, calendar, parser, cron)


async def _handle_message(chat_id: int, text: str, settings: Settings, telegram: TelegramClient, calendar: CalendarClient, parser: GroqParser, cron: CronJobClient | None = None) -> None:
    command = _telegram_command(text)
    lowered = command if command in {"reminders", "calendars", "now"} else text.lower().strip()
    if command == "help":
        pending_event_drafts.pop(chat_id, None)
        pending_event_conflicts.pop(chat_id, None)
        pending_actions.pop(chat_id, None)
        pending_calendar_deletions.pop(chat_id, None)
        recent_reminder_lists.pop(chat_id, None)
        await _send_help(chat_id, telegram)
        return
    if command == "reminder_status" or lowered == "reminder status":
        pending_event_drafts.pop(chat_id, None)
        pending_event_conflicts.pop(chat_id, None)
        pending_actions.pop(chat_id, None)
        recent_reminder_lists.pop(chat_id, None)
        await _show_reminder_status(chat_id, telegram, calendar, cron)
        return
    saved = pending_actions.get(chat_id)
    if saved and saved.action in {"created", "created_edit", "created_calendar", "created_moving"}:
        pending_actions.pop(chat_id, None)
        if lowered == "cancel" or command == "cancel":
            message = "The calendar change was already submitted. Please check its result." if saved.action == "created_moving" else "Event shortcuts cancelled. The saved event is unchanged."
            await telegram.send_message(chat_id, message)
            return
        if saved.action == "created_edit" and not text.lstrip().startswith("/") and not _is_explicit_add_request(lowered) and not _is_standalone_reminder_request(lowered):
            if saved.expires_at <= time.monotonic():
                await telegram.send_message(chat_id, "That date/time-change prompt expired. Please send a new edit request.")
                return
            await _edit_event(chat_id, f"move {saved.request_text or 'this event'} to {text}", saved.events[0], settings, telegram, calendar, parser,
                              preserve_clock=True)
            return
    elif saved and saved.action == "undo_delete" and lowered != "undo":
        pending_actions.pop(chat_id, None)
    conflict = pending_event_conflicts.get(chat_id)
    if conflict:
        choice = {"add anyway": "add", "apply anyway": "add", "change time": "change", "suggest times": "suggest", "cancel": "cancel", "/cancel": "cancel"}.get(lowered)
        if choice:
            await handle_conflict_callback(chat_id, "", f"conflict:{conflict.token}:{choice}", settings, telegram, calendar)
            return
        pending_event_conflicts.pop(chat_id, None)
    draft = pending_event_drafts.get(chat_id)
    if draft:
        if lowered == "cancel" or command == "cancel":
            pending_event_drafts.pop(chat_id, None)
            await telegram.send_message(chat_id, "Event creation cancelled.")
            return
        if text.lstrip().startswith("/") or (_is_explicit_add_request(lowered) and not (chat_id in media_requests and _media_field_correction(text))) or _is_standalone_reminder_request(lowered):
            pending_event_drafts.pop(chat_id, None)
        elif draft.expires_at <= time.monotonic():
            pending_event_drafts.pop(chat_id, None)
            await telegram.send_message(chat_id, "That event draft has expired. Please send the event details again.")
            return
        else:
            combined = draft.request_text if lowered == "retry" else f"{draft.request_text}\nUser follow-up: {text}"
            await _add_event(chat_id, combined, settings, telegram, calendar, parser, draft.now)
            return
    recent_reminders = recent_reminder_lists.get(chat_id)
    if recent_reminders and recent_reminders.expires_at <= time.monotonic():
        recent_reminder_lists.pop(chat_id, None)
        recent_reminders = None
    numbered_removal = re.fullmatch(r"(?:remove|delete|cancel)\s+([1-9]\d*)", lowered)
    if recent_reminders and (lowered == "cancel" or command == "cancel"):
        recent_reminder_lists.pop(chat_id, None)
        await telegram.send_message(chat_id, "Reminder selection cancelled.")
        return
    numbered_edit = re.fullmatch(r"(?:update|move|change|edit|reschedule)\s+(\d+)\b(.*)", lowered)
    if numbered_edit:
        if not recent_reminders:
            await telegram.send_message(chat_id, "Please send 'reminders' again, then update a number from that list.")
            return
        index = int(numbered_edit.group(1)) - 1
        if not 0 <= index < len(recent_reminders.reminders):
            await telegram.send_message(chat_id, f"Choose a reminder number from 1 to {len(recent_reminders.reminders)}.")
            return
        selected_reminder = recent_reminders.reminders[index]
        clock = re.fullmatch(r"\s+to\s+(1[0-2]|[1-9])(?:[:.](\d{2}))?\s*(am|pm)", numbered_edit.group(2))
        if not clock or int(clock.group(2) or 0) > 59:
            await telegram.send_message(chat_id, "Use 'update 1 to 11.30am' to change the time on the reminder's existing date.")
            return
        if not selected_reminder.event_id.startswith("cron:") or not cron:
            await telegram.send_message(chat_id, "Numbered time changes support independent reminders only. For an event reminder, specify the event and lead time, such as 'set a reminder for Dental 1 hour before'.")
            return
        due_at = selected_reminder.due_at.astimezone(settings.timezone).replace(
            hour=int(clock.group(1)) % 12 + (12 if clock.group(3) == "pm" else 0),
            minute=int(clock.group(2) or 0), second=0, microsecond=0,
        )
        if due_at <= datetime.now(settings.timezone):
            await telegram.send_message(chat_id, "That reminder time is in the past. Choose a future time on the reminder's date.")
            return
        job_id = int(selected_reminder.event_id.removeprefix("cron:"))
        key = (chat_id, job_id)
        if key in standalone_deliveries_in_flight:
            await telegram.send_message(chat_id, "That reminder is being delivered or updated. Please try again shortly.")
            return
        recent_reminder_lists.pop(chat_id, None)
        label = selected_reminder.reminder.message or "Reminder"
        standalone_deliveries_in_flight.add(key)
        try:
            await cron.update_reminder(job_id, chat_id, due_at)
            delivered_standalone_reminders.pop(key, None)
            reminder_delivery_history.pop(key, None)
        finally:
            standalone_deliveries_in_flight.discard(key)
        await telegram.send_message(chat_id, f"✅ Reminder updated: {label}\n📅 {_format_time(due_at)}")
        return
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
    if pending and pending.action in {"delete_confirm", "undo_delete"}:
        if pending.expires_at <= time.monotonic():
            pending_actions.pop(chat_id, None)
            if lowered in {"cancel", "/cancel"} or command == "cancel":
                await telegram.send_message(chat_id, "That deletion choice expired. Nothing else was changed.")
                return
            if pending.action == "delete_confirm" and lowered in {"confirm delete", "confirm deletion"}:
                await telegram.send_message(chat_id, "That deletion confirmation expired. Please send a new delete request.")
                return
            if pending.action == "undo_delete" and lowered == "undo":
                await telegram.send_message(chat_id, "That Undo option expired. Please check Google Calendar.")
                return
        elif pending.action == "delete_confirm" and (lowered in {"cancel", "/cancel"} or command == "cancel"):
            pending_actions.pop(chat_id, None)
            await telegram.send_message(chat_id, "Event deletion cancelled.")
            return
        elif pending.action == "delete_confirm" and lowered in {"confirm delete", "confirm deletion"}:
            pending_actions.pop(chat_id, None)
            await _complete_event_deletion(chat_id, pending, telegram, calendar)
            return
        elif pending.action == "undo_delete" and lowered == "undo":
            pending_actions.pop(chat_id, None)
            await _restore_deleted_event(chat_id, pending, telegram, calendar)
            return
        else:
            pending_actions.pop(chat_id, None)
    elif pending and pending.expires_at > time.monotonic():
        if lowered == "cancel" or command == "cancel":
            pending_actions.pop(chat_id, None)
            await telegram.send_message(chat_id, "Selection cancelled.")
            return
        selected = _selection(text, pending.events)
        if selected:
            pending_actions.pop(chat_id, None)
            await _execute_selection(chat_id, pending, selected, settings, telegram, calendar, parser, cron)
            return
        pending_actions.pop(chat_id, None)
    if command == "now":
        now = datetime.now(settings.timezone)
        await telegram.send_message(chat_id, f"🕒 {now:%A}, {now.day} {now:%B %Y} · {_format_clock(now)}\n🌏 {settings.user_timezone}")
        return
    if (_is_explicit_add_request(lowered) and not _calendar_create_name(text)
            and not _is_standalone_reminder_request(lowered) and not _is_existing_reminder_request(lowered)):
        await _add_event(chat_id, text, settings, telegram, calendar, parser)
        return
    if calendar_name := _calendar_create_name(text):
        existing = await asyncio.to_thread(calendar.list_calendars)
        if not _turn_is_current(chat_id):
            return
        if any(item.name.casefold() == calendar_name.casefold() for item in existing):
            await telegram.send_message(chat_id, f"A calendar named '{calendar_name}' already exists.")
            return
        created_calendar = await asyncio.to_thread(calendar.create_calendar, calendar_name)
        await telegram.send_message(chat_id, f"✅ Created calendar: {created_calendar.name}")
    elif calendar_name := _calendar_delete_name(text):
        target_calendar = await asyncio.to_thread(calendar.resolve_calendar, calendar_name)
        if not _turn_is_current(chat_id):
            return
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
    elif _management_action(lowered) is None and any(phrase in lowered for phrase in FREE_TIME_PHRASES):
        tomorrow = "tomorrow" in lowered or "tmr" in lowered
        explicit_day = _date_from_text(lowered, settings)
        if tomorrow or explicit_day:
            target_day = explicit_day or (datetime.now(settings.timezone).date() + timedelta(days=1))
            events = await asyncio.to_thread(calendar.list_events_for_day, target_day)
            await telegram.send_message(chat_id, _format_free_slots(events, settings, 1, target_day))
        else:
            events = await asyncio.to_thread(calendar.list_events, 7)
            await telegram.send_message(chat_id, _format_free_slots(events, settings, 7))
    elif _management_action(lowered) is None and _is_calendar_list_request(lowered):
        calendars = await asyncio.to_thread(calendar.list_calendars)
        await telegram.send_message(chat_id, _format_calendar_list(calendars))
    elif _is_standalone_reminder_request(lowered):
        if not cron:
            await telegram.send_message(chat_id, "Independent reminders are not configured. Add CRON_JOB_API_KEY and SERVICE_BASE_URL to the service environment.")
            return
        now = datetime.now(settings.timezone)
        standalone = _simple_weekday_reminder(text, now)
        if standalone is None:
            standalone = await asyncio.to_thread(parser.parse_standalone_reminder, text, now, settings.user_timezone)
        if not _turn_is_current(chat_id):
            return
        _apply_standalone_clock_from_text(standalone, text, now)
        if standalone.confidence == "low" or standalone.due_at.tzinfo is None:
            await telegram.send_message(chat_id, "Tell me what to remind you about and when, for example: 'remind me to pay the bill tomorrow at 9am'.")
            return
        if standalone.due_at <= now:
            await telegram.send_message(chat_id, "That reminder time is in the past. Please specify a future date and time.")
            return
        await cron.create_reminder(chat_id, standalone.message, standalone.due_at)
        await telegram.send_message(chat_id, f"⏰ Reminder set: {standalone.message}\n📅 {_format_time(standalone.due_at)}")
    elif _is_reminder_removal_request(lowered):
        events = await asyncio.to_thread(calendar.list_events, 30)
        events += await asyncio.to_thread(calendar.list_standalone_reminder_events, 30)
        if cron:
            cron_reminders = await cron.list_reminders(chat_id)
            events += [CalendarEvent(
                event_id=reminder.event_id, title=reminder.reminder.message or "Reminder",
                start=reminder.due_at, end=reminder.due_at, is_standalone_reminder=True,
            ) for reminder in cron_reminders]
        match = await asyncio.to_thread(parser.match_event, text, events)
        if not _turn_is_current(chat_id):
            return
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
    elif _is_reminder_list_request(lowered):
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
        if not _turn_is_current(chat_id):
            return
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
    elif _management_action(lowered) == "delete":
        events = await asyncio.to_thread(calendar.list_events, 30)
        if not events:
            await telegram.send_message(chat_id, "There are no events in the next 30 days to delete.")
            return
        if ("weekly" in lowered or "recurring" in lowered) and (series_matches := [event for event in events if event.title.lower() in lowered and event.recurring_event_id]):
            unique_series = {event.recurring_event_id for event in series_matches}
            if len(unique_series) == 1:
                await _ask_delete_confirmation(chat_id, series_matches[0], telegram, series=True)
                return
        exact_matches = [event for event in events if event.title.casefold() in lowered.casefold()]
        if len(exact_matches) == 1:
            await _ask_delete_confirmation(
                chat_id, exact_matches[0], telegram,
                series=_is_explicit_series_delete(text, exact_matches[0]),
            )
            return
        match = await asyncio.to_thread(parser.match_event, text, events)
        if not _turn_is_current(chat_id):
            return
        selected = next((e for e in events if e.event_id == match.matched_event_id), None)
        if selected and not match.ambiguous:
            await _ask_delete_confirmation(
                chat_id, selected, telegram, series=_is_explicit_series_delete(text, selected),
            )
            return
        await _ask_to_select(chat_id, "delete", text, events, match, telegram)
    elif _is_existing_reminder_request(lowered):
        events = await asyncio.to_thread(calendar.list_events, 30)
        if not events:
            await telegram.send_message(chat_id, "There are no events in the next 30 days to remind you about.")
            return
        match = await asyncio.to_thread(parser.match_event, text, events)
        if not _turn_is_current(chat_id):
            return
        selected = next((event for event in events if event.event_id == match.matched_event_id), None)
        if selected and not match.ambiguous:
            await _set_reminder(chat_id, text, selected, settings, telegram, calendar, parser)
            return
        await _ask_to_select(chat_id, "remind", text, events, match, telegram)
    elif _management_action(lowered) == "edit":
        events = await asyncio.to_thread(calendar.list_events, 30)
        if not events:
            await telegram.send_message(chat_id, "There are no events in the next 30 days to edit.")
            return
        match = await asyncio.to_thread(parser.match_event, text, events)
        if not _turn_is_current(chat_id):
            return
        selected = next((event for event in events if event.event_id == match.matched_event_id), None)
        if selected and not match.ambiguous:
            await _edit_event(chat_id, text, selected, settings, telegram, calendar, parser)
            return
        await _ask_to_select(chat_id, "edit", text, events, match, telegram)
    elif re.match(r"^(?:list|show|what's on|whats on|what are my|upcoming|plan|plans|schedule)\b", _command_text(lowered)) and not _is_explicit_add_request(lowered):
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
        if not _turn_is_current(chat_id):
            return
        recent_event_lists[chat_id] = ConversationContext(list(events), time.monotonic() + PENDING_TTL_SECONDS)
        if not events:
            await telegram.send_message(chat_id, f"No events for {heading.lower().replace(' events', '')}.")
        else:
            lines = [_format_event_listing(event, index=index) for index, event in enumerate(events, 1)]
            await telegram.send_message(chat_id, heading + ":\n\n" + "\n\n".join(lines))
    else:
        await _add_event(chat_id, text, settings, telegram, calendar, parser)


async def _add_event(chat_id, text, settings, telegram, calendar, parser, now=None):
    if not _turn_is_current(chat_id):
        return
    media_generation = media_requests.get(chat_id)
    media_source = None
    if media_generation is not None:
        media_source = ("", time.monotonic() + PENDING_TTL_SECONDS)
        pending_media[chat_id] = media_source
    lowered = text.lower()
    now = now or datetime.now(settings.timezone)
    date_text = _latest_detail_text(text, DATE_HINT_PATTERN, remove_ranges=True)
    time_text = _latest_detail_text(text, TIME_HINT_PATTERN)
    try:
        question = _weekday_date_question(date_text, settings, now)
        clocks = _clock_range_from_text(time_text)
    except ValueError as exc:
        question = str(exc)
    if question:
        await _ask_event_detail(chat_id, text, question, now, telegram)
        return
    if (
        _is_explicit_add_request(lowered)
        and "\nUser follow-up:" not in text
        and _date_from_text(lowered, settings)
        and not _has_explicit_event_time(lowered)
        and not re.search(r"\b(?:all|whole)[\s-]+day\b", lowered)
    ):
        await _ask_event_detail(chat_id, text, "What time? You can also say 'all day'.", now, telegram)
        return
    try:
        event = _explicit_all_day_event(text, settings) or await asyncio.to_thread(
            parser.parse_event, _event_parser_text(text), now, settings.user_timezone,
        )
    except Exception:
        if not _turn_is_current(chat_id) or (media_generation is not None and (media_requests.get(chat_id) != media_generation or pending_media.get(chat_id) is not media_source)):
            return
        await _ask_event_detail(chat_id, text, "I couldn't interpret that just now. Your draft is retained. Reply 'retry', give clearer details, or /cancel. No event was added.", now, telegram)
        return
    if not _turn_is_current(chat_id) or (media_generation is not None and (media_requests.get(chat_id) != media_generation or pending_media.get(chat_id) is not media_source)):
        return
    if clocks:
        _apply_clock_range(event, clocks, date_text, settings, now)
    missing = getattr(event, "missing_fields", [])
    question = next((prompt for field, prompt in (
        ("date", "Which date?"),
        ("time", "What time? You can also say 'all day'."),
        ("duration", "How long? For example, '1 hour' or 'until 4pm'."),
    ) if field in missing and (field == "date" or not event.all_day)), None)
    if question or event.confidence == "low" or event.start is None or event.end is None:
        await _ask_event_detail(chat_id, text, question or "What date and time should I use? Include an end time or duration, or say 'all day'.", now, telegram)
        return
    event = ParsedEvent.model_validate(event.model_dump())
    try:
        event.reminders = _reminders_from_text(text)
    except ValueError as exc:
        await _ask_event_detail(chat_id, text, str(exc), now, telegram)
        return
    reminder_instructions = _reminder_instructions(text)
    if reminder_instructions and any(action.endswith("_model") for action, _ in reminder_instructions[:-1]):
        await _ask_event_detail(chat_id, text, "Please restate all desired reminders in minutes, for example '60 minutes before and 15 minutes before'.", now, telegram)
        return
    if reminder_instructions and reminder_instructions[-1][0].endswith("_model"):
        action, instruction = reminder_instructions[-1]
        if action == "remove_model" or not event.reminder_minutes:
            await _ask_event_detail(chat_id, text, "Please specify the reminder lead time in minutes, for example '15 minutes before'.", now, telegram)
            return
        reminder = ReminderSpec(minutes_before=event.reminder_minutes, message=_reminder_message_from_text(instruction))
        if not any((item.minutes_before, item.message) == (reminder.minutes_before, reminder.message) for item in event.reminders):
            event.reminders.append(reminder)
    elif reminder_instructions is None and not event.reminders and event.reminder_minutes:
        event.reminders = [ReminderSpec(minutes_before=event.reminder_minutes, message=_reminder_message_from_text(text))]
    event.reminder_minutes = event.reminders[0].minutes_before if event.reminders else None
    _apply_recurrence_from_text(event, text)
    if event.start.tzinfo is not None and event.end.tzinfo is not None:
        _apply_all_day_from_text(event, time_text, settings)
    if event.start.tzinfo is None or event.end.tzinfo is None:
        await _ask_event_detail(chat_id, text, "I couldn't resolve the date/time in your timezone. Please resend the intended date and time.", now, telegram)
        return
    if event.end <= event.start:
        await _ask_event_detail(chat_id, text, "When should it end? Please give an end time after the start, or a positive duration.", now, telegram)
        return
    pending_event_drafts.pop(chat_id, None)
    _apply_weekday_from_text(event, date_text, now)
    if chat_id in media_requests:
        token = uuid4().hex
        media_previews[chat_id] = PendingEventConflict(event, text, now, token, time.monotonic() + PENDING_TTL_SECONDS)
        media_requests[chat_id] = time.monotonic() + PENDING_TTL_SECONDS
        details = f"Suggested event: {event.title} — {_format_event_range(event)}"
        if event.location:
            details += f"\n📍 {event.location}"
        details += f"\nCalendar: {event.calendar_name or 'your default calendar'}"
        if event.recurrence:
            details += f"\nRepeats: {event.recurrence}"
        if event.reminders:
            details += "\n⏰ " + _reminder_confirmation(event.reminders)
        await telegram.send_message(chat_id, details + "\nAdd this event? You can correct the details first. Choose within 5 minutes.", {
            "inline_keyboard": [[{"text": "Add event", "callback_data": f"media:{token}:add"},
                                 {"text": "Cancel", "callback_data": f"media:{token}:cancel"}]]})
        return
    await _check_and_create_event(chat_id, event, text, now, telegram, calendar)


async def _check_and_create_event(chat_id, event, text, now, telegram, calendar):
    if not _turn_is_current(chat_id):
        return
    lowered = text.lower()
    conflicts = [existing for existing in await asyncio.to_thread(calendar._list_events_between, event.start, event.end) if existing.start < event.end and (existing.end or existing.start) > event.start]
    if not _turn_is_current(chat_id):
        return
    if conflicts and "add anyway" not in lowered:
        details = "\n".join(f"• {existing.title} — {_format_event_range(existing)}" for existing in conflicts[:3])
        token = uuid4().hex
        pending_event_conflicts[chat_id] = PendingEventConflict(event, text, now, token, time.monotonic() + PENDING_TTL_SECONDS)
        buttons = [[{"text": label, "callback_data": f"conflict:{token}:{action}"} for label, action in (
            ("Add anyway", "add"), ("Change time", "change"), ("Cancel", "cancel"),
        )]]
        if not event.all_day:
            buttons.append([{"text": "Suggest times", "callback_data": f"conflict:{token}:suggest"}])
        await telegram.send_message(chat_id, f"⚠️ This overlaps with:\n{details}\n\nPending: {event.title} — {_format_event_range(event)}\nChoose an option within 5 minutes.", {"inline_keyboard": buttons})
        return
    await _create_event(chat_id, event, telegram, calendar)


@_conversation_handler(callback=True)
async def handle_conflict_callback(chat_id, callback_id, data, settings, telegram, calendar):
    match = re.fullmatch(r"conflict:([a-f0-9]{32}):(add|change|cancel|suggest|slot:[01])", data)
    pending = pending_event_conflicts.get(chat_id)
    if not match or not pending or match.group(1) != pending.token or pending.expires_at <= time.monotonic():
        if pending and pending.expires_at <= time.monotonic():
            pending_event_conflicts.pop(chat_id, None)
        message = "This choice has expired or was already used. Please send the event details again."
        if callback_id:
            await telegram.answer_callback_query(callback_id, message)
        else:
            await telegram.send_message(chat_id, message)
        return
    # Consume before awaiting so repeated taps cannot create the same draft twice.
    pending_event_conflicts.pop(chat_id, None)
    action = match.group(2)
    if action == "suggest":
        lookup_token = uuid4().hex
        pending.token = lookup_token
        pending_event_conflicts[chat_id] = pending
        if callback_id:
            await telegram.answer_callback_query(callback_id)
        await _suggest_conflict_times(chat_id, pending, telegram, calendar, lookup_token)
        return
    if callback_id:
        await telegram.answer_callback_query(callback_id)
    if action.startswith("slot:"):
        index = int(action[-1])
        if index >= len(pending.suggestions):
            await telegram.send_message(chat_id, "That time choice is no longer available. Please send a new request.")
            return
        proposed = pending.suggestions[index]
        if pending.existing:
            events = await asyncio.to_thread(calendar._list_events_between, proposed.start, proposed.end)
            if not _turn_is_current(chat_id):
                return
            conflicts = [event for event in events
                         if (event.event_id, event.calendar_id) != (pending.existing.event_id, pending.existing.calendar_id)
                         and event.start < proposed.end and (event.end or event.start) > proposed.start]
            if conflicts:
                await telegram.send_message(chat_id, "That slot is now occupied. Please send a new time for the event.")
                return
            await _commit_edit(chat_id, pending.existing, proposed, pending.series, telegram, calendar)
        else:
            request_text = re.sub(r"\badd\s+anyway\b", "add", pending.request_text, flags=re.I)
            await _check_and_create_event(chat_id, proposed, request_text, pending.now, telegram, calendar)
        return
    if action == "cancel":
        await telegram.send_message(chat_id, "Event edit cancelled." if pending.existing else "Event creation cancelled.")
    elif pending.existing:
        if action != "add":
            await telegram.send_message(chat_id, "Please send a new edit request to change the time.")
            return
        await _commit_edit(chat_id, pending.existing, pending.event, pending.series, telegram, calendar)
    elif action == "change":
        duration = (pending.event.end - pending.event.start).total_seconds() / 60
        text = _event_parser_text(pending.request_text)
        text += f"\nUser follow-up: Change the event's date/time using my next answer. Keep its duration of {duration:g} minutes unless I explicitly change it. Its current start is {pending.event.start.isoformat()}."
        await _ask_event_detail(chat_id, text, "What date or time should I use instead? For example, 'tomorrow at 4pm'.", pending.now, telegram)
    else:
        await _create_event(chat_id, pending.event, telegram, calendar)


async def _suggest_conflict_times(chat_id, pending, telegram, calendar, lookup_token):
    def is_current():
        if not _turn_is_current(chat_id) or pending_event_conflicts.get(chat_id) is not pending or pending.token != lookup_token:
            return False
        if pending.expires_at <= time.monotonic():
            pending_event_conflicts.pop(chat_id, None)
            return False
        return True

    if not is_current():
        return
    if pending.event.all_day:
        pending_event_conflicts.pop(chat_id, None)
        await telegram.send_message(chat_id, "Please choose another date for this all-day event.")
        return
    start = max(pending.event.end, datetime.now(pending.event.start.tzinfo))
    horizon = start + timedelta(days=7)
    duration = pending.event.end - pending.event.start
    try:
        events = await asyncio.to_thread(calendar._list_events_between, start, horizon)
    except Exception:
        if is_current():
            pending_event_conflicts.pop(chat_id, None)
            raise
        return
    if not is_current():
        return
    occupied = sorted((event for event in events if not pending.existing or
                       (event.event_id, event.calendar_id) != (pending.existing.event_id, pending.existing.calendar_id)), key=lambda event: event.start)
    choices = []
    candidate = start
    while candidate + duration <= horizon and len(choices) < 2:
        overlaps = [event for event in occupied if event.start < candidate + duration and (event.end or event.start) > candidate]
        if overlaps:
            candidate = max(event.end or event.start for event in overlaps)
            continue
        choices.append(pending.event.model_copy(update={"start": candidate, "end": candidate + duration}))
        candidate += duration
    if not choices:
        pending_event_conflicts.pop(chat_id, None)
        await telegram.send_message(chat_id, "I couldn't find another slot of that length in the next 7 days. Try a different date or duration.")
        return
    pending.token = uuid4().hex
    pending.expires_at = time.monotonic() + PENDING_TTL_SECONDS
    pending.suggestions = choices
    pending_event_conflicts[chat_id] = pending
    buttons = [[{"text": _format_event_range(choice), "callback_data": f"conflict:{pending.token}:slot:{i}"}] for i, choice in enumerate(choices)]
    buttons.append([{"text": "Cancel", "callback_data": f"conflict:{pending.token}:cancel"}])
    await telegram.send_message(chat_id, f"Other available times for {pending.event.title}:\nChoose within 5 minutes; I'll check again before saving.", {"inline_keyboard": buttons})


async def _create_event(chat_id, event, telegram, calendar):
    if not _turn_is_current(chat_id):
        return
    target_calendar = await asyncio.to_thread(calendar.resolve_calendar, event.calendar_name)
    if not _turn_is_current(chat_id):
        return
    if target_calendar is None:
        await telegram.send_message(chat_id, f"I couldn't find the {event.calendar_name or 'configured default'} calendar. Send 'calendar types' to see your available calendars.")
        return
    if target_calendar.access_role not in {"owner", "writer"}:
        await telegram.send_message(chat_id, f"I can see {target_calendar.name}, but you do not have permission to add events to it.")
        return
    created = await asyncio.to_thread(calendar.create_event, event, target_calendar.calendar_id)
    reminder_confirmation = ""
    if event.reminders:
        reminder_confirmation = "\n⏰ " + _reminder_confirmation(event.reminders)
    recurrence_confirmation = ""
    if event.recurrence:
        frequency = re.search(r"(?:^|[;:])FREQ=(DAILY|WEEKLY|MONTHLY|YEARLY)(?:;|$)", event.recurrence)
        label = "Repeats " + frequency[1].lower() if frequency else "Recurring event"
        if re.search(r"(?:^|;)INTERVAL=(?!1(?:;|$))", event.recurrence):
            label = "Recurring event"
        recurrence_confirmation = "\n🔁 " + label
    calendar_confirmation = f"\n🗓️ Calendar: {target_calendar.name}"
    created = created.model_copy(update={"calendar_name": target_calendar.name})
    await _send_saved_event(chat_id, created, bool(event.recurrence), telegram,
                            f"✅ Added: {created.title} — {_format_event_range(created)}{calendar_confirmation}{recurrence_confirmation}{reminder_confirmation}")


async def _send_saved_event(chat_id, created, recurring, telegram, message):
    if not _turn_is_current(chat_id):
        await telegram.send_message(chat_id, message)
        return
    token = uuid4().hex
    if recurring:
        created = created.model_copy(update={"recurring_event_id": created.event_id})
    _remember_event(chat_id, created)
    pending_actions[chat_id] = PendingAction([created], time.monotonic() + PENDING_TTL_SECONDS,
                                           "created", "recurring series" if recurring else "", token)
    buttons = [{"text": label, "callback_data": f"created:{token}:{action}"} for label, action in (
        ("Change series date/time" if recurring else "Change date/time", "change"),
        ("Change series calendar" if recurring else "Change calendar", "calendar"),
        ("Delete series" if recurring else "Delete this event", "delete"),
    )]
    controls = "\nThese buttons affect the whole series." if recurring else ""
    await telegram.send_message(chat_id, f"{message}{controls}\nChange or delete using the buttons within 5 minutes.", {"inline_keyboard": [buttons[:2], buttons[2:]]})


@_conversation_handler(callback=True)
async def handle_created_callback(chat_id, callback_id, data, telegram, calendar):
    match = re.fullmatch(r"created:([a-f0-9]{32}):(change|delete|calendar|cancel|pick:(\d+))", data)
    pending = pending_actions.get(chat_id)
    action = match.group(2) if match else ""
    expected_state = "created_calendar" if action.startswith("pick:") or action == "cancel" else "created"
    if not match or not pending or pending.action != expected_state or pending.token != match.group(1) or pending.expires_at <= time.monotonic():
        if pending and pending.expires_at <= time.monotonic():
            pending_actions.pop(chat_id, None)
        await telegram.answer_callback_query(callback_id, "These event shortcuts expired or were already used. Please send a new edit/delete request.")
        return
    pending_actions.pop(chat_id, None)
    if action == "change":
        pending.action = "created_edit"
        pending.token = uuid4().hex
        pending.expires_at = time.monotonic() + PENDING_TTL_SECONDS
        pending_actions[chat_id] = pending
    elif action == "calendar" or action.startswith("pick:"):
        pending.action = "created_calendar" if action == "calendar" else "created_moving"
        pending.token = uuid4().hex
        pending.expires_at = time.monotonic() + PENDING_TTL_SECONDS
        pending_actions[chat_id] = pending
    await telegram.answer_callback_query(callback_id)
    if not _turn_is_current(chat_id):
        return
    event = pending.events[0]
    if action == "cancel":
        await telegram.send_message(chat_id, "Calendar change cancelled. The saved event is unchanged.")
        return
    if action == "calendar":
        if event.recurring_event_id and not pending.request_text:
            if pending_actions.get(chat_id) is pending:
                pending_actions.pop(chat_id, None)
            await telegram.send_message(chat_id, "Changing calendar for one recurring occurrence is unavailable. Please send an explicit request for the whole series.")
            return
        try:
            choices = await asyncio.to_thread(calendar.list_calendars)
        except Exception:
            if pending_actions.get(chat_id) is pending:
                pending_actions.pop(chat_id, None)
            await telegram.send_message(chat_id, "I couldn't load your calendars. The event is unchanged. Please send a new request.")
            return
        if pending_actions.get(chat_id) is not pending:
            return
        pending.calendars = [c for c in choices if c.access_role in {"owner", "writer"} and c.calendar_id != event.calendar_id]
        if not pending.calendars:
            pending_actions.pop(chat_id, None)
            await telegram.send_message(chat_id, "There are no other writable calendars. The saved event is unchanged.")
            return
        buttons = [[{"text": c.name, "callback_data": f"created:{pending.token}:pick:{i}"}] for i, c in enumerate(pending.calendars)]
        buttons.append([{"text": "Cancel", "callback_data": f"created:{pending.token}:cancel"}])
        scope = "whole series" if pending.request_text else "event"
        await telegram.send_message(chat_id, f"Choose a calendar for this {scope}: {event.title}\nCurrent calendar: {event.calendar_name}\nChoose within 5 minutes, or send /cancel.", {"inline_keyboard": buttons})
        return
    if action.startswith("pick:"):
        index = int(match.group(3))
        if index >= len(pending.calendars):
            if pending_actions.get(chat_id) is pending:
                pending_actions.pop(chat_id, None)
            await telegram.send_message(chat_id, "Invalid calendar choice. Please send a new request.")
            return
        try:
            moved = await asyncio.to_thread(calendar.move_event, event, pending.calendars[index])
        except CalendarMoveError as exc:
            await telegram.send_message(chat_id, f"The event moved to {exc.event.calendar_name}, but reminder preservation failed. Please inspect its reminders before retrying.")
            return
        except Exception:
            logger.exception("Calendar move failed")
            await telegram.send_message(chat_id, "I couldn't complete the calendar change. Please inspect your calendars before sending a new request.")
            return
        finally:
            still_current = pending_actions.get(chat_id) is pending
            if still_current:
                pending_actions.pop(chat_id, None)
        message = f"✅ Moved: {moved.title} — {_format_event_range(moved)}\n🗓️ Calendar: {moved.calendar_name}"
        if still_current:
            await _send_saved_event(chat_id, moved, bool(pending.request_text), telegram, message)
        else:
            await telegram.send_message(chat_id, message)
        return
    if match.group(2) == "delete":
        await _ask_delete_confirmation(chat_id, event, telegram, series=bool(pending.request_text))
    else:
        scope = "the whole series" if pending.request_text else "this event"
        question = "What date or time should I use"
        example = "For example, 'Tue', '4pm' or 'Tue 8-10pm'. Date-only replies keep the current time and duration."
        await telegram.send_message(chat_id, f"{question} for {scope}: {event.title} — {_format_event_range(event)}?\n{example} Reply within 5 minutes, or send /cancel.")


async def _ask_event_detail(chat_id, text, question, now, telegram):
    if not _turn_is_current(chat_id):
        return
    if chat_id in media_requests:
        media_requests[chat_id] = time.monotonic() + PENDING_TTL_SECONDS
    pending_event_drafts[chat_id] = PendingEventDraft(text, now, time.monotonic() + PENDING_TTL_SECONDS)
    await telegram.send_message(chat_id, f"{question}\nFor: {text.split(chr(10))[0][:300]}\nReply within 5 minutes, or send /cancel.")


def _is_explicit_series_delete(text: str, event: CalendarEvent) -> bool:
    if not event.recurring_event_id:
        return False
    lowered = text.casefold()
    return (
        any(term in lowered for term in ("weekly", "recurring", "series", "every "))
        or ("all" in lowered and any(term in lowered for term in ("session", "occurrence")))
    )


async def _ask_delete_confirmation(
    chat_id: int, event: CalendarEvent, telegram: TelegramClient, *, series: bool = False,
) -> None:
    if not _turn_is_current(chat_id):
        return
    token = uuid4().hex
    pending_actions[chat_id] = PendingAction(
        [event], time.monotonic() + PENDING_TTL_SECONDS, "delete_confirm", "",
        token, series=series,
    )
    target = "the entire recurring series" if series else "this event"
    undo_note = (
        "This deletion cannot be undone by the bot."
        if series or event.recurring_event_id
        else "You can undo this deletion for 5 minutes."
    )
    await telegram.send_message(
        chat_id,
        f"⚠️ Delete {target}?\n{event.title} — {_format_event_range(event)}"
        f"{f' · {event.calendar_name}' if event.calendar_name else ''}\n{undo_note}",
        {"inline_keyboard": [[
            {"text": "Confirm deletion", "callback_data": f"delete:{token}:confirm"},
            {"text": "Cancel", "callback_data": f"delete:{token}:cancel"},
        ]]},
    )


async def _complete_event_deletion(chat_id: int, pending: PendingAction, telegram, calendar) -> None:
    if not _turn_is_current(chat_id):
        return
    event = pending.events[0]
    _clear_conversation(chat_id)
    if pending.series:
        await asyncio.to_thread(calendar.delete_series, event)
        await telegram.send_message(chat_id, f"✅ Deleted recurring series: {event.title}. This cannot be undone by the bot.")
        return
    if event.recurring_event_id:
        await asyncio.to_thread(calendar.delete_event, event)
        await telegram.send_message(chat_id, f"✅ Deleted recurring occurrence: {event.title} — {_format_time(event.start)}. This cannot be undone by the bot.")
        return
    resource = await asyncio.to_thread(calendar.delete_event_with_undo, event)
    if not _turn_is_current(chat_id) or chat_id in pending_actions:
        await telegram.send_message(
            chat_id, f"✅ Deleted: {event.title} — {_format_time(event.start)}",
        )
        return
    token = uuid4().hex
    pending_actions[chat_id] = PendingAction(
        [event], time.monotonic() + PENDING_TTL_SECONDS, "undo_delete", "",
        token, deleted_resource=resource,
    )
    await telegram.send_message(
        chat_id, f"✅ Deleted: {event.title} — {_format_time(event.start)}",
        {"inline_keyboard": [[{
            "text": "Undo", "callback_data": f"delete:{token}:undo",
        }]]},
    )


async def _restore_deleted_event(chat_id: int, pending: PendingAction, telegram, calendar) -> None:
    if not _turn_is_current(chat_id):
        return
    event = pending.events[0]
    if pending.deleted_resource is None:
        await telegram.send_message(chat_id, "I no longer have the deleted event details needed to restore it. Please check Google Calendar.")
        return
    restored = await asyncio.to_thread(
        calendar.restore_deleted_event,
        pending.deleted_resource,
        event.calendar_id,
        event.calendar_name,
    )
    await telegram.send_message(
        chat_id,
        f"↩️ Restored: {restored.title} — {_format_event_range(restored)}"
        f"{f' · {restored.calendar_name}' if restored.calendar_name else ''}",
    )


@_conversation_handler(callback=True)
async def handle_delete_callback(chat_id, callback_id, data, telegram, calendar) -> None:
    match = re.fullmatch(r"delete:([a-f0-9]{32}):(confirm|cancel|undo)", data)
    pending = pending_actions.get(chat_id)
    action = match.group(2) if match else ""
    expected_action = "undo_delete" if action == "undo" else "delete_confirm"
    if (
        not match or not pending or pending.action != expected_action
        or pending.token != match.group(1) or pending.expires_at <= time.monotonic()
    ):
        if pending and pending.expires_at <= time.monotonic():
            pending_actions.pop(chat_id, None)
        await telegram.answer_callback_query(callback_id, "This deletion choice expired or was already used.")
        return
    pending_actions.pop(chat_id, None)
    await telegram.answer_callback_query(callback_id)
    if action == "cancel":
        await telegram.send_message(chat_id, "Event deletion cancelled.")
    elif action == "undo":
        await _restore_deleted_event(chat_id, pending, telegram, calendar)
    else:
        await _complete_event_deletion(chat_id, pending, telegram, calendar)


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


async def _edit_event(chat_id: int, text: str, existing: CalendarEvent, settings: Settings, telegram: TelegramClient, calendar: CalendarClient, parser: GroqParser, *, preserve_clock=False) -> None:
    if not _turn_is_current(chat_id):
        return
    now = datetime.now(settings.timezone)
    destination = re.split(r"\bto\b", text, maxsplit=1, flags=re.IGNORECASE)[-1]
    date_text = _latest_detail_text(destination, DATE_HINT_PATTERN, remove_ranges=True)
    time_text = _latest_detail_text(destination, TIME_HINT_PATTERN)
    try:
        question = _weekday_date_question(date_text, settings, now)
        clocks = _clock_range_from_text(time_text)
    except ValueError as exc:
        question = str(exc)
    if question:
        await telegram.send_message(chat_id, f"{question}\nPlease resend the edit with the intended date and time.")
        return
    location_match = re.search(r"\b(?:to\s+be\s+)?at\s+(.+?)\s*$", text, flags=re.IGNORECASE)
    if location_match and not _has_explicit_event_time(text):
        edited = ParsedEdit(
            title=existing.title, start=existing.start, end=existing.end or existing.start,
            location=location_match.group(1).strip(), confidence="high", all_day=existing.all_day,
        )
    else:
        edited = await asyncio.to_thread(parser.parse_edit, text, existing, settings.user_timezone)
        if not _turn_is_current(chat_id):
            return
        if clocks:
            if not re.search(DATE_HINT_PATTERN, _without_clock_ranges(date_text), flags=re.IGNORECASE):
                edited.start = existing.start
                edited.end = existing.start
            _apply_clock_range(edited, clocks, date_text, settings, now)
        if edited.start.tzinfo is not None and edited.end.tzinfo is not None:
            _apply_weekday_from_text(edited, date_text, now)
    if edited.confidence == "low" or edited.start.tzinfo is None or edited.end.tzinfo is None:
        pending_actions[chat_id] = PendingAction([existing], time.monotonic() + PENDING_TTL_SECONDS, "followup_edit", text)
        await telegram.send_message(chat_id, f"What date or time should I use for {existing.title} — {_format_event_range(existing)}? For example, 'Friday after 7pm for 1 hour'. Reply within 5 minutes, or /cancel.")
        return
    _apply_all_day_from_text(edited, time_text, settings)
    if (preserve_clock and not _has_explicit_event_time(time_text)
            and not re.search(DURATION_HINT_PATTERN, time_text, flags=re.I)
            and not re.search(r"\b(?:all|whole)[\s-]+day\b", time_text, flags=re.I)):
        target = _local_schedule_date(date_text, settings, now) or edited.start.astimezone(settings.timezone).date()
        local_start = existing.start.astimezone(settings.timezone)
        local_end = (existing.end or existing.start).astimezone(settings.timezone)
        shift = target - local_start.date()
        edited.start, edited.end = local_start + shift, local_end + shift
        edited.all_day = existing.all_day
    if edited.end <= edited.start:
        await telegram.send_message(chat_id, "Please give an end time after the start.")
        return
    series = _is_series_edit(text, existing)
    if series:
        _apply_recurrence_from_text(edited, text)
    if edited.start != existing.start or edited.end != existing.end or edited.recurrence:
        events = await asyncio.to_thread(calendar._list_events_between, edited.start, edited.end)
        if not _turn_is_current(chat_id):
            return
        conflicts = [event for event in events
            if (event.event_id, event.calendar_id) != (existing.event_id, existing.calendar_id)
            and event.start < edited.end and (event.end or event.start) > edited.start]
        if conflicts:
            token = uuid4().hex
            pending_event_conflicts[chat_id] = PendingEventConflict(edited, text, datetime.now(settings.timezone), token,
                time.monotonic() + PENDING_TTL_SECONDS, existing, series)
            details = "\n".join(f"• {event.title} — {_format_event_range(event)}" for event in conflicts[:3])
            await telegram.send_message(chat_id, f"⚠️ This edit overlaps with:\n{details}\n\nProposed: {edited.title} — {_format_event_range(edited)}\nChoose within 5 minutes.",
                {"inline_keyboard": [[{"text": "Apply anyway", "callback_data": f"conflict:{token}:add"}, {"text": "Cancel", "callback_data": f"conflict:{token}:cancel"}]] +
                 ([[{"text": "Suggest times", "callback_data": f"conflict:{token}:suggest"}]] if not edited.all_day else [])})
            return
    await _commit_edit(chat_id, existing, edited, series, telegram, calendar)


async def _commit_edit(chat_id, existing, edited, series, telegram, calendar):
    if not _turn_is_current(chat_id):
        return
    current_pending = pending_actions.get(chat_id)
    if series:
        updated = await asyncio.to_thread(calendar.update_series, existing, edited)
        message = _format_series_update_confirmation(updated, edited.recurrence)
    else:
        updated = await asyncio.to_thread(calendar.update_event, existing, edited)
        message = _format_update_confirmation(updated)
    if pending_actions.get(chat_id) is current_pending:
        await _send_saved_event(chat_id, updated, series, telegram, message)
    else:
        await telegram.send_message(chat_id, message)


async def _set_reminder(chat_id: int, text: str, event: CalendarEvent, settings: Settings, telegram: TelegramClient, calendar: CalendarClient, parser: GroqParser) -> None:
    if not _turn_is_current(chat_id):
        return
    _remember_event(chat_id, event)
    reminder = await asyncio.to_thread(parser.parse_reminder_for_event, text, event, settings.user_timezone)
    if not _turn_is_current(chat_id):
        return
    if reminder.confidence == "low":
        pending_actions[chat_id] = PendingAction([event], time.monotonic() + PENDING_TTL_SECONDS, "followup_remind", text)
        await telegram.send_message(chat_id, f"When should I remind you about {event.title}? For example, '1 hour before'. Reply within 5 minutes, or /cancel.")
        return
    message = _reminder_message_from_text(text)
    reminder_instructions = _reminder_instructions(text)
    append = reminder_instructions[-1][0] in {"append", "append_model"} if reminder_instructions else any(word in text.split("\nUser follow-up:")[-1].lower() for word in ("another", "also", "additional"))
    await asyncio.to_thread(calendar.set_reminder, event, reminder.reminder_minutes, message, append)
    unit = "minute" if reminder.reminder_minutes == 1 else "minutes"
    action = "Additional reminder set" if append else "Reminder set"
    message_line = f"\n📝 {message}" if message else ""
    await telegram.send_message(chat_id, f"⏰ {action}: {event.title} — {reminder.reminder_minutes} {unit} before {_format_time(event.start)}{message_line}")


def _is_existing_reminder_request(text: str) -> bool:
    text = _command_text(text)
    return bool(re.match(r"^(?:remind me|(?:set|add) (?:a|another|additional) reminder)\b", text, flags=re.I)
                or re.match(r"^(?:change|edit|update|move|reschedule)\s+(?:(?:a|the)\s+)?reminder(?:\s+(?:for|to|before|at)\b|$)", text, flags=re.I))


def _is_standalone_reminder_request(text: str) -> bool:
    lowered = _command_text(text).lower()
    if lowered.startswith(("remind me to ", "remind me at ", "remind me in ")):
        return True

    # A lead time links a reminder to an existing event (for example,
    # "set a reminder one day before IPPT"). An absolute/relative clock time
    # instead describes an independent notification.
    if re.search(r"\b(?:minutes?|mins?|hours?|hrs?|days?)\s+before\b", lowered):
        return False
    command = re.match(
        r"^(?:please\s+)?(?:remind\s+me|(?:set(?:\s+me)?|add)(?:\s+up)?\s+(?:a\s+)?reminder)\b",
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
        or re.search(WORD_CLOCK_PATTERN, lowered)
    )


def _event_parser_text(text: str) -> str:
    """Remove routing controls and normalize all-day synonyms before LLM parsing."""
    normalized = re.sub(r"^\s*add\s+anyway\b", "add", text, flags=re.IGNORECASE)
    return re.sub(r"\bwhole[\s-]+day\b", "all day", normalized, flags=re.IGNORECASE)


def _explicit_all_day_event(text: str, settings: Settings) -> ParsedEvent | None:
    """Parse concise '<day> whole day with <title>' requests without the LLM."""
    normalized = re.sub(r"^\s*add(?:\s+anyway)?\s+", "", text.strip(), flags=re.IGNORECASE)
    match = re.match(
        rf"(?P<when>(?:on\s+)?(?:{WEEKDAY_NAMES}|"
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


def _calendar_date_from_text(text: str, settings: Settings, now=None):
    match = re.search(NAMED_DATE_PATTERN, text, flags=re.IGNORECASE)
    if not match:
        return None
    year = int(match.group(3) or (now or datetime.now(settings.timezone)).year)
    month = "Sep" if match.group(2).lower() == "sept" else match.group(2)
    value = f"{match.group(1)} {month} {year}"
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
    return _calendar_date_from_text(text, settings)


def _without_clock_ranges(text: str) -> str:
    return CLOCK_RANGE_PATTERN.sub(
        lambda match: "" if match[3] or match[6] or (match[2] is not None and match[5] is not None) else match[0], text,
    )


def _scheduling_text(text: str) -> str:
    if re.match(r"^\s*(?:(?:actually|sorry|instead)[, ]+)?(?:call it|rename|(?:change|set) (?:the )?(?:title|name|location|calendar) to)\b", text, flags=re.I):
        return ""
    title = re.match(r"^\s*(?:(?:add|create|put)\s+)?(?P<title>'[^'\n]*'|\"[^\"\n]*\")", text, flags=re.I)
    if title:
        start, end = title.span("title")
        text = text[:start] + " " * (end - start) + text[end:]
    if re.match(r"^\s*at\s+", text, flags=re.I) and not re.match(r"^\s*at\s+(?:\d|noon\b|midnight\b|midday\b)", text, flags=re.I):
        clock = re.search(r"\b\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)\b|" + WORD_CLOCK_PATTERN, _instruction_text(text), flags=re.I)
        return clock[0] if clock else ""
    reminder = re.search(r"\b(?:remind me|(?:set|add|change)(?: (?:a|the|another))? reminder)\b|" + REMINDER_LEAD_PATTERN, _instruction_text(text), flags=re.I)
    return text[:reminder.start()] if reminder else text


def _latest_detail_text(text: str, pattern: str, *, remove_ranges=False) -> str:
    parts = text.split("\nUser follow-up:")
    for part in reversed(parts):
        if part.strip().startswith("Change the event's date/time using my next answer."):
            continue
        scheduling = _scheduling_text(part)
        if re.search(pattern, _without_clock_ranges(scheduling) if remove_ranges else scheduling, flags=re.IGNORECASE):
            return scheduling
    return _scheduling_text(parts[0])


def _explicit_schedule_date(text: str, settings: Settings, now: datetime):
    text = _without_clock_ranges(text)
    named = list(re.finditer(NAMED_DATE_PATTERN, text, flags=re.IGNORECASE))
    numeric = list(re.finditer(NUMERIC_DATE_PATTERN, text))
    if len(named) + len(numeric) != 1:
        return None
    if named:
        day = _calendar_date_from_text(text, settings, now)
        if day is None:
            raise ValueError("That date is invalid. Which date should I use?")
        return day
    match = numeric[0]
    year, month, day = (int(match[1]), int(match[2]), int(match[3])) if match[1] else (
        int(match[6] or now.year), int(match[5]), int(match[4]),
    )
    try:
        return datetime(year, month, day).date()
    except ValueError as exc:
        raise ValueError("That date is invalid. Which date should I use?") from exc


def _weekday_date_question(text: str, settings: Settings, now: datetime) -> str | None:
    day = _explicit_schedule_date(text, settings, now)
    weekdays = re.findall(rf"\b({WEEKDAY_NAMES})\b", text, flags=re.IGNORECASE)
    if day is not None and len(weekdays) == 1 and weekdays[0][:3].lower() != day.strftime("%a").lower():
        return f"{weekdays[0].capitalize()} and {day.day} {day:%B %Y} ({day:%A}) don't match. Which date should I use?"
    return None


def _clock_range_from_text(text: str):
    matches = [match for match in CLOCK_RANGE_PATTERN.finditer(text)
               if match[3] or match[6] or (match[2] is not None and match[5] is not None)]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("Which time range should I use? Please give one start and end time.")
    match = matches[0]
    clocks = []
    for hour, minute, period in ((match[1], match[2], match[3] or match[6]), (match[4], match[5], match[6] or match[3])):
        hour, minute = int(hour), int(minute or 0)
        if minute > 59 or not (1 <= hour <= 12 if period else 0 <= hour <= 23):
            raise ValueError("Please give a valid time range, such as '8pm-10pm' or '20:00-22:00'.")
        if period:
            hour = hour % 12 + (12 if period.lower() == "pm" else 0)
        clocks.append((hour, minute))
    if clocks[0] == clocks[1] or (bool(match[3]) != bool(match[6]) and clocks[1] < clocks[0]):
        raise ValueError("Please make the start and end clear, including both AM/PM markers, such as '11pm-1am'.")
    return clocks


def _local_schedule_date(text, settings, now):
    day = _relative_weekday_date(text, now) or _explicit_schedule_date(text, settings, now)
    relative = re.findall(r"\b(today|tonight|tomorrow|tmr)\b", text, flags=re.IGNORECASE)
    if day is None and len(relative) == 1:
        day = now.date() + timedelta(days=relative[0].lower() in {"tomorrow", "tmr"})
    return day


def _apply_clock_range(event, clocks, text, settings, now):
    if event.all_day or (event.start is not None and event.start.tzinfo is None):
        return
    day = _local_schedule_date(text, settings, now)
    local_day = day or (event.start.astimezone(now.tzinfo).date() if event.start is not None else None)
    if local_day is None:
        return
    span = 0
    multiple_dates = len(re.findall(NAMED_DATE_PATTERN, text, flags=re.IGNORECASE)) + len(re.findall(NUMERIC_DATE_PATTERN, _without_clock_ranges(text))) > 1
    multiple_weekdays = len(re.findall(rf"\b({WEEKDAY_NAMES})\b", text, flags=re.IGNORECASE)) > 1
    if (multiple_dates or multiple_weekdays) and event.start is not None and event.end is not None and event.end.tzinfo is not None:
        span = max(0, (event.end.astimezone(now.tzinfo).date() - local_day).days)
    start = now.replace(year=local_day.year, month=local_day.month, day=local_day.day,
                        hour=clocks[0][0], minute=clocks[0][1], second=0, microsecond=0)
    end = start.replace(hour=clocks[1][0], minute=clocks[1][1]) + timedelta(days=span)
    if end < start:
        end += timedelta(days=1)
    event.start, event.end = start, end
    missing = getattr(event, "missing_fields", [])
    if missing:
        event.missing_fields = [field for field in missing if field not in {"time", "duration"} and not (field == "date" and day is not None)]
        if not event.missing_fields:
            event.confidence = "high"


def _apply_weekday_from_text(event: ParsedEvent, text: str, now: datetime) -> None:
    """Resolve a single relative weekday locally before checking conflicts."""
    target = _relative_weekday_date(text, now)
    if target is None:
        return
    local_start = event.start.astimezone(now.tzinfo)
    local_end = event.end.astimezone(now.tzinfo)
    shift = target - local_start.date()
    event.start = local_start + shift
    event.end = local_end + shift


def _relative_weekday_date(text: str, now: datetime):
    weekdays = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    matches = list(re.finditer(
        rf"\b(?:(next|this|on|every)\s+)?({WEEKDAY_NAMES})\b",
        text, flags=re.IGNORECASE,
    ))
    date_text = _without_clock_ranges(text)
    # Leave date ranges and qualified calendar dates to the parser.
    if len(matches) != 1 or re.search(
        r"\b\d{1,4}[-/]\d{1,2}|\b\d{1,2}(?:st|nd|rd|th)\b|"
        rf"\b(?:{MONTH_NAMES})\b|"
        r"\b(?:last|following|after|before|weeks?|months?|today|tomorrow)\b",
        date_text, flags=re.IGNORECASE,
    ):
        return
    match = matches[0]
    offset = (weekdays.index(match.group(2)[:3].lower()) - now.weekday()) % 7
    if offset == 0 and (match.group(1) or "").lower() == "next":
        offset = 7
    return now.date() + timedelta(days=offset)


def _upcoming_weekday_from_text(text: str, settings: Settings):
    match = re.search(rf"\b({WEEKDAY_NAMES})\b", text, flags=re.IGNORECASE)
    if not match:
        return None
    weekday_names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
    today = datetime.now(settings.timezone).date()
    offset = (weekday_names.index(match.group(1)[:3].lower()) - today.weekday()) % 7
    return today + timedelta(days=offset)


async def _execute_selection(chat_id, pending, selected, settings, telegram, calendar, parser, cron):
    if not _turn_is_current(chat_id):
        return
    _remember_event(chat_id, selected)
    if pending.action == "delete":
        await _ask_delete_confirmation(
            chat_id, selected, telegram,
            series=_is_explicit_series_delete(pending.request_text, selected),
        )
    elif pending.action == "edit":
        await _edit_event(chat_id, pending.request_text, selected, settings, telegram, calendar, parser, preserve_clock=True)
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


async def _ask_to_select(chat_id: int, action: str, request_text: str, events: list[CalendarEvent], match, telegram: TelegramClient) -> None:
    if not _turn_is_current(chat_id):
        return
    choices = [event for event in events if event.event_id in {candidate.event_id for candidate in match.candidates}] or events[:5]
    token = uuid4().hex
    pending_actions[chat_id] = PendingAction(choices, time.monotonic() + PENDING_TTL_SECONDS, action, request_text, token)
    lines = [f"{i}. {event.title} — {_format_time(event.start)}" for i, event in enumerate(choices, 1)]
    verb = {"edit": "edit", "delete": "delete", "remind": "set a reminder for", "clear_reminder": "remove the reminder for"}[action]
    buttons = [[{
        "text": f"{i}. {event.title[:60]} — {_format_time(event.start)}" + (f" · {event.calendar_name[:30]}" if event.calendar_name else ""),
        "callback_data": f"select:{token}:{i}",
    }] for i, event in enumerate(choices, 1)]
    buttons.append([{"text": "Cancel", "callback_data": f"select:{token}:cancel"}])
    await telegram.send_message(chat_id, f"Which event should I {verb}? Tap an event below.\n" + "\n".join(lines) + "\nChoose within 5 minutes. You can also reply with a number.", {"inline_keyboard": buttons})


@_conversation_handler(callback=True)
async def handle_selection_callback(chat_id, callback_id, data, settings, telegram, calendar, parser, cron=None):
    match = re.fullmatch(r"select:([a-f0-9]{32}):(cancel|[1-9]\d*)", data)
    pending = pending_actions.get(chat_id)
    if not match or not pending or pending.action in {
        "created", "created_edit", "created_calendar", "created_moving",
        "delete_confirm", "undo_delete",
    } or pending.token != match.group(1) or pending.expires_at <= time.monotonic():
        if pending and pending.expires_at <= time.monotonic():
            pending_actions.pop(chat_id, None)
        await telegram.answer_callback_query(callback_id, "This selection expired or was already used. Please send your request again.")
        return
    choice = match.group(2)
    if choice != "cancel" and int(choice) > len(pending.events):
        await telegram.answer_callback_query(callback_id, "Please choose one of the displayed events.")
        return
    pending_actions.pop(chat_id, None)
    await telegram.answer_callback_query(callback_id)
    if choice == "cancel":
        await telegram.send_message(chat_id, "Selection cancelled.")
        return
    await _execute_selection(chat_id, pending, pending.events[int(choice) - 1], settings, telegram, calendar, parser, cron)


def _selection(text: str, events: list[CalendarEvent]) -> CalendarEvent | None:
    match = re.fullmatch(r"\s*([1-9]\d*)\s*", text)
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


HELP_TOPICS = {
    "events": ("Events", "Events — send a message like:\n\n"
        "Dentist tomorrow 2–3pm\n"
        "Add Drills at TSA@JK on Monday 8-10pm\n"
        "move Drills to Tue 20:00-22:00\n"
        "add Friday whole day with Ames\n"
        "Gym every Monday at 8pm for 1 hour\n"
        "what are my plans tomorrow?\n"
        "move Dentist to Friday at 4pm\n"
        "delete Dentist tomorrow\n\n"
        "A weekday uses the next matching day, including today; 'next Monday' on Monday means one week later. "
        "Mon–Sun abbreviations and 24-hour ranges work too. If a weekday and date disagree, I ask which date to use. "
        "For overnight ranges, use '11pm-1am' or '23:00-01:00'. "
        "I ask for missing dates, times, or duration. Tap an event if several match. "
        "Every event or series deletion asks for confirmation; one-off event deletions can be undone for 5 minutes. "
        "Conflicts are checked at the requested date, even months ahead, and offer Add anyway (or Apply anyway for edits) and Cancel. "
        "Recurring events check only the first occurrence. "
        "After adding or updating, Change date/time, Change calendar and Delete this event buttons work for 5 minutes; date-only replies keep the time and duration. Recurring-series buttons affect the whole series. "
        "Say 'weekly series' when editing or deleting all occurrences. "
        "For 5 minutes after saving, say 'make it 4pm', 'actually Sunday', or 'remind me an hour before'. "
        "After listing events, say 'move the second one to Friday'. "
        "Send a voice note, audio file or invitation image to review an event suggestion before adding it."),
    "reminders": ("Reminders", "Reminders — send a message like:\n\n"
        "remind me in 10 minutes to check the oven\n"
        "remind me 15 minutes before Dental\n"
        "add another reminder for Dental 1 hour before\n"
        "/reminders — list upcoming reminders\n"
        "update 1 to 11.30am — change an independent reminder's time after listing\n"
        "/reminder_status — check delivery status\n"
        "remove 2 — remove item 2 after listing reminders\n\n"
        "Independent reminders send a Telegram message without creating an event. "
        "Event reminders are attached to appointments. Status history has limits after a restart."),
    "availability": ("Availability", "Availability — send a message like:\n\n"
        "when am I free tmr?\n"
        "when am I free on 19 September?\n"
        "find free time this week\n\n"
        "Results show gaps of at least one hour across your visible calendars, "
        "from 12:00 AM to 11:59 PM. /now shows the current date, time, and timezone."),
    "calendars": ("Calendars", "Calendars — send a message like:\n\n"
        "/calendars — list calendars and colours\n"
        "create calendar School\n"
        "Team meeting tomorrow 2–3pm in Work calendar\n"
        "delete calendar School\n\n"
        "Deleting a calendar requires 'confirm delete calendar'; reply 'cancel' to stop. "
        "The primary calendar cannot be deleted. Add events only to calendars you can write to."),
}


async def _send_help(chat_id, telegram, topic="menu"):
    if topic == "menu":
        text = "What would you like to do? Choose a category for example messages.\n\n/now — current date, time, and timezone\nSend a photo or voice note for an AI preview, then reply use media to process it.\nSend /help anytime to return here."
        buttons = [[{"text": title, "callback_data": f"help:{key}"}] for key, (title, _) in HELP_TOPICS.items()]
    else:
        text = HELP_TOPICS[topic][1]
        buttons = [[{"text": "Back", "callback_data": "help:menu"}]]
    await telegram.send_message(chat_id, text, {"inline_keyboard": buttons})


def _welcome_message(first_name: str) -> str:
    name = first_name or "there"
    return (
        f"Hi {name}! 👋 Welcome to SchedulingBot.\n\n"
        "I can manage your personal Google Calendar. Try:\n"
        "• Dinner tomorrow 7–9pm at La Pasta\n"
        "• What are my plans on 19 Aug?\n"
        "• When am I free tomorrow?\n"
        "• Remind me 30 minutes before IPPT\n\n"
        "Commands: /reminders · /reminder_status · /calendars · /now\n"
        "Send /help to explore features, or ‘list’ to see upcoming events."
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


async def _show_reminder_status(chat_id, telegram, calendar, cron):
    reminders = []
    unavailable = []
    try:
        reminders.extend(await asyncio.to_thread(calendar.list_reminders, 30, include_sent=True))
    except Exception:
        logger.exception("Could not retrieve calendar reminder status chat_id=%s", chat_id)
        unavailable.append("Google Calendar")
    if cron:
        try:
            reminders.extend(await cron.list_reminders(chat_id, include_history=True))
        except Exception:
            logger.exception("Could not retrieve independent reminder status chat_id=%s", chat_id)
            unavailable.append("independent reminders")
    else:
        unavailable.append("independent reminders (not configured)")
    for key, (expiry, observed) in list(reminder_delivery_history.items()):
        if expiry <= time.monotonic():
            reminder_delivery_history.pop(key, None)
        elif key[0] == chat_id:
            reminders = [item for item in reminders if not (item.standalone and item.event_id == observed.event_id)]
            reminders.append(observed)
    reminders.sort(key=lambda item: item.due_at)
    lines = [f"{_format_reminder_listing(item, i)}\n   Status: {item.status}" for i, item in enumerate(reminders, 1)]
    footer = "Independent delivery history covers observations from this process for 24 hours and is lost on restart."
    if unavailable:
        footer += f"\nUnavailable: {', '.join(unavailable)}."
    for message in _chunk_section_message("Reminder status:", lines or ["No reminder records found."], footer):
        await telegram.send_message(chat_id, message)


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
    for answer in reversed(text.split("\nUser follow-up:")):
        match = re.search(r"\bbefore\s*(?:to\s+|:\s*)(.+)$", answer, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def _instruction_text(text: str) -> str:
    # Quoted titles and message text are data, never reminder control clauses.
    return re.sub(r"(?<!\w)(?:'[^'\n]*'|\"[^\"\n]*\")(?!\w)", lambda match: " " * len(match[0]), text)


def _reminder_instructions(text: str) -> list[tuple[str, str]] | None:
    instructions = []
    for part in text.split("\nUser follow-up:"):
        control = _instruction_text(part)
        lead = re.search(REMINDER_LEAD_PATTERN, control, flags=re.I)
        if lead:
            prefix = control[:lead.start()]
            if re.match(r"^\s*(?:(?:actually|sorry|instead)[, ]+)?(?:remove|delete|disable|cancel)\b", prefix, flags=re.I):
                action = "remove"
            elif re.search(r"\b(?:also|another|additional)\b", prefix, flags=re.I):
                action = "append"
            else:
                action = "replace"
            instructions.append((action, part))
        elif re.search(r"\b(?:no|without|remove|delete|disable|cancel)\s+(?:(?:the|any|all)\s+)?reminders?\b|\b(?:don't|do not) remind me\b", control, flags=re.I):
            instructions.append(("clear", part))
        elif re.search(r"\b(?:remind me|(?:set|add|change|update) (?:a |the |another |additional )?reminder)\b", control, flags=re.I) and re.search(r"\bbefore\b", control, flags=re.I):
            action = "append_model" if re.search(r"\b(?:also|another|additional)\b", re.split("before", control, maxsplit=1, flags=re.I)[0], flags=re.I) else "replace_model"
            if re.match(r"^\s*(?:(?:actually|sorry|instead)[, ]+)?(?:remove|delete|disable|cancel)\b", part, flags=re.I):
                action = "remove_model"
            instructions.append((action, part))
    if not instructions:
        return None
    reset = max((i for i, (action, _) in enumerate(instructions) if action in {"replace", "clear", "replace_model"}), default=0)
    return instructions[reset:]


def _reminders_from_text(text: str) -> list[ReminderSpec]:
    reminders = []
    for action, part in _reminder_instructions(text) or []:
        if action in {"clear", "replace_model"}:
            reminders = []
            continue
        if action.endswith("_model"):
            continue
        parsed = []
        matches = list(re.finditer(REMINDER_LEAD_PATTERN, _instruction_text(part), flags=re.I))
        for index, match in enumerate(matches):
            amount = match[1].lower()
            words = {"one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "half": .5, "half a": .5, "half an": .5, "one and a half": 1.5, "two and a half": 2.5, "three and a half": 3.5}
            amount = words[amount] if amount in words else float(amount)
            unit = match[2].lower()
            minutes = amount * (1440 if unit.startswith("d") else 60 if unit.startswith("h") else 1)
            if not float(minutes).is_integer() or not 1 <= minutes <= 10080:
                raise ValueError("Reminder lead times must be whole minutes between 1 minute and 7 days.")
            remainder = part[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(part)]
            message_match = re.match(r"\s*(?:to\s+|:\s*)([^,;.]+)", remainder, flags=re.I)
            message = re.sub(r"\s+(?:and|also)\s*$", "", message_match[1].strip(), flags=re.I) if message_match else None
            parsed.append(ReminderSpec(minutes_before=int(minutes), message=message))
        if action == "remove":
            removed = {reminder.minutes_before for reminder in parsed}
            reminders = [reminder for reminder in reminders if reminder.minutes_before not in removed]
        else:
            if action == "replace":
                reminders = []
            for reminder in parsed:
                if not any((item.minutes_before, item.message) == (reminder.minutes_before, reminder.message) for item in reminders):
                    reminders.append(reminder)
    return reminders


def _simple_weekday_reminder(text: str, now: datetime) -> ParsedStandaloneReminder | None:
    match = re.fullmatch(
        r"remind\s+me\s+to\s+(?P<message>.+?)\s+"
        r"(?P<schedule>(?:(?:on|this|next)\s+)?"
        r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
        r"\s+(?:at\s+)?(?P<hour>\d{1,2})(?:[:.](?P<minute>\d{2}))?\s*(?:am|pm))\s*[.!]?",
        text.strip(), flags=re.IGNORECASE,
    )
    if (not match or not 1 <= int(match['hour']) <= 12
            or int(match['minute'] or 0) > 59 or _relative_weekday_date(text, now) is None
            or not 1 <= len(match['message']) <= 1000):
        return None
    reminder = ParsedStandaloneReminder(message=match['message'], due_at=now, confidence="high")
    _apply_standalone_clock_from_text(reminder, f"remind me {match['schedule']} to {match['message']}", now)
    return reminder


def _apply_standalone_clock_from_text(reminder, text: str, now: datetime) -> None:
    """Resolve simple local clocks without using dates inside the reminder message."""
    command = re.match(
        r"^(?:(?:set|add)(?:\s+me)?\s+(?:a\s+)?reminder|remind\s+me)\b(?P<schedule>.*?)\bto\b",
        text.strip(), flags=re.IGNORECASE,
    )
    if not command:
        return
    schedule = command.group("schedule")
    if not schedule.strip():
        # Message-first wording: only a simple trailing weekday/clock is a schedule.
        suffix = re.search(
            r"\b(?:(?:on|this|next)\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
            r"\s+(?:at\s+)?\d{1,2}(?:[:.]\d{2})?\s*(?:am|pm)\s*[.!]?\s*$",
            text[command.end():], flags=re.IGNORECASE,
        )
        if not suffix or _relative_weekday_date(text[command.end():], now) is None:
            return
        schedule = suffix.group()
    weekday = _relative_weekday_date(schedule, now)
    clock = re.search(r"\b(?:at\s+)?(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)\b", schedule, flags=re.IGNORECASE)
    if not clock or (weekday is None and re.search(
        r"\b(?:on|"
        r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
        schedule, flags=re.IGNORECASE,
    )):
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
    if weekday is not None:
        reminder.due_at = due_at.replace(year=weekday.year, month=weekday.month, day=weekday.day)
        return
    relative_day = re.search(r"\b(today|tonight|tomorrow|tmr)\b", schedule, flags=re.IGNORECASE)
    if relative_day:
        if relative_day.group(1).lower() in {"tomorrow", "tmr"}:
            due_at += timedelta(days=1)
    elif due_at <= now:
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
    """Repair only an unqualified weekly rule from the latest scheduling answer."""
    instruction = _instruction_text(_latest_detail_text(text, RECURRENCE_HINT_PATTERN + "|" + DATE_HINT_PATTERN, remove_ranges=True))
    if re.search(r"\b(?:(?:just|only)\s+once|one[- ]off)\b|\b(?:no|not|stop|remove|disable)\s+(?:repeat(?:s|ing)?|recurrence)\b|\b(?:don't|do not) repeat\b", instruction, flags=re.I):
        event.recurrence = None
        return
    match = re.search(rf"\bevery\s+({WEEKDAY_NAMES})\b", instruction, flags=re.I)
    if not match or len(re.findall(rf"\b(?:{WEEKDAY_NAMES})\b", instruction, flags=re.I)) != 1:
        return
    if re.search(r"\b(?:until|through|ending|count|interval|other)\b|\bfor\s+(?:\d+|one|two|three)\s+(?:weeks?|months?|times?|occurrences?)", instruction, flags=re.I):
        return
    weekdays = {"mon": (0, "MO"), "tue": (1, "TU"), "wed": (2, "WE"), "thu": (3, "TH"), "fri": (4, "FR"), "sat": (5, "SA"), "sun": (6, "SU")}
    target_day, rrule_day = weekdays[match[1][:3].lower()]
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
