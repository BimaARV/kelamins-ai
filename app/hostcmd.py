"""Host file + read-only system command access for the chat bot.

The bot container mounts the host root at ``/host-root`` (read-write) and runs
as root inside the container. To keep that blast radius tiny, everything goes
through an explicit allow-list:

  * **File zones** — only paths under ``HOSTCMD_ALLOWED_ROOTS`` (default
    ``["/home", "/tmp", "/var/log"]``) can be read/written/listed. Realpath is
    resolved so symlinks can't escape the allowed roots or ``/host-root``.
  * **Commands** — an exact argv map (no shell parsing of user input), plus an
    output cap so a busy ``ps``/``ss`` can't flood chat.
"""

from __future__ import annotations

import os
import re
import subprocess

from app.config import settings
from app.interfaces.formatter import bold, esc, mono

HOST_ROOT_MOUNT = "/host-root"
OUTPUT_MAX_CHARS = 3500

# ---------------------------------------------------------------------------
# Path handling (host semantics; container sees the same tree under /host-root)
# ---------------------------------------------------------------------------


def _allowed_roots() -> list[str]:
    raw = settings.load_fixtures("hostcmd_allowed_roots")
    roots = [str(r).rstrip("/") or "/" for r in raw]
    return roots or ["/home", "/tmp", "/var/log"]


def validate_allowed_path(host_path: str) -> tuple[str | None, str | None]:
    """Given a host path (e.g. ``/home/bima/x.txt``), return ``(container_path, error)``."""
    host_path = (host_path or "").strip()
    if not host_path.startswith("/"):
        return None, "path harus absolut (diawali '/'). Contoh: /home/bima/notes.txt"
    roots = _allowed_roots()
    if not any(host_path == r or host_path.startswith(r + "/") for r in roots):
        return None, (
            "path di luar zona yang boleh. Yang boleh: "
            + ", ".join(roots)
        )
    container = HOST_ROOT_MOUNT + host_path
    try:
        real = os.path.realpath(container)
    except OSError:
        return None, "path gak bisa di-resolve (mungkin gak ada / izin ditolak)."
    if not (real == HOST_ROOT_MOUNT or real.startswith(HOST_ROOT_MOUNT + "/")):
        return None, "path nyasar keluar dari /host-root — ditolak."
    return real, None


def host_read_file(host_path: str, max_chars: int = 2500) -> str:
    container, err = validate_allowed_path(host_path)
    if err:
        return f"{bold('Host file')} — {err}"
    if os.path.isdir(container):
        return "\n".join(sorted(os.listdir(container))) or "(kosong)"
    try:
        with open(container, "r", encoding="utf-8", errors="replace") as fh:
            data = fh.read(max_chars)
    except OSError as exc:
        return f"{bold('Host file')} — gagal baca: {esc(str(exc))}"
    if os.path.getsize(container) > max_chars:
        data += f"\n… (dipotong di {max_chars} char)"
    return data


def host_write_file(host_path: str, content: str) -> str:
    container, err = validate_allowed_path(host_path)
    if err:
        return f"{bold('Host file')} — {err}"
    if os.path.isdir(container):
        return f"{bold('Host file')} — {mono(esc(host_path))} itu folder, bukan file."
    try:
        parent = os.path.dirname(container)
        os.makedirs(parent, exist_ok=True)
        with open(container, "w", encoding="utf-8") as fh:
            fh.write(content)
    except OSError as exc:
        return f"{bold('Host file')} — gagal nulis: {esc(str(exc))}"
    return f"{bold('File ditulis')} — {mono(esc(host_path))} ({len(content)} char)."


def host_list_dir(host_path: str) -> str:
    container, err = validate_allowed_path(host_path)
    if err:
        return f"{bold('Host dir')} — {err}"
    try:
        entries = sorted(os.listdir(container))
    except OSError as exc:
        return f"{bold('Host dir')} — gagal list: {esc(str(exc))}"
    lines = [f"{bold('Isi')} {mono(esc(host_path))} ({len(entries)} entri):"]
    for e in entries[:60]:
        full = os.path.join(container, e)
        kind = "dir " if os.path.isdir(full) else "file"
        lines.append(f"  {mono(kind)} {esc(e)}")
    if len(entries) > 60:
        lines.append(f"  … dan {len(entries) - 60} entri lagi.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Read-only host commands (exact argv map, no shell)
# ---------------------------------------------------------------------------

COMMAND_ARGV = {
    "lscpu": ["lscpu"],
    "df": ["df", "-h"],
    "free": ["free", "-h"],
    "uptime": ["uptime"],
    "hostname": ["hostname"],
    "uname": ["uname", "-a"],
    "id": ["id"],
    "whoami": ["whoami"],
    "ps": ["ps", "aux"],
    "ss": ["ss", "-tulpn"],
    "top": ["top", "-b", "-n", "1"],
    "passwd": ["cat", "/host-root/etc/passwd"],
}


def run_host_command(name: str) -> str:
    argv = COMMAND_ARGV.get(name)
    if argv is None:
        return f"{bold('Host cmd')} — {mono(esc(name))} nggak ada di allowlist."
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return f"{bold('Host cmd')} — {mono(esc(name))} timeout (>20s)."
    except OSError as exc:
        return f"{bold('Host cmd')} — gagal jalan: {esc(str(exc))}"
    out = (proc.stdout or "") or (proc.stderr or "")
    out = out[-OUTPUT_MAX_CHARS:] if len(out) > OUTPUT_MAX_CHARS else out
    return out or "(tidak ada output)"


# ---------------------------------------------------------------------------
# Free-text intent
# ---------------------------------------------------------------------------

_WRITE_RE = re.compile(r"^(?:buat|buatin|bikin|bikinin|tulis|tulisin|create|write)\s+file\s+(\S+)", re.I)
_READ_RE = re.compile(
    r"^(?:baca|lihat|isi|cat|read|show|open)\s+file\s+(\S+)"
    r"|\b(?:isi|baca|cat)\s+(\/(?:home|tmp|var)\b\S*)$",
    re.I,
)
_DIR_RE = re.compile(r"^(?:lihat|ls|list)\s+dir\s+(\S+)|^ls\s+(\S+)$", re.I)
_CMD_NAMES = tuple(sorted(COMMAND_ARGV))
_CMD_RE = re.compile(
    r"^(?:jalanin|jalankan|run|tampilin|tampilkan|output|keluarin|cek)?\s*(%s)\s*$" % "|".join(_CMD_NAMES),
    re.I,
)
_USER_RE = re.compile(
    r"\b(?:list|daftar|kasih|tunjukin|tampilin|lihat|siapa)\s+"
    r"(?:user|pengguna|akun|user-user|list-user|user-list)"
    r"|list\s+user\b|daftar\s+user\b|user\s+yang\s+ada\b|user\s+di\s+host\b|passwd\b",
    re.I,
)


def detect_hostcmd_request(text: str) -> dict | None:
    """Classify a free-text message as a host-cmd request, else ``None``.

    Returns ``{"kind": "read"|"write"|"dir"|"cmd", "path": ..., "content": ...}``.
    """
    t = (text or "").strip()
    low = t.lower()
    if not t or t.startswith("/"):
        return None
    m = _WRITE_RE.match(low)
    if m:
        path = m.group(1).strip()
        content = t[m.end():]
        content = content.strip()
        content = re.sub(r"^(?:dengan\s+)?isi\s+['\"]?", "", content, flags=re.I).strip()
        content = content.strip("'\"")
        return {"kind": "write", "path": path, "content": (content or "")[:2000]}
    m = _READ_RE.match(low)
    if m:
        path = (m.group(1) or m.group(2) or "").strip().rstrip(".,!?")
        return {"kind": "read", "path": path, "content": ""}
    m = _DIR_RE.match(low)
    if m:
        path = (m.group(1) or m.group(2) or "").strip()
        return {"kind": "dir", "path": path, "content": ""}
    m = _CMD_RE.match(low)
    if m:
        return {"kind": "cmd", "path": "", "content": (m.group(1) or "").lower()}
    if _USER_RE.search(low):
        return {"kind": "cmd", "path": "", "content": "passwd"}
    return None


def render_hostcmd_result(req: dict, raw: str) -> str:
    kind = req.get("kind")
    head = {
        "read": f"{bold('Isi file')} {mono(esc(req.get('path') or ''))}",
        "write": f"{bold('Host file')}",
        "dir": f"{bold('Host dir')}",
        "cmd": f"{bold('Host cmd')} {mono(esc(req.get('content') or ''))}",
    }.get(kind, bold("Host"))
    if kind in ("read", "cmd"):
        raw = f"<pre>{esc(raw)}</pre>"
    return f"{head}\n{raw}"