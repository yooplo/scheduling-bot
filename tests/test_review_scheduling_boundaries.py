from datetime import timedelta

import pytest

import app.main as main
from app.models import ParsedEventDraft
from test_conversation_media import flow


@pytest.mark.asyncio
@pytest.mark.parametrize("correction", [
    "at Cafe on Sunday at 4pm",
    "actually at Cafe on Sunday at 4pm",
    'at "Cafe on Saturday" on Sunday at 4pm',
    "at Cafe at 4pm on Sunday",
    "at Cafe this Sunday at 4pm",
    "change the location to Cafe on Sunday at 4pm",
    'call it "Saturday Night" on Sunday at 4pm',
])
async def test_media_field_correction_preserves_new_date_and_clock_through_confirmation(flow, correction):
    send, media, _, _, calendar, parser = flow
    await media()
    original = main.media_previews[123].event
    sunday = original.start + timedelta(days=(6 - original.start.weekday()) % 7)
    parser.parse_event.return_value = ParsedEventDraft(**original.model_dump()).model_copy(update={
        "start": sunday.replace(hour=16), "end": sunday.replace(hour=18), "location": "Cafe",
    })
    await send(correction)
    suggested = main.media_previews[123].event
    assert suggested.start.weekday() == 6
    assert (suggested.start.hour, suggested.end.hour) == (16, 18)
    assert suggested.location == "Cafe"
    calendar.create_event.assert_not_called()
    await send("add event")
    saved = calendar.create_event.call_args.args[0]
    assert saved.start == suggested.start and saved.end == suggested.end


@pytest.mark.asyncio
@pytest.mark.parametrize("correction", [
    "at Cafe on 13 October 2030 at 4pm", "at Cafe tomorrow at 4pm",
    "at Cafe on 2030-10-13 at 4pm", "at Cafe on Sunday at noon",
])
async def test_media_location_correction_retains_other_date_forms_and_word_clocks(flow, correction):
    send, media, _, _, _, parser = flow
    await media()
    original = main.media_previews[123].event
    clock = 12 if "noon" in correction else 16
    changed = original.start.replace(day=13, hour=clock)
    parser.parse_event.return_value = ParsedEventDraft(**original.model_dump()).model_copy(update={
        "start": changed, "end": changed + timedelta(hours=2), "location": "Cafe",
    })
    await send(correction)
    result = main.media_previews[123].event
    assert result.start.hour == clock
    if "Sunday" in correction:
        assert result.start.weekday() == 6
    else:
        assert result.start == changed


@pytest.mark.asyncio
@pytest.mark.parametrize("correction", [
    'at "Cafe on Monday"', 'at "Cafe at Noon"', "at Clinic Monday",
    'call it "Friday at 4pm"', 'set the location to "Cafe on Sunday at 4pm"',
])
async def test_literal_field_names_do_not_override_schedule(flow, correction):
    send, media, _, _, _, parser = flow
    await media()
    original = main.media_previews[123].event
    parser.parse_event.return_value = ParsedEventDraft(**original.model_dump())
    await send(correction)
    result = main.media_previews[123].event
    assert (result.start, result.end) == (original.start, original.end)


@pytest.mark.asyncio
@pytest.mark.parametrize("expression,minutes", [
    ("twenty one minutes", 21), ("twenty-two minutes", 22),
    ("twenty‑three minutes", 23), ("twenty–three minutes", 23),
    ("one hundred and one minutes", 101), ("one hundred twenty-one minutes", 121),
    ("four and a half hours", 270), ("1,021 minutes", 1021),
    ("1/2 hour", 30), ("one point two hours", 72),
])
async def test_complete_unsupported_number_uses_validated_parser_value(flow, expression, minutes):
    send, _, _, _, calendar, parser = flow
    parser.parse_event.return_value = parser.parse_event.return_value.model_copy(update={"reminder_minutes": minutes})
    await send(f"add Dental on 12 October 2030 4–5pm, remind me {expression} before")
    calendar.create_event.assert_called_once()
    saved = calendar.create_event.call_args.args[0]
    assert [r.minutes_before for r in saved.reminders] == [minutes]


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix,expected", [("actually", [21]), ("also", [60, 21])])
async def test_compound_number_media_correction_obeys_replace_and_append(flow, prefix, expected):
    send, media, _, _, calendar, parser = flow
    await media()
    await send("remind me 60 minutes before")
    parser.parse_event.return_value = parser.parse_event.return_value.model_copy(update={"reminder_minutes": 21})
    await send(f"{prefix} remind me twenty one minutes before")
    assert [r.minutes_before for r in main.media_previews[123].event.reminders] == expected
    calendar.create_event.assert_not_called()
    await send("add event")
    assert [r.minutes_before for r in calendar.create_event.call_args.args[0].reminders] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("leads", [
    "60 minutes before and twenty one minutes before",
    "twenty-two minutes before and 15 minutes before",
    "twenty one minutes before and twenty two minutes before",
])
async def test_mixed_unsupported_leads_ask_instead_of_silently_dropping_reminders(flow, leads):
    send, _, _, _, calendar, parser = flow
    parser.parse_event.return_value = parser.parse_event.return_value.model_copy(update={"reminder_minutes": 21})
    reply = await send(f"add Dental on 12 October 2030 4–5pm, remind me {leads}")
    assert "all desired reminders in minutes" in reply
    calendar.create_event.assert_not_called()
    await send("actually remind me 60 minutes before and 21 minutes before")
    assert [r.minutes_before for r in calendar.create_event.call_args.args[0].reminders] == [60, 21]


@pytest.mark.asyncio
async def test_unsupported_number_without_parser_lead_retains_draft_for_retry(flow):
    send, _, _, _, calendar, _ = flow
    reply = await send("add Dental on 12 October 2030 4–5pm, remind me twenty one minutes before")
    assert "lead time in minutes" in reply
    calendar.create_event.assert_not_called()
    await send("actually remind me 21 minutes before")
    assert [r.minutes_before for r in calendar.create_event.call_args.args[0].reminders] == [21]


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", ["-1", "0", "0.5"])
async def test_invalid_numeric_leads_cannot_become_positive_integer_suffixes(flow, amount):
    send, _, _, _, calendar, _ = flow
    reply = await send(f"add Dental on 12 October 2030 4–5pm, remind me {amount} minutes before")
    assert "whole minutes between" in reply
    calendar.create_event.assert_not_called()


@pytest.mark.asyncio
async def test_location_clock_without_second_at_keeps_clock_correction(flow):
    send, media, _, _, _, parser = flow
    await media()
    original = main.media_previews[123].event
    parser.parse_event.return_value = ParsedEventDraft(**original.model_dump()).model_copy(update={
        "start": original.start.replace(hour=16), "end": original.end.replace(hour=18), "location": "Cafe",
    })
    await send("at Cafe 4pm")
    result = main.media_previews[123].event
    assert result.start.date() == original.start.date()
    assert (result.start.hour, result.end.hour) == (16, 18)


@pytest.mark.asyncio
async def test_week_lead_does_not_disappear_when_combined_with_minutes(flow):
    send, _, _, _, calendar, _ = flow
    await send("add Dental on 12 October 2030 4–5pm, remind me one week before and 15 minutes before")
    assert [r.minutes_before for r in calendar.create_event.call_args.args[0].reminders] == [10080, 15]


@pytest.mark.asyncio
async def test_week_lead_above_supported_bound_asks_for_correction(flow):
    send, _, _, _, calendar, _ = flow
    reply = await send("add Dental on 12 October 2030 4–5pm, remind me two weeks before")
    assert "whole minutes between" in reply
    calendar.create_event.assert_not_called()


@pytest.mark.parametrize("expression", ["twenty one", "twenty-two", "one hundred and one", "1,021"])
def test_legacy_minute_helper_cannot_return_partial_number(expression):
    assert main._reminder_minutes_from_text(f"remind me {expression} minutes before") is None
