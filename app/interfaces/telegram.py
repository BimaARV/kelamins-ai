"""Interactive Telegram bot — long-polling mode.

Run standalone: ``python -m app.interfaces.telegram``

Env vars required (from ``app.config``):
  TELEGRAM_BOT_TOKEN  — bot API token (BotFather)
  TELEGRAM_CHAT_ID    — only respond to this chat

Responses are HTML-formatted. AI chat is gated on ``OLLAMA_API_KEY_*`` presence.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import sys
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import settings
from app.interfaces.formatter import bold, chunk_html, esc, md_to_html, mono
from app.monitoring import detect_monitor_start, detect_monitor_stop
from app.shell import format_server_command, parse_server_request, run_network_cmd
from app.webscrape import detect_scrape_request, format_scrape, scrape_url
from app.whois import (
    extract_domain,
    extract_ip,
    format_whois,
    format_whois_domain,
    is_public_ip,
    rdap_domain_lookup,
    rdap_lookup,
)

TELEGRAM_API = "https://api.telegram.org"
TOKEN: str = settings.telegram_bot_token or ""
CHAT_ID: str = settings.telegram_chat_id or ""
POLL_INTERVAL: float = settings.telegram_poll_interval_seconds
NEWS_PUSH_INTERVAL: float = settings.telegram_news_push_interval_seconds
NEWS_PUSH_COUNT: int = settings.telegram_news_push_count
NEWS_PUSH_KEY = "bot:last_news_push"
DIGEST_HOUR: int = settings.telegram_digest_hour
DIGEST_KEY = "bot:last_digest"
CRON_POLL_INTERVAL: float = settings.cron_poll_interval_seconds

logger = logging.getLogger("kela.telegram")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# Persistent HTTP client reused across calls in the poll loop
_http: httpx.AsyncClient | None = None


def _get_http() -> httpx.AsyncClient:
    global _http  # noqa: PLW0603
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(timeout=30.0)
    return _http


# ---------------------------------------------------------------------------
# Telegram Bot API helpers
# ---------------------------------------------------------------------------

async def _call_api(method: str, payload: dict[str, Any] | None = None) -> dict:
    url = f"{TELEGRAM_API}/bot{TOKEN}/{method}"
    client = _get_http()
    if payload:
        resp = await client.post(url, json=payload)
    else:
        resp = await client.get(url)
    data = resp.json()
    if resp.status_code != 200 or not data.get("ok"):
        desc = data.get("description") or f"http {resp.status_code}"
        raise RuntimeError(f"Telegram API {method} failed: {desc}")
    return data


async def send_message(
    chat_id: str,
    text: str,
    reply_to: int | None = None,
    markup: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_to is not None:
        payload["reply_to_message_id"] = reply_to
    if markup is not None:
        payload["reply_markup"] = markup
    try:
        await _call_api("sendMessage", payload)
    except Exception:
        logger.exception("sendMessage failed")


def main_keyboard() -> dict[str, Any]:
    return {
        "keyboard": [
            ["/news", "/gempa"],
            ["/weather", "/status"],
            ["/help"],
        ],
        "resize_keyboard": True,
    }


async def send_document(chat_id: str, path: str, caption: str | None = None) -> bool:
    """Upload a local file as a Telegram document."""
    if not os.path.isfile(path):
        logger.warning("sendDocument skipped — file not found: %s", path)
        return False
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    try:
        with open(path, "rb") as fh:
            resp = await _get_http().post(
                f"{TELEGRAM_API}/bot{TOKEN}/sendDocument",
                data=data,
                files={"document": (os.path.basename(path), fh)},
            )
        result = resp.json()
        if resp.status_code != 200 or not result.get("ok"):
            logger.warning("sendDocument failed: %s", result.get("description"))
            return False
        return True
    except Exception:
        logger.exception("sendDocument failed")
        return False


async def send_typing(chat_id: str) -> None:
    try:
        await _call_api("sendChatAction", {"chat_id": chat_id, "action": "typing"})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Incoming message handling
# ---------------------------------------------------------------------------

def _allowed(update: dict) -> bool:
    if not CHAT_ID:
        return True
    msg = update.get("message") or update.get("edited_message") or {}
    from_id = str(msg.get("from", {}).get("id", ""))
    chat_obj = msg.get("chat", {})
    chat_tid = str(chat_obj.get("id", ""))
    return from_id == CHAT_ID or chat_tid == CHAT_ID


async def _dispatch_text(text: str, chat_id: str, message_id: int) -> None:
    from app.db.session import session_factory
    from app.interfaces.commands import dispatch

    if text.startswith("/"):
        async with session_factory() as session:
            reply = await dispatch(text, session)
        markup = (
            main_keyboard()
            if text.lower().strip() in ("/start", "/help")
            else None
        )
        await send_message(chat_id, reply, reply_to=message_id, markup=markup)
        return

    await _reply_ai(text, chat_id, message_id)


async def _reply_ai(text: str, chat_id: str, message_id: int) -> None:
    """Route free-text in order: shell → monitor → memory → scrape → whois → cron → AI."""
    shell_args = parse_server_request(text)
    if shell_args is not None:
        await _handle_server_cmd(shell_args, chat_id, message_id)
        return

    stop_target = detect_monitor_stop(text)
    mon_target = None if stop_target is not None else detect_monitor_start(text)
    if stop_target is not None or mon_target is not None:
        from app.db.session import session_factory
        from app.interfaces.commands import start_monitoring, stop_monitoring

        async with session_factory() as session:
            if stop_target is not None:
                reply = await stop_monitoring(session, stop_target)
            else:
                reply = await start_monitoring(session, mon_target or "")
        await send_message(chat_id, reply, reply_to=message_id)
        return

    mem_req = _detect_memory_request(text)
    if mem_req is not None:
        await _handle_remember(mem_req, chat_id, message_id)
        return

    scrape_target = detect_scrape_request(text)
    if scrape_target is not None:
        await _handle_scrape(scrape_target, chat_id, message_id)
        return

    whois_target = _detect_whois_request(text)
    if whois_target is not None:
        await _handle_whois(whois_target, chat_id, message_id)
        return

    if await _handle_cron_request(text, chat_id, message_id):
        return

    from app.db.session import session_factory
    from app.interfaces.context import build_situation_block, get_history, push_history
    from app.kela_ai.gateway import AIUnavailable, build_gateway
    from sqlalchemy import func, select
    from app.db.models import Article, Event, Earthquake, Alert

    async with session_factory() as session:
        counts = {}
        for model, label in [
            (Article, "articles"),
            (Event, "events"),
            (Earthquake, "earthquakes"),
            (Alert, "alerts"),
        ]:
            counts[label] = (await session.execute(select(func.count(model.id)))).scalar() or 0
        situation = await build_situation_block(session)

    gw = build_gateway(settings)
    if not gw.configured:
        await send_message(
            chat_id,
            "AI belum aktif — belum ada OLLAMA_API_KEY diisi.\n"
            "Gunakan perintah: /help",
            reply_to=message_id,
        )
        return

    now = datetime.now(ZoneInfo("Asia/Jakarta"))
    system = (
        "Kamu KELA, assistant intelligence dari The KELAMINS Project — 'Knowledge-driven "
        "Engineering for Layered Architecture, Modular Infrastructure, Networking & Systems'. "
        "Lo paham networking, coding, arsitektur sistem, Data Center, NAP, ISP, polow "
        "monitoring — jawab apa aja dengan cara berpikir sistematis, selama basisnya fakta. "
        "Vibe Brooklyn yang udah dijakartain: dominan Bahasa Indonesia gaul — santuy, "
        "nyablak, nggak kaku, bukan kayak customer service. Sesekali selip slang Inggris "
        "santai kayak 'no cap', 'aight', 'straight up'. Komposisi bahasa: ±70% Indonesia, "
        "±30% Inggris — jangan sampe 90% Inggris. Nggak usah kasar banget; kalau ada yang "
        "salah, tegur santai tapi tetep fakta-first, akurat, dan nyebut sumber. JANGAN pernah "
        "pakai notasi LaTeX (\\$, \\rightarrow, \\frac, dst) — pakai "
        "panah '→' atau kata 'ke'/'menuju'; jawaban panjang pecah per poin dengan **teks "
        "tebal** buat sub-judul dan bullet list biar kebaca. "
        f"Sekarang {now:%A, %d %B %Y, %H:%M} WIB (Asia/Jakarta) — pakai ini kalau "
        f"ditanya tanggal/jam, JANGAN nebak tahun.\n"
        f"Dalam sistem: {counts.get('events',0)} events, "
        f"{counts.get('articles',0)} articles, "
        f"{counts.get('earthquakes',0)} gempa, "
        f"{counts.get('alerts',0)} alerts."
    )
    if situation:
        system += f"\n\nKONDISI SEKARANG (pakai fakta ini, jangan ngarang):\n{situation}"
    history = await get_history(chat_id)
    messages = [
        {"role": "system", "content": system},
        *history,
        {"role": "user", "content": text},
    ]
    try:
        result = await gw.complete(messages, max_tokens=settings.ai_chat_max_tokens)
        content = result.content or ""
        parts = list(chunk_html(md_to_html(content)))
        for i, part in enumerate(parts):
            await send_message(chat_id, part, reply_to=message_id if i == 0 else None)
        if parts:
            await push_history(chat_id, text, " ".join(parts))
        await _maybe_send_file(text, content, chat_id)
    except AIUnavailable as exc:
        await send_message(
            chat_id,
            f"AI tidak tersedia ({exc}). Gunakan /help untuk perintah.",
            reply_to=message_id,
        )
    except Exception as exc:
        logger.exception("AI gateway error")
        await send_message(
            chat_id,
            f"Terjadi error di AI gateway: {str(exc)[:200]}",
            reply_to=message_id,
        )


# ---------------------------------------------------------------------------
# Jarvis memory (free-text: "inget/catat/...", route BEFORE whois so short
# phrases containing "ip/domain" are never hijacked into a WHOIS lookup)
# ---------------------------------------------------------------------------

_MEMORY_STORE_RE = re.compile(
    r"^(inget|ingat|catat|hafal|simpen|simpan|remember|note)\b", re.IGNORECASE
)


def _detect_memory_request(text: str) -> str | None:
    m = _MEMORY_STORE_RE.search((text or "").strip())
    if not m:
        return None
    rest = (text or "").strip()[m.end():].strip(" .:,;-\t")
    return rest if rest else None


async def _handle_remember(content: str, chat_id: str, message_id: int) -> None:
    from app.db.session import session_factory
    from app.memory import remember

    async with session_factory() as session:
        mem = await remember(session, content, kind="note", source="chat")
    if mem is None:
        await send_message(
            chat_id, "Itu udah gua catet sebelumnya, bro.", reply_to=message_id
        )
        return
    await send_message(
        chat_id,
        f"{bold('Dicatet')} — {esc(content[:200])}\nLihat semua: /memory",
        reply_to=message_id,
    )


# ---------------------------------------------------------------------------
# On-demand web scraping from free text (no slash command needed)
# ---------------------------------------------------------------------------

async def _handle_scrape(url: str, chat_id: str, message_id: int) -> None:
    from app.db.session import session_factory
    from app.memory import remember

    await send_typing(chat_id)
    data = await scrape_url(url)
    await send_message(chat_id, format_scrape(data), reply_to=message_id)
    if not data.get("error"):
        try:
            async with session_factory() as session:
                await remember(session, f"scrape {url}", kind="action", source="chat")
        except Exception:  # noqa: BLE001
            logger.debug("scrape memory skip", exc_info=True)


async def _maybe_send_file(text: str, content: str, chat_id: str) -> None:
    """Generate and upload a file when the user asked for pdf/word/excel/etc."""
    fmt = _detect_file_request(text)
    if fmt is None:
        return
    try:
        from app.config import settings
        from app.documents.generate import render_document

        title = text.strip().splitlines()[0][:60]
        path = render_document(
            content, fmt, title=title, output_dir=settings.report_storage_path
        )
        label = {"pdf": "PDF", "docx": "Word", "xlsx": "Excel",
                 "txt": "TXT", "md": "Markdown", "png": "PNG"}.get(fmt, fmt.upper())
        await send_document(chat_id, path, caption=f"Nih file {label}-nya, kampret.")
    except Exception:
        logger.exception("file generation failed")


# ---------------------------------------------------------------------------
# File-request detection (auto-generate pdf/word/excel/md/txt/png from chat)
# ---------------------------------------------------------------------------

_FILE_FORMAT_PATTERNS: list[tuple[str, str]] = [
    ("pdf", r"\b(pdf|laporan+pdf)\b"),
    ("docx", r"\b(word|docx|dokumen+word)\b"),
    ("xlsx", r"\b(excel|xlsx)\b"),
    ("txt", r"\b(txt|teks polos|file+txt)\b"),
    ("md", r"\b(markdown|file+md|\.md)\b"),
    ("png", r"\b(png|gambar|image|kartu+png)\b"),
]


def _detect_file_request(text: str) -> str | None:
    """Return the file format the user asked for, or None."""
    low = text.lower()
    for fmt, pattern in _FILE_FORMAT_PATTERNS:
        if re.search(pattern, low):
            return fmt
    return None


async def _handle_server_cmd(args: list[str], chat_id: str, message_id: int) -> None:
    result = await run_network_cmd(args)
    await send_message(
        chat_id,
        format_server_command(args, result),
        reply_to=message_id,
    )


_WHOIS_MAX_TOKENS = 6
_WHOIS_QUESTION_RE = re.compile(
    r"\b(gimana|kenapa|kok|kapan|apa|berapa|cara|sering|selalu|timeout|error)\b"
)


def _detect_whois_request(text: str) -> str | None:
    """Return an IPv4/domain when the user clearly asks for a WHOIS lookup."""
    low = text.lower().strip()
    ip = extract_ip(text)
    target = ip if (ip is not None and is_public_ip(ip)) else extract_domain(text)
    if target is None:
        return None
    if "whois" in low or low == target:
        return target
    tokens = low.split()
    if (
        len(tokens) <= _WHOIS_MAX_TOKENS
        and not _WHOIS_QUESTION_RE.search(low)
        and re.search(r"\b(cek|ip|domain)\b", low)
    ):
        return target
    return None


async def _handle_whois(target: str, chat_id: str, message_id: int) -> None:
    ip = extract_ip(target)
    if ip is not None:
        info = await rdap_lookup(ip)
        if info is None:
            reply = f"{bold('WHOIS')} — {mono(esc(ip))} tidak ditemukan di RDAP."
        elif info.get("error"):
            reply = f"{bold('WHOIS')} — {esc(info['error'])}"
        else:
            reply = format_whois(ip, info)
    else:
        domain = extract_domain(target) or target
        info = await rdap_domain_lookup(domain)
        if info is None:
            reply = f"{bold('WHOIS')} — {mono(esc(domain))} tidak ditemukan di RDAP."
        elif info.get("error"):
            reply = f"{bold('WHOIS')} — {esc(info['error'])}"
        else:
            reply = format_whois_domain(target, info)
    await send_message(chat_id, reply, reply_to=message_id)


# ---------------------------------------------------------------------------
# Cron / reminder (buat jadwal dari chat, mis. "ingetin pulang jam 17.00")
# ---------------------------------------------------------------------------

async def _handle_cron_request(text: str, chat_id: str, message_id: int) -> bool:
    """Handle list/cancel/create cron. Returns True when the message was consumed."""
    from app.cron import (
        add_job,
        build_cron_from_text,
        cron_manage_request,
        delete_job,
        format_ack,
        format_jobs_list,
        list_jobs,
    )

    action, job_id = cron_manage_request(text)
    if action == "list":
        await send_message(chat_id, format_jobs_list(await list_jobs()), reply_to=message_id)
        return True
    if action == "cancel":
        if await delete_job(job_id):
            await send_message(
                chat_id,
                f"{bold('Cron dihapus')} — {mono(esc(job_id))}.",
                reply_to=message_id,
            )
        else:
            await send_message(
                chat_id,
                f"{bold('?')} Cron {mono(esc(job_id))} nggak ketemu.",
                reply_to=message_id,
            )
        return True

    res = build_cron_from_text(text, str(chat_id))
    if res.kind == "not_cron":
        return False
    if res.kind == "no_time":
        await send_message(chat_id, res.hint, reply_to=message_id)
        return True
    if res.kind == "past":
        await send_message(chat_id, res.hint, reply_to=message_id)
        return True

    status = await add_job(res.job)  # type: ignore[arg-type]
    if status == "ok":
        await send_message(chat_id, format_ack(res.job), reply_to=message_id)  # type: ignore[arg-type]
    elif status == "full":
        await send_message(
            chat_id,
            f"Cron udah penuh (max 50). Hapus yang lama lewat {mono('/cron cancel <id>')}.",
            reply_to=message_id,
        )
    else:
        await send_message(
            chat_id,
            "Redis lagi ngadat, cron nggak kesimpen. Coba lagi nanti.",
            reply_to=message_id,
        )
    return True


# ---------------------------------------------------------------------------
# News auto-push (1 digest per news_push_interval, NEWS_PUSH_COUNT items)
# ---------------------------------------------------------------------------

_last_news_push_ts: float = 0.0


def _news_push_due(now: float, last_push: float, interval: float) -> bool:
    return last_push <= 0 or now - last_push >= interval


async def _get_last_news_push() -> float:
    global _last_news_push_ts  # noqa: PLW0603
    from app.cache import get_redis

    client = get_redis()
    if client is not None:
        try:
            raw = await client.get(NEWS_PUSH_KEY)
            if raw:
                _last_news_push_ts = float(raw)
        except Exception:
            logger.warning("news push: gagal baca redis, pakai memori", exc_info=True)
    return _last_news_push_ts


async def _set_last_news_push(ts: float) -> None:
    global _last_news_push_ts  # noqa: PLW0603
    _last_news_push_ts = ts
    from app.cache import get_redis

    client = get_redis()
    if client is not None:
        try:
            ttl = max(int(NEWS_PUSH_INTERVAL) * 2, 3600)
            await client.set(NEWS_PUSH_KEY, str(ts), ex=ttl)
        except Exception:
            logger.warning("news push: redis unavailable, pakai in-memory", exc_info=True)


_PUSH_OPENERS = [
    "Bro, update berita nih:",
    "Eh, ini yang lagi happening:",
    "No cap, berita terbaru buat lo:",
    "Santai dulu, nih beritanya:",
]


async def _push_latest_news() -> None:
    from app.db.session import session_factory
    from app.interfaces.commands import _render_news

    async with session_factory() as session:
        body = await _render_news(
            session,
            title="Berita terbaru (auto)",
            tech_only=False,
            limit=NEWS_PUSH_COUNT,
        )
    opener = random.choice(_PUSH_OPENERS)
    footer = "\n\nMau berita lain? Ketik /news, /news-tech, atau /news-sport."
    await send_message(CHAT_ID, f"{opener}\n\n{body}{footer}")


async def news_push_loop() -> None:
    if not TOKEN or not CHAT_ID:
        logger.info("news push: TELEGRAM_CHAT_ID belum diisi — skip auto-push.")
        return
    last = await _get_last_news_push()
    if last <= 0:
        await _set_last_news_push(time.time())
    while True:
        now = time.time()
        try:
            last = await _get_last_news_push()
            if _news_push_due(now, last, NEWS_PUSH_INTERVAL):
                logger.info("news push due — kirim %d berita", NEWS_PUSH_COUNT)
                await _push_latest_news()
                await _set_last_news_push(now)
        except Exception:
            logger.exception("news push gagal")
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Morning digest (1x per hari jam TELEGRAM_DIGEST_HOUR WIB)
# ---------------------------------------------------------------------------


def _digest_due(now_wib: datetime, last_date: str, hour: int) -> bool:
    return now_wib.hour == hour and now_wib.minute < 5 and last_date != now_wib.date().isoformat()


async def _get_digest_date() -> str:
    from app.cache import get_redis

    client = get_redis()
    if client is None:
        return ""
    try:
        raw = await client.get(DIGEST_KEY)
        return (raw or "").decode() if isinstance(raw, bytes) else (raw or "")
    except Exception:
        logger.warning("digest: redis unavailable", exc_info=True)
        return ""


async def _set_digest_date(value: str) -> None:
    from app.cache import get_redis

    client = get_redis()
    if client is None:
        return
    try:
        await client.set(DIGEST_KEY, value, ex=26 * 3600)
    except Exception:
        logger.warning("digest: redis unavailable", exc_info=True)


async def daily_digest_loop() -> None:
    if not TOKEN or not CHAT_ID:
        logger.info("daily digest: TELEGRAM_CHAT_ID belum diisi — skip.")
        return
    today = datetime.now(ZoneInfo("Asia/Jakarta")).date().isoformat()
    if not await _get_digest_date():
        await _set_digest_date(today)
    while True:
        try:
            now = datetime.now(ZoneInfo("Asia/Jakarta"))
            if _digest_due(now, await _get_digest_date(), DIGEST_HOUR):
                logger.info("daily digest due — kirim summary")
                from app.db.session import session_factory
                from app.interfaces.digest import build_digest

                async with session_factory() as session:
                    body = await build_digest(session)
                await send_message(CHAT_ID, body)
                await _set_digest_date(now.date().isoformat())
        except Exception:
            logger.exception("daily digest gagal")
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Cron loop (fire due reminders, reschedule recurring)
# ---------------------------------------------------------------------------

async def cron_loop() -> None:
    if not TOKEN or not CHAT_ID:
        logger.info("cron: TELEGRAM_CHAT_ID belum diisi — skip.")
        return
    await asyncio.sleep(CRON_POLL_INTERVAL)
    while True:
        try:
            from app.cron import fire_due_crons

            fired = await fire_due_crons(lambda cid, body: send_message(cid, body))
            if fired:
                logger.info("cron: %d reminder dikirim", fired)
        except Exception:
            logger.exception("cron loop gagal")
        await asyncio.sleep(CRON_POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Long-poll loop
# ---------------------------------------------------------------------------

async def poll_loop() -> None:
    if not TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN belum diisi — bot tidak bisa jalan.")
        return
    if not CHAT_ID:
        logger.warning(
            "TELEGRAM_CHAT_ID belum diisi — bot akan merespons semua pesan."
        )

    me = (await _call_api("getMe")).get("result", {})
    bot_name = me.get("first_name") or me.get("username") or "KELA"
    logger.info("Bot started as @%s — polling interval %.1fs", me.get("username"), POLL_INTERVAL)

    offset = 0
    backoff = 0.0
    while True:
        try:
            resp = await _call_api(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 10,
                    "allowed_updates": ["message"],
                },
            )
            updates = resp.get("result", [])
            backoff = 0.0
        except Exception:
            backoff = min(backoff * 2 + 1.0, 30.0)
            logger.warning("getUpdates failed — retrying in %.1fs", backoff, exc_info=True)
            await asyncio.sleep(backoff)
            continue

        for upd in updates:
            offset = max(offset, upd.get("update_id", 0) + 1)
            if not _allowed(upd):
                continue
            msg = upd.get("message", {})
            text = msg.get("text", "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))
            msg_id = msg.get("message_id")
            if not text or not chat_id:
                continue
            asyncio.create_task(_safe_handle(text, chat_id, msg_id))

        await asyncio.sleep(POLL_INTERVAL)


async def _safe_handle(text: str, chat_id: str, msg_id: int) -> None:
    try:
        await send_typing(chat_id)
        await _dispatch_text(text, chat_id, msg_id)
    except Exception:
        logger.exception("Unhandled error processing message: %s", text[:100])


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def main() -> None:
    try:
        await asyncio.gather(
            poll_loop(),
            news_push_loop(),
            daily_digest_loop(),
            cron_loop(),
        )
    finally:
        if _http and not _http.is_closed:
            await _http.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped.")
        sys.exit(0)