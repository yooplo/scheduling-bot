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
class CalendarAccount:
    """One fixed Telegram user and the Google Calendar they are allowed to use."""

    telegram_user_id: int
    google_refresh_token: str
    google_calendar_id: str


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_webhook_secret: str
    groq_api_key: str
    groq_model: str
    google_client_id: str
    google_client_secret: str
    calendar_accounts: tuple[CalendarAccount, ...]
    user_timezone: str
    scheduler_secret: str
    daily_agenda_hour: int
    cron_job_api_key: str | None
    service_base_url: str | None

    @property
    def timezone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.user_timezone)
        except ZoneInfoNotFoundError as exc:
            raise ConfigurationError(f"Invalid USER_TIMEZONE: {self.user_timezone}") from exc

    def account_for(self, telegram_user_id: int) -> CalendarAccount | None:
        return next((account for account in self.calendar_accounts if account.telegram_user_id == telegram_user_id), None)


def _calendar_accounts() -> tuple[CalendarAccount, ...]:
    """Read one or two fixed account mappings from the deployment environment."""
    accounts: list[CalendarAccount] = []
    for index in (1, 2):
        user_name = f"TELEGRAM_USER_{index}_ID"
        refresh_name = f"GOOGLE_USER_{index}_REFRESH_TOKEN"
        calendar_name = f"GOOGLE_USER_{index}_CALENDAR_ID"
        user_id_value = os.getenv(user_name, "").strip()
        refresh_token = os.getenv(refresh_name, "").strip()
        if not user_id_value and not refresh_token:
            continue
        if not user_id_value or not refresh_token:
            raise ConfigurationError(f"{user_name} and {refresh_name} must both be set")
        try:
            user_id = int(user_id_value)
        except ValueError as exc:
            raise ConfigurationError(f"{user_name} must be numeric") from exc
        accounts.append(CalendarAccount(user_id, refresh_token, os.getenv(calendar_name, "primary").strip() or "primary"))
    # Preserve an existing single-user deployment until its environment values
    # are migrated to the numbered names.
    if not accounts:
        legacy_user_id = os.getenv("ALLOWED_TELEGRAM_USER_ID", "").strip()
        legacy_refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN", "").strip()
        if legacy_user_id and legacy_refresh_token:
            try:
                accounts.append(CalendarAccount(int(legacy_user_id), legacy_refresh_token, os.getenv("GOOGLE_CALENDAR_ID", "primary").strip() or "primary"))
            except ValueError as exc:
                raise ConfigurationError("ALLOWED_TELEGRAM_USER_ID must be numeric") from exc
    if not accounts:
        raise ConfigurationError("Set TELEGRAM_USER_1_ID and GOOGLE_USER_1_REFRESH_TOKEN")
    if len({account.telegram_user_id for account in accounts}) != len(accounts):
        raise ConfigurationError("Each configured Telegram user ID must be unique")
    return tuple(accounts)


def get_settings() -> Settings:
    try:
        daily_agenda_hour = int(os.getenv("DAILY_AGENDA_HOUR", "8"))
    except ValueError as exc:
        raise ConfigurationError("DAILY_AGENDA_HOUR must be 0-23") from exc
    if not 0 <= daily_agenda_hour <= 23:
        raise ConfigurationError("DAILY_AGENDA_HOUR must be 0-23")
    return Settings(
        telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
        telegram_webhook_secret=_required("TELEGRAM_WEBHOOK_SECRET"),
        groq_api_key=_required("GROQ_API_KEY"),
        groq_model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
        google_client_id=_required("GOOGLE_CLIENT_ID"),
        google_client_secret=_required("GOOGLE_CLIENT_SECRET"),
        calendar_accounts=_calendar_accounts(),
        user_timezone=os.getenv("USER_TIMEZONE", "Asia/Singapore"),
        scheduler_secret=_required("SCHEDULER_SECRET"),
        daily_agenda_hour=daily_agenda_hour,
        cron_job_api_key=os.getenv("CRON_JOB_API_KEY", "").strip() or None,
        service_base_url=os.getenv("SERVICE_BASE_URL", "").strip().rstrip("/") or None,
    )
