"""Unit tests for host/network diagnostics helpers
(hoststats, netinfo, geotrace, shell ping whitelist).
"""

from __future__ import annotations

import pytest

from app.geotrace import parse_traceroute_hops
from app.hoststats import (
    format_status,
    format_uptime,
    parse_cpu_stat,
    parse_loadavg,
    parse_meminfo,
    parse_netdev,
    parse_uptime,
)
from app.netinfo import _normalize_ipapi, _normalize_ipwho
from app.shell import parse_server_request


# ---------------------------------------------------------------------------
# hoststats parsers
# ---------------------------------------------------------------------------

def test_parse_cpu_stat():
    text = "cpu  100 0 100 1000 0 0 0 0\ncpu0 10 0 10 100 0 0 0 0\n"
    idle, total = parse_cpu_stat(text)
    assert idle == 1000
    assert total == 1200


def test_parse_cpu_stat_empty():
    assert parse_cpu_stat("") == (0, 0)


def test_parse_loadavg():
    assert parse_loadavg("0.10 0.20 0.30 1/100 1234") == (0.1, 0.2, 0.3)
    assert parse_loadavg("") == (0.0, 0.0, 0.0)


def test_parse_meminfo():
    text = "MemTotal:       16400000 kB\nMemAvailable:    4000000 kB\nSwapTotal: 0 kB\n"
    mem = parse_meminfo(text)
    assert mem["total_gb"] == pytest.approx(15.64, rel=0.01)
    assert mem["used_gb"] == pytest.approx(11.83, rel=0.02)
    assert 70 < mem["pct"] < 80


def test_parse_uptime_smoke():
    assert parse_uptime("123456.12 98765.43") == pytest.approx(123456.12)
    assert parse_uptime("") == 0.0


def test_format_uptime():
    assert format_uptime(0) == "?"
    assert format_uptime(95) == "1 mnt"
    assert "jam" in format_uptime(7200)
    assert "hari" in format_uptime(90000)


def test_parse_netdev():
    text = (
        "Inter-|   Receive\n"
        " face |rx-ok\n"
        "wlan0: 100 10 0 0 0 0 0 0 200 20 0 0 0 0 0 0\n"
        "lo:    5 5 0 0 0 0 0 0 5 5 0 0 0 0 0 0\n"
    )
    dev = parse_netdev(text)
    assert dev["wlan0"] == (100, 200)
    assert dev["lo"] == (5, 5)
    assert "lo0" not in dev


def test_format_status_host_lines():
    stats = {
        "uptime": "3 hari 2 jam",
        "cpu_percent": 12.5,
        "load": (0.1, 0.2, 0.1),
        "memory": {"pct": 55.0, "used_gb": 8.0, "total_gb": 16.0},
        "disk": {"pct": 44.0, "used_gb": 220.0, "total_gb": 500.0, "mount": "/host-root"},
        "network": {"wlan0": {"rx_mbps": 1.2, "tx_mbps": 0.3}},
    }
    out = format_status(stats, {"sources": 3, "events": 2})
    assert "Uptime" in out
    assert "CPU" in out
    assert "RAM" in out
    assert "Disk" in out
    assert "Network" in out
    assert "DB:" in out
    assert "sources 3" in out
    assert "events 2" in out


def test_format_status_disk_missing():
    stats = {"uptime": "1 mnt", "cpu_percent": 1.0, "load": (0, 0, 0),
             "memory": None, "disk": None, "network": {}}
    out = format_status(stats, None)
    assert "n/a" in out


# ---------------------------------------------------------------------------
# netinfo normalizers
# ---------------------------------------------------------------------------

def test_normalize_ipwho():
    item = {
        "success": True,
        "country": "US", "country_code": "US", "region": "CA",
        "city": "Mountain View",
        "timezone": {"id": "America/Los_Angeles", "offset": -25200},
        "connection": {"asn_number": 15169, "asn": "AS15169 Google LLC",
                       "isp": "Google", "org": "Google LLC"},
    }
    info = _normalize_ipwho(item)
    assert info["asn"] == 15169
    assert info["org"] == "Google LLC"
    assert info["tz"] == "America/Los_Angeles"


def test_normalize_ipwho_int_asn():
    item = {
        "success": True,
        "country": "US", "country_code": "US", "region": "CA",
        "city": "Mountain View",
        "timezone": {"id": "America/Los_Angeles", "offset": -25200},
        "connection": {"asn": 15169, "isp": "Google", "org": "Google LLC"},
    }
    info = _normalize_ipwho(item)
    assert info["asn"] == 15169
    assert info["asn_name"] == "AS15169"


def test_normalize_ipwho_fallback_asn_and_tz_dict():
    item = {
        "success": True,
        "country": "US", "country_code": "US", "region": "CA",
        "city": "Mountain View",
        "timezone": {"id": "America/Los_Angeles", "abbr": "PDT", "is_dst": True,
                     "offset": -25200, "utc": "-07:00"},
        "connection": {"asn_number": None, "asn": "AS15169 Google LLC",
                       "isp": "Google", "org": "Google LLC"},
    }
    info = _normalize_ipwho(item)
    assert info["asn"] == 15169
    assert info["tz"] == "America/Los_Angeles"


def test_normalize_ipwho_failure():
    assert _normalize_ipwho({"success": False}) is None


def test_normalize_ipapi():
    item = {
        "status": "success", "as": "AS15169 Google LLC", "isp": "Google LLC",
        "org": "Google LLC", "country": "United States", "countryCode": "US",
        "regionName": "California", "city": "Mountain View", "timezone": "America/Los_Angeles",
    }
    info = _normalize_ipapi(item)
    assert info["asn"] == 15169
    assert info["asn_name"] == "Google LLC"


def test_normalize_ipapi_failure():
    assert _normalize_ipapi({"status": "fail"}) is None


# ---------------------------------------------------------------------------
# geotrace
# ---------------------------------------------------------------------------

def test_parse_traceroute_hops():
    text = (
        "traceroute to 8.8.8.8 (8.8.8.8), 30 hops max\n"
        " 1  192.168.1.1  1.2 ms  1.1 ms\n"
        " 2  * * *\n"
        " 3  as13237.example.net (103.10.22.1)  15.4 ms\n"
        " 4  8.8.8.8  20.1 ms\n"
    )
    hops = parse_traceroute_hops(text)
    assert hops == ["192.168.1.1", "103.10.22.1", "8.8.8.8"]


def test_parse_traceroute_hops_empty():
    assert parse_traceroute_hops("") == []
    assert parse_traceroute_hops("all stars * * *") == []


# ---------------------------------------------------------------------------
# shell ping whitelist
# ---------------------------------------------------------------------------

def test_ping_parse_valid():
    assert parse_server_request("ping 8.8.8.8") == ["ping", "-c", "4", "-W", "2", "8.8.8.8"]
    assert parse_server_request("ping google.com")[-1] == "google.com"


def test_ping_parse_reject_localhost():
    assert parse_server_request("ping 127.0.0.1") is None


def test_ping_parse_allows_private():
    assert parse_server_request("ping 10.10.70.118") is not None


def test_ping_parse_reject_extra_args():
    assert parse_server_request("ping 8.8.8.8 -f") is None
    assert parse_server_request("ping") is None