# Spec: Telegram → Google Calendar Bot

## 1. Overview

A personal Telegram bot that lets the user create, list, edit, delete, and schedule reminders for Google
Calendar events by sending natural-language messages (e.g. "dentist
checkup tomorrow 2-3pm"). The bot parses the message using the Groq
API into structured event data, then calls the Google Calendar
API to perform the action.

Single-user tool. No multi-tenant auth, no database beyond a small local
state file if needed. Runs as a webhook-based web service on a free host
(Render/Railway/Fly.io).

## 2. Goals / Non-Goals

**Goals**
- Add one-off and weekly recurring events via free-text message
- List upcoming events, daily schedules, reminders, and free time
- Delete/cancel or edit events and reminders via free-text reference (fuzzy match against
  upcoming events)
- Confirm every action back to the user in chat
- Run reliably on a free-tier host with a webhook (no polling)

**Non-goals (v1)**
- Multi-user support
- Voice message input
- Multi-calendar support, attendee invitations, and undo

## 3. Architecture

### 3.1 Technology Stack

| Layer | Technology | Purpose |
|---|---|---|
| Bot transport | Telegram Bot API webhooks | Receive messages and send replies |
| Web service | Python 3.12, FastAPI, Uvicorn | HTTPS webhook and scheduler endpoints |
| Natural-language parsing | Groq API (`openai/gpt-oss-20b`) | Extract event, edit, delete, and reminder data |
| Calendar | Google Calendar API v3, OAuth 2.0 | Event CRUD, reminder metadata, and availability data |
| Hosting | Render free web service | Public HTTPS runtime and GitHub deployment |
| External scheduler | cron-job.org | Minute-by-minute reminders and daily agenda delivery |
| Testing | pytest | Parser, Calendar conversion, and formatting tests |

### 3.2 Scheduled Notifications

- `POST /scheduled/reminders` is called every minute by cron-job.org. It validates `SCHEDULER_SECRET`, finds due reminders, sends Telegram messages, and marks each reminder as sent in the event's private Google Calendar metadata.
- `POST /scheduled/daily-agenda` is called once daily at the configured `DAILY_AGENDA_HOUR` in `USER_TIMEZONE` and sends the day's agenda to the owner.
- Scheduler requests use an `Authorization: Bearer <SCHEDULER_SECRET>` header; direct unauthorised calls are rejected.
- Users can add a reminder while creating an event or request one for an existing event. Existing-event reminders use the same event matching and disambiguation flow as edits and deletes.
- **Deployment status:** cron-job.org reminder and daily-agenda jobs are configured and verified. Successful scheduled calls return `204 No Content`.
```
Telegram (user) → Telegram webhook → Web app (FastAPI) → Router
                                                              ├─ Add flow    → LLM (parse) → Google Calendar API (insert)
                                                              ├─ List flow   → Google Calendar API (list)
                                                              ├─ Delete flow → Google Calendar API (list) → LLM/fuzzy match → Google Calendar API (delete)
                                                              └─ Edit flow   → Google Calendar API (list) → LLM (parse change) → Google Calendar API (patch)
                                                              ↓
                                                        Reply to Telegram
```

- **Transport**: Telegram Bot API, webhook mode (not long polling)
- **Web framework**: FastAPI (async, plays well with webhook handlers)
- **NLP parsing**: Groq API, prompted to return structured JSON
- **Calendar**: Google Calendar API v3, OAuth 2.0 (installed-app flow, one-time),
  with a separate refresh token stored as a secret/env var for each fixed user
- **Hosting**: Render or Railway free web service, single process

## 4. File / Project Structure

```
calendar-bot/
├── app/
│   ├── main.py              # FastAPI app, /webhook route, dispatch logic
│   ├── telegram_client.py   # Send messages, verify webhook secret
│   ├── parser.py            # Groq API calls: parse_event(), match_event(), parse_edit()
│   ├── calendar_client.py   # Google Calendar wrapper: create/list/delete
│   ├── config.py            # Loads env vars, validates required secrets
│   └── models.py            # Pydantic models for ParsedEvent, EventMatch, etc.
├── auth/
│   └── get_refresh_token.py # One-time local script for Google OAuth
├── tests/
│   ├── test_parser.py
│   └── test_calendar_client.py
├── requirements.txt
├── .env.example
├── render.yaml               # or Procfile, depending on host
└── README.md
```

## 5. External Services & Credentials

| Service | Purpose | Credential |
|---|---|---|
| Telegram Bot API | Receive/send messages | `TELEGRAM_BOT_TOKEN` |
| Telegram webhook | Verify incoming requests are genuine | `TELEGRAM_WEBHOOK_SECRET` |
| Groq API | Parse natural language into structured event data | `GROQ_API_KEY` |
| Google Calendar API | Create/list/delete events | Shared `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`; per-user `GOOGLE_USER_1_REFRESH_TOKEN` and optional `GOOGLE_USER_2_REFRESH_TOKEN` |

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
  "recurrence": "RFC5545 RRULE string or null"
}
```

- `confidence: low` triggers a clarifying reply instead of creating the
  event outright (e.g. ambiguous date, missing time).
- If `end` cannot be inferred, default to `start + 1 hour`.
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

## 7. Command / Message Handling

No slash commands required for v1 — every message is treated as natural
language and routed by intent:

| User intent (examples) | Detected action |
|---|---|
| "dentist checkup tomorrow 2-3pm" | add |
| "cancel my dentist appointment" / "delete the 2pm meeting" | delete |
| "move IPPT to 4pm" / "change dentist to Friday" | edit |
| "reminders" / "show upcoming reminders" | list reminders |
| "when am I free tmr?" | find free time |
| "Gym every Monday at 8pm" | add weekly recurring event |
| "disable reminder for IPPT" | remove reminder |
| "what's on my calendar this week" / "list upcoming" | list |
| "what are my plans on 19 Aug" | list events for that specific day |
| `/start` | personalised welcome message with usage examples |
| Reply to a pending disambiguation ("2" or "the second one") | resolve pending delete |

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
4. Call `calendar_client.create_event(parsed_event)`
5. Reply: `✅ Added: {title} — {formatted start}–{formatted end}`

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
- A reminder can be attached while creating an event or added, changed, listed, or removed later.
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

- User's home timezone is a fixed config value (`USER_TIMEZONE` env var,
  e.g. `Asia/Singapore`) shared by the one or two fixed users
- All relative date/time phrases are resolved against this timezone
- All events are created in Google Calendar with this timezone explicitly
  set (not UTC-naive)

## 11. Security Notes

- Telegram webhook secret token validated on every request
- Bot only responds to configured Telegram user IDs (`TELEGRAM_USER_1_ID` and
  optional `TELEGRAM_USER_2_ID`) — reject/ignore all others silently
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

## 13. Acceptance Criteria (v1 done when)

- [ ] Sending a natural-language add message creates a correctly-timed
      event on the real Google Calendar
- [ ] Sending "list" / "what's on my calendar" returns accurate upcoming
      events
- [ ] Sending a delete phrase removes the correct event, with
      disambiguation when multiple events could match
- [ ] Sending an edit phrase updates the correct event, with
      disambiguation when multiple events could match
- [ ] Recurring events, reminder management, conflict warnings, and free-time queries work as documented
- [ ] Bot ignores/rejects messages from any Telegram user other than the
      owner
- [ ] Bot recovers gracefully (no crash, clear message) from a Google or
      Groq API failure
- [ ] App runs continuously on the chosen free host with the webhook
      correctly registered
- [x] Free external scheduler calls the reminder endpoint every minute and the daily-agenda endpoint at 8:00 AM Asia/Singapore

## 14. Open Questions (resolve before/during build)

- Exact Groq model string and current pricing to confirm before
  implementation (verify at build time, not assumed from this spec)
- Default list window (7 days vs. configurable) — start with 7, revisit
