# Spec: Telegram → Google Calendar Bot

## 1. Overview

A private Telegram bot that lets one or two preconfigured users manage their own Google Calendar account by sending natural-language messages. It creates, lists, edits, and deletes events; handles weekly recurrence and multiple calendars; reports availability; and delivers independent or event-linked Telegram reminders.

Groq converts free text into validated structured data. Google Calendar stores events and event-linked reminder metadata. Independent reminders are expiring jobs managed through the cron-job.org REST API, so they create no Calendar entry. The service runs as a FastAPI webhook application on Render.

## 2. Goals / Non-Goals

**Goals**
- Add timed or native all-day one-off and weekly recurring events, with optional locations, target calendars, and multiple custom reminders
- List upcoming events, daily schedules, reminders, and free time
- Delete/cancel or edit events and reminders via free-text reference (fuzzy match against
  upcoming events)
- Support independent reminders and reminders linked to existing events
- Isolate one or two fixed Telegram users, each with a separate Google authorization
- Confirm every action back to the user in chat
- Run reliably on a free-tier host with a webhook (no polling)

**Non-goals (v1)**
- Open registration, dynamic account linking, or more than two configured users
- Voice message input
- Attendee invitations, arbitrary recurrence rules, and undo

## 3. Architecture

### 3.1 Technology Stack

| Layer | Technology | Purpose |
|---|---|---|
| Bot transport | Telegram Bot API webhooks | Receive messages and send replies |
| Web service | Python 3.12, FastAPI, Uvicorn | HTTPS webhook and scheduler endpoints |
| Natural-language parsing | Groq API (`openai/gpt-oss-20b`) | Extract event, edit, delete, and reminder data |
| Calendar | Google Calendar API v3, OAuth 2.0 | Multi-calendar event CRUD, calendar colours, reminder metadata, and availability data |
| Hosting | Render free web service | Public HTTPS runtime and GitHub deployment |
| External scheduler | cron-job.org | Event-reminder polling, daily agenda delivery, and one-time independent reminder jobs |
| Testing | pytest | Parser, Calendar conversion, and formatting tests |

### 3.2 Scheduled Notifications

- `POST /scheduled/reminders` is called every minute by a fixed cron-job.org job. It validates `SCHEDULER_SECRET`, finds due event-linked reminders for every configured user, sends Telegram messages, and marks them sent in private event metadata.
- Each independent reminder creates an expiring cron-job.org job through `CRON_JOB_API_KEY`. At its requested minute the job sends a secured `POST /scheduled/standalone-reminder` callback containing its job ID, Telegram user ID, and message. The endpoint sends Telegram and deletes the completed job. No Google Calendar event is created.
- cron-job.org calls `POST /scheduled/daily-agenda` once daily at `DAILY_AGENDA_HOUR` in `USER_TIMEZONE`. The endpoint sends each configured user the upcoming events returned by the bot's one-day (24-hour) window.
- Scheduler requests use an `Authorization: Bearer <SCHEDULER_SECRET>` header; direct unauthorised calls are rejected.
- Users can add reminders while creating an event, request one for an existing event, or schedule an independent reminder. Existing-event reminders use the same event matching and disambiguation flow as edits and deletes. The `reminders` command combines both types chronologically and labels them as independent or event-linked.
- **Deployment status:** cron-job.org reminder and daily-agenda jobs are configured and verified. Successful scheduled calls return `204 No Content`.
```
Telegram (user) → Telegram webhook → Web app (FastAPI) → Router
                                                              ├─ Add flow    → LLM (parse) → Google Calendar API (insert)
                                                              ├─ List flow   → Google Calendar API (list)
                                                              ├─ Delete flow → Google Calendar API (list) → LLM/fuzzy match → Google Calendar API (delete)
                                                              ├─ Edit flow   → Google Calendar API (list) → LLM (parse change) → Google Calendar API (patch)
                                                              └─ Reminder    → LLM (parse/match) → private Calendar metadata
                                                              ↓
                                                        Reply to Telegram

cron-job.org → /scheduled/reminders → due metadata → Telegram notification
             → /scheduled/daily-agenda → today's events → Telegram agenda
             → /scheduled/standalone-reminder → Telegram notification → delete one-time job
```

- **Transport**: Telegram Bot API, webhook mode (not long polling)
- **Web framework**: FastAPI (async, plays well with webhook handlers)
- **NLP parsing**: Groq API, prompted to return structured JSON
- **Calendar**: Google Calendar API v3, OAuth 2.0 (installed-app flow, one-time),
  with a separate refresh token stored as a secret/env var for each fixed user
- **Hosting**: Render free web service, single process

## 4. File / Project Structure

```
calendar-bot/
├── app/
│   ├── main.py              # FastAPI app, /webhook route, dispatch logic
│   ├── telegram_client.py   # Send messages, verify webhook secret
│   ├── parser.py            # Groq parsing for events, edits, matching, and reminders
│   ├── calendar_client.py   # Multi-calendar CRUD, recurrence, and reminder metadata
│   ├── config.py            # Loads env vars, validates required secrets
│   └── models.py            # Pydantic models for events, matching, and reminders
├── auth/
│   └── get_refresh_token.py # One-time local script for Google OAuth
├── tests/
│   ├── test_parser.py
│   ├── test_calendar_client.py
│   ├── test_config.py
│   └── test_event_formatting.py
├── requirements.txt
├── .env.example
├── render.yaml               # Render Blueprint and environment variables
└── README.md
```

## 5. External Services & Credentials

| Service | Purpose | Credential |
|---|---|---|
| Telegram Bot API | Receive/send messages | `TELEGRAM_BOT_TOKEN` |
| Telegram webhook | Verify incoming requests are genuine | `TELEGRAM_WEBHOOK_SECRET` |
| Groq API | Parse natural language into structured event data | `GROQ_API_KEY` |
| Google Calendar API | Create/list/delete events | Shared `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`; per-user `GOOGLE_USER_1_REFRESH_TOKEN` and optional `GOOGLE_USER_2_REFRESH_TOKEN` |
| cron-job.org | Invoke reminders and daily agenda; persist independent reminders | Callback bearer token `SCHEDULER_SECRET`; REST API key `CRON_JOB_API_KEY`; public `SERVICE_BASE_URL` |

All secrets are stored as environment variables on the host. `.env.example`
documents every required variable with a placeholder value and a comment.

## 6. Data Contracts

### 6.1 ParsedEvent (Groq → app)

Groq is prompted to return **only** this JSON shape, no prose:

```json
{
  "action": "add",
  "title": "string",
  "start": "ISO 8601 datetime with timezone",
  "end": "ISO 8601 datetime with timezone",
  "location": "string or null",
  "confidence": "high | low",
  "reminder_minutes": "integer or null",
  "recurrence": "RFC5545 RRULE string or null",
  "calendar_name": "string or null",
  "all_day": "boolean"
}
```

- `confidence: low` triggers a clarifying reply instead of creating the
  event outright (e.g. ambiguous date, missing time).
- If `end` cannot be inferred, default to `start + 1 hour`.
- Explicit `all day` wording sets `all_day: true`. Google Calendar writes use `start.date` and an exclusive `end.date`, never `dateTime` values such as 00:00–23:59.
- Prompt must include the current datetime and the user's timezone so
  relative phrases ("tomorrow", "next Tuesday") resolve correctly.

### 6.2 DeleteMatch (Groq/fuzzy match → app)

For delete requests, the app first fetches upcoming events (e.g. next 30
days) from Google Calendar, then asks Groq to pick the best match:

```json
{
  "action": "delete",
  "matched_event_id": "string or null",
  "matched_title": "string or null",
  "ambiguous": true,
  "candidates": [
    {"event_id": "string", "title": "string", "start": "ISO 8601"}
  ]
}
```

- If `ambiguous: true` or no confident match, the bot replies with a
  numbered list of candidates and waits for the user to pick one
  (simple in-memory pending-action state keyed by chat ID, with a
  short TTL).

### 6.3 Telegram outgoing messages

All replies are plain text, no Markdown parsing issues — escape any
special characters if using Telegram's MarkdownV2 mode, or default to
plain text mode to avoid formatting bugs.

### 6.4 Reminder contracts

- `ReminderSpec` contains a stable reminder ID, an optional number of minutes before an event, optional custom Telegram text, and a sent flag. Multiple specs are serialized into the event's private `telegram_reminders` extended property.
- `ParsedStandaloneReminder` contains the notification message, an absolute timezone-aware `due_at`, and parse confidence.
- `ScheduledReminder` normalizes both reminder types for listing and delivery. It contains the due time, source calendar/event IDs, optional event title, and a `standalone` flag.
- Independent `ScheduledReminder` values are reconstructed from enabled cron-job.org jobs scoped by Telegram user ID. Event-linked reminders are due at `event.start - minutes_before`.

## 7. Command / Message Handling

No slash commands required for v1 — every message is treated as natural
language and routed by intent:

| User intent (examples) | Detected action |
|---|---|
| "dentist checkup tomorrow 2-3pm" | add |
| "SPD offer email all day on 19 Aug" | add a native all-day event |
| "cancel my dentist appointment" / "delete the 2pm meeting" | delete |
| "move IPPT to 4pm" / "change dentist to Friday" | edit |
| "reminders" / "show upcoming reminders" | list reminders |
| "when am I free tmr?" | find free time |
| "Gym every Monday at 8pm" | add weekly recurring event |
| "change the weekly gym series to Tuesdays at 7pm" | edit every occurrence and its recurrence rule |
| "disable reminder for IPPT" | remove reminder |
| "remind me to pay the bill tomorrow at 9am" | independent Telegram reminder |
| "set me a reminder tonight at 11.50pm to book the court" | independent Telegram reminder |
| "set a reminder in 15minutes to shower" | independent Telegram reminder |
| "remind me 15 minutes before Dental" | match Dental and attach an event reminder |
| "what's on my calendar this week" / "list upcoming" | list |
| "what are my plans on 19 Aug" | list events for that specific day |
| "what are my plans on Monday" | list events for the next matching weekday |
| "calendar types" / "show my calendars" | list accessible calendars and their colours |
| "Team meeting tomorrow 2pm in Work calendar" | add to the named writable calendar |
| `/start` | personalised welcome message with usage examples |
| Reply to a pending disambiguation (`2` or `second`) | resolve pending delete, edit, reminder attachment, or reminder removal |

Intent detection can be a single Groq call that returns `action` as
part of the JSON, or a lightweight keyword pre-check (e.g. "cancel",
"delete", "remove" → delete; "what's on", "list", "show" → list;
everything else → add) with Groq only used for add/delete/edit field
extraction. **Recommendation: keyword pre-check for action routing,
Groq only for field extraction** — cheaper and more predictable.

## 8. Core Flows

### 8.1 Add event
1. Receive message → keyword check doesn't match delete/list → treat as add
2. Call `parser.parse_event(message, now, timezone)`
3. If `confidence: low` → reply asking for clarification, stop
4. For explicit all-day wording, normalize to local-midnight date boundaries and set `all_day`; otherwise retain timed values
5. Call `calendar_client.create_event(parsed_event)` using Google `date` fields for all-day events or `dateTime` fields for timed events
6. Reply with the created event range; native all-day events are labelled `All day`

### 8.2 List upcoming
1. Detect "list" intent
2. Call `calendar_client.list_events(days_ahead=7)` (default window;
   allow "this month" etc. to adjust window later)
3. Reply with a formatted list, one line per event

### 8.3 Delete event
1. Detect "delete" intent
2. Call `calendar_client.list_events(days_ahead=30)`
3. Call `parser.match_event(message, candidate_events)`
4. If single confident match → delete immediately, confirm
5. If ambiguous → reply with numbered candidates, store pending state
   `{chat_id: [event_ids]}` with a 5-minute TTL
6. On next message, if pending state exists and message looks like a
   selection (digit or ordinal), resolve and delete; otherwise clear
   pending state and treat as a new message

### 8.4 Edit event
1. Detect an edit keyword such as "change", "move", "reschedule", or "update"
2. Fetch upcoming events for the next 30 days and match the referenced event
3. If ambiguous, present numbered candidates and retain the original edit request for five minutes
4. Parse the requested change against the selected event, preserving fields the user did not change
5. If confident, patch the Google Calendar event and confirm the updated time

### 8.5 Reminders, recurring events, conflicts, and free time
- An event can have multiple attached reminders, each with optional custom Telegram text. `another`, `also`, or `additional` appends instead of replacing the current reminder metadata.
- Reminder intent is determined before generic event creation. A due time or delay (`tonight at 11.50pm`, `tomorrow at 9am`, `in 15minutes`) creates an independent reminder. A lead time before a referenced event (`15 minutes before Dental`) invokes event matching and attaches the reminder.
- An independent reminder is not linked to an existing event and never creates a Google Calendar entry. The bot creates an enabled cron-job.org job scheduled for the requested local minute with an expiry one day later. After creation it patches the secured callback body with the returned job ID. If that patch fails, creation is rolled back by deleting the incomplete job.
- `reminders` and equivalent phrases list unsent reminders due in the next 30 days, combining both types chronologically. Output identifies `Independent reminder` or `Event reminder`; linked entries also show the event name and lead time.
- Removing a standalone reminder deletes its cron-job.org job. Removing an event reminder clears its Telegram reminder metadata without deleting the event.
- The Calendar API's calendar list is used to show each accessible calendar's name and `backgroundColor` hex value. Lists, free-time checks, edits, deletes, and reminders span accessible calendars. A new event uses the default configured calendar unless its message explicitly names one; read-only calendars are never selected for insertion.
- The scheduler checks reminder metadata every minute and sends the Telegram notification once.
- Common weekly wording (`every Monday`) becomes a Google Calendar `RRULE:FREQ=WEEKLY;BYDAY=...` series. Recurring-series deletion removes the series master.
- Before inserting an event, the app checks the next 30 days for overlap. The user must include `add anyway` to override a conflict warning.
- Free-time requests scan upcoming events and report one-hour-or-longer gaps from 12:00 AM through 11:59 PM.

## 9. Error Handling

- Google API errors (expired token, quota, network) → catch, log, reply
  with a generic "couldn't reach your calendar, try again" message —
  never expose raw stack traces to the user
- Groq API errors/timeouts → same pattern, generic retry message
- Malformed/unparseable JSON from Groq → retry once with a stricter
  prompt; if it fails again, ask the user to rephrase
- Telegram webhook requests without the correct secret token → reject
  with 401, do not process
- All exceptions logged server-side with enough context to debug
  (message text, action detected, timestamp) — no need for a full
  logging stack, stdout logs on the host are sufficient for v1

## 10. Timezone Handling

- The home timezone is a fixed config value (`USER_TIMEZONE`, default `Asia/Singapore`) shared by the one or two fixed users.
- All relative date/time phrases are resolved against this timezone
- All events are created in Google Calendar with this timezone explicitly
  set (not UTC-naive)

## 11. Security Notes

- Telegram webhook secret token validated on every request
- Bot performs calendar operations only for configured Telegram user IDs
  (`TELEGRAM_USER_1_ID` and optional `TELEGRAM_USER_2_ID`); other users receive
  an access-denied response
- Every configured Telegram ID maps to exactly one Google refresh token and
  calendar. Requests, reminders, and agendas use only that user's calendar.
- Webhook messages are processed only from private chats, preventing calendar
  responses from being exposed to Telegram groups.
- No secrets committed to source control; `.env` gitignored
- Google refresh token has calendar scope only
  (`https://www.googleapis.com/auth/calendar`), not broader Google
  account access

## 12. Build Milestones

1. **Skeleton**: FastAPI app + Telegram webhook wired up, echoes
   messages back. Confirms hosting + webhook delivery work.
2. **Google auth**: One-time OAuth script produces a refresh token;
   hardcoded test event created successfully via `calendar_client.py`.
3. **Add flow**: Groq parsing integrated, real events created from
   free-text messages.
4. **List flow**: upcoming events fetched and formatted in chat.
5. **Delete and edit flows**: fuzzy match + disambiguation + deletion or patching.
6. **Scheduling and availability**: reminders, daily agenda, conflicts, recurring events, and free-time listing.
7. **Hardening**: error handling, timezone edge cases, unauthorized user
   rejection, logging.

## 13. Implementation Status

- [x] Sending a natural-language add message creates a correctly-timed
      event on the real Google Calendar
- [x] Sending "list" / "what's on my calendar" returns accurate upcoming
      events
- [x] Sending a delete phrase removes the correct event, with
      disambiguation when multiple events could match
- [x] Sending an edit phrase updates the correct event, with
      disambiguation when multiple events could match
- [x] Recurring events, both reminder types, combined reminder listings, conflict warnings, named calendars, and free-time queries work as documented
- [x] Bot rejects calendar access for Telegram users that are not configured
- [x] Bot recovers gracefully (no crash, clear message) from a Google or
      Groq API failure
- [x] App runs on the chosen free host with the webhook
      correctly registered
- [x] Free external scheduler calls the reminder endpoint every minute and the daily-agenda endpoint at 8:00 AM Asia/Singapore

## 14. Current Operational Constraints

- Upcoming event lists use a 7-day window; matching, conflicts, reminder management, and reminder listings use 30-day windows.
- Pending numbered choices are held in memory for five minutes and are lost on a restart.
- Independent reminder persistence depends on the cron-job.org REST API and its account quotas (normally 100 API requests per day). Without both `CRON_JOB_API_KEY` and `SERVICE_BASE_URL`, independent reminder creation is disabled while calendar features remain available.
- Delivery depends on cron-job.org reaching the sleeping Render service; the first request after idle may be delayed.
