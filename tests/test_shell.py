"""Tests for app.shell — whitelisted host command runner."""

import pytest

from app.shell import format_server_command, parse_server_request, run_network_cmd


def test_parse_server_request_cache_cases():
    assert parse_server_request("ip a") == ["ip", "a"]
    assert parse_server_request("ip addr") == ["ip", "addr"]
    assert parse_server_request("ip -4 a") == ["ip", "-4", "a"]
    assert parse_server_request("ip -6 a") == ["ip", "-6", "a"]
    assert parse_server_request("ifconfig") == ["ifconfig"]
    assert parse_server_request("traceroute 8.8.8.8") == ["traceroute", "8.8.8.8"]
    assert parse_server_request("traceroute google.com") == ["traceroute", "google.com"]


def test_parse_server_request_rejects_dangerous_input():
    assert parse_server_request("ip b") is None
    assert parse_server_request("ifconfig -a") is None
    assert parse_server_request("traceroute 999.1.1.1") is None
    assert parse_server_request("traceroute 127.0.0.1") is None
    assert parse_server_request("traceroute") is None
    assert parse_server_request("rm -rf /") is None
    assert parse_server_request("ip a && whoami") is None
    assert parse_server_request("") is None
    assert parse_server_request("halo bro") is None


def test_format_server_command_escapes_output():
    result = {"rc": 0, "stdout": "inet 10.0.0.1 <b>tag</b> & ampersand", "stderr": ""}
    out = format_server_command(["ip", "a"], result)
    assert "10.0.0.1" in out
    assert "<b>tag</b>" not in out
    assert "&lt;b&gt;" in out
    assert "Server" in out


@pytest.mark.asyncio
async def test_run_network_cmd_echo():
    result = await run_network_cmd(["echo", "hello"])
    assert result["rc"] == 0
    assert result["stdout"].strip() == "hello"