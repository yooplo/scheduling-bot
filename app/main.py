from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime

from fastapi import FastAPI, Header, HTTPException, Request

from .calendar_client import CalendarClient
from .config import ConfigurationError, Settings, get_settings
from .models import CalendarEvent
from .parser import ClaudeParser, ParseError
from .telegram_client import TelegramClient, valid_webhook_secret

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = FastAPI(title="Telegram Calendar Bot")

DELETE_WORDS = ("delete", "cancel", "remove")
LIST_WORDS = ("list", "show", "what's on", "whats on", "upcoming", "calendar")
PENDING_TTL_SECONDS = 300


@dataclass
class PendingDelete:
    events: list[CalendarEvent]
    expires_at: float


pending_deletes: dict[int, PendingDelete] = {}
_settings: Settings | None = None
_telegram: TelegramClient | None = None
_calendar: CalendarClient | None = None
_parser: ClaudeParser | None = None


def dependencies() -> tuple[Settings, TelegramClient, CalendarClient, ClaudeParser]:
    global _settings, _telegram, _calendar, _parser
    if _settings is None:
        _settings = get_settings()
        _telegram = TelegramClient(_settings.telegram_bot_token)
        _calendar = CalendarClient(_settings)
        _parser = ClaudeParser(_settings.anthropic_api_key, _settings.anthropic_model)
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


async def handle_message(chat_id: int, text: str, settings: Settings, telegram: TelegramClient, calendar: CalendarClient, parser: ClaudeParser) -> None:
    pending = pending_deletes.get(chat_id)
    if pending and pending.expires_at > time.monotonic():
        selected = _selection(text, pending.events)
        if selected:
            await asyncio.to_thread(calendar.delete_event, selected.event_id)
            pending_deletes.pop(chat_id, None)
            await telegram.send_message(chat_id, f"✅ Deleted: {selected.title} — {_format_time(selected.start)}")
            return
        pending_deletes.pop(chat_id, None)
    lowered = text.lower()
    if any(word in lowered for word in DELETE_WORDS):
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
        choices = [e for e in events if e.event_id in {c.event_id for c in match.candidates}] or events[:5]
        pending_deletes[chat_id] = PendingDelete(choices, time.monotonic() + PENDING_TTL_SECONDS)
        lines = [f"{i}. {event.title} — {_format_time(event.start)}" for i, event in enumerate(choices, 1)]
        await telegram.send_message(chat_id, "Which event should I delete? Reply with a number:\n" + "\n".join(lines))
    elif any(word in lowered for word in LIST_WORDS):
        events = await asyncio.to_thread(calendar.list_events, 7)
        if not events:
            await telegram.send_message(chat_id, "No events in the next 7 days.")
        else:
            lines = [f"• {event.title} — {_format_time(event.start)}" for event in events]
            await telegram.send_message(chat_id, "Upcoming events:\n" + "\n".join(lines))
    else:
        now = datetime.now(settings.timezone)
        event = await asyncio.to_thread(parser.parse_event, text, now, settings.user_timezone)
        if event.confidence == "low":
            await telegram.send_message(chat_id, "I need a clearer date and time. For example: 'dentist tomorrow 2–3pm'.")
            return
        if event.start.tzinfo is None or event.end.tzinfo is None:
            await telegram.send_message(chat_id, "Please include a date and time with enough detail for me to schedule it.")
            return
        created = await asyncio.to_thread(calendar.create_event, event)
        await telegram.send_message(chat_id, f"✅ Added: {created.title} — {_format_time(created.start)}–{_format_time(created.end)}")


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
