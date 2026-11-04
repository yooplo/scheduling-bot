import asyncio
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from app.models import MediaEventSuggestion, ParsedEventDraft
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

import app.main as main
from app.media import PHOTO_LIMIT, interpret_media
from app.telegram_client import TelegramClient


@pytest.fixture
def bot(monkeypatch):
    settings = SimpleNamespace(telegram_webhook_secret="secret", telegram_group_id=-100,
        timezone=ZoneInfo("Asia/Singapore"), user_timezone="Asia/Singapore", groq_vision_model="vision", groq_transcription_model="whisper")
    telegram = AsyncMock()
    telegram.download_file.return_value = b"image"
    calendar = Mock()
    interpreter = AsyncMock(return_value="Add dentist tomorrow 2-3pm")
    async def consume_message(chat_id, text, *args):
        # This fixture isolates webhook routing; actual confirmation writes are
        # exercised by test_conversation_media.
        if text == "add event" or text in {"cancel", "/cancel"}:
            main._clear_conversation(chat_id)
    handler = AsyncMock(side_effect=consume_message)
    parser = Mock()
    parser.suggest_media_event.side_effect = lambda *args: MediaEventSuggestion(event_text=interpreter.return_value)
    parser.parse_event.return_value = ParsedEventDraft(title="Dentist", start="2030-11-20T14:00:00+08:00", end="2030-11-20T15:00:00+08:00", confidence="high")
    monkeypatch.setattr(main, "dependencies", lambda: (settings, telegram, {111: calendar}, parser, None))
    monkeypatch.setattr(main, "interpret_media", interpreter)
    monkeypatch.setattr(main, "handle_message", handler)
    main.pending_media.clear()
    main.media_requests.clear()
    main.media_previews.clear()
    main.processed_message_updates.clear()

    async def send(content, user=111, chat=111, kind="private", update_id=None):
        request = Mock()
        request.json = AsyncMock(return_value={"update_id": update_id, "message": {
            "chat": {"id": chat, "type": kind}, "from": {"id": user}, **content,
        }})
        return await main.webhook(request, "secret")

    yield send, telegram, interpreter, handler
    main.pending_media.clear()
    main.media_requests.clear()
    main.media_previews.clear()
    main.processed_message_updates.clear()


PHOTO = {"photo": [{"file_id": "small", "width": 10, "height": 10},
                    {"file_id": "large", "width": 100, "height": 100}]}


@pytest.mark.asyncio
async def test_photo_preview_then_explicit_single_use(bot):
    send, telegram, interpreter, handler = bot
    await send({**PHOTO, "caption": "Read appointment"}, update_id=1)
    telegram.download_file.assert_awaited_once_with("large", PHOTO_LIMIT)
    assert interpreter.call_args.args[1:3] == ("photo", "Read appointment")
    assert "Suggested event" in telegram.send_message.call_args.args[1]
    handler.assert_not_awaited()
    await send(PHOTO, update_id=1)  # Telegram redelivery
    interpreter.assert_awaited_once()
    await send({"text": "use media"})
    assert handler.call_args.args[1] == "add event"
    await send({"text": "use media"})
    handler.assert_awaited_once()
    assert "No active" in telegram.send_message.call_args.args[1]


@pytest.mark.asyncio
async def test_voice_preview_and_cancel(bot):
    send, telegram, interpreter, handler = bot
    await send({"voice": {"file_id": "voice", "duration": 30}})
    assert interpreter.call_args.args[1] == "voice"
    await send({"text": "/cancel"})
    await send({"text": "use media"})
    handler.assert_awaited_once()
    assert handler.call_args.args[1] == "/cancel"
    assert 111 not in main.pending_media


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [
    {"voice": {"file_id": "v", "duration": 121}},
    {"voice": {"file_id": "v", "file_size": 11 * 1024 * 1024}},
    {"photo": [{"file_id": "p", "file_size": PHOTO_LIMIT + 1}]},
])
async def test_oversize_rejected_before_download(bot, content):
    send, telegram, interpreter, handler = bot
    await send(content)
    telegram.download_file.assert_not_awaited()
    interpreter.assert_not_awaited()
    assert not main.pending_media


@pytest.mark.asyncio
@pytest.mark.parametrize("user,chat,kind", [(222, 222, "private"), (111, -100, "supergroup"), (111, -200, "group"), (111, 222, "private")])
async def test_authorization_before_media_processing(bot, user, chat, kind):
    send, telegram, interpreter, handler = bot
    await send(PHOTO, user=user, chat=chat, kind=kind)
    telegram.download_file.assert_not_awaited()
    interpreter.assert_not_awaited()
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_expiry_replacement_and_corrected_text(bot):
    send, telegram, interpreter, handler = bot
    main.pending_media[111] = ("old request", 0)
    await send({"text": "use media"})
    handler.assert_not_awaited()
    main.pending_event_drafts[111] = Mock()
    await send(PHOTO)
    assert 111 not in main.pending_event_drafts
    await send({"text": "Add dentist Friday 3-4pm"})
    assert handler.call_args.args[1] == "Add dentist Friday 3-4pm"
    assert not main.pending_media


@pytest.mark.asyncio
async def test_slow_interpretation_cannot_restore_cancelled_preview(bot):
    send, telegram, interpreter, handler = bot
    started, finish = asyncio.Event(), asyncio.Event()

    async def slow(*args):
        started.set()
        await finish.wait()
        return "obsolete"

    interpreter.side_effect = slow
    task = asyncio.create_task(send(PHOTO))
    await started.wait()
    await send({"text": "/cancel"})
    finish.set()
    await task
    assert not main.pending_media
    assert not any("obsolete" in c.args[1] for c in telegram.send_message.call_args_list)


@pytest.mark.asyncio
async def test_provider_failure_has_no_preview_or_sensitive_logs(bot, caplog):
    send, telegram, interpreter, handler = bot
    interpreter.side_effect = RuntimeError("secret-token-url")
    await send(PHOTO)
    assert not main.pending_media
    assert "couldn't read" in telegram.send_message.call_args.args[1]
    assert "secret-token-url" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("vision_model", ["qwen/qwen3.8-27b", "custom-image-model"])
async def test_groq_photo_and_voice_requests(monkeypatch, vision_model):
    client = AsyncMock()
    client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Meeting Friday"))])
    client.audio.transcriptions.create.return_value = SimpleNamespace(text="hello")
    context = AsyncMock()
    context.__aenter__.return_value = client
    monkeypatch.setattr("app.media.AsyncGroq", lambda **kwargs: context)
    settings = SimpleNamespace(groq_api_key="key", groq_vision_model=vision_model, groq_transcription_model="whisper")
    assert await interpret_media(b"jpg", "photo", "Read it", settings) == "Meeting Friday"
    request = client.chat.completions.create.call_args.kwargs
    assert request["model"] == vision_model
    if vision_model == "qwen/qwen3.8-27b":
        assert request["reasoning_effort"] == "none"
    else:
        assert "reasoning_effort" not in request
    assert request["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert await interpret_media(b"ogg", "voice", "", settings) == "hello"
    assert client.audio.transcriptions.create.call_args.kwargs["file"] == ("voice.ogg", b"ogg", "audio/ogg")
    client.audio.transcriptions.create.return_value = SimpleNamespace(text="")
    with pytest.raises(ValueError):
        await interpret_media(b"ogg", "voice", "", settings)


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_size,body,ok", [(3, b"abc", True), (4, b"abc", False), (0, b"abcd", False), (0, b"", False)])
async def test_download_enforces_metadata_and_stream_size(monkeypatch, metadata_size, body, ok):
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/file.jpg", "file_size": metadata_size}})
        return httpx.Response(200, content=body)

    original = httpx.AsyncClient
    monkeypatch.setattr("app.telegram_client.httpx.AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    telegram = TelegramClient("fake")
    if ok:
        assert await telegram.download_file("id", 3) == body
    else:
        with pytest.raises(ValueError):
            await telegram.download_file("id", 3)
    if metadata_size > 3:
        assert len(calls) == 1


@pytest.mark.asyncio
async def test_private_callback_dismisses_preview(bot):
    send, telegram, interpreter, handler = bot
    await send(PHOTO)
    request = Mock()
    request.json = AsyncMock(return_value={"callback_query": {
        "id": "cb", "data": "help:menu", "from": {"id": 111},
        "message": {"chat": {"id": 111, "type": "private"}},
    }})
    await main.webhook(request, "secret")
    assert not main.pending_media
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_webhook_secret_cannot_download_media(bot):
    send, telegram, interpreter, handler = bot
    request = Mock()
    request.json = AsyncMock(return_value={"message": PHOTO})
    with pytest.raises(main.HTTPException) as error:
        await main.webhook(request, "wrong")
    assert error.value.status_code == 401
    request.json.assert_not_awaited()
    telegram.download_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_photo_preview_formats_display_but_routes_original_text(bot):
    send, telegram, interpreter, handler = bot
    original = '**Deadline:** Friday 20 Nov 2026\n* Submit A&B <claim>'
    interpreter.return_value = original
    await send(PHOTO)
    preview = next(call for call in telegram.send_message.call_args_list if call.kwargs.get('parse_mode') == 'HTML')
    assert '<b>Deadline:</b>' in preview.args[1]
    assert '• Submit A&amp;B &lt;claim&gt;' in preview.args[1]
    handler.assert_not_awaited()
    await send({'text': 'use media'})
    assert handler.call_args.args[1] == 'add event'
