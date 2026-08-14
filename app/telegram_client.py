from __future__ import annotations

import hmac

import httpx


def valid_webhook_secret(received: str | None, expected: str) -> bool:
    return bool(received) and hmac.compare_digest(received, expected)


class TelegramClient:
    def __init__(self, bot_token: str) -> None:
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    async def send_message(self, chat_id: int, text: str) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(self._url, json={"chat_id": chat_id, "text": text})
            response.raise_for_status()

