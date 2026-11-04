"""Render the small Markdown subset used in image previews as safe Telegram HTML."""
from __future__ import annotations

import re
from html import escape

INLINE_MARKUP = re.compile(r"\*\*([^\n]+?)\*\*|`([^`\n]+)`")


def _inline(text: str) -> str:
    parts = []
    end = 0
    for match in INLINE_MARKUP.finditer(text):
        parts.append(escape(text[end:match.start()]))
        tag, value = ("b", match[1]) if match[1] is not None else ("code", match[2])
        parts.append(f"<{tag}>{escape(value)}</{tag}>")
        end = match.end()
    parts.append(escape(text[end:]))
    return "".join(parts)


def format_media_preview(text: str, *, photo: bool) -> str:
    if photo:
        lines = []
        for line in text.splitlines():
            heading = re.match(r"^#{1,6}\s+(.+)$", line)
            if heading:
                # Telegram does not allow code entities nested inside bold.
                title = INLINE_MARKUP.sub(lambda match: match[1] if match[1] is not None else match[2], heading[1])
                lines.append(f"<b>{escape(title)}</b>")
            else:
                line = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", line)
                lines.append(_inline(line))
        body = "\n".join(lines)
    else:
        body = escape(text)
    return (
        f"<b>I read:</b>\n\n{body}\n\n"
        "Check the details.\n"
        "Reply <code>use media</code> within 5 minutes to process this as a request.\n"
        "Send corrected text or /cancel."
    )
