from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ParsedEvent(BaseModel):
    action: Literal["add"] = "add"
    title: str = Field(min_length=1, max_length=300)
    start: datetime
    end: datetime
    location: str | None = Field(default=None, max_length=500)
    confidence: Literal["high", "low"]
    reminder_minutes: int | None = Field(default=None, ge=1, le=10080)


class ParsedEdit(BaseModel):
    """A complete replacement representation of an existing event."""
    action: Literal["edit"] = "edit"
    title: str = Field(min_length=1, max_length=300)
    start: datetime
    end: datetime
    location: str | None = Field(default=None, max_length=500)
    confidence: Literal["high", "low"]


class ParsedReminder(BaseModel):
    reminder_minutes: int = Field(ge=1, le=10080)
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
