from datetime import datetime
from zoneinfo import ZoneInfo

from app.main import LIST_WORDS, _apply_all_day_from_text, _apply_recurrence_from_text, _calendar_colour_emoji, _date_from_text, _format_calendar_list, _format_event_listing, _format_event_range, _format_reminder_listing, _format_update_confirmation, _is_calendar_list_request, _is_explicit_add_request, _is_series_edit, _is_standalone_reminder_request, _reminder_message_from_text, _reminder_minutes_from_text, _reminders_from_text, _unauthorised_message, _upcoming_weekday_from_text, _welcome_message
from app.models import ParsedEvent
from app.models import CalendarEvent, CalendarInfo, ReminderSpec, ScheduledReminder


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


def test_explicit_all_day_event_uses_native_day_boundaries_and_label():
    class Settings:
        timezone = ZoneInfo("Asia/Singapore")

    event = ParsedEvent(
        title="SPD offer email",
        start="2026-08-19T00:00:00+08:00",
        end="2026-08-19T23:59:00+08:00",
        confidence="high",
    )
    _apply_all_day_from_text(event, "SPD offer email all day on 19 Aug", Settings())

    assert event.all_day is True
    assert event.start.isoformat() == "2026-08-19T00:00:00+08:00"
    assert event.end.isoformat() == "2026-08-20T00:00:00+08:00"
    calendar_event = CalendarEvent(event_id="all-day", title=event.title, start=event.start, end=event.end, all_day=True)
    assert _format_event_range(calendar_event) == "Wed 19 Aug · All day"


def test_created_all_day_confirmation_range_does_not_show_midnight_times():
    event = CalendarEvent(
        event_id="all-day",
        title="SPD offer",
        start=datetime.fromisoformat("2026-08-21T00:00:00+08:00"),
        end=datetime.fromisoformat("2026-08-22T00:00:00+08:00"),
        all_day=True,
    )

    output = _format_event_range(event)
    assert output == "Fri 21 Aug · All day"
    assert "12:00 AM" not in output


def test_event_listing_includes_location_when_provided():
    event = CalendarEvent(
        event_id="1", title="Dinner", location="La Pasta",
        start=datetime.fromisoformat("2026-08-15T19:00:00+08:00"),
        end=datetime.fromisoformat("2026-08-15T21:00:00+08:00"),
    )
    assert "📍 La Pasta" in _format_event_listing(event, index=1)


def test_event_listing_includes_calendar_name_when_available():
    event = CalendarEvent(
        event_id="1", title="Meeting", calendar_name="Work",
        start=datetime.fromisoformat("2026-08-15T19:00:00+08:00"),
        end=datetime.fromisoformat("2026-08-15T21:00:00+08:00"),
    )
    assert "🗓️ Work" in _format_event_listing(event, index=1)


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


def test_set_me_a_reminder_with_a_due_time_is_standalone():
    assert _is_standalone_reminder_request(
        "set me a reminder for tonight at 11.50pm to book the bedok court"
    )


def test_relative_duration_reminder_is_standalone():
    assert _is_standalone_reminder_request("set a reminder in 15minutes to shower")


def test_lead_time_reminder_stays_linked_to_an_existing_event():
    assert not _is_standalone_reminder_request("set a reminder one day before IPPT")
    assert not _is_standalone_reminder_request("remind me 15 minutes before Dental")


def test_reminder_listing_distinguishes_both_reminder_types():
    due_at = datetime.fromisoformat("2026-08-20T13:45:00+08:00")
    independent = ScheduledReminder(
        event_id="standalone",
        reminder=ReminderSpec(message="Shower"),
        due_at=due_at,
        standalone=True,
    )
    linked = ScheduledReminder(
        event_id="dental",
        reminder=ReminderSpec(minutes_before=15),
        due_at=due_at,
        event_title="Dental at Bedok",
    )

    assert "Independent reminder" in _format_reminder_listing(independent, 1)
    linked_text = _format_reminder_listing(linked, 2)
    assert "Event reminder" in linked_text
    assert "Dental at Bedok" in linked_text


def test_unauthorised_message_includes_access_contact():
    assert "@juzteeeen" in _unauthorised_message()


def test_weekday_query_resolves_to_the_next_matching_day():
    class Settings:
        timezone = ZoneInfo("Asia/Singapore")

    target = _upcoming_weekday_from_text("what are my plans on monday", Settings())
    assert target is not None
    assert target.weekday() == 0
    assert target >= datetime.now(ZoneInfo("Asia/Singapore")).date()


def test_calendar_list_includes_calendar_colours():
    output = _format_calendar_list([CalendarInfo(calendar_id="work", name="Work", background_color="#a4bdfc")])
    assert "Work" in output and "🟦" in output


def test_calendar_colour_emoji_uses_nearest_supported_colour():
    assert _calendar_colour_emoji("#7bd148") == "🟩"


def test_add_request_wins_over_calendar_list_keyword():
    assert _is_explicit_add_request("add ifg training in pickleball calendar at 7pm")


def test_calendar_list_aliases_are_recognised():
    for request in ("calendar", "calendars", "calendar list", "calendarslist", "/calendarslist"):
        assert _is_calendar_list_request(request)


def test_named_calendar_in_event_is_not_an_event_list_request():
    request = "PE1101A Tutorial 02 on 31 Aug 2026 from 10am to 12pm at SDE1-SR1 in School calendar"
    assert not _is_calendar_list_request(request)
    assert not any(word in request.lower() for word in LIST_WORDS)
