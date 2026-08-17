# Telegram → Google Calendar bot

A private, webhook-based FastAPI service that understands natural-language Telegram messages and creates, lists, or deletes events in a fixed user's paired Google Calendar. It supports one or two preconfigured Telegram users; each is isolated to their own Google Calendar.

Include a location naturally when creating an event, for example: `Dinner at La Pasta Saturday 7–9pm`. Locations are shown in upcoming lists when provided.

## What the bot can do

All requests use `USER_TIMEZONE` (normally `Asia/Singapore`).

| Task | Example message |
|---|---|
| Add an event | `Dentist tomorrow 2–3pm` |
| Add a location | `Dinner at La Pasta Saturday 7–9pm` |
| Add custom or multiple event reminders | `Dentist tomorrow 2pm, remind me 1 hour before to bring ID and 15 minutes before` |
| Add an independent reminder | `remind me to pay the bill tomorrow at 9am` |
| Add a recurring event | `Gym every Monday at 8pm` |
| Start the bot | `/start` — receives a personalised welcome and examples |
| List the next 7 days | `list`, `upcoming`, or `schedule` |
| List a specific day | `what are my plans on 19 Aug?` |
| List the next named weekday | `what are my plans on Monday?` |
| List today/tomorrow | `plans tmr`, `what are my plans tomorrow?`, or `show my schedule today` |
| Find availability | `when am I free tmr?` or `find free time this week` |
| Edit an event | `move IPPT to 4pm` or `update carousel JOOLA to be at Amelia's house` |
| Edit a whole recurring series | `change the weekly gym series to Tuesdays at 7pm` or `update all gym sessions to be at Studio A` |
| Delete one event | `remove dentist tomorrow` |
| Delete a recurring series | `remove the weekly Monday gym sessions` |
| Set/change a reminder | `set a reminder one day before IPPT` or `change the reminder for supper to 8:50pm` |
| Add another reminder | `add another reminder for IPPT 15 minutes before to leave now` |
| Remove a reminder | `disable reminder for IPPT` |
| List reminders | `reminders` or `show me all upcoming reminders` |

The bot warns before creating an event that overlaps an upcoming event. To deliberately create it anyway, repeat the request with `add anyway`, for example `add anyway meeting tomorrow 2–3pm`.

Free-time results cover the full day, from 12:00 AM through 11:59 PM, and show slots of at least one hour.

## Current limitations

- One or two preconfigured Telegram users and calendars only. Adding users or allowing account changes requires a database and web OAuth flow.
- No voice-message transcription, invitees/attendees, multiple calendars, or undo action.
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
   - In [Google Cloud Console](https://console.cloud.google.com/), create a project, enable **Google Calendar API**, and configure the OAuth consent screen. For a personal app, add your Google account as a test user if the app is in Testing.
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
3. Generate a webhook secret locally with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Enter it, along with the other values marked `sync: false`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USER_1_ID`, `GOOGLE_USER_1_REFRESH_TOKEN`, `GROQ_API_KEY`, `GOOGLE_CLIENT_ID`, and `GOOGLE_CLIENT_SECRET`. For two people, also enter `TELEGRAM_USER_2_ID` and `GOOGLE_USER_2_REFRESH_TOKEN`. Keep the webhook secret available for the next step.
4. Deploy. Once healthy, copy its URL, for example `https://telegram-calendar-bot.onrender.com`.
5. Register the webhook from PowerShell. Replace all placeholders; do not paste the command into a shell history if it contains your token:

   ```powershell
   Invoke-RestMethod -Method Post -Uri "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" -Body @{url="https://<YOUR-SERVICE>.onrender.com/webhook"; secret_token="<TELEGRAM_WEBHOOK_SECRET>"}
   ```

6. Send the bot a message such as `dentist tomorrow 2-3pm`, then `list`, then a delete request to verify all flows.

Render currently offers free Python web services, but they sleep after 15 minutes idle and can take about a minute to wake. Telegram retries failed webhook deliveries, so the bot should recover, but the first reply after idling can be delayed. Render free services also have ephemeral disks, which is why pending delete choices are intentionally short-lived in memory. See [Render's free-service limits](https://render.com/docs/free).

## Test

```powershell
pytest
```

## Free scheduled reminders and daily agenda

Create an account at [cron-job.org](https://cron-job.org/). Generate `SCHEDULER_SECRET` with `python -c "import secrets; print(secrets.token_urlsafe(32))"`, add it to Render, and redeploy. Create these HTTPS POST jobs with header `Authorization: Bearer <SCHEDULER_SECRET>`:

- Every minute: `https://YOUR-SERVICE.onrender.com/scheduled/reminders`
- Daily at 08:00, timezone `Asia/Singapore`: `https://YOUR-SERVICE.onrender.com/scheduled/daily-agenda`

Add custom or multiple reminders in natural language, for example: `Dentist tomorrow at 2pm, remind me 1 hour before to bring ID and 15 minutes before`.
For an existing event, say: `Set a reminder one day before IPPT` or `add another reminder for IPPT 15 minutes before to leave now`.
For an independent reminder, say: `remind me to pay the bill tomorrow at 9am`. It is stored as a private, transparent Google Calendar entry and excluded from normal event lists.

### Verified scheduler status

The cron-job.org reminder and daily-agenda jobs have been configured and verified with successful `204 No Content` responses. The reminder job runs every minute; the daily agenda runs at 8:00 AM in `Asia/Singapore`.

## Security notes

- The webhook validates Telegram's secret header before processing anything.
- Messages from any Telegram user not configured as `TELEGRAM_USER_1_ID` or `TELEGRAM_USER_2_ID` are ignored.
- Each configured user is routed only to their paired Google refresh token and calendar; the bot never exposes one user's events to the other.
- The bot processes configured users only in private Telegram chats, never groups.
- Each Google token uses only the Calendar scope.
- Rotate any credential immediately if it is ever committed or pasted into a ticket/chat.
