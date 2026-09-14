"""Tests for the cron/reminder scheduling module (parser + formatter, Redis-free)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.cron import (
    _ACK_OPENERS,
    build_cron_from_text,
    compute_next_run,
    cron_manage_request,
    format_ack,
    format_jobs_list,
    format_reminder,
)

WIB = ZoneInfo("Asia/Jakarta")


def _now(*args) -> datetime:
    return datetime(*args, tzinfo=WIB)


def test_example_today_once():
    res = build_cron_from_text(
        "Buatin gua cron buat hari ini aja, buat ingetin waktu pulang. Di jam 23.00",
        chat_id="123",
        now=_now(2026, 9, 13, 14, 0),
    )
    assert res.kind == "ok"
    assert res.job["message"] == "waktu pulang"
    assert res.job["repeat"] == "once"
    assert res.job["next_run"] == _now(2026, 9, 13, 23, 0).timestamp()
    assert res.job["chat_id"] == "123"
    assert res.job["weekday"] is None


def test_tomorrow_morning():
    res = build_cron_from_text(
        "Besok jam 7 pagi ingetin meeting",
        chat_id="1",
        now=_now(2026, 9, 13, 14, 0),
    )
    assert res.kind == "ok"
    assert res.job["message"] == "meeting"
    assert res.job["repeat"] == "once"
    assert res.job["next_run"] == _now(2026, 9, 14, 7, 0).timestamp()


def test_daily_future_and_past():
    res = build_cron_from_text(
        "Tiap hari jam 8 pagi ingetin sarapan",
        chat_id="1",
        now=_now(2026, 9, 13, 6, 0),
    )
    assert res.kind == "ok"
    assert res.job["repeat"] == "daily"
    assert res.job["next_run"] == _now(2026, 9, 13, 8, 0).timestamp()

    res2 = build_cron_from_text(
        "ingetin sarapan tiap hari jam 8 pagi",
        chat_id="1",
        now=_now(2026, 9, 13, 9, 0),
    )
    assert res2.kind == "ok"
    assert res2.job["next_run"] == _now(2026, 9, 14, 8, 0).timestamp()


def test_weekly():
    res = build_cron_from_text(
        "Tiap senin jam 9 pagi ingetin sprint planning",
        chat_id="1",
        now=_now(2026, 9, 13, 14, 0),  # Minggu (weekday 6)
    )
    assert res.kind == "ok"
    assert res.job["repeat"] == "weekly"
    assert res.job["weekday"] == 0
    assert res.job["next_run"] == _now(2026, 9, 14, 9, 0).timestamp()


def test_word_time_sore():
    res = build_cron_from_text(
        "Ingetin gua sore jam 5 buat olahraga",
        chat_id="1",
        now=_now(2026, 9, 13, 9, 0),
    )
    assert res.kind == "ok"
    assert res.job["message"] == "olahraga"
    assert res.job["next_run"] == _now(2026, 9, 13, 17, 0).timestamp()


def test_jangan_lupa_trigger():
    res = build_cron_from_text(
        "jangan lupa minum air jam 8 malam",
        chat_id="1",
        now=_now(2026, 9, 13, 9, 0),
    )
    assert res.kind == "ok"
    assert res.job["message"] == "minum air"
    assert res.job["next_run"] == _now(2026, 9, 13, 20, 0).timestamp()


def test_no_time_hint():
    res = build_cron_from_text("cron buat ingetin waktu pulang", chat_id="1", now=_now(2026, 9, 13, 14, 0))
    assert res.kind == "no_time"
    assert "jam berapa" in res.hint


def test_past_time_today():
    res = build_cron_from_text("cron hari ini jam 12.30", chat_id="1", now=_now(2026, 9, 13, 14, 0))
    assert res.kind == "past"
    assert "udah lewat" in res.hint


def test_not_cron_plain_message():
    assert build_cron_from_text("gua lagi makan siang hari ini", "1", _now(2026, 9, 13, 14, 0)).kind == "not_cron"
    assert build_cron_from_text("apakah besok libur?", "1", _now(2026, 9, 13, 14, 0)).kind == "not_cron"


def test_not_cron_server_question_goes_to_ai():
    res = build_cron_from_text(
        "bikin cron expression buat nginx reload tiap jam 3",
        "1",
        _now(2026, 9, 13, 14, 0),
    )
    assert res.kind == "not_cron"
    res2 = build_cron_from_text("cron buat shutdown server jam 12", "1", _now(2026, 9, 13, 14, 0))
    assert res2.kind == "not_cron"


def test_manage_detection():
    assert cron_manage_request("list cron") == ("list", "")
    assert cron_manage_request("crons") == ("list", "")
    assert cron_manage_request("hapus cron a1b2c3") == ("cancel", "a1b2c3")
    assert cron_manage_request("cancel cron abc123") == ("cancel", "abc123")
    assert cron_manage_request("gua cek cron lu dong") == ("", "")


def test_compute_next_run_weekly_boundary():
    now = _now(2026, 9, 14, 10, 0)  # Senin
    nxt = compute_next_run("weekly", 0, 9, 0, now)  # senin jam 9, sekarang udah lewat
    assert nxt == _now(2026, 9, 21, 9, 0)
    assert nxt.weekday() == 0
    nxt2 = compute_next_run("weekly", 1, 9, 0, now)  # selasa, besok
    assert nxt2 == _now(2026, 9, 15, 9, 0)


def test_compute_next_run_daily():
    now = _now(2026, 9, 13, 6, 0)
    assert compute_next_run("daily", None, 8, 0, now) == _now(2026, 9, 13, 8, 0)
    assert compute_next_run("daily", None, 5, 0, now) == _now(2026, 9, 14, 5, 0)


def test_format_ack_and_reminder():
    res = build_cron_from_text("Besok jam 7 pagi ingetin meeting", "1", _now(2026, 9, 13, 14, 0))
    ack = format_ack(res.job)
    assert res.job["id"] in ack
    assert "meeting" in ack
    assert "07:00 WIB" in ack
    rem = format_reminder(res.job)
    assert "meeting" in rem
    assert "Pengingat" in rem


def test_format_ack_opens_with_confirmation_line():
    res = build_cron_from_text("ingetin pulang jam 17.00", "1", _now(2026, 9, 13, 10, 0))
    ack = format_ack(res.job)
    assert any(opener in ack for opener in _ACK_OPENERS)
    assert res.job["id"] in ack
    assert "17:00 WIB" in ack


def test_format_ack_escapes_literal_id_tag():
    res = build_cron_from_text("ingetin pulang jam 17.00", "1", _now(2026, 9, 13, 10, 0))
    ack = format_ack(res.job)
    assert "<id>" not in ack
    assert "&lt;id&gt;" in ack


def test_format_jobs_list_empty_and_filled():
    assert "Belum ada cron" in format_jobs_list([])
    res = build_cron_from_text("ingetin pulang jam 17.00", "1", _now(2026, 9, 13, 10, 0))
    out = format_jobs_list([res.job])
    assert res.job["id"] in out
    assert "pulang" in out