import base64
from types import SimpleNamespace
from unittest.mock import Mock
from datetime import datetime, timezone

import httpx
import pytest
from pydantic import ValidationError

from app.parser import GroqParser
from app.telegram_client import TelegramClient


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ok", "metadata_size", "stream_size", "bad_path"])
async def test_telegram_download_bounds_and_paths(monkeypatch, mode):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {
                "file_path": "../outside" if mode == "bad_path" else "photos/image.jpg",
                "file_size": 100 if mode == "metadata_size" else 3}})
        return httpx.Response(200, content=b"123456" if mode == "stream_size" else b"123")

    client = httpx.AsyncClient
    monkeypatch.setattr("app.telegram_client.httpx.AsyncClient", lambda **kwargs:
        client(transport=httpx.MockTransport(respond), **kwargs))
    telegram = TelegramClient("test-token")
    if mode == "ok":
        assert await telegram.download_media("file", max_bytes=5) == b"123"
        assert requests[-1].url.path == "/file/bottest-token/photos/image.jpg"
    else:
        with pytest.raises(ValueError):
            await telegram.download_media("file", max_bytes=5)
        assert len(requests) == (2 if mode == "stream_size" else 1)


def test_media_parser_passes_image_to_selected_model_and_validates_result():
    parser = GroqParser.__new__(GroqParser)
    parser._client, parser._model = Mock(), "text-model"
    response = parser._client.chat.completions.create
    response.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"event_text": "Dinner Friday 7pm"}'))])
    result = parser.suggest_media_event("caption", datetime.now(timezone.utc), "Asia/Singapore", b"image", "image/png", "vision-model")
    assert result.event_text == "Dinner Friday 7pm"
    payload = response.call_args.kwargs
    assert payload["model"] == "vision-model"
    assert payload["messages"][1]["content"][1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(b"image").decode()
    assert "untrusted" in payload["messages"][0]["content"]
    response.return_value.choices[0].message.content = '{"event_text": {"invalid": 1}}'
    with pytest.raises(ValidationError):
        parser.suggest_media_event("text", datetime.now(timezone.utc), "Asia/Singapore")
