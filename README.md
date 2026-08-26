# Telegram → Google Calendar bot

A private, webhook-based FastAPI service that combines deterministic command routing with Groq-backed natural-language parsing to manage Google Calendar events, calendars, Telegram reminders, recurring schedules, and availability. It supports one or two preconfigured Telegram users; each is isolated to their own Google account and accessible calendars.

Include a location naturally when creating an event, for example: `Dinner at La Pasta Saturday 7–9pm`. Locations are shown in upcoming lists when provided.

## What the bot can do

All requests use `USER_TIMEZONE` (normally `Asia/Singapore`).

| Task | Example message |
|---|---|
| Add an event | `Dentist tomorrow 2–3pm` |
| Add a native all-day event | `SPD offer email all day on 19 Aug` or `add Friday whole day with Ames` |
| Add a location | `Dinner at La Pasta Saturday 7–9pm` |
| Add custom or multiple event reminders | `Dentist tomorrow 2pm, remind me 1 hour before to bring ID and 15 minutes before` |
| Add an independent reminder | `remind me to pay the bill tomorrow at 9am`, `remind me in an hour to call Amelia`, or `set a reminder in 15 minutes to shower` |
| Add a recurring event | `Gym every Monday at 8pm` |
| Start the bot | `/start` — receives a personalised welcome and examples |
| List reminders | `/reminders` |
| List calendars | `/calendars` |
| Show current local time | `/now` |
| List the next 7 days | `list`, `upcoming`, or `schedule` |
| List a specific day | `what are my plans on 19 Aug?` |
| List the next named weekday | `what are my plans on Monday?` |
| Show calendar types/colours | `calendar types` or `show my calendars` (closest colour-square emoji) |
| Create a calendar | `create calendar School`, `add a calendar named School`, or `add School calendar` |
| Delete a calendar | `delete calendar School` or `remove School calendar`, then `confirm delete calendar` |
| Add to a specific calendar | `Team meeting tomorrow 2pm in Work calendar` |
| List today/tomorrow | `plans tmr`, `what are my plans tomorrow?`, or `show my schedule today` |
| Find availability | `when am I free tmr?` or `find free time this week` |
| Edit an event | `move IPPT to 4pm` or `update carousel JOOLA to be at Amelia's house` |
| Edit a whole recurring series | `change the weekly gym series to Tuesdays at 7pm` or `update all gym sessions to be at Studio A` |
| Delete one event | `remove dentist tomorrow` |
| Delete a recurring series | `remove the weekly Monday gym sessions` |
| Set/change an event reminder | `set a reminder one day before IPPT`, `remind me 15 minutes before Dental`, or `change the reminder for supper to 8:50pm` |
| Add another reminder | `add another reminder for IPPT 15 minutes before to leave now` |
| Remove a reminder | `disable reminder for IPPT`, or list reminders and reply `remove 2` |
| List all reminder types | `reminders` or `show me all upcoming reminders` (shows independent and event-linked reminders together) |

The bot warns before creating an event that overlaps an upcoming event. To deliberately create it anyway, repeat the request with `add anyway`, for example `add anyway meeting tomorrow 2–3pm`. The control words `add anyway` are removed before title parsing.

Free-time results cover the full day, from 12:00 AM through 11:59 PM, and show slots of at least one hour. They include events from every calendar the user can view. New events go to the configured default calendar unless a writable named calendar is explicitly specified.

Messages that explicitly say `all day` or `whole day` create native Google Calendar all-day events using date-only boundaries, rather than timed events from 12:00 AM to 11:59 PM. Concise forms such as `add Friday whole day with Ames` are parsed deterministically. Multi-day all-day events use Google's exclusive end-date convention.

Calendar deletion requires confirmation within five minutes. The bot refuses to delete the primary Google Calendar or a shared calendar the user does not own.

To expose the slash-command menu in Telegram, send `/setcommands` to BotFather and enter:

```text
reminders - Shows all upcoming reminders
calendars - Shows the list of calendars
now - Tells you the date and time now
```

Reminder wording determines whether a notification is independent or event-linked:

- A due time or delay creates an independent reminder: `tonight at 11.50pm`, `tomorrow at 9am`, or `in 15 minutes`.
- A lead time before a named event links the reminder to that event: `15 minutes before Dental` or `one day before IPPT`.
- `reminders` lists both types chronologically. Independent reminders are labelled `🔔 Independent reminder`; attached reminders are labelled `🔗 Event reminder` and show the calendar event.
- The displayed reminder list remains selectable for five minutes. `remove 2`, `delete 2`, or `cancel 2` removes that reminder rather than a calendar event; removing one attached reminder preserves the others on the same event.
- In `set a reminder at 11.55pm to book a court for 7 September`, the unqualified clock time controls delivery and `7 September` remains message text. Put an explicit date before `to`—for example `at 11.55pm on 7 September to ...`—to schedule delivery on that date.

## Current limitations

- One or two preconfigured Telegram/Google accounts only. Each account can access and manage multiple calendars; adding users or changing linked accounts dynamically requires a database and web OAuth flow.
- No voice-message transcription, attendee/invitation management, arbitrary user sign-up, or undo action.
- Only common weekly recurrence wording is supported; arbitrary recurrence schedules are not exposed as a dedicated command flow.
- Scheduler-based reminders and daily agenda require the cron-job.org setup below; confirm successful `204` job runs before relying on them.

## What you need before deployment

1. **Telegram bot token and user IDs**
   - In Telegram, open [@BotFather](https://t.me/BotFather), run `/newbot`, and save its API token as `TELEGRAM_BOT_TOKEN`.
   - Each permitted person messages [@userinfobot](https://t.me/userinfobot) to obtain their numeric ID. Store the first person's ID as `TELEGRAM_USER_1_ID` and, if using a second person, the other as `TELEGRAM_USER_2_ID`.
   - Generate `TELEGRAM_WEBHOOK_SECRET` locally with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Do not use the sample value from `.env.example`.

2. **Groq key**
   - Create an account and API key in the [Groq Console](https://console.groq.com/keys).
   - Store it as `GROQ_API_KEY`. Groq's free tier is sufficient for a personal calendar bot, subject to its rate limits.
   - `GROQ_MODEL` defaults to `openai/gpt-oss-20b`.

3. **Google Calendar OAuth credentials**
   - In [Google Cloud Console](https://console.cloud.google.com/), create a project, enable **Google Calendar API**, and configure the OAuth consent screen. For a personal app, add your Google account as a test user while configuring it. An External app left in **Testing** receives Calendar refresh tokens that expire after seven days; move the consent screen to **In production** before relying on the deployed bot long-term.
   - Create an OAuth client: **APIs & Services → Credentials → Create credentials → OAuth client ID → Desktop app**. Download the JSON as `client_secret.json` in this project root. It is gitignored.
   - Create a virtual environment, install dependencies, and run the local authorization helper:

     ```powershell
     py -m venv .venv
     .\.venv\Scripts\Activate.ps1
     pip install -r requirements.txt
     python auth/get_refresh_token.py
     ```

     A browser opens. Sign into the first person's Google account and grant the Calendar-only scope. Copy the client ID, client secret, and `GOOGLE_USER_1_REFRESH_TOKEN` shown into your `.env`/Render secrets.

     To connect the optional second fixed user, run the helper again in a new terminal after setting `GOOGLE_USER_SLOT=2`, sign into the second person's Google account, and save the resulting `GOOGLE_USER_2_REFRESH_TOKEN`. Both people use the same client ID and client secret. Delete the downloaded JSON afterwards if you no longer need it; never commit it.

## Run locally

Copy `.env.example` to `.env`, set every value, then run:

```powershell
.\.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8000
```

The webhook needs a public HTTPS URL. For local testing, use a tunnel such as Cloudflare Tunnel, then register `<public-url>/webhook` with Telegram using the call shown in the deployment section.

## GitHub

Create a new **private** GitHub repository, then from this folder run:

```powershell
git init
git add .
git commit -m "Build Telegram Google Calendar bot"
git branch -M main
git remote add origin https://github.com/YOUR-ACCOUNT/telegram-calendar-bot.git
git push -u origin main
```

Before `git add`, run `git status` and make sure `.env` and `client_secret.json` are not listed. GitHub Actions secrets are unnecessary for this project: secrets belong in Render's environment-variable panel, never in the repository.

## Deploy on Render's free tier

1. Create an account at [Render](https://render.com/) and connect GitHub.
2. Click **New → Blueprint**, select this repository, and approve `render.yaml`.
3. Run `python -c "import secrets; print(secrets.token_urlsafe(32))"` twice to generate separate `TELEGRAM_WEBHOOK_SECRET` and `SCHEDULER_SECRET` values. Enter them along with the other required values marked `sync: false`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USER_1_ID`, `GOOGLE_USER_1_REFRESH_TOKEN`, `GROQ_API_KEY`, `GOOGLE_CLIENT_ID`, and `GOOGLE_CLIENT_SECRET`. For two people, also enter `TELEGRAM_USER_2_ID` and `GOOGLE_USER_2_REFRESH_TOKEN`. Keep the webhook secret available for the next step; `CRON_JOB_API_KEY` and `SERVICE_BASE_URL` are configured when enabling independent reminders below.
4. Deploy. Once healthy, copy its URL, for example `https://telegram-calendar-bot.onrender.com`.
5. In Render, set `SERVICE_BASE_URL` to that origin without a trailing slash, then redeploy. Independent reminders also require `CRON_JOB_API_KEY`, configured in the scheduler section below.
6. Register the webhook from PowerShell. Replace all placeholders; do not paste the command into a shell history if it contains your token:

   ```powershell
   Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" -Body @{url="https://<YOUR-SERVICE>.onrender.com/webhook"; secret_token="<TELEGRAM_WEBHOOK_SECRET>"}
   ```

7. Send the bot a message such as `dentist tomorrow 2-3pm`, then `list`, then a delete request to verify the calendar flows.

Render currently offers free Python web services, but they sleep after 15 minutes idle and can take about a minute to wake. Telegram retries failed webhook deliveries, so the bot should recover, but the first reply after idling can be delayed. Render free services also have ephemeral disks, which is why pending delete choices are intentionally short-lived in memory. See [Render's free-service limits](https://render.com/docs/free).

## Test

```powershell
pytest
```

## Free scheduled reminders and daily agenda

Create an account at [cron-job.org](https://cron-job.org/). Generate `SCHEDULER_SECRET` with `python -c "import secrets; print(secrets.token_urlsafe(32))"`, add it to Render, and redeploy. Create these HTTPS POST jobs with header `Authorization: Bearer <SCHEDULER_SECRET>`:

- Every minute: `https://YOUR-SERVICE.onrender.com/scheduled/reminders`
- Daily at 08:00, timezone `Asia/Singapore`: `https://YOUR-SERVICE.onrender.com/scheduled/daily-agenda`

In the cron-job.org Console, open **Settings → API keys**, generate an API key, and store it in Render as `CRON_JOB_API_KEY`. Set `SERVICE_BASE_URL=https://YOUR-SERVICE.onrender.com`. The bot uses the API key to create an expiring one-time job for every independent reminder; cron-job.org calls the secured `/scheduled/standalone-reminder` endpoint at the requested minute. The callback sends Telegram and deletes its job.

Add custom or multiple reminders in natural language, for example: `Dentist tomorrow at 2pm, remind me 1 hour before to bring ID and 15 minutes before`.
For an existing event, say: `Set a reminder one day before IPPT` or `add another reminder for IPPT 15 minutes before to leave now`.
For an independent reminder, say: `remind me to pay the bill tomorrow at 9am`, `remind me in an hour to call Amelia`, `set me a reminder tonight at 11.50pm to book the court`, or `set a reminder in 15 minutes to shower`.

Independent reminders are not associated with an existing event and create no Google Calendar entry. Each is persisted as an expiring cron-job.org job, delivered through a secured callback, and deleted after delivery. Event-linked reminders remain in private metadata on their corresponding Calendar events. Both types appear together under `reminders`; long lists are split across Telegram messages, and a temporary numbered list enables `remove N`.

After upgrading from the previous calendar-backed implementation, existing pending records remain deliverable and are deleted as they fire. Once no legacy reminders remain, the old `Telegram Reminders` calendar can be deleted manually from Google Calendar.

### Verified scheduler status

The fixed cron-job.org event-reminder and daily-agenda jobs have been configured and verified with successful `204 No Content` responses. The event-reminder job runs every minute; the daily agenda runs at 8:00 AM in `Asia/Singapore`.

After setting `CRON_JOB_API_KEY` and `SERVICE_BASE_URL`, verify the dynamic flow separately: create an independent reminder a few minutes ahead, confirm a new job appears in cron-job.org, receive the Telegram notification, and confirm the completed job is removed. This deployment-specific flow is not considered verified until that end-to-end check succeeds.

## Security notes

- The webhook validates Telegram's secret header before processing anything.
- Messages from any Telegram user not configured as `TELEGRAM_USER_1_ID` or `TELEGRAM_USER_2_ID` receive an access-denied reply and no calendar operation is performed.
- Each configured user is routed only to their paired Google refresh token and calendar; the bot never exposes one user's events to the other.
- The bot processes configured users only in private Telegram chats, never groups.
- Each Google token uses only the Calendar scope.
- Rotate any credential immediately if it is ever committed or pasted into a ticket/chat.
