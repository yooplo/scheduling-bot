import pytest

import app.main as main


@pytest.fixture(autouse=True)
def isolate_conversation_context(monkeypatch):
    for name in ("recent_events", "recent_event_lists", "media_requests", "media_previews"):
        monkeypatch.setattr(main, name, {})
