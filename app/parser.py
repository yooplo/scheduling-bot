from __future__ import annotations

import json
from datetime import datetime

from groq import Groq
from pydantic import ValidationError

from .models import CalendarEvent, DeleteMatch, ParsedEdit, ParsedEvent, ParsedReminder


class ParseError(RuntimeError):
    pass


class GroqParser:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = Groq(api_key=api_key)
        self._model = model

    def parse_event(self, message: str, now: datetime, timezone_name: str) -> ParsedEvent:
        prompt = f"""Extract one calendar event from the user's message. Return JSON only.
Current datetime: {now.isoformat()}. User timezone: {timezone_name}.
Resolve relative dates against that datetime. All datetime values must include an offset.
If no end is given, set it to one hour after start. Extract reminder_minutes if the user says "remind me X minutes/hours before"; otherwise use null. For recurring requests, return an RFC5545 RRULE such as `RRULE:FREQ=WEEKLY;BYDAY=MO`; otherwise null. Use low confidence for unclear date or time.
Schema: {{\"action\":\"add\",\"title\":string,\"start\":ISO8601,\"end\":ISO8601,\"location\":string|null,\"confidence\":\"high\"|\"low\",\"reminder_minutes\":integer|null,\"recurrence\":string|null}}
User message: {message}"""
        return self._request_model(prompt, ParsedEvent)

    def match_event(self, message: str, candidates: list[CalendarEvent]) -> DeleteMatch:
        options = [{"event_id": e.event_id, "title": e.title, "start": e.start.isoformat()} for e in candidates]
        prompt = f"""Match the user's deletion request to one of the supplied calendar events. Return JSON only.
Never invent an event ID. If uncertain or more than one is plausible, set ambiguous true, matched_event_id null, and provide plausible candidates.
Schema: {{\"action\":\"delete\",\"matched_event_id\":string|null,\"matched_title\":string|null,\"ambiguous\":boolean,\"candidates\":[{{\"event_id\":string,\"title\":string,\"start\":ISO8601}}]}}
User request: {message}
Events: {json.dumps(options)}"""
        result = self._request_model(prompt, DeleteMatch)
        valid_ids = {event.event_id for event in candidates}
        if result.matched_event_id not in valid_ids:
            result.matched_event_id = None
            result.ambiguous = True
        result.candidates = [event for event in result.candidates if event.event_id in valid_ids]
        return result

    def parse_edit(self, message: str, existing: CalendarEvent, timezone_name: str) -> ParsedEdit:
        current = {
            "title": existing.title,
            "start": existing.start.isoformat(),
            "end": existing.end.isoformat() if existing.end else None,
            "location": existing.location,
        }
        prompt = f"""Apply the user's requested change to the existing calendar event. Return JSON only.
Timezone: {timezone_name}. Preserve every existing field that the user does not explicitly change.
If the user specifies only a new start time, preserve the original duration. All datetime values must include an offset.
Use low confidence if the requested change is ambiguous or incomplete.
Schema: {{\"action\":\"edit\",\"title\":string,\"start\":ISO8601,\"end\":ISO8601,\"location\":string|null,\"confidence\":\"high\"|\"low\"}}
Existing event: {json.dumps(current)}
User request: {message}"""
        return self._request_model(prompt, ParsedEdit)

    def parse_reminder(self, message: str) -> ParsedReminder:
        prompt = f"""Extract only the reminder lead time from the user's request. Return JSON only.
Convert days and hours to whole minutes (one day is 1440 minutes). Use low confidence if no clear lead time is given.
Schema: {{\"reminder_minutes\":integer,\"confidence\":\"high\"|\"low\"}}
User request: {message}"""
        return self._request_model(prompt, ParsedReminder)

    def parse_reminder_for_event(self, message: str, event: CalendarEvent, timezone_name: str) -> ParsedReminder:
        prompt = f"""Extract the requested Telegram reminder lead time for this existing event. Return JSON only.
Event start: {event.start.isoformat()}. User timezone: {timezone_name}.
If the user gives a clock time (for example, "change the reminder to 8:50pm"), calculate the number of minutes before the event start. Convert days/hours to minutes. Use low confidence if the resulting reminder is not before the event.
Schema: {{\"reminder_minutes\":integer,\"confidence\":\"high\"|\"low\"}}
User request: {message}"""
        return self._request_model(prompt, ParsedReminder)

    def _request_model(self, prompt: str, model_type):
        last_error: Exception | None = None
        for suffix in ("", "\nYour last response was invalid. Output one valid JSON object only, with no code fence."):
            try:
                response = self._client.chat.completions.create(
                    model=self._model, max_tokens=700, temperature=0,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": "Return valid JSON only. Do not use Markdown."},
                        {"role": "user", "content": prompt + suffix},
                    ],
                )
                text = response.choices[0].message.content
                if not text:
                    raise ValueError("Groq returned an empty response")
                return model_type.model_validate(json.loads(text))
            except (json.JSONDecodeError, ValidationError, AttributeError, IndexError, ValueError) as exc:
                last_error = exc
        raise ParseError("Groq returned invalid structured data") from last_error
