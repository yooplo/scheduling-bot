from datetime import datetime
from zoneinfo import ZoneInfo

from app.main import LIST_WORDS, _apply_recurrence_from_text, _date_from_text, _format_event_listing, _format_event_range, _format_update_confirmation, _is_series_edit, _reminder_message_from_text, _reminder_minutes_from_text, _reminders_from_text, _unauthorised_message, _upcoming_weekday_from_text, _welcome_message
from app.models import ParsedEvent
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


def test_weekly_recurrence_moves_event_to_named_weekday():
    event = ParsedEvent(title="Gym", start="2026-08-21T20:00:00+08:00", end="2026-08-21T21:00:00+08:00", confidence="high")
    _apply_recurrence_from_text(event, "gym every monday at 8pm")
    assert event.start.weekday() == 0
    assert event.recurrence == "RRULE:FREQ=WEEKLY;BYDAY=MO"


def test_update_confirmation_includes_location():
    event = CalendarEvent(event_id="1", title="Carousel", location="Amelia's house", start=datetime.fromisoformat("2026-08-17T14:00:00+08:00"), end=datetime.fromisoformat("2026-08-17T15:00:00+08:00"))
    assert "📍 Amelia's house" in _format_update_confirmation(event)


def test_explicit_date_is_extracted_from_free_time_query():
    class Settings:
        timezone = ZoneInfo("Asia/Singapore")
    # A real Settings instance is unnecessary: the parser only needs its timezone name.
    assert _date_from_text("when am i free on 19 august", Settings()).day == 19
    assert _date_from_text("what are my plans on 19 aug", Settings()).day == 19


def test_welcome_message_uses_telegram_first_name():
    assert _welcome_message("Justin").startswith("Hi Justin! 👋")


def test_singular_plan_is_a_list_intent():
    assert "plan" in LIST_WORDS


def test_explicit_series_wording_edits_recurring_series_only():
    recurring = CalendarEvent(
        event_id="occurrence", recurring_event_id="series", title="Gym",
        start=datetime.fromisoformat("2026-08-17T20:00:00+08:00"),
        end=datetime.fromisoformat("2026-08-17T21:00:00+08:00"),
    )
    assert _is_series_edit("change the weekly gym series to Tuesday at 7pm", recurring)
    assert not _is_series_edit("move gym to 7pm", recurring)


def test_reminder_text_supports_multiple_custom_reminders():
    reminders = _reminders_from_text("dentist tomorrow, remind me 1 hour before to bring ID and 15 minutes before")
    assert [reminder.minutes_before for reminder in reminders] == [60, 15]
    assert _reminder_message_from_text("remind me 1 hour before to bring ID") == "bring ID"


def test_unauthorised_message_includes_access_contact():
    assert "@juzteeeen" in _unauthorised_message()


def test_weekday_query_resolves_to_the_next_matching_day():
    class Settings:
        timezone = ZoneInfo("Asia/Singapore")

    target = _upcoming_weekday_from_text("what are my plans on monday", Settings())
    assert target is not None
    assert target.weekday() == 0
    assert target >= datetime.now(ZoneInfo("Asia/Singapore")).date()
