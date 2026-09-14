"""Actual health-check implementations.

Supported: ping (ICMP via `ping`), tcp, http(s), dns.
SNMP is deliberately NOT implemented yet - returns an `error` status so a
target is never silently marked up/down based on missing tooling.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from typing import Any

import httpx

from app.db.models import CheckStatus, NetworkTarget

logger = logging.getLogger(__name__)

_PING_TIME_RE = re.compile(r"time[=<]([\d.]+)")

RESULT = dict[Any, Any]


async def check_ping(host: str, timeout_seconds: int) -> RESULT:
    start = asyncio.get_event_loop().time()
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", str(timeout_seconds), host,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds + 2)
        elapsed_ms = round((asyncio.get_event_loop().time() - start) * 1000, 2)
        text = (stdout + stderr).decode(errors="ignore")
        if proc.returncode == 0:
            m = _PING_TIME_RE.search(text)
            return {
                "status": CheckStatus.up,
                "latency_ms": float(m.group(1)) if m else elapsed_ms,
                "packet_loss": 0.0,
                "error_message": None,
            }
        loss = 100.0
        return {
            "status": CheckStatus.down,
            "latency_ms": None,
            "packet_loss": loss,
            "error_message": text.strip()[:500] or "ping failed",
        }
    except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001 - report check as timeout
        return {
            "status": CheckStatus.timeout,
            "latency_ms": None,
            "packet_loss": 100.0,
            "error_message": f"{type(exc).__name__}: {exc}"[:500],
        }


async def check_tcp(host: str, port: int, timeout_seconds: int) -> RESULT:
    start = asyncio.get_event_loop().time()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout_seconds
        )
        elapsed_ms = round((asyncio.get_event_loop().time() - start) * 1000, 2)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return {"status": CheckStatus.up, "latency_ms": elapsed_ms, "packet_loss": None,
                "error_message": None}
    except asyncio.TimeoutError:
        return {"status": CheckStatus.timeout, "latency_ms": None, "packet_loss": None,
                "error_message": f"tcp connect timeout after {timeout_seconds}s"}
    except Exception as exc:  # noqa: BLE001
        return {"status": CheckStatus.down, "latency_ms": None, "packet_loss": None,
                "error_message": f"{type(exc).__name__}: {exc}"[:500]}


async def check_http(target: str, timeout_seconds: int) -> RESULT:
    start = asyncio.get_event_loop().time()
    url = target if target.startswith("http") else f"https://{target}"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds), follow_redirects=True,
        ) as client:
            resp = await client.get(url)
        elapsed_ms = round((asyncio.get_event_loop().time() - start) * 1000, 2)
        if resp.status_code < 400:
            return {"status": CheckStatus.up, "latency_ms": elapsed_ms, "packet_loss": None,
                    "error_message": None}
        return {"status": CheckStatus.down, "latency_ms": elapsed_ms, "packet_loss": None,
                "error_message": f"http status {resp.status_code}"}
    except asyncio.TimeoutError:
        return {"status": CheckStatus.timeout, "latency_ms": None, "packet_loss": None,
                "error_message": f"http timeout after {timeout_seconds}s"}
    except Exception as exc:  # noqa: BLE001
        return {"status": CheckStatus.down, "latency_ms": None, "packet_loss": None,
                "error_message": f"{type(exc).__name__}: {exc}"[:500]}


async def check_dns(host: str, timeout_seconds: int) -> RESULT:
    start = asyncio.get_event_loop().time()
    try:
        await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, host, 53), timeout=timeout_seconds
        )
        elapsed_ms = round((asyncio.get_event_loop().time() - start) * 1000, 2)
        return {"status": CheckStatus.up, "latency_ms": elapsed_ms, "packet_loss": None,
                "error_message": None}
    except asyncio.TimeoutError:
        return {"status": CheckStatus.timeout, "latency_ms": None, "packet_loss": None,
                "error_message": f"dns resolve timeout after {timeout_seconds}s"}
    except Exception as exc:  # noqa: BLE001
        return {"status": CheckStatus.down, "latency_ms": None, "packet_loss": None,
                "error_message": f"{type(exc).__name__}: {exc}"[:500]}


async def _check_snmp(target: str, timeout_seconds: int) -> RESULT:
    return {"status": CheckStatus.error, "latency_ms": None, "packet_loss": None,
            "error_message": "SNMP not implemented yet (Phase 5)"}


async def run_check(target: NetworkTarget) -> RESULT:
    """Run the check matching a target's type. Result only - no persistence."""
    ms = target.timeout_seconds or 5
    handler = {
        "ping": lambda: check_ping(target.target, ms),
        "tcp": lambda: check_tcp(target.target, target.port or 80, ms),
        "http": lambda: check_http(target.target, ms),
        "dns": lambda: check_dns(target.target, ms),
        "snmp": lambda: _check_snmp(target.target, ms),
    }.get(target.target_type.value)
    if handler is None:
        return {"status": CheckStatus.error, "latency_ms": None, "packet_loss": None,
                "error_message": f"unsupported target_type: {target.target_type}"}
    logger.info("network check started: type=%s target=%s", target.target_type.value, target.target)
    return await handler()