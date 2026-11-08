from __future__ import annotations

import hmac
import logging

import httpx

# Telegram embeds credentials in URLs, including media download URLs.
logging.getLogger("httpx").setLevel(logging.WARNING)


def valid_webhook_secret(received: str | None, expected: str) -> bool:
    return bool(received) and hmac.compare_digest(received, expected)


class TelegramClient:
    def __init__(self, bot_token: str) -> None:
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self._file_url = f"https://api.telegram.org/file/bot{bot_token}"

    async def download_file(self, file_id: str, max_bytes: int) -> bytes:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{self._base_url}/getFile", json={"file_id": file_id})
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                raise ValueError("Telegram could not retrieve the file")
            info = payload["result"]
            if info.get("file_size", 0) > max_bytes:
                raise ValueError("Media exceeds the size limit")
            path = info["file_path"]
            if not path or path.startswith("/") or ".." in path.split("/") or ":" in path:
                raise ValueError("Invalid Telegram file path")
            data = bytearray()
            async with client.stream("GET", f"{self._file_url}/{path}") as download:
                download.raise_for_status()
                async for chunk in download.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise ValueError("Media exceeds the size limit")
            if not data:
                raise ValueError("Empty media file")
            return bytes(data)

    async def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None, *, parse_mode: str | None = None) -> None:
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
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

    async def download_media(self, file_id: str, max_bytes: int = 10 * 1024 * 1024) -> bytes:
        return await self.download_file(file_id, max_bytes)
