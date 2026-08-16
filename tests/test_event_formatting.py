from datetime import datetime

from app.main import _format_event_listing, _format_event_range, _reminder_minutes_from_text
from app.models import CalendarEvent


def test_event_range_shows_one_date_for_same_day_event():
    event = CalendarEvent(
        event_id="1", title="IPPT",
        start=datetime.fromisoformat("2026-08-15T17:00:00+08:00"),
        end=datetime.fromisoformat("2026-08-15T18:30:00+08:00"),
    )
    assert _format_event_range(event) == "Sat 15 Aug · 5:00 PM–6:30 PM"


def test_event_range_shows_both_dates_when_crossing_midnight():
    event = CalendarEvent(
        event_id="1", title="Flight",
        start=datetime.fromisoformat("2026-08-15T23:00:00+08:00"),
        end=datetime.fromisoformat("2026-08-16T01:00:00+08:00"),
    )
    assert _format_event_range(event) == "Sat 15 Aug 11:00 PM → Sun 16 Aug 1:00 AM"


def test_event_listing_includes_location_when_provided():
    event = CalendarEvent(
        event_id="1", title="Dinner", location="La Pasta",
        start=datetime.fromisoformat("2026-08-15T19:00:00+08:00"),
        end=datetime.fromisoformat("2026-08-15T21:00:00+08:00"),
    )
    assert "📍 La Pasta" in _format_event_listing(event, index=1)


def test_reminder_minutes_supports_compact_minute_phrase():
    assert _reminder_minutes_from_text("remind me 20minutes before") == 20
