from datetime import datetime

import pytest

from app.models import ParsedEvent, ParsedEventDraft
from app.parser import GroqParser


class FakeCompletions:
    def create(self, **kwargs):
        class Message:
            content = '{"action":"add","title":"Dentist","start":"2026-08-16T14:00:00+08:00","end":"2026-08-16T15:00:00+08:00","location":null,"confidence":"high"}'
        class Choice:
            message = Message()
        class Response:
            choices = [Choice()]
        return Response()


class FakeClient:
    class Chat:
        completions = FakeCompletions()
    chat = Chat()


def test_parse_event_returns_valid_event(monkeypatch):
    parser = GroqParser("test", "test")
    monkeypatch.setattr(parser, "_client", FakeClient())
    event = parser.parse_event("dentist tomorrow 2pm", datetime(2026, 8, 15, 12), "Asia/Singapore")
    assert isinstance(event, ParsedEvent)
    assert event.title == "Dentist"
    assert event.start.hour == 14


def test_parse_event_accepts_missing_details_without_fabricated_dates(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock

    parser = GroqParser("test", "test")
    create = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content='{"title":"Dentist","start":null,"end":null,"confidence":"low","missing_fields":["time","duration"]}',
    ))]))
    monkeypatch.setattr(parser, "_client", SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    event = parser.parse_event("Dentist tomorrow", datetime(2026, 9, 10, 12), "Asia/Singapore")
    assert isinstance(event, ParsedEventDraft)
    assert event.start is None and event.end is None
    assert event.missing_fields == ["time", "duration"]
    prompt = create.call_args.kwargs["messages"][1]["content"]
    assert "Do not assume a date, time, or one-hour duration" in prompt
