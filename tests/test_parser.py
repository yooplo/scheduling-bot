from datetime import datetime

import pytest

from app.models import ParsedEvent
from app.parser import ClaudeParser


class FakeMessages:
    def create(self, **kwargs):
        class Block:
            type = "text"
            text = '{"action":"add","title":"Dentist","start":"2026-08-16T14:00:00+08:00","end":"2026-08-16T15:00:00+08:00","location":null,"confidence":"high"}'
        class Response:
            content = [Block()]
        return Response()


def test_parse_event_returns_valid_event(monkeypatch):
    parser = ClaudeParser("test", "test")
    monkeypatch.setattr(parser._client, "messages", FakeMessages())
    event = parser.parse_event("dentist tomorrow 2pm", datetime(2026, 8, 15, 12), "Asia/Singapore")
    assert isinstance(event, ParsedEvent)
    assert event.title == "Dentist"
    assert event.start.hour == 14

