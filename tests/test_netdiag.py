"""Free-text network-diag interception — real tools, no AI guessing.

Detection is pure (no network); ``run_diag`` calls are rendered with
monkeypatched tools so tests stay hermetic.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.interfaces.netdiag import detect_diag_request, detect_mention_diag, run_diag


_MONITORS = {
    "ro-bios": "103.153.42.237",
    "ro-univ": "103.153.42.130",
}

# Mirrors the live DB setup where a monitor's *name* contains the tool word
# "ping" ("Google Ping" = 8.8.8.8) — that name must NOT swallow requests meant
# for other, later monitors.
_MONITORS_FULL = {
    "google ping": "8.8.8.8",
    "cloudflare dns": "1.1.1.1",
    "sw-pop cyber": "192.168.248.2",
    "sw-pop datahall": "192.168.248.1",
    # Shared last-token "univ": router (ro-univ) vs switch (sw-main univ).
    # A "switch main univ" ask must reach the SWITCH, not the router.
    "ro-univ": "103.153.42.130",
    "sw-main univ": "172.16.88.254",
    **_MONITORS,
}


# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------

def test_detects_traceroute_intents():
    assert detect_diag_request("traceroute ke 8.8.8.8") == {
        "kind": "traceroute", "target": "8.8.8.8"
    }
    assert detect_diag_request("traceroute google.com")["kind"] == "traceroute"
    assert detect_diag_request("tolong traceroutein 1.1.1.1 dong")["kind"] == "traceroute"
    assert detect_diag_request("trace rute ke 8.8.8.8")["kind"] == "traceroute"
    assert detect_diag_request("cek rute ke 8.8.8.8") == {
        "kind": "traceroute", "target": "8.8.8.8"
    }
    assert detect_diag_request("lihat route ke google.com")["kind"] == "traceroute"
    assert detect_diag_request("liat rute ke 8.8.8.8")["kind"] == "traceroute"
    assert detect_diag_request("jelasin rute ke 8.8.8.8")["kind"] == "traceroute"
    assert detect_diag_request("rute ke 8.8.8.8")["kind"] == "traceroute"
    assert detect_diag_request("route ke google.com")["kind"] == "traceroute"
    assert detect_diag_request("trace 172.217.194.142") == {
        "kind": "traceroute", "target": "172.217.194.142"
    }


def test_detects_ping_intents():
    assert detect_diag_request("ping 1.1.1.1") == {"kind": "ping", "target": "1.1.1.1"}
    assert detect_diag_request("ping-in 8.8.8.8")["kind"] == "ping"
    assert detect_diag_request("cekin ping ke google.com dong")["kind"] == "ping"
    assert detect_diag_request("pingin install nginx bro") is None  # "pingin" != ping


def test_detects_asn_intents():
    assert detect_diag_request("cek asn 8.8.8.8") == {"kind": "asn", "target": "8.8.8.8"}
    assert detect_diag_request("asn 140444") == {"kind": "asn", "target": "140444"}
    assert detect_diag_request("AS 140444") == {"kind": "asn", "target": "140444"}
    assert detect_diag_request("autonomous dari 8.8.8.8")["kind"] == "asn"


def test_detects_geotrace_intents():
    assert detect_diag_request("geo-trace google.com") == {
        "kind": "geotrace", "target": "google.com"
    }
    assert detect_diag_request("geotrace 8.8.8.8")["kind"] == "geotrace"


def test_detects_ipinfo_intents():
    assert detect_diag_request("info ip") == {"kind": "ipinfo", "target": None}
    assert detect_diag_request("tampilin ipinfo")["kind"] == "ipinfo"
    assert detect_diag_request("kasih ifconfig dong")["kind"] == "ipinfo"
    assert detect_diag_request("tampilin ip a")["kind"] == "ipinfo"


def test_does_not_hijack_normal_chat():
    assert detect_diag_request("gua ada 2 VPS dengan ip 103.76.91.23, buatin nginx conf") is None
    assert detect_diag_request("cara cek koneksi yang bener?") is None
    assert detect_diag_request("whois 8.8.8.8") is None
    assert detect_diag_request("monitorin 8.8.8.8 terus") is None
    assert detect_diag_request("apa itu traceroute sih?") is None
    assert detect_diag_request("buatin route table ku dong") is None
    assert detect_diag_request("buatin route table ke 10.0.0.0/8") is None  # config, bukan trace


def test_rejects_bad_targets():
    assert detect_diag_request("traceroute ke 127.0.0.1") is None
    assert detect_diag_request("traceroute 999.1.1.1") is None
    assert detect_diag_request("ping") is None  # no target
    assert detect_diag_request("asn") is None
    assert detect_diag_request("/traceroute 8.8.8.8") is None  # slash -> commands


def test_bare_rute_not_hijacked_as_config():
    assert detect_diag_request("buatin route ke 10.0.0.0/8") is None
    assert detect_diag_request("ada config route ke 192.168.0.0?") is None


def test_detect_mention_diag():
    assert detect_mention_diag("Tolong trace ke BIOS dong", _MONITORS) == {
        "kind": "traceroute", "target": "103.153.42.237"
    }
    assert detect_mention_diag("ping ke RO-UNIV", _MONITORS) == {
        "kind": "ping", "target": "103.153.42.130"
    }
    assert detect_mention_diag("coba trace ke ro-bios", _MONITORS)["kind"] == "traceroute"
    assert detect_mention_diag("BIOS BIOS BIOS", _MONITORS) is None  # no diag word
    assert detect_mention_diag("trace ke apapun", _MONITORS) is None  # no known monitor
    assert detect_mention_diag("buatin route ro-bios", _MONITORS) is None  # config-talk
    assert detect_mention_diag("/trace", _MONITORS) is None


def test_mention_diag_intent_word_not_swallowed_by_monitor_name():
    # "ping ke switch cyber" must reach SW-POP CYBER, NOT "Google Ping" (8.8.8.8)
    # — the tool word inside a monitor name must not match itself.
    assert detect_mention_diag("tolong ping ke switch cyber", _MONITORS_FULL) == {
        "kind": "ping", "target": "192.168.248.2"
    }
    assert detect_mention_diag("ping switch cyber", _MONITORS_FULL)["target"] == "192.168.248.2"
    assert detect_mention_diag("tolong ping ke datahall", _MONITORS_FULL)["target"] == "192.168.248.1"
    assert detect_mention_diag("trace ke switch cyber", _MONITORS_FULL)["target"] == "192.168.248.2"


def test_mention_diag_toolword_named_monitor_still_usable():
    # The "Google Ping" monitor itself is still addressable by its full name.
    assert detect_mention_diag("ping ke google ping", _MONITORS_FULL) == {
        "kind": "ping", "target": "8.8.8.8"
    }
    assert detect_mention_diag("tolong ping ke cloudflare dns", _MONITORS_FULL)["target"] == "1.1.1.1"


def test_mention_diag_scoring_prefers_qualifier_over_shared_token():
    # "univ" is the last token of BOTH ro-univ and sw-main univ; the "switch"
    # qualifier must select the switch, not the router that comes first.
    assert detect_mention_diag("ping ke switch main univ", _MONITORS_FULL) == {
        "kind": "ping", "target": "172.16.88.254"
    }
    # Router ask still resolves to the router monitor.
    assert detect_mention_diag("ping ke router univ", _MONITORS_FULL) == {
        "kind": "ping", "target": "103.153.42.130"
    }
    assert detect_mention_diag("trace ke main univ", _MONITORS_FULL)["target"] == "172.16.88.254"
    # Ambiguous bare "univ" (no switch/router qualifier) -> first monitor wins.
    assert detect_mention_diag("ping ke univ", _MONITORS_FULL)["target"] == "103.153.42.130"


# ---------------------------------------------------------------------------
# run_diag rendering (tools monkeypatched)
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture()
async def fake_network_cmd(monkeypatch):
    async def _fake(args, timeout=None):
        return {
            "args": args,
            "rc": 0,
            "stdout": f"fake output for {args[0]}",
            "stderr": "",
        }

    monkeypatch.setattr("app.interfaces.netdiag.run_network_cmd", _fake)
    yield


async def test_run_diag_traceroute(fake_network_cmd):
    out = await run_diag("traceroute", "8.8.8.8")
    assert "traceroute 8.8.8.8" in out
    assert "fake output" in out
    assert "<pre>" in out


async def test_run_diag_ping(fake_network_cmd):
    out = await run_diag("ping", "google.com")
    assert "ping" in out and "google.com" in out


async def test_run_diag_ipinfo(fake_network_cmd):
    out = await run_diag("ipinfo", None)
    assert "ip a" in out


async def test_run_diag_traceroute_rejects_bad_target(fake_network_cmd):
    out = await run_diag("traceroute", "127.0.0.1")
    assert "gak valid" in out


async def test_run_diag_geotrace(monkeypatch):
    async def _fake_geo(target):
        return {"target": target, "rc": 0, "hops": ["1.1.1.1", "8.8.8.8"], "info": {}}

    monkeypatch.setattr("app.interfaces.netdiag.geotrace", _fake_geo)
    out = await run_diag("geotrace", "google.com")
    assert "Geo-Traceroute" in out
    assert "google.com" in out
    assert "1.1.1.1" in out


async def test_run_diag_asn_ip(monkeypatch):
    async def _fake_asn(ip):
        return {"asn": 15169, "org": "Google LLC", "country": "United States", "city": "Mountain View"}

    monkeypatch.setattr("app.interfaces.netdiag.asn_lookup", _fake_asn)
    out = await run_diag("asn", "8.8.8.8")
    assert "ASN" in out
    assert "AS15169" in out
    assert "Google LLC" in out


async def test_run_diag_asn_number(monkeypatch):
    async def _fake_asn_num(asn):
        return {"asn_name": "UNIV-ID", "asn_handle": "AS140444", "org": "Univ", "country": "ID"}

    monkeypatch.setattr("app.interfaces.netdiag.asn_number_lookup", _fake_asn_num)
    out = await run_diag("asn", "140444")
    assert "AS140444" in out
    assert "UNIV-ID" in out