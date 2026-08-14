# Telegram → Google Calendar bot

A private, webhook-based FastAPI service that understands natural-language Telegram messages and creates, lists, or deletes events in one Google Calendar. It only accepts messages from the configured Telegram user ID.

## What you need before deployment

1. **Telegram bot token and user ID**
   - In Telegram, open [@BotFather](https://t.me/BotFather), run `/newbot`, and save its API token as `TELEGRAM_BOT_TOKEN`.
   - Message [@userinfobot](https://t.me/userinfobot) to obtain your numeric ID; use it as `ALLOWED_TELEGRAM_USER_ID`.
   - Generate `TELEGRAM_WEBHOOK_SECRET` locally with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Do not use the sample value from `.env.example`.

2. **Anthropic key**
   - Create an account and API key in the [Anthropic Console](https://console.anthropic.com/).
   - Add billing/credits: the hosting can be free, but Claude API usage is billed by Anthropic. Store the key as `ANTHROPIC_API_KEY`.
   - `ANTHROPIC_MODEL` defaults to `claude-sonnet-4-20250514`; confirm the model remains available in Anthropic's model documentation before first deploy.

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

     A browser opens. Sign into the Google account whose calendar the bot should manage and grant the Calendar-only scope. Copy the three displayed values into your `.env`/Render secrets. Delete the downloaded JSON afterwards if you no longer need it; never commit it.

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
3. Enter the required values marked `sync: false`: `TELEGRAM_BOT_TOKEN`, `ALLOWED_TELEGRAM_USER_ID`, `ANTHROPIC_API_KEY`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `GOOGLE_REFRESH_TOKEN`. Render generates `TELEGRAM_WEBHOOK_SECRET`; retrieve its value from the service Environment page for the next step.
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

## Security notes

- The webhook validates Telegram's secret header before processing anything.
- Messages from any Telegram user except `ALLOWED_TELEGRAM_USER_ID` are ignored.
- The Google token uses only the Calendar scope.
- Rotate any credential immediately if it is ever committed or pasted into a ticket/chat.
