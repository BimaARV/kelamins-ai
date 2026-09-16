"""Tests for host executor (app/hostcmd.py): allow-list zones, file ops, commands.

The bot mounts host root at /host-root:rw; tests stub that mount to a temp dir.
"""

from __future__ import annotations

from app.hostcmd import (
    detect_hostcmd_request,
    host_list_dir,
    host_read_file,
    host_write_file,
    render_hostcmd_result,
    run_host_command,
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


def test_render_hostcmd_escapes_content():
    out = render_hostcmd_result({"kind": "read", "path": "/tmp/x"}, "<b>aaa</b>")
    assert "&lt;b&gt;aaa&lt;/b&gt;" in out