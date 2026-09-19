"""Tests for host executor (app/hostcmd.py): allow-list zones, file ops, commands.

The bot mounts host root at /host-root:rw; tests stub that mount to a temp dir.
"""

from __future__ import annotations

from app.hostcmd import (
    detect_hostcmd_request,
    host_delete_path,
    host_list_dir,
    host_mkdir,
    host_read_file,
    host_rmdir,
    host_write_file,
    render_hostcmd_result,
    run_host_command,
    run_host_shell,
    validate_allowed_path,
)


def _mount(tmp_path, monkeypatch):
    import app.hostcmd as hc

    monkeypatch.setattr(hc, "HOST_ROOT_MOUNT", str(tmp_path))
    return tmp_path


def test_allowed_path_live(tmp_path, monkeypatch):
    _mount(tmp_path, monkeypatch)
    container, err = validate_allowed_path("/home/bima/test.txt")
    assert err is None and container.startswith(str(tmp_path))
    container, err = validate_allowed_path("/etc/passwd")
    assert err is not None and "zona" in err
    container, err = validate_allowed_path("relative/path")
    assert err is not None and "absolut" in err


def test_host_write_read_roundtrip(tmp_path, monkeypatch):
    _mount(tmp_path, monkeypatch)
    out = host_write_file("/home/bima/notes.txt", "halo dunia")
    assert "File ditulis" in out
    haystack = host_read_file("/home/bima/notes.txt")
    assert "halo dunia" in haystack


def test_host_read_outside_zone_denied(tmp_path, monkeypatch):
    _mount(tmp_path, monkeypatch)
    out = host_read_file("/etc/passwd")
    assert "zona" in out
    assert "/etc/passwd" not in out or "zona" in out


def test_host_list_dir(tmp_path, monkeypatch):
    _mount(tmp_path, monkeypatch)
    p = tmp_path / "home" / "bima"
    p.mkdir(parents=True)
    (p / "a.txt").write_text("a")
    out = host_list_dir("/home/bima")
    assert "a.txt" in out
    assert "1 entri" in out


def test_run_host_command_existing(monkeypatch):
    out = run_host_command("hostname")
    assert out.strip()
    assert "<pre>" not in out


def test_run_host_command_unknown():
    out = run_host_command("rm -rf /")
    assert "allowlist" in out


def test_detect_hostcmd_intents():
    assert detect_hostcmd_request("lscpu") == {"kind": "cmd", "path": "", "content": "lscpu"}
    assert detect_hostcmd_request("tampilkan free") == {"kind": "cmd", "path": "", "content": "free"}
    assert detect_hostcmd_request("buat file /home/bima/test.txt isi 'hai bro'") == {
        "kind": "write", "path": "/home/bima/test.txt", "content": "hai bro",
    }
    assert detect_hostcmd_request("cat file /var/log/syslog") == {"kind": "read", "path": "/var/log/syslog", "content": ""}
    assert detect_hostcmd_request("apa itu nginx?") is None
    assert detect_hostcmd_request("/sysinfo") is None
    assert detect_hostcmd_request("buatin config nginx buat gua") is None
    assert detect_hostcmd_request("list user yang ada di host") == {
        "kind": "cmd", "path": "", "content": "passwd",
    }
    assert detect_hostcmd_request("daftar user di host dong") == {
        "kind": "cmd", "path": "", "content": "passwd",
    }
    assert detect_hostcmd_request("passwd") == {"kind": "cmd", "path": "", "content": "passwd"}
    assert detect_hostcmd_request("kasih tau user yang ada") == {"kind": "cmd", "path": "", "content": "passwd"}


def test_render_hostcmd_escapes_content():
    out = render_hostcmd_result({"kind": "read", "path": "/tmp/x"}, "<b>aaa</b>")
    assert "&lt;b&gt;aaa&lt;/b&gt;" in out


# ---------------------------------------------------------------------------
# File ops: delete / mkdir / rmdir
# ---------------------------------------------------------------------------


def test_host_mkdir_delete_rmdir(tmp_path, monkeypatch):
    _mount(tmp_path, monkeypatch)
    out = host_mkdir("/home/bima/lab")
    assert "Folder dibuat" in out
    out = host_delete_path("/home/bima/lab")
    assert "Folder dihapus" in out
    out = host_rmdir("/home/bima/lab")
    assert "bukan folder" in out


def test_host_delete_file_and_nonempty_dir(tmp_path, monkeypatch):
    _mount(tmp_path, monkeypatch)
    p = tmp_path / "home" / "bima" / "x.txt"
    p.parent.mkdir(parents=True)
    p.write_text("hi")
    out = host_delete_path("/home/bima/x.txt")
    assert "File dihapus" in out
    (p.parent / "y.txt").write_text("b")
    out = host_delete_path("/home/bima")
    assert "gak kosong" in out and "rm -r" in out
    out = host_rmdir("/home/bima")
    assert "gak kosong" in out


def test_host_mkdir_delete_outside_zone(tmp_path, monkeypatch):
    _mount(tmp_path, monkeypatch)
    out = host_mkdir("/etc/lab")
    assert "zona" in out
    out = host_delete_path("/etc/passwd")
    assert "zona" in out


# ---------------------------------------------------------------------------
# Host shell: chroot fallback, blocklist, intent
# ---------------------------------------------------------------------------


def test_run_host_shell_echo():
    out = run_host_shell("echo halo")
    assert "halo" in out


def test_run_host_shell_blocked():
    out = run_host_shell("rm -rf /")
    assert "diblokir" in out
    out = run_host_shell("reboot")
    assert "diblokir" in out
    out = run_host_shell("dd if=/dev/zero of=/dev/sda")
    assert "diblokir" in out


def test_run_host_shell_disabled(monkeypatch):
    import app.hostcmd as hc
    monkeypatch.setattr(hc.settings, "hostcmd_enable_shell", False)
    out = run_host_shell("echo halo")
    assert "dinonaktifkan" in out


def test_detect_host_delete_mkdir_rmdir_intents():
    assert detect_hostcmd_request("hapus file /home/bima/notes.txt") == {
        "kind": "delete", "path": "/home/bima/notes.txt", "content": "",
    }
    assert detect_hostcmd_request("hapusin /tmp/x.txt dong") == {
        "kind": "delete", "path": "/tmp/x.txt", "content": "",
    }
    assert detect_hostcmd_request("hapus folder /tmp/x") == {
        "kind": "rmdir", "path": "/tmp/x", "content": "",
    }
    assert detect_hostcmd_request("buat folder /home/bima/lab") == {
        "kind": "mkdir", "path": "/home/bima/lab", "content": "",
    }
    assert detect_hostcmd_request("bikin dir /tmp/lab2") == {
        "kind": "mkdir", "path": "/tmp/lab2", "content": "",
    }


def test_detect_read_with_tolong_prefix():
    assert detect_hostcmd_request("tolong baca file /home/deimonji/Downloads") == {
        "kind": "read", "path": "/home/deimonji/Downloads", "content": "",
    }
    assert detect_hostcmd_request("baca isi /var/log/syslog") == {
        "kind": "read", "path": "/var/log/syslog", "content": "",
    }


def test_detect_raw_shell_first_token(monkeypatch):
    import app.hostcmd as hc
    monkeypatch.setattr(hc, "host_command_exists", lambda name: name == "myprog")
    assert detect_hostcmd_request("myprog -la /home/x 5") == {
        "kind": "shell", "path": "", "content": "myprog -la /home/x 5",
    }


def test_detect_raw_shell_prose_not_hijacked(monkeypatch):
    """Chat that merely *starts with* a host binary ('ip semua switch') must
    never be executed as an arbitrary shell command on the host."""
    import app.hostcmd as hc
    monkeypatch.setattr(hc, "host_command_exists", lambda name: True)
    assert detect_hostcmd_request("ip semua switch dong") is None
    assert detect_hostcmd_request("ip semua switch") is None
    assert detect_hostcmd_request("ping semua switch yang dipantau") is None
    assert detect_hostcmd_request("pingin switch yang lagi up") is None
    # command-looking lines (no prose filler) still run raw
    assert detect_hostcmd_request("ls -la /home") == {
        "kind": "shell", "path": "", "content": "ls -la /home",
    }
    assert detect_hostcmd_request("git status") == {
        "kind": "shell", "path": "", "content": "git status",
    }


def test_detect_voice_shell(monkeypatch):
    import app.hostcmd as hc
    monkeypatch.setattr(hc, "host_command_exists", lambda name: name == "myprog")
    assert detect_hostcmd_request("jalanin myprog buat gua") == {
        "kind": "shell", "path": "", "content": "myprog",
    }


def test_detect_shell_question_not_hijacked(monkeypatch):
    import app.hostcmd as hc
    monkeypatch.setattr(hc, "host_command_exists", lambda name: name == "myprog")
    assert detect_hostcmd_request("myprog harus jalanin apa?") is None
    assert detect_hostcmd_request("hapus /home/x gimana caranya?") is None
    assert detect_hostcmd_request("kenapa hapus /home/x?") is None
    assert detect_hostcmd_request("cara bikin server apa sekarang?") is None


def test_detect_question_gates_file_ops():
    assert detect_hostcmd_request("hapus file /home/bima/notes.txt gimana?") is None
    assert detect_hostcmd_request("buat folder /tmp/x untuk apa?") is None


def test_detect_colloquial_command_map():
    assert detect_hostcmd_request("lscpu dong") == {
        "kind": "shell", "path": "", "content": "lscpu",
    }
    assert detect_hostcmd_request("jalanin top dong") == {
        "kind": "cmd", "path": "", "content": "top",
    }
    assert detect_hostcmd_request("cek free") == {"kind": "cmd", "path": "", "content": "free"}


def test_render_hostcmd_shell_pre():
    out = render_hostcmd_result({"kind": "shell", "path": "", "content": "ls -la"}, "row <b>x</b>")
    assert "<pre>" in out and "&lt;b&gt;" in out