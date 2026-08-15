from datetime import datetime

import pytest

from app.models import ParsedEvent
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
