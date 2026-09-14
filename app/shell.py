"""Whitelisted host-command runner for KELA — only a fixed set of read-only
network diagnostics may be executed (``ip a`` / ``ifconfig`` / ``traceroute`` /
``ping``). Anything else is rejected before spawn; output is capped and
HTML-escaped.
"""

from __future__ import annotations

import asyncio
import re

_IP_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")
_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,252})[a-z0-9]\.[a-z]{2,}$", re.IGNORECASE)

ALLOWED_IP_ARGS = (
    ("ip", "a"),
    ("ip", "addr"),
    ("ip", "-4", "a"),
    ("ip", "-6", "a"),
)


def _traceroute_target_ok(target: str) -> bool:
    m = _IP_RE.match(target)
    if m:
        return all(int(g) <= 255 for g in m.groups()) and not target.startswith("127.")
    return bool(_DOMAIN_RE.match(target))


def parse_server_request(text: str) -> list[str] | None:
    """Return allowed argv for a server-diagnostic request, or None."""
    low = (text or "").strip().lower()
    if not low:
        return None
    parts = low.split()
    if parts[0] == "ifconfig" and len(parts) == 1:
        return ["ifconfig"]
    if tuple(parts) in ALLOWED_IP_ARGS:
        return parts
    if parts[0] == "traceroute" and len(parts) == 2 and _traceroute_target_ok(parts[1]):
        return parts
    if parts[0] == "ping" and len(parts) == 2 and _traceroute_target_ok(parts[1]):
        return ["ping", "-c", "4", "-W", "2", parts[1]]
    return None


async def run_network_cmd(args: list[str], timeout: float = 20.0) -> dict:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        rc = proc.returncode
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.communicate()
        except ProcessLookupError:
            pass
        return {"args": args, "rc": -1, "stdout": "", "stderr": f"timeout >{timeout:g}s"}
    return {
        "args": args,
        "rc": rc,
        "stdout": (out or b"").decode("utf-8", "replace"),
        "stderr": (err or b"").decode("utf-8", "replace"),
    }


MAX_OUTPUT_CHARS = 3500


def format_server_command(args: list[str], result: dict) -> str:
    from app.interfaces.formatter import bold, esc, mono

    cmd = " ".join(args)
    out = (result.get("stdout") or "").strip() or (result.get("stderr") or "").strip()
    if not out:
        out = "(tidak ada output)"
    out = re.sub(r"\n{3,}", "\n\n", out)
    truncated = len(out) > MAX_OUTPUT_CHARS
    out = out[:MAX_OUTPUT_CHARS]
    lines = [bold(f"Server — {mono(cmd)}")]
    if result.get("rc"):
        lines.append(f"\n  exit code: {result['rc']}")
    lines.append("\n<pre>" + esc(out) + "</pre>")
    if truncated:
        lines.append("\n(hasil dipotong, output-nya panjang banget)")
    return "\n".join(lines)