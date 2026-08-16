"""Run locally once to create the Google Calendar refresh token for deployment."""
from __future__ import annotations

import json
import os
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPE = ["https://www.googleapis.com/auth/calendar"]


def main() -> None:
    credentials_file = Path(os.getenv("GOOGLE_OAUTH_CLIENT_FILE", "client_secret.json"))
    if not credentials_file.exists():
        raise SystemExit(f"Missing {credentials_file}. Download Desktop app OAuth credentials from Google Cloud.")
    flow = InstalledAppFlow.from_client_secrets_file(credentials_file, SCOPE)
    credentials = flow.run_local_server(port=0, open_browser=True)
    slot = os.getenv("GOOGLE_USER_SLOT", "1").strip()
    if slot not in {"1", "2"}:
        raise SystemExit("GOOGLE_USER_SLOT must be 1 or 2")
    print("\nAdd these values to your local .env and Render environment variables:\n")
    print(f"GOOGLE_CLIENT_ID={credentials.client_id}")
    print(f"GOOGLE_CLIENT_SECRET={credentials.client_secret}")
    print(f"GOOGLE_USER_{slot}_REFRESH_TOKEN={credentials.refresh_token}")


if __name__ == "__main__":
    main()
