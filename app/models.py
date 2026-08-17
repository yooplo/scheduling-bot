from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class ParsedEvent(BaseModel):
    action: Literal["add"] = "add"
    title: str = Field(min_length=1, max_length=300)
    start: datetime
    end: datetime
    location: str | None = Field(default=None, max_length=500)
    confidence: Literal["high", "low"]
    reminder_minutes: int | None = Field(default=None, ge=1, le=10080)
    reminders: list["ReminderSpec"] = Field(default_factory=list)
    recurrence: str | None = None


class ParsedEdit(BaseModel):
    """A complete replacement representation of an existing event or series."""
    action: Literal["edit"] = "edit"
    title: str = Field(min_length=1, max_length=300)
    start: datetime
    end: datetime
    location: str | None = Field(default=None, max_length=500)
    confidence: Literal["high", "low"]
    recurrence: str | None = None


class ParsedReminder(BaseModel):
    reminder_minutes: int = Field(ge=1, le=10080)
    confidence: Literal["high", "low"]


class ReminderSpec(BaseModel):
    """Persistent Telegram reminder metadata stored privately on a calendar event."""

    reminder_id: str = Field(default_factory=lambda: uuid4().hex)
    minutes_before: int | None = Field(default=None, ge=1, le=10080)
    message: str | None = Field(default=None, max_length=1000)
    sent: bool = False


class ParsedStandaloneReminder(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    due_at: datetime
    confidence: Literal["high", "low"]


class CandidateEvent(BaseModel):
    event_id: str
    title: str
    start: datetime


class DeleteMatch(BaseModel):
    action: Literal["delete"] = "delete"
    matched_event_id: str | None = None
    matched_title: str | None = None
    ambiguous: bool
    candidates: list[CandidateEvent] = []


class CalendarEvent(BaseModel):
    event_id: str
    title: str
    start: datetime
    end: datetime | None = None
    location: str | None = None
    reminder_minutes: int | None = None
    reminder_sent: bool = False
    reminders: list[ReminderSpec] = Field(default_factory=list)
    is_standalone_reminder: bool = False
    recurring_event_id: str | None = None


class ScheduledReminder(BaseModel):
    event_id: str
    reminder: ReminderSpec
    due_at: datetime
    event_title: str | None = None
    standalone: bool = False
