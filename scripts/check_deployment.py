"""Wait for the intended Render revision without accessing bot credentials."""
from __future__ import annotations

import json
import os
import sys
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def wait_for_deployment(service_url: str, expected_commit: str, *, timeout: float = 900, interval: float = 20) -> None:
    origin = urlsplit(service_url)
    if (origin.scheme != "https" or not origin.hostname or origin.username or origin.password
            or origin.path not in {"", "/"} or origin.query or origin.fragment):
        raise ValueError("Set BOT_SERVICE_URL to the public HTTPS origin in repository Actions variables.")
    if not expected_commit:
        raise ValueError("EXPECTED_COMMIT is required.")
    deadline = time.monotonic() + timeout
    request = Request(service_url.rstrip("/") + "/healthz", headers={
        "Accept": "application/json", "Cache-Control": "no-cache",
    })
    while time.monotonic() < deadline:
        try:
            with urlopen(request, timeout=min(15, max(0.1, deadline - time.monotonic()))) as response:
                if response.status != 200:
                    raise ValueError("Health endpoint did not return HTTP 200")
                body = response.read(8193)
                if len(body) > 8192:
                    raise ValueError("Health response exceeds 8 KiB")
                health = json.loads(body)
                if isinstance(health, dict) and health.get("status") == "ok" and health.get("commit") == expected_commit:
                    print(f"Deployment is healthy at commit {expected_commit}.", flush=True)
                    return
                print("Waiting: health status or deployed commit does not match yet.", flush=True)
        except (URLError, OSError, ValueError):
            # Do not log response bodies or exception URLs.
            print("Waiting: health endpoint is unavailable or returned an invalid response.", flush=True)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(interval, remaining))
    raise TimeoutError(f"Deployment did not become healthy at commit {expected_commit} within {timeout:g} seconds.")


def main() -> int:
    try:
        wait_for_deployment(os.getenv("BOT_SERVICE_URL", "").strip(), os.getenv("EXPECTED_COMMIT", "").strip())
    except (ValueError, TimeoutError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
