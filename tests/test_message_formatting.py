import json

import httpx
import pytest

from app.message_formatting import format_media_preview
from app.telegram_client import TelegramClient


def test_photo_preview_renders_labels_headings_bullets_and_spacing():
    result = format_media_preview(
        '## Email Content\n\n* **Subject:** SoC Claims Hub\n* **Deadline:** Friday, 20 Nov 2026, 11:59pm\n  * Claim `CS3227`', photo=True,
    )
    assert '<b>I read:</b>\n\n<b>Email Content</b>\n\n' in result
    assert '• <b>Subject:</b> SoC Claims Hub' in result
    assert '• <b>Deadline:</b> Friday, 20 Nov 2026, 11:59pm' in result
    assert '  • Claim <code>CS3227</code>' in result
    assert '**' not in result
    assert '\nReply <code>use media</code>' in result


def test_preview_escapes_untrusted_html_and_preserves_punctuation():
    result = format_media_preview('**A&B <script>**\n- 2 < 3 & 4 > 1\nUnmatched ** and _literal_\n<b>unsafe</b>', photo=True)
    assert '<b>A&amp;B &lt;script&gt;</b>' in result
    assert '• 2 &lt; 3 &amp; 4 &gt; 1' in result
    assert 'Unmatched ** and _literal_' in result
    assert '&lt;b&gt;unsafe&lt;/b&gt;' in result
    assert '<script>' not in result


def test_voice_transcript_is_verbatim_instead_of_markdown():
    result = format_media_preview('**Important**\n* Buy A&B <today>\n`literal`', photo=False)
    assert '**Important**\n* Buy A&amp;B &lt;today&gt;\n`literal`' in result


@pytest.mark.asyncio
@pytest.mark.parametrize('parse_mode', [None, 'HTML'])
async def test_telegram_parse_mode_is_opt_in_and_keeps_buttons(monkeypatch, parse_mode):
    received = []

    def respond(request):
        received.append(json.loads(request.content))
        return httpx.Response(200, json={'ok': True})

    original = httpx.AsyncClient
    monkeypatch.setattr('app.telegram_client.httpx.AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    markup = {'inline_keyboard': [[{'text': 'Cancel', 'callback_data': 'cancel'}]]}
    await TelegramClient('fake').send_message(111, 'A&B <text>', markup, parse_mode=parse_mode)
    assert received[0]['text'] == 'A&B <text>'
    assert received[0]['reply_markup'] == markup
    if parse_mode:
        assert received[0]['parse_mode'] == 'HTML'
    else:
        assert 'parse_mode' not in received[0]


def test_heading_does_not_nest_code_or_bold_entities():
    result = format_media_preview('### **Claim** `CS3227` & details', photo=True)
    assert '<b>Claim CS3227 &amp; details</b>' in result
    assert '<b><b>' not in result
    assert '<b>Claim <code>' not in result
