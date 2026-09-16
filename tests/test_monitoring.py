"""Tests for chat-driven network monitoring (monitoring.py + command handlers).

Redis is absent in the test env, so every state function falls back to the
in-memory dict — replicating the degraded path. No real pings are issued here,
the alert sender is monkeypatched.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import NetworkTarget, TargetType
from app.interfaces.commands import _monitor, monitoring_list
from app.monitoring import (
    clear_monitor_name_pending,
    detect_monitor_edit,
    detect_monitor_start,
    detect_monitor_stop,
    get_monitor_name_pending,
    get_monitor_status,
    is_monitor_target,
    is_monitored,
    maybe_notify_monitor,
    parse_monitor_command,
    remove_monitor_state,
    resolve_monitor_name_prompt,
    set_monitor_name_pending,
    set_monitored,
    set_monitor_status,
    update_monitor_target,
    upsert_monitor_target,
)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    from app.monitoring import _MEM_FALLBACK, _PENDING_FALLBACK

    _MEM_FALLBACK["ids"] = set()
    _MEM_FALLBACK["status"] = {}
    _PENDING_FALLBACK.clear()
    monkeypatch.setattr("app.monitoring._redis", lambda: None)
    yield


@pytest_asyncio.fixture()
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------

def test_detect_monitor_start_public_ip():
    assert detect_monitor_start("monitor 8.8.8.8 kalo down alert") == "8.8.8.8"


def test_detect_monitor_start_private_ip_ok():
    assert detect_monitor_start("pantau 192.168.1.1 terus") == "192.168.1.1"


def test_detect_monitor_start_loopback_rejected():
    assert detect_monitor_start("monitor 127.0.0.1 terus") is None


def test_detect_monitor_start_domain():
    assert detect_monitor_start("awasi google.com terus ya") == "google.com"


def test_detect_monitor_start_plain_cek_no_intent():
    assert detect_monitor_start("tolong cek 8.8.8.8") is None
    assert detect_monitor_start("gua mau cek server nih") is None


def test_detect_monitor_start_down_keyword():
    assert detect_monitor_start("cek 114.120.14.5 kalo down notif gua") == "114.120.14.5"


def test_detect_monitor_stop():
    assert detect_monitor_start("stop monitoring 8.8.8.8") is None
    assert detect_monitor_stop("stop monitoring 8.8.8.8") == "8.8.8.8"


def test_detect_monitor_stop_berhenti():
    assert detect_monitor_stop("berhentiin pantau google.com dong") == "google.com"


def test_detect_monitor_stop_monitor_off():
    assert detect_monitor_stop("monitor stop 8.8.8.8") == "8.8.8.8"


def test_detect_monitor_start_not_canceled_by_jangan_lupa():
    assert detect_monitor_start("jangan lupa monitor 8.8.8.8 terus") == "8.8.8.8"


def test_is_monitor_target():
    assert is_monitor_target("8.8.8.8")
    assert is_monitor_target("192.168.1.1")
    assert not is_monitor_target("127.0.0.1")
    assert not is_monitor_target("0.0.0.0")
    assert not is_monitor_target("255.255.255.255")
    assert is_monitor_target("google.com")
    assert not is_monitor_target("")
    assert not is_monitor_target("not a target")


# ---------------------------------------------------------------------------
# Redis-backed state (in-memory fallback path)
# ---------------------------------------------------------------------------

async def test_set_get_monitored_fallback():
    await set_monitored(42)
    assert await is_monitored(42) is True
    assert await is_monitored(43) is False
    await remove_monitor_state(42)
    assert await is_monitored(42) is False


async def test_status_roundtrip_fallback():
    await set_monitor_status(7, "down")
    assert await get_monitor_status(7) == "down"
    await remove_monitor_state(7)
    assert await get_monitor_status(7) is None


# ---------------------------------------------------------------------------
# Transition detection: alerts fire only on a real up<->down flip
# ---------------------------------------------------------------------------

def _t(ident: int = 1) -> NetworkTarget:
    return NetworkTarget(
        id=ident,
        name="8.8.8.8",
        target_type=TargetType.ping,
        target="8.8.8.8",
        interval_seconds=60,
        timeout_seconds=5,
        enabled=True,
    )


async def test_first_seen_seeds_without_alert(monkeypatch):
    sent = []

    async def fake_send(body, chat_id=None):
        sent.append(body)
        return True

    monkeypatch.setattr("app.monitoring.send_monitor_alert", fake_send)
    await set_monitored(1)
    ok = await maybe_notify_monitor(_t(), {"status": "down", "latency_ms": None})
    assert ok is False
    assert sent == []
    assert await get_monitor_status(1) == "down"


async def test_alert_fires_on_down_flip(monkeypatch):
    sent = []

    async def fake_send(body, chat_id=None):
        sent.append(body)
        return True

    monkeypatch.setattr("app.monitoring.send_monitor_alert", fake_send)
    await set_monitored(1)
    await set_monitor_status(1, "up")

    ok = await maybe_notify_monitor(_t(), {"status": "down", "latency_ms": None})
    assert ok is True
    assert len(sent) == 1
    assert "[DOWN]" in sent[0]

    # Same bucket again -> no new alert
    ok = await maybe_notify_monitor(_t(), {"status": "down", "latency_ms": None})
    assert ok is False
    assert len(sent) == 1


async def test_alert_fires_on_up_flip(monkeypatch):
    sent = []

    async def fake_send(body, chat_id=None):
        sent.append(body)
        return True

    monkeypatch.setattr("app.monitoring.send_monitor_alert", fake_send)
    await set_monitored(1)
    await set_monitor_status(1, "down")

    ok = await maybe_notify_monitor(_t(), {"status": "up", "latency_ms": 12.3})
    assert ok is True
    assert len(sent) == 1
    assert "[PULIH]" in sent[0]


async def test_no_alert_for_unmonitored_target(monkeypatch):
    sent = []

    async def fake_send(body, chat_id=None):
        sent.append(body)
        return True

    monkeypatch.setattr("app.monitoring.send_monitor_alert", fake_send)
    ok = await maybe_notify_monitor(_t(), {"status": "down"})
    assert ok is False
    assert sent == []


# ---------------------------------------------------------------------------
# Database upsert + command handlers (offline, no ping issued)
# ---------------------------------------------------------------------------

async def test_upsert_monitor_target_creates_ping(session):
    row, created = await upsert_monitor_target(session, "8.8.8.8")
    assert created is True
    assert row.target_type is TargetType.ping
    assert row.target == "8.8.8.8"
    assert row.enabled is True
    assert row.interval_seconds == 60


async def test_upsert_monitor_target_reenables_existing(session):
    row, created = await upsert_monitor_target(session, "8.8.8.8")
    assert created is True
    row.enabled = False
    await session.commit()

    same, created = await upsert_monitor_target(session, "8.8.8.8")
    assert created is False
    assert same.id == row.id
    assert same.enabled is True


async def test_monitor_list_empty(session):
    from app.interfaces.commands import monitoring_list

    reply = await monitoring_list(session)
    assert "belum ada target" in reply


async def test_monitor_command_list_and_invalid(session):
    reply = await _monitor(session, "")
    assert "belum ada target" in reply
    reply = await _monitor(session, "stop")
    assert "gak valid" in reply.lower() or "valid" in reply.lower()
    reply = await _monitor(session, "list")
    assert "belum ada target" in reply


# ---------------------------------------------------------------------------
# Monitor naming + edit (Phase 6.6)
# ---------------------------------------------------------------------------

def test_parse_monitor_command_with_label():
    assert parse_monitor_command("8.8.8.8 nama RO UNIV") == ("8.8.8.8", "RO UNIV")
    assert parse_monitor_command("8.8.8.8") == ("8.8.8.8", None)
    assert parse_monitor_command("monitor 8.8.8.8 nama DNS GOOGLE") == ("8.8.8.8", "DNS GOOGLE")
    assert parse_monitor_command("") == ("", None)


def test_detect_monitor_edit():
    assert detect_monitor_edit("ubah monitor 8.8.8.8 jadi nama DNS GOOGLE") == (
        "8.8.8.8", {"name": "DNS GOOGLE"},
    )
    assert detect_monitor_edit(
        "ganti monitoring 114.120.14.5 jadi interval 120 timeout 10"
    ) == ("114.120.14.5", {"interval": 120, "timeout": 10})
    assert detect_monitor_edit("monitor 8.8.8.8 terus aja") is None
    assert detect_monitor_edit("tolong cek 8.8.8.8") is None


def test_resolve_monitor_name_prompt():
    assert resolve_monitor_name_prompt("Ngga usah") == "decline"
    assert resolve_monitor_name_prompt("gak") == "decline"
    assert resolve_monitor_name_prompt("batalin aja") == "none"
    assert resolve_monitor_name_prompt("Ya") == "yes"
    assert resolve_monitor_name_prompt("iya donk") == "yes"
    assert resolve_monitor_name_prompt("ya dong") == "yes"
    assert resolve_monitor_name_prompt("Ya, nama RO UNIV") == "name:RO UNIV"
    assert resolve_monitor_name_prompt("nama POLI TELKOM") == "name:POLI TELKOM"
    assert resolve_monitor_name_prompt("berita apa hari ini?") == "none"
    assert resolve_monitor_name_prompt("/status") == "none"
    assert resolve_monitor_name_prompt("") == "none"


async def test_update_monitor_target_name_and_ranges(session):
    await upsert_monitor_target(session, "8.8.8.8")
    row, detail = await update_monitor_target(session, "8.8.8.8", name="DNS GOOGLE")
    assert row is not None and row.name == "DNS GOOGLE"
    assert "nama" in detail
    row2, detail2 = await update_monitor_target(session, "8.8.8.8", interval=5)
    assert row2 is None
    assert "10" in detail2
    row3, _ = await update_monitor_target(session, "8.8.8.8", timeout=2)
    assert row3 is not None and row3.timeout_seconds == 2
    missing, detail3 = await update_monitor_target(session, "1.1.1.1", name="x")
    assert missing is None


async def test_set_monitor_name_and_pending_roundtrip():
    await set_monitor_name_pending("c1", "8.8.8.8")
    assert await get_monitor_name_pending("c1") == "8.8.8.8"
    await clear_monitor_name_pending("c1")
    assert await get_monitor_name_pending("c1") is None