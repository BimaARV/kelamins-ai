"""Tests for the interactive Telegram bot commands + formatting.

Uses an in-memory SQLite engine (same pattern as test_documents_repo.py).
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    Alert,
    AlertMessage,
    AlertMessageKind,
    Article,
    Document,
    DocumentType,
    Earthquake,
    Event,
    EventType,
    EventStatus,
    NetworkCheck,
    NetworkTarget,
    Source,
    WeatherForecast,
    WeatherLocation,
)
from app.interfaces.formatter import bold, chunk_html, dt, esc, md_to_html, mono, num


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture()
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


# ---------------------------------------------------------------------------
# Formatter unit tests
# ---------------------------------------------------------------------------

def test_esc_html():
    assert esc("<b>x</b>") == "&lt;b&gt;x&lt;/b&gt;"
    assert esc("normal") == "normal"


def test_bold_wraps():
    assert bold("test") == "<b>test</b>"
    assert bold("a<b>") == "<b>a&lt;b&gt;</b>"


def test_mono_wraps():
    assert mono("cmd") == "<code>cmd</code>"


def test_num_none():
    assert num(None) == "?"
    assert num(None, 2) == "?"


def test_num_rounded():
    assert num(3.14159, 1) == "3.1"
    assert num(5) == "5"


def test_dt_none():
    assert dt(None) == "—"


def test_dt_string():
    assert dt("2026-09-10T12:00:00+00:00") == "2026-09-10 12:00"


def test_md_to_html_bold_and_code():
    out = md_to_html("**goblok** dan `kode` saja.")
    assert "<b>goblok</b>" in out
    assert "<code>kode</code>" in out


def test_md_to_html_no_raw_injection():
    out = md_to_html("<script>alert(1)</script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" not in out


def test_md_to_html_neutralize_html():
    out = md_to_html('<b style="color: #00ff00;">GOOGLE</b>')
    assert "<b>GOOGLE</b>" in out
    assert "color" not in out
    out = md_to_html('<span style="color: #ff0000;">red</span> dan <i>x</i>')
    assert "red" in out and "color" not in out
    assert "<i>x</i>" in out
    out = md_to_html("<br>baris<br>baru")
    assert "\n" in out
    out = md_to_html("nya `<code>kode</code>`")
    assert "<code>kode</code>" in out


def test_md_to_html_neutralize_latex():
    out = md_to_html("arah $\\rightarrow$ kanan")
    assert "arah → kanan" in out
    assert "$" not in out
    out = md_to_html("kecepatan \\frac{1}{2}c")
    assert "kecepatan 1/2c" in out
    out = md_to_html("$x \\times y$ dan $a \\leq b$")
    assert "x × y" in out and "a ≤ b" in out
    out = md_to_html("alpha \\alpha gamma \\gamma")
    assert "α" in out and "γ" in out
    out = md_to_html("```\n$x$ \\rightarrow\n```")
    assert "<pre>" in out and "$x$ \\rightarrow" in out
    assert "→" not in out


def test_md_to_html_plain_unchanged():
    assert md_to_html("halo → dunia") == "halo → dunia"


def test_chunk_html_short():
    assert chunk_html("<b>hai</b>") == ["<b>hai</b>"]


def test_chunk_html_long_paragraph():
    text = "\n".join(f"line {i} " + "x" * 80 for i in range(300))
    chunks = chunk_html(text, max_chars=1000)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)
    assert "\n".join(chunks) == text


def test_chunk_html_big_pre_block_rebalanced():
    inner = "\n".join(f"code line {i}" for i in range(5000))
    text = f"<pre>{inner}</pre>"
    chunks = chunk_html(text, max_chars=1000)
    assert len(chunks) > 1
    assert all(c.startswith("<pre>") and c.endswith("</pre>") for c in chunks)
    assert all(len(c) <= 1011 for c in chunks)  # 1000 inner + <pre></pre> overhead
    # Recover original lines ignoring injected </pre> / <pre> split markers.
    recovered: list[str] = []
    for c in chunks:
        for line in c[len("<pre>") : -len("</pre>")].split("\n"):
            if line not in ("</pre>", "<pre>"):
                recovered.append(line)
    assert recovered == inner.split("\n")


def test_chunk_html_mixed_normal_and_pre():
    lines = [f"text {i} " + "y" * 50 for i in range(100)]
    code = [f"cfg {i}" for i in range(2000)]
    text = "\n".join(lines) + "\n<pre>" + "\n".join(code) + "</pre>\n" + "\n".join(lines)
    chunks = chunk_html(text, max_chars=1500)
    assert len(chunks) > 1
    assert all(len(c) <= len("<pre></pre>") + 1500 for c in chunks)
    import re
    flat = re.sub(r"</?pre>", "", "\n".join(chunks))
    skeleton = re.sub(r"</?pre>", "", text)
    assert flat == skeleton


def test_chunk_html_inner_tags_stay_on_single_line():
    text = "<b>bold</b> dan <i>miring</i> serta <code>kode</code>\n" * 200
    chunks = chunk_html(text, max_chars=800)
    assert all(len(c) <= 800 for c in chunks)
    assert "\n".join(chunks) == text


# ---------------------------------------------------------------------------
# Command dispatch tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_help_returns_help(session):
    from app.interfaces.commands import dispatch
    out = await dispatch("/help", session)
    assert "Perintah" in out
    assert "/news" in out
    assert "/gempa" in out


@pytest.mark.asyncio
async def test_help_alias(session):
    from app.interfaces.commands import dispatch
    out = await dispatch("/start", session)
    assert "Perintah" in out


@pytest.mark.asyncio
async def test_unknown_command(session):
    from app.interfaces.commands import dispatch
    out = await dispatch("/foobar", session)
    assert "tidak dikenal" in out
    assert "/help" in out


@pytest.mark.asyncio
async def test_news_empty(session):
    from app.interfaces.commands import dispatch
    out = await dispatch("/news", session)
    assert "berita" in out.lower() or "Belum ada" in out


@pytest.mark.asyncio
async def test_news_with_articles(session):
    from app.interfaces.commands import dispatch
    src = Source(name="TestFeed", base_url="https://example.com", feed_url="https://example.com/rss", enabled=True)
    session.add(src)
    await session.flush()
    art = Article(source_id=src.id, title="Gempa M6.0 di Papua", url="https://example.com/1")
    session.add(art)
    await session.commit()
    out = await dispatch("/news", session)
    assert "Gempa M6.0 di Papua" in out
    assert "TestFeed" in out


def test_news_push_due_logic():
    from app.interfaces.telegram import _news_push_due
    interval = 10800.0
    now = 1_000_000.0
    assert _news_push_due(now, last_push=0, interval=interval) is True
    assert _news_push_due(now + interval - 1, last_push=now, interval=interval) is False
    assert _news_push_due(now + interval, last_push=now, interval=interval) is True
    assert _news_push_due(now + interval * 3, last_push=now, interval=interval) is True


@pytest.mark.asyncio
async def test_news_tech_empty(session):
    from app.interfaces.commands import dispatch
    out = await dispatch("/news-tech", session)
    assert "belum ada data" in out.lower() or "Berita Tech" in out


@pytest.mark.asyncio
async def test_news_tech_filters_by_source(session):
    from app.interfaces.commands import dispatch
    tech_src = Source(name="CNN Indonesia Teknologi", base_url="https://www.cnnindonesia.com/teknologi", feed_url="https://www.cnnindonesia.com/teknologi/rss", enabled=True)
    news_src = Source(name="Antara", base_url="https://www.antaranews.com", feed_url="https://www.antaranews.com/rss/terkini", enabled=True)
    session.add_all([tech_src, news_src])
    await session.flush()
    session.add_all([
        Article(source_id=tech_src.id, title="Apple Rilis iPhone Duo", url="https://example.com/tech1"),
        Article(source_id=tech_src.id, title="Chip AI China Naik Harga", url="https://example.com/tech2"),
        Article(source_id=news_src.id, title="Gempa di Maluku Barat Daya", url="https://example.com/news1"),
    ])
    await session.commit()
    out = await dispatch("/news-tech", session)
    assert "iPhone Duo" in out
    assert "Chip AI China" in out
    assert "Gempa di Maluku" not in out


@pytest.mark.asyncio
async def test_news_sport_filters_by_source(session):
    from app.interfaces.commands import dispatch
    sport_src = Source(name="CNN Indonesia Olahraga", base_url="https://www.cnnindonesia.com/olahraga", feed_url="https://www.cnnindonesia.com/olahraga/rss", enabled=True)
    sport_src2 = Source(name="Antara Olahraga", base_url="https://www.antaranews.com/olahraga", feed_url="https://www.antaranews.com/rss/olahraga.xml", enabled=True)
    news_src = Source(name="Antara", base_url="https://www.antaranews.com", feed_url="https://www.antaranews.com/rss/terkini", enabled=True)
    session.add_all([sport_src, sport_src2, news_src])
    await session.flush()
    session.add_all([
        Article(source_id=sport_src.id, title="Timnas Indonesia Menang Atas Bahrain", url="https://example.com/sport1"),
        Article(source_id=sport_src2.id, title="Verstappen Juara F1 GP Monza", url="https://example.com/sport2"),
        Article(source_id=news_src.id, title="Gempa Guncang Bengkulu", url="https://example.com/news1"),
    ])
    await session.commit()
    out = await dispatch("/news-sport", session)
    assert "Timnas Indonesia Menang" in out
    assert "Verstappen" in out
    assert "Gempa Guncang" not in out
    assert "Berita Sport" in out


@pytest.mark.asyncio
async def test_news_sport_empty(session):
    from app.interfaces.commands import dispatch
    out = await dispatch("/news-sport", session)
    assert "belum ada data" in out.lower()


@pytest.mark.asyncio
async def test_gempa_empty(session):
    from app.interfaces.commands import dispatch
    out = await dispatch("/gempa", session)
    assert "tidak" in out.lower() or "Gempa" in out


@pytest.mark.asyncio
async def test_gempa_with_data(session):
    from app.interfaces.commands import dispatch
    eq = Earthquake(external_id="eq1", magnitude=6.2, depth_km=20.0, latitude=-3.5, longitude=130.0, place="Maluku", occurred_at=None, source="bmkg")
    session.add(eq)
    await session.commit()
    out = await dispatch("/gempa", session)
    assert "6.2" in out
    assert "Maluku" in out


@pytest.mark.asyncio
async def test_gempa_min_magnitude_filter(session):
    from app.interfaces.commands import dispatch
    session.add_all([
        Earthquake(external_id="a", magnitude=3.0, source="bmkg"),
        Earthquake(external_id="b", magnitude=5.5, source="bmkg"),
        Earthquake(external_id="c", magnitude=4.2, source="bmkg"),
    ])
    await session.commit()
    out = await dispatch("/gempa 5", session)
    assert "5.5" in out
    assert "3.0" not in out


@pytest.mark.asyncio
async def test_network_empty(session):
    from app.interfaces.commands import dispatch
    out = await dispatch("/network", session)
    assert "tidak ada" in out.lower() or "Network" in out


@pytest.mark.asyncio
async def test_network_with_targets(session):
    from app.interfaces.commands import dispatch
    tgt = NetworkTarget(name="Google DNS", target_type="dns", target="8.8.8.8")
    session.add(tgt)
    await session.flush()
    check = NetworkCheck(target_id=tgt.id, status="up", latency_ms=12.5)
    session.add(check)
    await session.commit()
    out = await dispatch("/network", session)
    assert "Google DNS" in out
    assert "up" in out


@pytest.mark.asyncio
async def test_status_counts(session):
    from app.interfaces.commands import dispatch
    session.add(Source(name="S", base_url="https://example.com", feed_url="https://example.com/rss", enabled=True))
    await session.flush()
    session.add(Event(title="E", event_type=EventType.news, status=EventStatus.active))
    await session.commit()
    out = await dispatch("/status", session)
    assert "sources" in out.lower()
    assert "events" in out.lower()


@pytest.mark.asyncio
async def test_alerts_empty(session):
    from app.interfaces.commands import dispatch
    out = await dispatch("/alerts", session)
    assert "ada alert" in out.lower()


@pytest.mark.asyncio
async def test_documents_empty(session):
    from app.interfaces.commands import dispatch
    out = await dispatch("/documents", session)
    assert "Dokumen" in out


# ---------------------------------------------------------------------------
# Weather command
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_monitor_edit_command(session):
    from app.interfaces.commands import dispatch
    from app.monitoring import upsert_monitor_target

    await upsert_monitor_target(session, "8.8.8.8")
    out = await dispatch("/monitor edit 8.8.8.8 nama DNS GOOGLE", session)
    assert "Monitor diubah" in out
    assert "DNS GOOGLE" in out
    out2 = await dispatch("/monitor edit 1.1.1.1 nama X", session)
    assert "tidak aktif" in out2.lower() or "tidak ditemukan" in out2.lower()
    out3 = await dispatch("/monitor edit 8.8.8.8", session)
    assert "gak ada param" in out3.lower() or "pakai" in out3.lower()


@pytest.mark.asyncio
async def test_weather_empty(session):
    from app.interfaces.commands import dispatch
    out = await dispatch("/weather", session)
    assert "Cuaca" in out
    assert "tidak ditemukan" in out.lower()


@pytest.mark.asyncio
async def test_weather_filters_by_location(session):
    from app.interfaces.commands import dispatch
    loc = WeatherLocation(name="Depok - Beji", adm4="32.76.06.1001", enabled=True)
    session.add(loc)
    await session.flush()
    f = WeatherForecast(
        location_id=loc.id,
        forecast_datetime=_now_utc(),
        weather_description="Berawan",
        temperature_c=29.4,
        humidity_pct=78.0,
        wind_speed_kmh=11.0,
        precipitation_mm=0.0,
    )
    session.add(f)
    await session.commit()
    out = await dispatch("/weather beji", session)
    assert "Depok - Beji" in out
    assert "Berawan" in out
    assert "29.4" in out
    assert "78%" in out
    out_full = await dispatch("/weather", session)
    assert "Depok - Beji" in out_full


def _now_utc():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# File-request detection + file generation
# ---------------------------------------------------------------------------

def test_detect_file_request_formats():
    from app.interfaces.telegram import _detect_file_request
    assert _detect_file_request("bikin pdf laporan gempa ya") == "pdf"
    assert _detect_file_request("buatkan word dokumennya") == "docx"
    assert _detect_file_request("tolong file excel-nya") == "xlsx"
    assert _detect_file_request("simpan sebagai markdown") == "md"
    assert _detect_file_request("generate gambar png") == "png"
    assert _detect_file_request("berapa magnitudo gempa tadi?") is None
    assert _detect_file_request("coba kasih tau cuaca") is None


def test_render_document_creates_files(tmp_path):
    from app.documents.generate import render_document
    text = "**Ringkasan**\n- gempa M 5.2\n- kedalaman 20 km"
    for fmt, ext in [("pdf", ".pdf"), ("docx", ".docx"), ("txt", ".txt"),
                     ("md", ".md"), ("xlsx", ".xlsx"), ("png", ".png")]:
        path = render_document(text, fmt, title="Tes Laporan", output_dir=str(tmp_path))
        assert path.endswith(ext)
        import os
        assert os.path.isfile(path)
        assert os.path.getsize(path) > 0


# ---------------------------------------------------------------------------
# WHOIS detection + lookup
# ---------------------------------------------------------------------------

def test_whois_ip_helpers():
    from app.whois import extract_ip, is_public_ip
    assert is_public_ip("8.8.8.8") is True
    assert is_public_ip("192.168.1.1") is False
    assert is_public_ip("127.0.0.1") is False
    assert is_public_ip("gibberish") is False
    assert extract_ip("cek 103.10.2.1 ya") == "103.10.2.1"
    assert extract_ip("gak ada ip disini") is None
    assert extract_ip("target: 999.1.1.1") is None
    assert extract_ip("versi 1.2.3.4.5") == "1.2.3.4"


def test_detect_whois_request():
    from app.interfaces.telegram import _detect_whois_request
    assert _detect_whois_request("whois 8.8.8.8") == "8.8.8.8"
    assert _detect_whois_request("cek ip 114.120.14.5") == "114.120.14.5"
    assert _detect_whois_request("gua minta whois 103.10.2.1") == "103.10.2.1"
    assert _detect_whois_request("8.8.8.8") == "8.8.8.8"
    assert _detect_whois_request("cara cek ip publik sendiri?") is None
    assert _detect_whois_request("jelasin sains 10.10.10.10") is None   # private
    assert _detect_whois_request("berapa nilai dari x^2") is None
    assert _detect_whois_request("gua cek 8.8.8.8 dong") == "8.8.8.8"
    assert _detect_whois_request("ip 8.8.8.8") == "8.8.8.8"
    assert (
        _detect_whois_request(
            "tolong cek kenapa 8.8.8.8 sering timeout"
        )
        is None  # pertanyaan diagnosis -> AI, bukan whois
    )
    assert (
        _detect_whois_request(
            "gua ada 2 VPS dengan ip 103.76.91.23 dan 103.76.91.24 "
            "gua pointing ke domain gua app-a.diybima.web.id dan "
            "app-b.diybima.web.id, buatin gua configurasi nginx"
        )
        is None  # pertanyaan config -> AI, bukan whois
    )


def test_detect_whois_request_not_hijack_monitor():
    from app.interfaces.telegram import _detect_whois_request
    assert _detect_whois_request("tolong monitorin ip 103.153.42.237") is None
    assert _detect_whois_request("minta tolong dipantau IP 103.153.42.237") is None
    assert _detect_whois_request("tolong awasin 103.153.42.237") is None
    assert _detect_whois_request("103.153.42.237 tolong dimonitorin") is None
    assert _detect_whois_request("cekin 8.8.8.8 terus") is None
    assert _detect_whois_request("pantau 8.8.8.8 terus") is None
    assert _detect_whois_request("tolong cekin ip 103.153.42.237") is None


@pytest.mark.asyncio
async def test_whois_command_usage_without_arg(session):
    from app.interfaces.commands import dispatch
    out = await dispatch("/whois", session)
    assert "IP publik" in out
    assert "/whois" in out


@pytest.mark.asyncio
async def test_whois_command_formats_real_lookup(monkeypatch, session):
    from app.interfaces.commands import dispatch
    import app.interfaces.commands as cmds_mod

    async def fake_lookup(ip):
        assert ip == "8.8.8.8"
        return {"ip": ip, "network": "GOGL", "range": "8.8.8.8 - 8.8.8.8", "country": "US",
                "orgs": ["Google LLC"], "type": "DIRECT ALLOCATION", "source": "rdap.apnic.net",
                "lookup_url": "https://rdap.apnic.net/ip/8.8.8.8"}

    monkeypatch.setattr(cmds_mod, "rdap_lookup", fake_lookup)
    out = await dispatch("/whois 8.8.8.8", session)
    assert "Google LLC" in out
    assert "WHOIS" in out
    assert "8.8.8.8" in out
    assert "RDAP" in out


@pytest.mark.asyncio
async def test_whois_command_rejects_private_ip(monkeypatch, session):
    from app.interfaces.commands import dispatch
    import app.interfaces.commands as cmds_mod

    async def fake_lookup(ip):
        return {"error": f"{ip} bukan IP publik."}

    monkeypatch.setattr(cmds_mod, "rdap_lookup", fake_lookup)
    out = await dispatch("/whois 192.168.1.1", session)
    assert "bukan IP publik" in out


def test_whois_domain_helpers():
    from app.whois import extract_domain
    assert extract_domain("mampir ke www.cnnindonesia.com dong") == "cnnindonesia.com"
    assert extract_domain("whois google.co.id") == "google.co.id"
    assert extract_domain("https://www.antaranews.com/rss") == "antaranews.com"
    assert extract_domain("8.8.8.8") is None
    assert extract_domain("gak ada domain disini") is None
    assert extract_domain("x") is None


def test_detect_whois_request_domain():
    from app.interfaces.telegram import _detect_whois_request
    assert _detect_whois_request("whois cnnindonesia.com") == "cnnindonesia.com"
    assert _detect_whois_request("cek domain google.co.id") == "google.co.id"
    assert _detect_whois_request("gua mau tau siapa pemilik detik.com") is None
    assert _detect_whois_request("traceroute cnnindonesia.com") is None
    assert _detect_whois_request("beli aja antaranews.com") is None


@pytest.mark.asyncio
async def test_whois_command_domain_lookup(monkeypatch, session):
    from app.interfaces.commands import dispatch
    import app.interfaces.commands as cmds_mod

    async def fake_domain_lookup(domain):
        assert domain == "cnnindonesia.com"
        return {"domain": "CNNINDONESIA.COM", "registrar": "CSL Computer Service Langenbach GmbH",
                "orgs": ["PT Trans Media Corpora"], "created": "1998-12-01T00:00:00Z",
                "expires": "2027-12-01T00:00:00Z", "status": ["client transfer prohibited"],
                "nameservers": ["ns1.cnnindonesia.com"], "source": "rdap.org",
                "lookup_url": "https://rdap.org/domain/cnnindonesia.com"}

    monkeypatch.setattr(cmds_mod, "rdap_domain_lookup", fake_domain_lookup)
    out = await dispatch("/whois cnnindonesia.com", session)
    assert "WHOIS" in out
    assert "CNNINDONESIA.COM" in out
    assert "registrar" in out.lower() or "Trans Media" in out
    assert "rdap.org" in out


# ---------------------------------------------------------------------------
# Sport sub-commands (football / motogp / f1 by article title)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sport_football_filters_title(session):
    from app.interfaces.commands import dispatch
    sport = Source(name="CNN Indonesia Olahraga", base_url="https://www.cnnindonesia.com/olahraga",
                   feed_url="https://www.cnnindonesia.com/olahraga/rss", enabled=True)
    session.add(sport)
    await session.flush()
    session.add_all([
        Article(source_id=sport.id, title="Timnas Indonesia kalah dari Bahrain di kualifikasi", url="https://e.com/s1"),
        Article(source_id=sport.id, title="Verstappen dominasi GP Monza", url="https://e.com/s2"),
        Article(source_id=sport.id, title="Harga minyak naik lagi", url="https://e.com/s3"),
    ])
    await session.commit()
    out = await dispatch("/sport-football", session)
    assert "Timnas Indonesia" in out
    assert "Verstappen" not in out
    assert "Harga minyak" not in out


@pytest.mark.asyncio
async def test_sport_f1_and_motogp_filters_title(session):
    from app.interfaces.commands import dispatch
    sport = Source(name="Antara Olahraga", base_url="https://www.antaranews.com/olahraga",
                   feed_url="https://www.antaranews.com/rss/olahraga.xml", enabled=True)
    session.add(sport)
    await session.flush()
    session.add_all([
        Article(source_id=sport.id, title="Jorge Martin juara MotoGP", url="https://e.com/m1"),
        Article(source_id=sport.id, title="Leclerc finis podium F1", url="https://e.com/f1"),
    ])
    await session.commit()
    out_f1 = await dispatch("/sport-f1", session)
    assert "Leclerc" in out_f1
    assert "MotoGP" not in out_f1
    out_moto = await dispatch("/sport-motogp", session)
    assert "Jorge Martin" in out_moto


@pytest.mark.asyncio
async def test_sport_subcommand_empty(session):
    from app.interfaces.commands import dispatch
    out = await dispatch("/sport-f1", session)
    assert "belum ada data" in out.lower()


# ---------------------------------------------------------------------------
# Server commands
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ipinfo_command(monkeypatch, session):
    from app.interfaces.commands import dispatch
    import app.interfaces.commands as cmds_mod

    async def fake_run(args):
        return {"args": args, "rc": 0, "stdout": "1: lo: <LOOPBACK> inet 127.0.0.1/8", "stderr": ""}

    monkeypatch.setattr(cmds_mod, "run_network_cmd", fake_run)
    out = await dispatch("/ipinfo", session)
    assert "ip a" in out
    assert "127.0.0.1" in out


@pytest.mark.asyncio
async def test_traceroute_command_target_validation(session):
    from app.interfaces.commands import dispatch
    out = await dispatch("/traceroute 999.1.1.1", session)
    assert "gak valid" in out
    out2 = await dispatch("/traceroute", session)
    assert "target gak valid" in out2 or "Contoh" in out2


# ---------------------------------------------------------------------------
# Alerts card format (2-baris, priority tanpa "P" rangkap)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_alerts_card_no_double_p(session):
    from datetime import datetime, timezone

    from app.db.models import Alert, AlertPriority
    from app.interfaces.commands import dispatch

    ev = Event(title="Gempa", event_type=EventType.news, status=EventStatus.active)
    session.add(ev)
    await session.flush()
    session.add(
        Alert(event_id=ev.id, priority=AlertPriority.p1, title="Gempa M5.2",
              status="active", message_count=3,
              created_at=datetime(2026, 9, 12, 6, 0, tzinfo=timezone.utc))
    )
    await session.commit()
    out = await dispatch("/alerts", session)
    assert "<b>P1</b>" in out
    assert "PP1" not in out
    assert "WIB" in out
    assert "3 msg" in out
    assert f"event #{ev.id}" in out


@pytest.mark.asyncio
async def test_alerts_card_single_message(session):
    from app.db.models import Alert, AlertPriority
    from app.interfaces.commands import dispatch

    ev = Event(title="E", event_type=EventType.news, status=EventStatus.active)
    session.add(ev)
    await session.flush()
    session.add(Alert(event_id=ev.id, priority=AlertPriority.p3, title="T",
                      status="resolved", message_count=1))
    await session.commit()
    out = await dispatch("/alerts", session)
    assert "1 msg" not in out
    assert "RESOLVED" in out


# ---------------------------------------------------------------------------
# Reply keyboard + morning digest scheduling
# ---------------------------------------------------------------------------

def test_main_keyboard_has_commands():
    from app.interfaces.telegram import main_keyboard
    kb = main_keyboard()
    assert kb["resize_keyboard"] is True
    flat = [c for row in kb["keyboard"] for c in row]
    assert "/news" in flat
    assert "/help" in flat
    assert "/status" in flat


def test_digest_due_logic():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.interfaces.telegram import _digest_due

    wib = datetime(2026, 9, 12, 6, 1, tzinfo=ZoneInfo("Asia/Jakarta"))
    assert _digest_due(wib, "2026-09-11", 6) is True
    assert _digest_due(wib, "2026-09-12", 6) is False
    assert _digest_due(wib.replace(hour=7), "2026-09-11", 6) is False
    assert _digest_due(wib.replace(minute=6), "2026-09-11", 6) is False


# ---------------------------------------------------------------------------
# New commands: /asn, /geo-trace, /ping, /status host metrics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_asn_command(monkeypatch, session):
    from app.interfaces.commands import dispatch
    import app.interfaces.commands as cmds_mod

    async def fake_asn(ip):
        return {"asn": 15169, "org": "Google LLC", "country": "US",
                "country_code": "US", "region": "CA", "city": "Mountain View",
                "tz": "America/Los_Angeles"}

    monkeypatch.setattr(cmds_mod, "asn_lookup", fake_asn)
    out = await dispatch("/asn 8.8.8.8", session)
    assert "AS15169" in out
    assert "Google LLC" in out


@pytest.mark.asyncio
async def test_geo_trace_command(monkeypatch, session):
    from app.interfaces.commands import dispatch
    import app.interfaces.commands as cmds_mod

    async def fake_geo(target):
        return {"target": target, "rc": 0,
                "hops": ["192.168.1.1", "8.8.8.8"],
                "info": {"8.8.8.8": {"asn": 15169, "org": "Google LLC",
                                     "country": "US", "city": "Mountain View"}}}

    monkeypatch.setattr(cmds_mod, "geotrace", fake_geo)
    out = await dispatch("/geo-trace 8.8.8.8", session)
    assert "Geo-Traceroute" in out
    assert "AS15169" in out
    monkeypatch.setattr(cmds_mod, "geotrace", fake_geo)
    out2 = await dispatch("/geo-trace", session)
    assert "gak valid" in out2


@pytest.mark.asyncio
async def test_ping_command(monkeypatch, session):
    from app.interfaces.commands import dispatch
    import app.interfaces.commands as cmds_mod

    captured = {}

    async def fake_run(args):
        captured["args"] = args
        return {"args": args, "rc": 0,
                "stdout": "64 bytes from 8.8.8.8: icmp_seq=1 ttl=115 time=12.3 ms\n4 packets transmitted, 4 received, 0% packet loss",
                "stderr": ""}

    monkeypatch.setattr(cmds_mod, "run_network_cmd", fake_run)
    out = await dispatch("/ping 8.8.8.8", session)
    assert captured["args"][0] == "ping"
    assert "-c" in captured["args"]
    assert "packet loss" in out


@pytest.mark.asyncio
async def test_status_host_metrics(monkeypatch, session):
    from app.interfaces.commands import dispatch
    import app.interfaces.commands as cmds_mod

    async def fake_collect():
        return {
            "uptime": "2 hari",
            "cpu_percent": 7.5,
            "load": (0.1, 0.2, 0.1),
            "memory": {"pct": 50.0, "used_gb": 8.0, "total_gb": 16.0},
            "disk": {"pct": 40.0, "used_gb": 200.0, "total_gb": 500.0, "mount": "/host-root"},
            "network": {"wlan0": {"rx_mbps": 0.5, "tx_mbps": 0.2}},
        }

    monkeypatch.setattr(cmds_mod, "collect_host_stats", fake_collect)
    session.add(Source(name="S", base_url="https://example.com", feed_url="https://example.com/rss", enabled=True))
    session.add(Event(title="E", event_type=EventType.news, status=EventStatus.active))
    await session.commit()
    out = await dispatch("/status", session)
    assert "CPU" in out
    assert "RAM" in out
    assert "Disk" in out
    assert "Network" in out
    assert "sources 1" in out
    assert "events 1" in out


# ---------------------------------------------------------------------------
# Finance sudah dihapus total
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_finance_not_recognized(session):
    from app.interfaces.commands import dispatch
    out = await dispatch("/finance", session)
    assert "tidak dikenal" in out.lower()