"""Lightweight output formatting for chat/command replies (Telegram HTML)."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def chunk_html(text: str, max_chars: int = 3950) -> list[str]:
    """Split HTML into Telegram-safe chunks (<= max_chars, tags balanced).

    Telegram caps a message at 4096 chars. `md_to_html` output only has one
    multi-line element — <pre> code blocks — so when a split must land inside
    a code block, we close </pre> on the last line of the previous chunk and
    reopen <pre> on the first line of the next one. Everything else (inline
    tags) stays on a single line and is never cut.
    """
    chunks: list[str] = []
    cur: list[str] = []
    in_pre = False

    for orig in text.split("\n"):
        line = orig
        if cur and len("\n".join(cur + [line])) > max_chars:
            if in_pre:
                cur[-1] = cur[-1] + "</pre>"
                chunks.append("\n".join(cur))
                cur = []
                line = "<pre>" + line
            else:
                chunks.append("\n".join(cur))
                cur = []
        if not in_pre and "<pre>" in line:
            in_pre = True
        elif in_pre and "</pre>" in line and "<pre>" not in line:
            in_pre = False
        cur.append(line)

    if cur:
        if in_pre and not cur[-1].endswith("</pre>"):
            cur[-1] = cur[-1] + "</pre>"
        chunks.append("\n".join(cur))
    return chunks or [text]


def md_to_html(text: str) -> str:
    """Convert a small markdown subset to Telegram HTML.

    Supports: headings (#), bold (**x**), italic (*x* / _x_), inline code
    (`x`), fenced code blocks (```...```), bullet lists (- / * / +), and
    hard line breaks. Input is escaped first so AI text can't inject raw
    HTML tags.
    """
    out = html.escape(str(text), quote=False)
    out = re.sub(
        r"```[a-zA-Z]*\n(.*?)```", lambda m: f"<pre>{m.group(1)}</pre>", out, flags=re.S
    )
    out = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)
    out = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", out)
    out = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", out)
    out = re.sub(r"(?m)^\s*#{1,6}\s+(.+?)\s*$", r"<b>\1</b>", out)
    out = re.sub(r"(?m)^\s*[-*+]\s+", "• ", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", out)
    out = re.sub(r"(?<![\w_])_([^_\n]+)_(?![\w_])", r"<i>\1</i>", out)
    return out


def bold(text: str) -> str:
    return f"<b>{esc(text)}</b>"


def mono(text: str) -> str:
    return f"<code>{esc(text)}</code>"


def dt(value) -> str:
    """Format a datetime/str into a compact 'YYYY-MM-DD HH:MM' UTC label."""
    if value is None:
        return "—"
    s = str(value).replace("T", " ").replace("+00:00", "").replace("Z", "")
    return s[:16]


def dt_wib(value) -> str:
    """Format a (usually naive-UTC) datetime as 'Kamis, 12 Sep 2026 06:19 WIB'."""
    if value is None:
        return "—"
    try:
        if isinstance(value, str):
            d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        else:
            d = value
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        local = d.astimezone(ZoneInfo("Asia/Jakarta"))
        return local.strftime("%A, %d %b %Y, %H:%M WIB")
    except (ValueError, TypeError, OverflowError):
        return str(value)[:16]


def num(value, digits: int | None = None) -> str:
    if value is None:
        return "?"
    return str(round(float(value), digits)) if digits is not None else str(value)