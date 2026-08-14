from __future__ import annotations

import json
from datetime import datetime, timedelta

from anthropic import Anthropic
from pydantic import ValidationError

from .models import CalendarEvent, DeleteMatch, ParsedEvent


class ParseError(RuntimeError):
    pass


class ClaudeParser:
    def __init__(self, api_key: str, model: str) -> None:
        self._client = Anthropic(api_key=api_key)
        self._model = model

    def parse_event(self, message: str, now: datetime, timezone_name: str) -> ParsedEvent:
        prompt = f"""Extract one calendar event from the user's message. Return JSON only.
Current datetime: {now.isoformat()}. User timezone: {timezone_name}.
Resolve relative dates against that datetime. All datetime values must include an offset.
If no end is given, set it to one hour after start. Use low confidence for unclear date or time.
Schema: {{\"action\":\"add\",\"title\":string,\"start\":ISO8601,\"end\":ISO8601,\"location\":string|null,\"confidence\":\"high\"|\"low\"}}
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

    def _request_model(self, prompt: str, model_type):
        last_error: Exception | None = None
        for suffix in ("", "\nYour last response was invalid. Output one valid JSON object only, with no code fence."):
            try:
                response = self._client.messages.create(
                    model=self._model, max_tokens=700, temperature=0,
                    messages=[{"role": "user", "content": prompt + suffix}],
                )
                text = "".join(block.text for block in response.content if block.type == "text")
                return model_type.model_validate(json.loads(text))
            except (json.JSONDecodeError, ValidationError, AttributeError) as exc:
                last_error = exc
        raise ParseError("Claude returned invalid structured data") from last_error

