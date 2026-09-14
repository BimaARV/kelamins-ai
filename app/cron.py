"""Cron / reminder scheduling for the Telegram bot.

Parses free-text cron/reminder requests (e.g. "Buatin gua cron buat hari ini
aja, buat ingetin waktu pulang. Di jam 23.00"), computes the next WIB run time
and stores the job in Redis (key ``bot:cron:jobs``).

Jobs are plain dicts:
  {
    "id": "a1b2c3",
    "message": "waktu pulang",
    "next_run": <epoch float>,      # aware WIB instant
    "repeat": "once" | "daily" | "weekly",
    "weekday": int | None,          # Python weekday() (Monday=0), weekly only
    "chat_id": str,
    "created_at": <epoch float>,
  }

Redis is optional at runtime — mirrors app/cache.get_redis() behaviour.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from app.interfaces.formatter import bold, esc, mono

logger = logging.getLogger(__name__)

CRON_KEY = "bot:cron:jobs"
TZ = ZoneInfo("Asia/Jakarta")
MAX_JOBS = 50

_DATE_LABELS = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
_WEEKDAYS = {
    "senin": 0, "selasa": 1, "rabu": 2, "kamis": 3,
    "jumat": 4, "jum'at": 4, "sabtu": 5, "minggu": 6,
}

_INTENT_RE = re.compile(
    r"\b(n?ingatin|n?ingetin|n?ingatkan|remind\w*|reminder\w*|pengingat\w*|cron)\b|jangan\s+lupa|jangan\s+lupain",
    re.I,
)
# Teks yang menyebut infrastruktur/script → biarkan AI jawab (edit pertanyaan cron Unix)
_AI_WORDS_RE = re.compile(
    r"\b(bash|script|nginx|server|vps|docker|kubernetes|crontab|expression|command|syntax|restart|shutdown)\b",
    re.I,
)

_TIME_CLOCK_RE = re.compile(r"\b(?:jam|pukul|at)\s+(\d{1,2})(?:[.:]\s*(\d{2}))?")
_TIME_DIGIT_RE = re.compile(r"\b(\d{1,2})[.:](\d{2})\b")
_TIME_WORD_RE = re.compile(r"\btengah\s+malam\b|\bsubuh\b|\bpagi\b|\bsiang\b|\bsore\b|\bmalam\b", re.I)
_TIME_WORD_MAP = {
    "tengah malam": (0, 0),
    "subuh": (4, 30),
    "pagi": (6, 0),
    "siang": (12, 0),
    "sore": (17, 0),
    "malam": (20, 0),
}
_TIME_NIGHT_RE = re.compile(r"\b(malam|sore)\b", re.I)

_DAY_DAILY_RE = re.compile(r"\btiap\s+hari\b|\bsetiap\s+hari\b|\bharian\b|\btiap\s+hari\s+aja\b", re.I)
_DAY_WEEKLY_RE = re.compile(
    r"\b(?:tiap|setiap)\s+(senin|selasa|rabu|kamis|jumat|jum'at|sabtu|minggu)\b", re.I
)
_DAY_TOMORROW_RE = re.compile(r"\bbesok\b", re.I)
_DAY_TODAY_SPAN_RE = re.compile(r"\b(?:tiap|setiap)\s+hari\s+aja\b|\bbesok\b|\bhari\s+ini\b", re.I)

_CANCEL_RE = re.compile(r"\b(hapus|hapusin|cancel|batal\w*|delete)\s+cron\w*\s+([a-z0-9]{2,12})\b", re.I)
_LIST_RE = re.compile(r"^\s*(?:list|lihat|daftar|show)?\s*crons?\s*$", re.I)

_INTENT_WORDS = [
    "cron", "pengingat", "pengingatan", "reminder", "remind", "remind me",
    "ngingatin", "ngingetin", "ngingatkan", "ingatin", "ingetin", "ingatkan", "ingetkan", "ingat",
]
_FILLER_WORDS = [
    "buatin", "buatkan", "bikinin", "bikin", "buat", "tolong", "tolongin",
    "gua", "gue", "gw", "lu", "elo", "aku", "saya", "minta", "mau", "pengen", "pingin",
    "aja", "dong", "deh", "yah", "ya", "sama", "dan", "di", "pada", "untuk", "same",
    "please", "at", "tepat", "sekitar", "kalau", "kalo",
    "jam", "pukul", "besok", "hari", "ini", "tiap", "setiap", "harian", "besoknya",
    "tengah", "malam", "subuh", "pagi", "siang", "sore",
    "senin", "selasa", "rabu", "kamis", "jumat", "jum'at", "sabtu", "minggu",
    "jangan", "lupa", "lupain", "lupakan", "silahkan", "dih", "biar", "supaya", "nanti", "ntar", "kapan-kapan",
]


@dataclass
class CronResult:
    kind: str  # "ok" | "no_time" | "past" | "not_cron"
    job: dict | None = None
    hint: str = ""


def _local_now() -> datetime:
    return datetime.now(TZ)


def compute_next_run(day: str, weekday: int | None, hour: int, minute: int, now: datetime) -> datetime:
    """Next occurrence for a day spec, always in the future for daily/weekly."""
    today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if day == "tomorrow":
        return today + timedelta(days=1)
    if day == "daily":
        return today if today > now else today + timedelta(days=1)
    if day == "weekly" and weekday is not None:
        delta = (weekday - now.weekday()) % 7
        cand = today + timedelta(days=delta)
        if cand <= now:
            cand += timedelta(days=7)
        return cand
    # today (one-shot)
    return today


def _next_after(ts: float, repeat: str) -> datetime:
    base = datetime.fromtimestamp(ts, TZ)
    if repeat == "daily":
        return base + timedelta(days=1)
    if repeat == "weekly":
        return base + timedelta(days=7)
    return base


def _extract_time(low: str) -> tuple[int, int] | None:
    m = _TIME_CLOCK_RE.search(low)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if minute >= 60:
            return None
        if _TIME_NIGHT_RE.search(low):
            if hour == 12:
                hour = 0
            elif hour < 12:
                hour += 12
        return hour, minute
    m = _TIME_DIGIT_RE.search(low)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour >= 24 or minute >= 60:
            return None
        if _TIME_NIGHT_RE.search(low) and hour < 12:
            hour += 12
        return hour, minute
    m = _TIME_WORD_RE.search(low)
    if m:
        return _TIME_WORD_MAP.get(m.group(0).lower())
    return None


def _extract_day(low: str) -> tuple[str, int | None]:
    if _DAY_DAILY_RE.search(low):
        return "daily", None
    m = _DAY_WEEKLY_RE.search(low)
    if m:
        return "weekly", _WEEKDAYS[m.group(1).lower()]
    if _DAY_TOMORROW_RE.search(low):
        return "tomorrow", None
    return "today", None


def _extract_message(text: str) -> str:
    msg = text
    m = _TIME_CLOCK_RE.search(text)
    if m:
        msg = msg[: m.start()] + " " + msg[m.end():]
    m = _TIME_DIGIT_RE.search(msg)
    if m:
        msg = msg[: m.start()] + " " + msg[m.end():]
    m = _DAY_TODAY_SPAN_RE.search(msg)
    if m:
        msg = msg[: m.start()] + " " + msg[m.end():]
    for word in _INTENT_WORDS + _FILLER_WORDS:
        msg = re.sub(rf"\b{re.escape(word)}\b", " ", msg, flags=re.I)
    msg = re.sub(r"\s+", " ", msg)
    msg = msg.strip(" .,;:!?·—–-")
    msg = msg.strip()
    return msg or "Pengingat dari KELA"


def _schedule_label(day: str, weekday: int | None) -> str:
    if day == "daily":
        return "Tiap hari"
    if day == "weekly" and weekday is not None:
        return f"Tiap {_DATE_LABELS[weekday]}"
    if day == "tomorrow":
        return "Besok (sekali)"
    return "Hari ini (sekali)"


def build_cron_from_text(text: str, chat_id: str, now: datetime | None = None) -> CronResult:
    """Parse a free-text cron/reminder request.

    Returns:
      - ``ok``      → a job ready to be stored
      - ``no_time`` → cron intent present but no hour mentioned
      - ``past``    → one-shot "hari ini" with a time that already passed
      - ``not_cron``→ not a cron request at all (let the AI answer)
    """
    low = text.strip().lower()
    if not low or not _INTENT_RE.search(low) or _AI_WORDS_RE.search(low):
        return CronResult("not_cron")

    hh_mm = _extract_time(low)
    if hh_mm is None:
        return CronResult(
            "no_time",
            hint=(
                f"{bold('Cron')} — jamnya jam berapa? Contoh: 'ingetin pulang jam 17.00' "
                f"atau 'cron besok jam 8 pagi buat sikat gigi'."
            ),
        )
    hour, minute = hh_mm
    day, weekday = _extract_day(low)

    now = now or _local_now()
    next_run = compute_next_run(day, weekday, hour, minute, now)
    if day == "today" and next_run <= now:
        return CronResult(
            "past",
            hint=(
                "Jam itu udah lewat buat hari ini. Mau dijadwalin besok? "
                "Contoh: 'ingetin waktu pulang besok jam 23.00'."
            ),
        )

    repeat = "daily" if day == "daily" else "weekly" if day == "weekly" else "once"
    job = {
        "id": uuid.uuid4().hex[:6],
        "message": _extract_message(text),
        "next_run": next_run.timestamp(),
        "repeat": repeat,
        "weekday": weekday,
        "chat_id": str(chat_id),
        "created_at": time.time(),
    }
    job["schedule_label"] = _schedule_label(day, weekday)
    return CronResult("ok", job=job)


def cron_manage_request(text: str) -> tuple[str, str]:
    """Detect list/cancel requests in free text: (action, job_id)."""
    m = _CANCEL_RE.search(text)
    if m:
        return "cancel", m.group(2)
    if _LIST_RE.search(text):
        return "list", ""
    return "", ""


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_wib(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, TZ).strftime("%A, %d %B %Y, %H:%M WIB")


def _fmt_repeat(job: dict) -> str:
    if job.get("repeat") == "daily":
        return "Tiap hari"
    if job.get("repeat") == "weekly" and job.get("weekday") is not None:
        return f"Tiap {_DATE_LABELS[job['weekday']]}"
    return "Sekali"


_ACK_OPENERS = [
    "Oke, cron lu udah gua catet, bro.",
    "Beres — cron lu udah gua simpen, santuy aja.",
    "Sip, gua catet ya — nanti gua ingetin.",
    "Oke aman, cron udah kejadwal. Deal.",
    "Udah gua tandain, bro. Pantengin chat aja nanti.",
]


def format_ack(job: dict) -> str:
    return (
        f"{random.choice(_ACK_OPENERS)}\n\n"
        f"{bold('ID')}: {mono(job['id'])}\n"
        f"{bold('Pesan')}: {esc(job['message'])}\n"
        f"{bold('Jadwal')}: {_fmt_repeat(job)} · {_fmt_wib(job['next_run'])}\n"
        f"Batal: {mono('/cron cancel ' + job['id'])} (atau ketik 'hapus cron {esc('<id>')}')."
    )


def format_reminder(job: dict) -> str:
    return f"{bold('Pengingat')} — {esc(job['message'])}"


def format_jobs_list(jobs: list[dict]) -> str:
    if not jobs:
        return "Belum ada cron aktif. Ketik sesuatu kayak 'ingetin pulang jam 17.00'."
    ordered = sorted(jobs, key=lambda j: j.get("next_run", 0))
    lines = [f"{bold('Cron aktif')} ({len(ordered)})"]
    for i, j in enumerate(ordered, 1):
        lines.append(
            f"{i}. {mono(j['id'])} · {esc(j['message'])} — {_fmt_repeat(j)} · {_fmt_wib(j['next_run'])}"
        )
    lines.append(f"\nBatal: {mono('/cron cancel <id>')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Storage (Redis, optional)
# ---------------------------------------------------------------------------

def _get_redis():
    from app.cache import get_redis

    return get_redis()


async def list_jobs() -> list[dict]:
    client = _get_redis()
    if client is None:
        return []
    try:
        raw = await client.get(CRON_KEY)
        if not raw:
            return []
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        logger.warning("cron: redis unavailable (read)", exc_info=True)
        return []


async def save_jobs(jobs: list[dict]) -> bool:
    client = _get_redis()
    if client is None:
        return False
    try:
        await client.set(CRON_KEY, json.dumps(jobs))
        return True
    except Exception:
        logger.warning("cron: redis unavailable (write)", exc_info=True)
        return False


async def add_job(job: dict) -> str:
    """Persist a new job. Returns 'ok' | 'full' | 'redis_unavailable'."""
    jobs = await list_jobs()
    if len(jobs) >= MAX_JOBS:
        return "full"
    jobs.append(job)
    return "ok" if await save_jobs(jobs) else "redis_unavailable"


async def delete_job(job_id: str) -> bool:
    jobs = [j for j in await list_jobs() if j.get("id") != job_id]
    current = await list_jobs()
    if len(jobs) == len(current):
        return False
    return await save_jobs(jobs)


async def fire_due_crons(send: Callable[[str, str], Awaitable[None]]) -> int:
    """Fire due reminders. Returns how many reminders were sent."""
    jobs = await list_jobs()
    now = time.time()
    due = [j for j in jobs if j.get("next_run", 0) <= now]
    if not due:
        return 0
    remaining = [j for j in jobs if j.get("next_run", 0) > now]
    for j in due:
        try:
            await send(str(j.get("chat_id", "")), format_reminder(j))
        except Exception:
            logger.exception("cron: reminder gagal dikirim untuk %s", j.get("id"))
            remaining.append(j)
            continue
        if j.get("repeat") in ("daily", "weekly"):
            j["next_run"] = _next_after(j["next_run"], j.get("repeat", "once")).timestamp()
            remaining.append(j)
    await save_jobs(remaining[:MAX_JOBS])
    return len(due)