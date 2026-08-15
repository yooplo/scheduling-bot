"""Environment-backed application settings."""
from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigurationError(RuntimeError):
    """Raised when required runtime configuration is missing or invalid."""


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value == "replace-me":
        raise ConfigurationError(f"Missing required environment variable: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_webhook_secret: str
    allowed_telegram_user_id: int
    groq_api_key: str
    groq_model: str
    google_client_id: str
    google_client_secret: str
    google_refresh_token: str
    google_calendar_id: str
    user_timezone: str
    scheduler_secret: str
    daily_agenda_hour: int

    @property
    def timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.user_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ConfigurationError(f"Invalid USER_TIMEZONE: {self.user_timezone}") from exc


def get_settings() -> Settings:
    try:
        allowed_user_id = int(_required("ALLOWED_TELEGRAM_USER_ID"))
    except ValueError as exc:
        raise ConfigurationError("ALLOWED_TELEGRAM_USER_ID must be numeric") from exc
    try:
        daily_agenda_hour = int(os.getenv("DAILY_AGENDA_HOUR", "8"))
    except ValueError as exc:
        raise ConfigurationError("DAILY_AGENDA_HOUR must be 0-23") from exc
    if not 0 <= daily_agenda_hour <= 23:
        raise ConfigurationError("DAILY_AGENDA_HOUR must be 0-23")
    return Settings(
        telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
        telegram_webhook_secret=_required("TELEGRAM_WEBHOOK_SECRET"),
        allowed_telegram_user_id=allowed_user_id,
        groq_api_key=_required("GROQ_API_KEY"),
        groq_model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
        google_client_id=_required("GOOGLE_CLIENT_ID"),
        google_client_secret=_required("GOOGLE_CLIENT_SECRET"),
        google_refresh_token=_required("GOOGLE_REFRESH_TOKEN"),
        google_calendar_id=os.getenv("GOOGLE_CALENDAR_ID", "primary"),
        user_timezone=os.getenv("USER_TIMEZONE", "Asia/Singapore"),
        scheduler_secret=_required("SCHEDULER_SECRET"),
        daily_agenda_hour=daily_agenda_hour,
    )
