"""Chat-driven network monitoring with down/up Telegram alerts.

The scheduler owns the actual pinging (a single ping authority): ``_loop_network``
runs ``run_check`` → ``store_network_check`` for every enabled target. This
module only *decides* what to alert about:

  * detects monitoring requests in free text (public/private IP or domain),
  * upserts ``NetworkTarget`` rows so the scheduler picks them up,
  * keeps the last known status per target in Redis,
  * fires a Telegram alert ONLY on a real up ↔ down transition.

Redis keys (db 0):
  bot:monitor:ids                       — SET of target ids opted in from chat
  bot:monitor:status:<target_id>        — last raw status ("up"/"down"/"timeout"/"error")

The status key also acts as the opt-in marker for the scheduler. A scheduler
restart never causes an alert storm: the first pass just re-seeds the status.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import random
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.cache import get_redis
from app.config import settings
from app.db.models import NetworkTarget, TargetType
from app.interfaces.formatter import bold, esc, mono
from app.whois import extract_domain, extract_ip

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"

MONITOR_IDS_KEY = "bot:monitor:ids"
MONITOR_STATUS_KEY = "bot:monitor:status:{}"
MONITOR_STATE_TTL = 30 * 24 * 3600  # refreshed below from settings

_IP_LOOPBACK_TEST = re.compile(r"^127\.|^0\.|^255\.|^224\.|^169\.254\.")
_DOMAIN_RE = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}"
)

_MONITOR_INTENT = re.compile(
    r"("
    r"\b(?:monitor(?:ing|in|innya)?|memonitor|dimonitor(?:in)?"
    r"|pantau(?:in|innya)?|dipantau(?:in)?|awasi(?:in|n)?|amatin|intip|watch|uptime|always\s*on)\b"
    r"|\b(?:ping|cek|cekin|monitor|monitorin|pantau|pantauin)\b[^\n]{0,30}\b(?:terus|berkala|rutin|tiap)\b"
    r"|\b(?:kalo|kalau|kalok|jika)\b[^\n]{0,40}\b(?:down|mati|putus|turun)\b"
    r"|\b(?:alert|notif\w*)\b"
    r")",
    re.IGNORECASE,
)

_MONITOR_CANCEL = re.compile(
    r"("
    r"\b(?:stop|berhenti|berhentiin|berhentikan|batalin|batalkan|matiin|nonaktifin|hapus)\b"
    r"[^\n]{0,30}\b(?:monitor\w*|pantau|pantauin|pantauan|awasi|amatin|cek)\b"
    r"|\b(?:monitor|monitoring|pantau|awasi)\s+(?:stop|berhenti|matiin|off|jangan|hapus)\b"
    r")",
    re.IGNORECASE,
)

_MONITOR_LIST_RE = re.compile(
    r"\b(?:list|daftar|lihat|liat|cek|cekin|tunjukin|kasih|tampilin|tampilkan|info)\s+"
    r"(?:ip|alamat)\s+(?:semua\s+)?"
    r"(?:switch\w*|monitor\w*|pantau\w*|network\w*|device\w*|perangkat\w*)\b",
    re.IGNORECASE,
)


def detect_monitor_list_request(text: str) -> bool:
    """True for inventory requests like 'daftar ip semua switch/monitoring'.

    Routed to ``/monitor list`` so the IP list is real (from the DB) rather
    than something the AI invents.
    """
    t = (text or "").strip()
    if not t or t.startswith("/"):
        return False
    return bool(_MONITOR_LIST_RE.search(t))

_MEM_FALLBACK: dict[str, set[int] | dict[int, str]] = {
    "ids": set(),
    "status": {},
}


def _state_ttl() -> int:
    return max(int(settings.monitor_state_ttl_seconds or 2_592_000), 3600)


def _ids_ttl() -> int:
    return max(int(settings.monitor_ids_key_ttl_seconds or 31_536_000), 86400)


def is_monitor_target(value: str) -> bool:
    """Accept a non-loopback IPv4 (public OR private) or a DNS domain."""
    value = (value or "").strip().lower()
    if not value:
        return False
    ip = extract_ip(value)
    if ip is not None:
        if _IP_LOOPBACK_TEST.match(ip):
            return False
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return not addr.is_loopback and not addr.is_multicast and not addr.is_unspecified
    return bool(extract_domain(value))


def _extract_target(text: str) -> str | None:
    """Pull the first usable target out of free text (IP wins over domain)."""
    low = (text or "").lower()
    low = re.sub(r"https?://[^\s]+", " ", low)
    ip = extract_ip(low)
    candidate = ip if ip is not None else extract_domain(low)
    if candidate is None or not is_monitor_target(candidate):
        return None
    return candidate


def detect_monitor_start(text: str) -> str | None:
    if _MONITOR_CANCEL.search(text):
        return None
    if not _MONITOR_INTENT.search(text):
        return None
    return _extract_target(text)


def detect_monitor_stop(text: str) -> str | None:
    if not _MONITOR_CANCEL.search(text):
        return None
    return _extract_target(text)


# ---------------------------------------------------------------------------
# Redis state (with in-memory fallback so tests / degraded Redis still work)
# ---------------------------------------------------------------------------


def _redis() -> Any:
    from app.cache import get_redis

    return get_redis()


async def is_monitored(target_id: int) -> bool:
    try:
        client = _redis()
        if client is not None:
            return bool(await client.sismember(MONITOR_IDS_KEY, str(target_id)))
    except Exception:  # noqa: BLE001
        logger.warning("monitor: redis unavailable (ids read)", exc_info=True)
    ids = _MEM_FALLBACK.get("ids", set())
    return int(target_id) in ids


async def set_monitored(target_id: int) -> None:
    try:
        client = _redis()
        if client is not None:
            await client.sadd(MONITOR_IDS_KEY, str(target_id))
            await client.expire(MONITOR_IDS_KEY, _ids_ttl())
            return
    except Exception:  # noqa: BLE001
        logger.warning("monitor: redis unavailable (ids write)", exc_info=True)
    _MEM_FALLBACK.setdefault("ids", set()).add(int(target_id))


async def list_monitored_ids() -> list[int]:
    try:
        client = _redis()
        if client is not None:
            raw = await client.smembers(MONITOR_IDS_KEY)
            ids = sorted(int(x) for x in (raw or set()))
            return [i for i in ids if i > 0]
    except Exception:  # noqa: BLE001
        logger.warning("monitor: redis unavailable (ids list)", exc_info=True)
    return sorted(_MEM_FALLBACK.get("ids", set()))


async def get_monitor_status(target_id: int) -> str | None:
    try:
        client = _redis()
        if client is not None:
            raw = await client.get(MONITOR_STATUS_KEY.format(target_id))
            return raw if isinstance(raw, str) else (raw.decode() if isinstance(raw, bytes) else None)
    except Exception:  # noqa: BLE001
        logger.warning("monitor: redis unavailable (status read)", exc_info=True)
    return _MEM_FALLBACK.setdefault("status", {}).get(int(target_id))


async def set_monitor_status(target_id: int, status: str) -> None:
    try:
        client = _redis()
        if client is not None:
            await client.set(MONITOR_STATUS_KEY.format(target_id), status, ex=_state_ttl())
            return
    except Exception:  # noqa: BLE001
        logger.warning("monitor: redis unavailable (status write)", exc_info=True)
    _MEM_FALLBACK.setdefault("status", {})[int(target_id)] = status


async def remove_monitor_state(target_id: int) -> None:
    try:
        client = _redis()
        if client is not None:
            await client.srem(MONITOR_IDS_KEY, str(target_id))
            await client.delete(MONITOR_STATUS_KEY.format(target_id))
            return
    except Exception:  # noqa: BLE001
        logger.warning("monitor: redis unavailable (state remove)", exc_info=True)
    _MEM_FALLBACK.setdefault("ids", set()).discard(int(target_id))
    _MEM_FALLBACK.setdefault("status", {}).pop(int(target_id), None)


# ---------------------------------------------------------------------------
# Target persistence
# ---------------------------------------------------------------------------


async def get_target_by_string(session, target: str) -> NetworkTarget | None:
    from sqlalchemy import select

    result = await session.execute(
        select(NetworkTarget).where(NetworkTarget.target == target).limit(1)
    )
    return result.scalars().first()


async def upsert_monitor_target(session, target: str) -> tuple[NetworkTarget, bool]:
    """Reuse an existing row (re-enable it) or insert a fresh ping target."""
    existing = await get_target_by_string(session, target)
    if existing is not None:
        if not existing.enabled:
            existing.enabled = True
        await session.commit()
        return existing, False
    row = NetworkTarget(
        name=target,
        target_type=TargetType.ping,
        target=target,
        interval_seconds=60,
        timeout_seconds=5,
        enabled=True,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row, True


async def disable_monitor_target(session, target: NetworkTarget) -> None:
    target.enabled = False
    await session.commit()


async def _remember_monitor(session, row, text: str) -> None:
    """Best-effort memory write for monitor name changes."""
    try:
        from app.memory import remember
        await remember(session, text, kind="monitor", source="chat", meta={"target_id": row.id})
    except Exception:  # noqa: BLE001
        pass


async def update_monitor_target(
    session,
    target: str,
    name: str | None = None,
    interval: int | None = None,
    timeout: int | None = None,
) -> tuple[NetworkTarget | None, str]:
    """Update name/interval/timeout of an active monitor target.

    Returns (row, detail). row is None when target not found/disabled or a
    range check fails. Validates: interval 10–3600, timeout 1–60.
    """
    row = await get_target_by_string(session, target)
    if row is None or not row.enabled:
        return None, "Target tidak aktif atau tidak ditemukan."
    changed = []
    if name is not None:
        row.name = name.strip() or row.name
        changed.append(f"nama → {row.name}")
        await _remember_monitor(session, row, f"monitor {target} dinamai {row.name}")
    if interval is not None:
        if not 10 <= int(interval) <= 3600:
            return None, "Interval harus 10–3600 detik."
        row.interval_seconds = int(interval)
        changed.append(f"interval → {row.interval_seconds}s")
    if timeout is not None:
        if not 1 <= int(timeout) <= 60:
            return None, "Timeout harus 1–60 detik."
        row.timeout_seconds = int(timeout)
        changed.append(f"timeout → {row.timeout_seconds}s")
    if not changed:
        return row, "Tidak ada perubahan yang diminta."
    await session.commit()
    return row, ", ".join(changed)


async def set_monitor_name(session, target: str, name: str) -> NetworkTarget | None:
    """Give a friendly name to an active monitor target."""
    row = await get_target_by_string(session, target)
    if row is None or not row.enabled:
        return None
    row.name = name.strip() or row.name
    await session.commit()
    await _remember_monitor(session, row, f"monitor {target} dinamai {row.name}")
    return row


async def resolve_monitor_number(session, number_str: str) -> str | None:
    """Resolve a 1-based list index to the target's IP/domain string.

    The ordering matches ``monitoring_list`` (``NetworkTarget.id ASC``).
    Returns ``None`` when the index is out of range or not a valid number.
    """
    if not number_str or not number_str.isdigit():
        return None
    idx = int(number_str)
    if idx < 1:
        return None
    ids = await list_monitored_ids()
    if not ids or idx > len(ids):
        return None
    # ids are ordered by id ASC (from list_monitored_ids → Redis SET)
    target_id = ids[idx - 1]
    from sqlalchemy import select as _select

    row = (
        await session.execute(
            _select(NetworkTarget.target).where(NetworkTarget.id == target_id)
        )
    ).scalar_one_or_none()
    return row


# ---------------------------------------------------------------------------
# Monitor command parsing: target + optional label ("nama ...")
# ---------------------------------------------------------------------------

import re as _re  # noqa: E402

_LABEL_RE = _re.compile(
    r"\b(?:nama|label|name|named?)\s*[:=]?\s*(.+?)$",
    _re.IGNORECASE,
)


def parse_monitor_command(text: str) -> tuple[str, str | None]:
    """Parse ``/monitor <target> [nama <label>]`` → (target, label | None)."""
    if not text:
        return "", None
    text = text.strip()
    target = _extract_target(text) or ""
    if not target:
        return "", None
    rest = text
    ti = text.lower().find(target.lower())
    if ti >= 0:
        rest = text[ti + len(target):]
    label = None
    m = _LABEL_RE.search(rest)
    if m:
        cand = m.group(1).strip().rstrip(".")
        if len(cand) >= 2:
            label = cand
    return target, label


# ---------------------------------------------------------------------------
# Monitor edit detection (free-text, before start/stop)
# ---------------------------------------------------------------------------

_EDIT_RE = _re.compile(
    r"\b(?:ubah|ganti|edit|update|reschedule)\s+monitor\w*\s+"
    r"(.+?)\s+(?:jadi|ke|to|:)\s+(.+)",
    _re.IGNORECASE,
)


def detect_monitor_edit(text: str) -> tuple[str, dict] | None:
    """Detect ``ubah monitor <target> jadi nama X interval 60``.

    Returns ``(target, {name, interval, timeout})`` or None.
    """
    m = _EDIT_RE.search(text or "")
    if not m:
        return None
    target = _extract_target(m.group(1) or "")
    if not target:
        return None
    params = m.group(2) or ""
    edits: dict = {}
    nm = _re.search(r"\bnama\s*[:=]?\s*(.+?)(?:\s+(?:interval|timeout)\b|$)", params, _re.I)
    if nm:
        edits["name"] = nm.group(1).strip().rstrip(".")
    iv = _re.search(r"\binterval\s*[:=]?\s*(\d+)", params, _re.I)
    if iv:
        edits["interval"] = int(iv.group(1))
    tt = _re.search(r"\btimeout\s*[:=]?\s*(\d+)", params, _re.I)
    if tt:
        edits["timeout"] = int(tt.group(1))
    if not edits:
        return None
    return target, edits


# ---------------------------------------------------------------------------
# Pending-name helpers (Redis / fallback memory)
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402

_MONITOR_PENDING_KEY = "bot:monitor:pending_name:{chat_id}"
_PENDING_FALLBACK: dict[str, dict | None] = {}


def _pending_ttl() -> int:
    try:
        from app.config import settings

        return int(settings.monitor_name_prompt_ttl_seconds or 900)
    except Exception:  # noqa: BLE001
        return 900


async def set_monitor_name_pending(chat_id: str, target_str: str) -> None:
    data = _json.dumps({"target": target_str})
    try:
        client = _redis()
        if client is not None:
            await client.set(_MONITOR_PENDING_KEY.format(chat_id=chat_id), data, ex=_pending_ttl())
            return
    except Exception:  # noqa: BLE001
        logger.warning("monitor pending: redis unavailable (write)", exc_info=True)
    _PENDING_FALLBACK[chat_id] = {"target": target_str}


async def get_monitor_name_pending(chat_id: str) -> str | None:
    try:
        client = _redis()
        if client is not None:
            raw = await client.get(_MONITOR_PENDING_KEY.format(chat_id=chat_id))
            if raw:
                data = _json.loads(raw)
                return data.get("target")
    except Exception:  # noqa: BLE001
        logger.warning("monitor pending: redis unavailable (read)", exc_info=True)
    data = _PENDING_FALLBACK.get(chat_id)
    return data.get("target") if data else None


async def clear_monitor_name_pending(chat_id: str) -> None:
    try:
        client = _redis()
        if client is not None:
            await client.delete(_MONITOR_PENDING_KEY.format(chat_id=chat_id))
    except Exception:  # noqa: BLE001
        pass
    _PENDING_FALLBACK.pop(chat_id, None)


def resolve_monitor_name_prompt(text: str) -> str:
    """Classify the user reply to the pending-name prompt.

    Returns one of:
      - ``"none"``     → unrelated text; drop the pending prompt, route normally
      - ``"decline"``  → user does not want a name
      - ``"yes"``      → user agreed but no name given yet (re-prompt for it)
      - ``"name:<l>"`` → user supplied a name to apply
    """
    t = (text or "").strip()
    low = t.lower()
    if not t or t.startswith("/"):
        return "none"
    # Decline: "nggak / ngga usah / gak / gausah / tidak / no ..."
    clean = _re.sub(r"[^a-z]", "", low)
    if clean in {
        "nggak", "ngga", "enggak", "gak", "gk", "tidak", "no", "nope", "ga",
        "gausah", "gakusah", "nggakusah", "nggausah", "enggakusah",
        "nggakperlu", "nggaperlu", "skip",
    }:
        return "decline"
    m = _re.match(r"^(?:ya|yap|yaps|iya|iyaa|ok|oke|sip)\b[\s:,.!-]*(?:nama\b\s*)?(.*)$", t, _re.I)
    if m:
        label = m.group(1).strip().strip(".,! -")
        if not label or len(label) <= 3 or label.lower() in {"dong", "donk", "doh", "deh", "dah", "aja", "sip", "oke", "ok", "yes", "yoi", "iya"}:
            return "yes"
        return f"name:{label}"
    m2 = _re.match(r"^nama\b\s+(.+)$", t, _re.I)
    if m2:
        label = m2.group(1).strip().strip(".,! -")
        if not label or len(label) <= 3 or label.lower() in {"dong", "donk", "doh", "deh", "dah", "aja", "sip", "oke", "ok"}:
            return "yes"
        return f"name:{label}"
    return "none"


# ---------------------------------------------------------------------------
# Transition detection + alert delivery
# ---------------------------------------------------------------------------

_PERSONA_DOWN = [
    "Bro, ada yang down nih di monitoring kita:",
    "Woi perhatian — jaringan yang kita pantau putus:",
    "KELA lapor: ada target yang gak kedengeran:",
]
_PERSONA_UP = [
    "Update bagus dari monitoring:",
    "Eh, targetnya balik lagi nih:",
    "KELA lapor: udah balik UP nih:",
]


def _bucket(status: str) -> str:
    return "up" if (status or "").lower() == "up" else "down"


async def maybe_notify_monitor(target: NetworkTarget, result: dict, chat_id: str | None = None) -> bool:
    """Alert on a real up↔down transition. Returns True when an alert was sent.

    Called by the scheduler right after ``store_network_check``. A missing
    opt-in marker or an unchanged bucket → silently re-seeds and returns False.
    """
    if not await is_monitored(target.id):
        return False
    raw = result.get("status")
    status = getattr(raw, "value", raw)
    status = str(status).lower()
    bucket = _bucket(status)

    prev_raw = await get_monitor_status(target.id)
    prev_bucket = _bucket(prev_raw) if prev_raw else None
    await set_monitor_status(target.id, status)

    if prev_bucket is None or prev_bucket == bucket:
        return False

    if bucket == "up":
        body = await format_monitor_up(target, result)
    else:
        body = await format_monitor_down(target, result)
    return await send_monitor_alert(body, chat_id=chat_id)


async def _resolve_ip(target: str) -> str | None:
    if extract_ip(target) is not None:
        return target
    try:
        ip = await asyncio.to_thread(__import__("socket").gethostbyname, target)
        return ip or None
    except Exception:  # noqa: BLE001
        return None


async def _detail_lines(target: NetworkTarget, result: dict) -> list[str]:
    status = getattr(result.get("status"), "value", result.get("status"))
    status = str(status).lower()
    lines = [f"  Status: {mono(status)}"]
    latency = result.get("latency_ms")
    loss = result.get("packet_loss")
    if latency is not None:
        lines.append(f"  Latency: {float(latency):.0f} ms")
    else:
        lines.append("  Latency: —")
    if loss is not None:
        lines.append(f"  Loss: {esc(f'{loss:g}')}%")
    ip = await _resolve_ip(target.target)
    if ip and ip != target.target:
        lines.append(f"  Resolve: {mono(ip)}")
    lines.append(f"  Waktu: {datetime.now(ZoneInfo('Asia/Jakarta')):%A, %d %b %Y, %H:%M WIB}")
    return lines


async def format_monitor_down(target: NetworkTarget, result: dict) -> str:
    opener = random.choice(_PERSONA_DOWN)
    name = target.name or target.target
    lines = [f"{opener}", f"{bold('[DOWN] JARINGAN DOWN')} — {bold(esc(name))}"]
    lines.extend(await _detail_lines(target, result))
    return "\n".join(lines)


async def format_monitor_up(target: NetworkTarget, result: dict) -> str:
    opener = random.choice(_PERSONA_UP)
    name = target.name or target.target
    lines = [f"{opener}", f"{bold('[UP] JARINGAN KEMBALI')} — {bold(esc(name))}"]
    lines.extend(await _detail_lines(target, result))
    return "\n".join(lines)


async def send_monitor_alert(body: str, chat_id: str | None = None) -> bool:
    token = (settings.telegram_bot_token or "").strip()
    cid = (chat_id or settings.telegram_chat_id or "").strip()
    if not token or not cid:
        logger.warning("monitor alert skipped - telegram credentials missing")
        return False
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{TELEGRAM_API}/bot{token}/sendMessage",
                json={"chat_id": cid, "text": body, "parse_mode": "HTML"},
            )
        data = resp.json()
        if resp.status_code != 200 or not data.get("ok"):
            logger.warning("monitor alert failed: %s", data.get("description"))
            return False
        return True
    except Exception:  # noqa: BLE001
        logger.exception("monitor alert send error")
        return False