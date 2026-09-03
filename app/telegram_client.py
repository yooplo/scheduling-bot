from __future__ import annotations

import hmac

import httpx


def valid_webhook_secret(received: str | None, expected: str) -> bool:
    return bool(received) and hmac.compare_digest(received, expected)


class TelegramClient:
    def __init__(self, bot_token: str) -> None:
        self._base_url = f"https://api.telegram.org/bot{bot_token}"

    async def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> None:
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(f"{self._base_url}/sendMessage", json=payload)
            response.raise_for_status()

    async def answer_callback_query(self, callback_query_id: str, text: str | None = None) -> None:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(f"{self._base_url}/answerCallbackQuery", json=payload)
            response.raise_for_status()
