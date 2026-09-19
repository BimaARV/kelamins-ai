"""Host file + shell + system-command access for the chat bot.

The bot container mounts the host root at ``/host-root`` (read-write) and runs
as root inside the container. Everything goes through guards, but this module
now behaves like a remote shell into the host:

  * **File zones** — only paths under ``HOSTCMD_ALLOWED_ROOTS`` (default
    ``["/home", "/tmp", "/var/log"]``) can be read/written/listed/deleted.
    Realpath is resolved so symlinks can't escape the allowed roots or
    ``/host-root``.
  * **Shell** — ``run_host_shell`` executes arbitrary ``sh -c`` commands
    *chroot'd into the host root*, so binaries, libraries, ``/etc``, ``/home``
    and ``/proc`` all reflect the HOST (not the container). A configurable
    blocklist (``HOSTCMD_BLOCKED_RE``) rejects clearly destructive commands
    (reboot/shutdown/mkfs/dd/``rm -rf /``…). 20s timeout + output cap.
  * **File ops** — read, write, list (dir), delete (file / empty dir), mkdir,
    rmdir — all path-validated.
  * **Command map** — a small exact-argv map (lscpu/df/free/top/…) stays as a
    fast path so those keep canonical flags; everything else can run via shell.

When ``/host-root`` is not mounted (tests / non-bot containers), the shell
runs directly against the container and ``host_command_exists`` returns False
for host-binary lookups — so unit tests never touch the real host.
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


def _kind(path: str) -> str:
    return "dir " if os.path.isdir(path) else "file"


def _size_label(path: str) -> str:
    try:
        n = os.path.getsize(path)
    except OSError:
        return ""
    if n >= 1 << 20:
        return f" · {n / (1 << 20):.1f} MB"
    if n >= 1 << 10:
        return f" · {n / (1 << 10):.0f} KB"
    return f" · {n} B"


def host_read_file(host_path: str, max_chars: int = 2500) -> str:
    container, err = validate_allowed_path(host_path)
    if err:
        return f"{bold('Host file')} — {err}"
    if os.path.isdir(container):
        try:
            entries = sorted(os.listdir(container))
        except OSError as exc:
            return f"{bold('Host file')} — gagal list: {esc(str(exc))}"
        lines = [f"{bold('Isi')} {mono(esc(host_path))} ({len(entries)} entri):"]
        for e in entries[:60]:
            full = os.path.join(container, e)
            lines.append(f"  {mono(_kind(full))} {esc(e)}{_size_label(full)}")
        if len(entries) > 60:
            lines.append(f"  … dan {len(entries) - 60} entri lagi.")
        return "\n".join(lines)
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
        lines.append(f"  {mono(_kind(full))} {esc(e)}{_size_label(full)}")
    if len(entries) > 60:
        lines.append(f"  … dan {len(entries) - 60} entri lagi.")
    return "\n".join(lines)


def host_mkdir(host_path: str) -> str:
    container, err = validate_allowed_path(host_path)
    if err:
        return f"{bold('Host dir')} — {err}"
    try:
        os.makedirs(container, exist_ok=True)
    except OSError as exc:
        return f"{bold('Host dir')} — gagal buat folder: {esc(str(exc))}"
    return f"{bold('Folder dibuat')} — {mono(esc(host_path))}."


def host_delete_path(host_path: str) -> str:
    """Delete a file, or an *empty* directory. Non-empty dirs hint at shell."""
    container, err = validate_allowed_path(host_path)
    if err:
        return f"{bold('Host file')} — {err}"
    if os.path.isdir(container):
        try:
            os.rmdir(container)
        except OSError as exc:
            return (
                f"{bold('Host file')} — folder {mono(esc(host_path))} gak kosong "
                f"({esc(str(exc))}).\nBuat hapus pohonnya: {mono('rm -r ' + host_path)}"
            )
        return f"{bold('Folder dihapus')} — {mono(esc(host_path))}."
    try:
        os.remove(container)
    except OSError as exc:
        return f"{bold('Host file')} — gagal hapus: {esc(str(exc))}"
    return f"{bold('File dihapus')} — {mono(esc(host_path))}."


def host_rmdir(host_path: str) -> str:
    """Delete a directory (empty). Files are rejected."""
    container, err = validate_allowed_path(host_path)
    if err:
        return f"{bold('Host dir')} — {err}"
    if not os.path.isdir(container):
        return f"{bold('Host dir')} — {mono(esc(host_path))} itu bukan folder."
    try:
        os.rmdir(container)
    except OSError as exc:
        return (
            f"{bold('Host dir')} — folder {mono(esc(host_path))} gak kosong "
            f"({esc(str(exc))}).\nBuat hapus pohonnya: {mono('rm -r ' + host_path)}"
        )
    return f"{bold('Folder dihapus')} — {mono(esc(host_path))}."


# ---------------------------------------------------------------------------
# Command map (exact argv, no shell) + arbitrary host shell (chroot'd)
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

_DEFAULT_BLOCKED = (
    r"(?:^|[;&|]\s*)("
    r"reboot|poweroff|halt|shutdown|"
    r"systemctl\s+(?:poweroff|reboot|halt)|"
    r"init\s+[06]|"
    r"mkfs(?:\.[a-z0-9]+)?\b|"
    r"fdisk|cfdisk|sfdisk|parted|"
    r"dd\b|"
    r"nc\b|ncat\b|netcat\b|"
    r"chmod(?:\s+-R)?\s+[0-7]{3,4}\s+/\s*$|"
    r"chown(?:\s+-R)?\s+[0-9a-z_.-]+:[0-9a-z_.-]+\s+/\s*$|"
    r"rm\s+(?:-rf|-fr|-r\s*-f|-f\s*-r|-r)\s+(/?\s*$|/etc|/usr|/var|/bin|/sbin|/opt|/boot|/lib|/root|/home|/tmp)|"
    r"find\s+.*-delete|find\s+.*-exec\s+.*\brm\b|"
    r":\(\)\s*\{|fork\s+bomb"
    r")"
)


def _output_cap() -> int:
    try:
        return max(int(settings.hostcmd_output_max_chars or OUTPUT_MAX_CHARS), 500)
    except Exception:  # noqa: BLE001
        return OUTPUT_MAX_CHARS


def _blocked(command: str) -> bool:
    raw = (getattr(settings, "hostcmd_blocked_re", "") or "").strip()
    pattern = raw or _DEFAULT_BLOCKED
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error:
        regex = re.compile(_DEFAULT_BLOCKED, re.IGNORECASE)
    return bool(regex.search(command or ""))


def _can_chroot() -> bool:
    return os.path.isdir(HOST_ROOT_MOUNT) and os.path.exists(
        os.path.join(HOST_ROOT_MOUNT, "bin", "sh")
    )


def _shell_argv(command: str) -> list[str]:
    prefix = ["chroot", HOST_ROOT_MOUNT] if _can_chroot() else []
    return [*prefix, "/bin/sh", "-c", command]


def run_host_command(name: str) -> str:
    """Fast path: exact-argv map (keeps canonical flags, e.g. top -b -n 1)."""
    argv = COMMAND_ARGV.get(name)
    if argv is None:
        if _blocked(name):
            return f"{bold('Host cmd')} — {mono(esc(name[:80]))} diblokir allowlist (command destruktif/berbahaya)."
        return f"{bold('Host cmd')} — {mono(esc(name))} nggak ada di command map/allowlist."
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return f"{bold('Host cmd')} — {mono(esc(name))} timeout (>20s)."
    except OSError as exc:
        return f"{bold('Host cmd')} — gagal jalan: {esc(str(exc))}"
    out = (proc.stdout or "") or (proc.stderr or "")
    return _cap(out) or "(tidak ada output)"


def run_host_shell(command: str, timeout: int = 20) -> str:
    """Run arbitrary shell against the HOST root (chroot /host-root /bin/sh -c).

    When ``/host-root`` isn't mounted (tests), falls back to a direct ``sh -c``.
    """
    command = (command or "").strip()
    if not command:
        return f"{bold('Host cmd')} — command kosong."
    if not bool(getattr(settings, "hostcmd_enable_shell", True)):
        return f"{bold('Host cmd')} — shell dinonaktifkan (HOSTCMD_ENABLE_SHELL=false)."
    if _blocked(command):
        return (
            f"{bold('Host cmd')} — {mono(esc(command[:80]))} diblokir "
            f"(command destruktif/berbahaya)."
        )
    argv = _shell_argv(command)
    env = {
        **os.environ,
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    }
    cwd = HOST_ROOT_MOUNT if os.path.isdir(HOST_ROOT_MOUNT) else None
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd)
    except subprocess.TimeoutExpired:
        return f"{bold('Host cmd')} — timeout (> {timeout}s)."
    except OSError as exc:
        return f"{bold('Host cmd')} — gagal jalan: {esc(str(exc))}"
    out = (proc.stdout or "") or (proc.stderr or "")
    return _cap(out) or "(tidak ada output)"


def _cap(out: str) -> str:
    cap = _output_cap()
    if len(out) > cap:
        return out[:cap] + f"\n… (dipotong di {cap} char)"
    return out


def host_command_exists(name: str) -> bool:
    """True when ``name`` is an executable on the HOST (via /host-root PATH)."""
    name = (name or "").strip()
    if not name or "/" in name or any(ch in name for ch in (";", "&", "|", "$", "`")):
        return False
    if name in COMMAND_ARGV:
        return True
    base = HOST_ROOT_MOUNT
    for d in ("/usr/local/bin", "/usr/local/sbin", "/usr/bin", "/usr/sbin", "/bin", "/sbin"):
        full = base + d + "/" + name
        try:
            if os.path.isfile(full) and os.access(full, os.X_OK):
                return True
        except OSError:
            continue
    return False


# ---------------------------------------------------------------------------
# Free-text intent
# ---------------------------------------------------------------------------

_P_PATH = r"(/(?:home|tmp|var)(?:/[^\s'\"!?;()]*)?)"

_READ_VERB = r"(?:baca|bacaain|lihat|liat|cat|open|show|read|tampilin|tampilkan|isi)"
_READ_RE = re.compile(
    rf"(?:^|[\s.,:;]){_READ_VERB}(?:(?:\s+)(?:file|folder|dir|direktori|isi))?(?:\s*)"
    rf"['\"]*{_P_PATH}",
    re.I,
)
_DIR_VERB = r"(?:lihat|liat|ls|list|daftar|tampilin|tampilkan|catin)"
_DIR_RE = re.compile(
    rf"(?:^|[\s.,:;]){_DIR_VERB}(?:(?:\s+)(?:folder|dir|direktori|isi))?(?:\s*)"
    rf"['\"]*{_P_PATH}",
    re.I,
)
_DELETE_RE = re.compile(
    r"(?:^|[\s.,:;])"
    r"(?:hapus|hapusin|delete|remove|buang|rm)\s+(?:(?:file)\s+)?"
    rf"['\"]*{_P_PATH}",
    re.I,
)
_MKDIR_RE = re.compile(
    r"(?:^|[\s.,:;])"
    r"(?:buat|bikin|create|tambah|new|mkdir)\s+(?:folder|dir|direktori|directory)\s+"
    rf"['\"]*{_P_PATH}",
    re.I,
)
_RMDIR_RE = re.compile(
    r"(?:^|[\s.,:;])"
    r"(?:hapus|hapusin|delete|remove|buang|rmdir)\s+(?:folder|dir|direktori|directory)\s+"
    rf"['\"]*{_P_PATH}",
    re.I,
)
_WRITE_RE = re.compile(
    rf"^(?:buat|buatin|bikin|bikinin|tulis|tulisin|create|write)\s+file\s+({_P_PATH})", re.I
)
_CMD_NAMES = tuple(sorted(COMMAND_ARGV))
_CMD_RE = re.compile(
    r"^(?:tolong\s+)?(?:jalanin|jalankan|run|tampilin|tampilkan|output|keluarin|cek|eksekusi)?\s*(%s)\s*$" % "|".join(_CMD_NAMES),
    re.I,
)
_VOICE_RE = re.compile(
    r"^(?:tolong\s+)?(?:jalanin|jalankan|run|tampilin|tampilkan|output|keluarin|eksekusi)\s+([a-z][a-z0-9_.\-]*)(?:\s+(.+))?$",
    re.I,
)
# Words that are BOTH common in casual chat and map/shell names -> only auto-run
# with an explicit voice verb or a bare exact match, never with sentence tail.
_RISKY_NAMES = {
    "id", "top", "ps", "ss", "free", "hostname",
    "traceroute", "trace", "route", "rute", "ping", "geo", "geotrace",
    "asn", "ipinfo", "ifconfig",
}
_QUESTION_RE = re.compile(
    r"\b(?:gimana|bagaimana|kenapa|kok\b|kapan|apa|berapa|cara|sering|selalu|siapa|dimana|kemana|apakah)\b",
    re.I,
)
_USER_RE = re.compile(
    r"\b(?:list|daftar|kasih|tunjukin|tampilin|lihat|siapa)\s+(?:user|pengguna|akun|user-user|list-user|user-list)"
    r"|list\s+user\b|daftar\s+user\b|user\s+yang\s+ada\b|user\s+di\s+host\b|passwd\b",
    re.I,
)


def _is_question(text: str) -> bool:
    return bool(_QUESTION_RE.search(text or ""))


# Conversational/operational words that signal a chat message rather than a
# real shell invocation ("ip semua switch" asks about switches, it does NOT
# mean run the ``ip`` binary). Used only as a gate on the *bare-first-token*
# arbitrary-shell branch; explicitly voiced commands still run.
_PROSE_RE = re.compile(
    r"\b("
    r"semua|semuanya|yang|itu|ini|itu|situ|nya|aja|dong|deh|dah|dung|ya|yah|"
    r"tolong|minta|bantu|plis|please|bos|bro|cuy|woi|hey|bang|kak|mas|mbak|"
    r"gw|gua|gue|aku|saya|lo|lu|kamu|kalian|kami|kita|"
    r"switch|sw-|router|server|monitor|monitoring|pantau|pantauan|dipantau|"
    r"network|jaringan|vps|vpn|perangkat|device|berarti|gitu|gini|"
    r"dan|atau|kalau|kalo|biar|supaya|buat|untuk|dari|ke|pada|sama|di|"
    r"harus|bisa|mau|sedang|lagi|habis|udah|sudah|belum|nggak|gak|ngga|"
    r"dll|dst|etc|dst|apa|kapan|kenapa|gimana|berapa|yang"
    r")\b",
    re.I,
)


def _has_prose(text: str) -> bool:
    return bool(_PROSE_RE.search(text or ""))


def _strip_filler(text: str) -> str:
    t = (text or "").strip().strip(" .,!?;:")
    t = re.sub(r"\s+(dong|deh|donk|dung|ya|yah|plis|please|mas|mbak|kak|bang|bro|cuy|woi|hey)\s*$", "", t, flags=re.I)
    return t.strip()


def _looks_like_shell(text: str) -> bool:
    if not text or len(text) > 300 or _is_question(text):
        return False
    if any(sep in text for sep in ("\n", "\t")):
        return False
    toks = text.split()
    first = toks[0]
    if first in _CMD_NAMES:
        return first not in _RISKY_NAMES or len(toks) <= 1
    # Bare first-token "raw shell" only fires for a command-looking line.
    # Casual chat that happens to start with a host binary ("ip semua switch",
    # "ping semua switch yang dipantau") must NOT be executed as a shell.
    if _has_prose(text):
        return False
    return host_command_exists(first)


def _path_of(text: str, match) -> str:
    """Case-preserving path capture (regex ran on lowercased text)."""
    return text[match.start(1):match.end(1)].strip().rstrip(".,!?;")


def detect_hostcmd_request(text: str) -> dict | None:
    """Classify a free-text message as a host-cmd request, else ``None``.

    Kinds: ``read|write|dir|delete|mkdir|rmdir|cmd|shell``.
    """
    t = (text or "").strip()
    low = t.lower()
    if not t or t.startswith("/"):
        return None
    question = _is_question(low)
    if question:
        return None

    m = _WRITE_RE.match(low)
    if m:
        path = _path_of(t, m)
        content = t[m.end():]
        content = content.strip()
        content = re.sub(r"^(?:dengan\s+)?isi\s+['\"]?", "", content, flags=re.I).strip()
        content = content.strip("'\"")
        return {"kind": "write", "path": path, "content": (content or "")[:2000]}

    m = _RMDIR_RE.search(low)
    if m:
        return {"kind": "rmdir", "path": _path_of(t, m), "content": ""}
    m = _MKDIR_RE.search(low)
    if m:
        return {"kind": "mkdir", "path": _path_of(t, m), "content": ""}
    m = _DELETE_RE.search(low)
    if m:
        return {"kind": "delete", "path": _path_of(t, m), "content": ""}

    m = _READ_RE.search(low)
    if m:
        return {"kind": "read", "path": _path_of(t, m), "content": ""}
    m = _DIR_RE.search(low)
    if m:
        return {"kind": "dir", "path": _path_of(t, m), "content": ""}

    m = _CMD_RE.match(low)
    if m:
        return {"kind": "cmd", "path": "", "content": (m.group(1) or "").lower()}

    m = _VOICE_RE.match(low)
    if m:
        name = m.group(1).lower()
        if name in COMMAND_ARGV:
            return {"kind": "cmd", "path": "", "content": name}
        if host_command_exists(name):
            return {"kind": "shell", "path": "", "content": name}

    low2 = _strip_filler(low)
    if _looks_like_shell(low2):
        return {"kind": "shell", "path": "", "content": low2}

    if _USER_RE.search(low):
        return {"kind": "cmd", "path": "", "content": "passwd"}
    return None


def render_hostcmd_result(req: dict, raw: str) -> str:
    kind = req.get("kind")
    head = {
        "read": f"{bold('Isi file')} {mono(esc(req.get('path') or ''))}",
        "write": f"{bold('Host file')}",
        "dir": f"{bold('Host dir')}",
        "delete": f"{bold('Host delete')}",
        "mkdir": f"{bold('Host mkdir')}",
        "rmdir": f"{bold('Host rmdir')}",
        "cmd": f"{bold('Host cmd')} {mono(esc(req.get('content') or ''))}",
        "shell": f"{bold('Host shell')} {mono(esc(req.get('content') or ''))}",
    }.get(kind, bold("Host"))
    if kind in ("read", "cmd", "shell"):
        raw = f"<pre>{esc(raw)}</pre>"
    return f"{head}\n{raw}"