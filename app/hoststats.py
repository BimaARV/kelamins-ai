"""Host-level system metrics read from ``/proc`` (host-wide values, no privilege).

Because the bot container runs with ``network_mode: host``, ``/proc/net/dev``
reflects the host's real interfaces and ``/proc/stat``/``/proc/meminfo``/
``/proc/uptime`` are host-wide counters. Disk usage is read via ``/host-root``
(host ``/`` bind-mounted read-only into the bot container).
"""

from __future__ import annotations

import asyncio
import os
import re

from app.interfaces.formatter import bold, esc, mono, num

PROC_DIR = "/proc"
HOST_ROOT = "/host-root"

_IGNORE_IFACE = re.compile(
    r"^(lo|docker\d*|veth[0-9a-f]*|br-|virbr\d*|dummy|sit[0-9]*|tun\d*|tap\d*|gretap|anyso|ip6tnl|wlan[0-9]+(?:\.\d+)*wl)"  # noqa: E501
)


async def _read_proc(name: str) -> str:
    path = f"{PROC_DIR}/{name}"

    def _read() -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""

    return await asyncio.to_thread(_read)


# ---------------------------------------------------------------------------
# /proc parsers (pure, unit-testable)
# ---------------------------------------------------------------------------

def parse_cpu_stat(text: str) -> tuple[int, int]:
    """Return ``(idle, total)`` jiffies from the first ``cpu`` line."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            parts = line.split()
            try:
                vals = [int(v) for v in parts[1:]]
            except ValueError:
                continue
            if not vals:
                continue
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            return idle, sum(vals)
    return 0, 0


async def collect_cpu_percent(sample_seconds: float = 0.5) -> float:
    idle0, total0 = parse_cpu_stat(await _read_proc("stat"))
    if total0 <= 0:
        return 0.0
    await asyncio.sleep(sample_seconds)
    idle1, total1 = parse_cpu_stat(await _read_proc("stat"))
    d_total = total1 - total0
    if d_total <= 0:
        return 0.0
    d_idle = idle1 - idle0
    return round(100.0 * (1.0 - d_idle / d_total), 1)


def parse_loadavg(text: str) -> tuple[float, float, float]:
    parts = (text or "").split()
    if len(parts) < 3:
        return (0.0, 0.0, 0.0)
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except ValueError:
        return (0.0, 0.0, 0.0)


def parse_meminfo(text: str) -> dict:
    values: dict[str, int] = {}
    for line in (text or "").splitlines():
        key = line.split(":", 1)[0]
        m = re.search(r"(\d+)", line.split(":", 1)[1] if ":" in line else "")
        if key and m:
            values[key] = int(m.group(1))
    total_kb = values.get("MemTotal", 0)
    avail_kb = values.get("MemAvailable", total_kb)
    used_kb = max(total_kb - avail_kb, 0)
    pct = (used_kb / total_kb * 100) if total_kb else 0.0
    return {
        "total_gb": total_kb / (1024 * 1024),
        "used_gb": used_kb / (1024 * 1024),
        "pct": round(pct, 1),
    }


def parse_uptime(text: str) -> float:
    parts = (text or "").split()
    try:
        return float(parts[0])
    except (ValueError, IndexError):
        return 0.0


def format_uptime(seconds: float) -> str:
    if seconds <= 0:
        return "?"
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m, _s = divmod(rem, 60)
    out = f"{m:g} mnt"
    if h:
        out = f"{h:g} jam {out}"
    if d:
        out = f"{d:g} hari {out}"
    return out


def parse_netdev(text: str) -> dict[str, tuple[int, int]]:
    """Return ``{iface: (rx_bytes, tx_bytes)}`` from /proc/net/dev."""
    result: dict[str, tuple[int, int]] = {}
    for line in (text or "").splitlines():
        if ":" not in line:
            continue
        iface, rest = line.split(":", 1)
        vals = rest.split()
        if len(vals) < 9:
            continue
        try:
            rx = int(vals[0])
            tx = int(vals[8])
        except ValueError:
            continue
        result[iface.strip()] = (rx, tx)
    return result


async def collect_network_speeds(sample_seconds: float = 0.5) -> dict[str, dict]:
    """Sample /proc/net/dev twice → ``{iface: {rx_mbps, tx_mbps}}`` (real ifaces)."""
    a0 = parse_netdev(await _read_proc("net/dev"))
    await asyncio.sleep(sample_seconds)
    a1 = parse_netdev(await _read_proc("net/dev"))
    speeds: dict[str, dict] = {}
    for iface, (rx1, tx1) in a1.items():
        if _IGNORE_IFACE.match(iface) or iface not in a0:
            continue
        rx0, tx0 = a0[iface]
        rx_bps = (rx1 - rx0) / sample_seconds
        tx_bps = (tx1 - tx0) / sample_seconds
        if rx_bps < 0 or tx_bps < 0:
            continue
        speeds[iface] = {
            "rx_mbps": round(rx_bps / 1e6, 2),
            "tx_mbps": round(tx_bps / 1e6, 2),
        }
    return speeds


async def collect_disk() -> dict | None:
    if not os.path.isdir(HOST_ROOT):
        return None
    try:
        s = os.statvfs(HOST_ROOT)
    except OSError:
        return None
    total = s.f_blocks * s.f_frsize
    free = s.f_bavail * s.f_frsize
    used = max(total - free, 0)
    if total <= 0:
        return None
    return {
        "mount": HOST_ROOT,
        "total_gb": total / (1024 ** 3),
        "used_gb": used / (1024 ** 3),
        "pct": round(used / total * 100, 1),
    }


# ---------------------------------------------------------------------------
# Aggregation + formatting
# ---------------------------------------------------------------------------

async def collect_all() -> dict:
    cpu_task = collect_cpu_percent()
    net_task = collect_network_speeds()
    cpu_percent, net = await asyncio.gather(cpu_task, net_task)
    l1, l5, l15 = parse_loadavg(await _read_proc("loadavg"))
    mem = parse_meminfo(await _read_proc("meminfo"))
    up = format_uptime(parse_uptime(await _read_proc("uptime")))
    disk = await collect_disk()
    return {
        "uptime": up,
        "cpu_percent": cpu_percent,
        "load": (l1, l5, l15),
        "memory": mem,
        "disk": disk,
        "network": net,
    }


def _net_totals(net: dict) -> tuple[float, float]:
    rx = sum(v["rx_mbps"] for v in net.values())
    tx = sum(v["tx_mbps"] for v in net.values())
    return round(rx, 2), round(tx, 2)


def format_status(stats: dict, db_counts: dict | None) -> str:
    lines = [bold("Status Sistem — host")]
    lines.append(f"  Uptime: {mono(esc(stats.get('uptime') or '?'))}")

    cpu = stats.get("cpu_percent")
    if cpu is not None:
        load = stats.get("load") or (0, 0, 0)
        lines.append(
            f"  CPU: {mono(f'{num(cpu,1)}% (load {num(load[0],1)} / {num(load[1],1)} / {num(load[2],1)})')}"
        )
    else:
        lines.append("  CPU: ?")

    mem = stats.get("memory")
    if mem:
        mem_s = "%s%% (%s / %s GB)" % (num(mem["pct"], 1), num(mem["used_gb"], 1), num(mem["total_gb"], 1))
        lines.append(f"  RAM: {mono(esc(mem_s))}")
    else:
        lines.append("  RAM: ?")

    disk = stats.get("disk")
    if disk:
        disk_s = "%s%% (%s / %s GB) on %s" % (
            num(disk["pct"], 1),
            num(disk["used_gb"], 1),
            num(disk["total_gb"], 1),
            disk["mount"],
        )
        lines.append(f"  Disk: {mono(esc(disk_s))}")
    else:
        lines.append("  Disk: n/a")

    net = stats.get("network") or {}
    rx, tx = _net_totals(net)
    lines.append(f"  Network: {mono(f'↓ {rx} Mbps · ↑ {tx} Mbps')}")
    for iface, v in sorted(net.items()):
        lines.append(f"    {esc(iface)}: ↓ {num(v['rx_mbps'],2)} · ↑ {num(v['tx_mbps'],2)} Mbps")

    if db_counts:
        first = " · ".join(
            f"{k} {v}" for k, v in db_counts.items() if k != "ai_ok"
        )
        lines.append(f"\n{mono('DB:')} {esc(first)}")
        ai_ok = db_counts.get("ai_ok")
        if ai_ok is not None:
            lines.append(f"  AI: {db_counts.get('ai_reqs', 0)} ({ai_ok} ok)")
    return "\n".join(lines)