"""Slash-command handlers for the Telegram interactive bot.

Public entry: ``dispatch(text, session) -> str``.
Raises ``ValueError`` for unknown commands so the caller can reply with help.
"""

from __future__ import annotations

import datetime as _dt
import html as _html

from sqlalchemy import and_, desc, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AIInterpretation,
    AIRequest,
    Alert,
    Article,
    Document,
    Earthquake,
    Event,
    NetworkCheck,
    NetworkTarget,
    Source,
    WeatherForecast,
    WeatherLocation,
)
from app.hoststats import collect_all as collect_host_stats
from app.hoststats import format_status
from app.interfaces.formatter import bold, dt, dt_wib, esc, mono, num
from app.netinfo import asn_lookup, format_asn
from app.geotrace import format_geotrace, geotrace
from app.shell import format_server_command, parse_server_request, run_network_cmd
from app.whois import (
    extract_domain,
    extract_ip,
    format_whois,
    format_whois_domain,
    rdap_domain_lookup,
    rdap_lookup,
)

HELP = (
    f"{bold('KELA AI — Perintah')}\n"
    f"\n"
    f"  /help       {esc('Tampilkan bantuan ini')}\n"
    f"  /news       {esc('Berita terbaru (max 8)')}\n"
    f"  /news-tech  {esc('Berita teknologi terbaru (max 8)')}\n"
    f"  /news-sport {esc('Berita olahraga (keseluruhan) (max 8)')}\n"
    f"  /sport-football {esc('Detail: berita sepakbola')}\n"
    f"  /sport-motogp {esc('Detail: berita MotoGP')}\n"
    f"  /sport-f1 {esc('Detail: berita Formula 1')}\n"
    f"  /gempa      {esc('Gempa terakhir (max 5)')}\n"
    f"  /gempa 5    {esc('Gempa M ≥ X (contoh: /gempa 4)')}\n"
    f"  /network    {esc('Daftar monitor aktif + status (sama kayak /monitor list)')}\n"
    f"  /weather    {esc('Cuaca BMKG (opsional: filter lokasi)')}\n"
    f"  /whois      {esc('Info pemilik IP/domain via RDAP (contoh: /whois 8.8.8.8, /whois cnnindonesia.com)')}\n"
    f"  /asn        {esc('Info ASN/ISP/geo sebuah IP (contoh: /asn 8.8.8.8)')}\n"
    f"  /geo-trace  {esc('Traceroute + resolve ASN tiap hop (contoh: /geo-trace google.com)')}\n"
    f"  /ping       {esc('Ping host, 4 paket (contoh: /ping 8.8.8.8)')}\n"
    f"  /ipinfo     {esc('Info network server (jalanin \"ip a\" di host)')}\n"
    f"  /traceroute {esc('Trace rute ke host (contoh: /traceroute 8.8.8.8)')}\n"
    f"  /sysinfo    {esc('Metrik host: CPU/RAM/disk/network/uptime')}\n"
    f"  /status     {esc('Ringkasan sistem (host + DB)')}\n"
    f"  /status ai  {esc('Status AI gateway')}\n"
    f"  /monitor   {esc('Mulai pantau target + auto-alert DOWN/UP (contoh: /monitor 8.8.8.8)')}\n"
    f"  /monitor stop {esc('<IP|domain> — berhenti pantau')}\n"
    f"  /monitor list {esc('Daftar target yang dipantau')}\n"
    f"  /memory    {esc('Catatan memori yang gua simpan (cari: /memory cari <kata>)')}\n"
    f"  /forget    {esc('<id> — hapus satu catatan memori')}\n"
    f"  /alerts     {esc('Alert terbaru')}\n"
    f"  /documents  {esc('Daftar dokumen')}\n"
    f"  /crons      {esc('Daftar cron/reminder aktif')}\n"
    f"  /cron cancel {esc('<id> — batalin cron (contoh: /cron cancel a1b2c3)')}\n"
   f"  /cron edit {esc('<id> <jadwal/pesan> — edit cron (contoh: /cron edit a1b2c3 besok jam 7 pagi)')}\n"
    f"\n"
    f"Ketik pesan biasa untuk chat natural language (perlu AI key aktif).\n"
    f"Contoh cron: 'ingetin pulang jam 17.00', 'cron besok jam 7 pagi ingetin meeting'."
)


async def dispatch(text: str, session: AsyncSession, *, chat_id: str | None = None) -> str:
    """Route ``/command [args]`` and return HTML reply."""
    parts = text.strip().split(None, 1)
    if not parts:
        return HELP
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    table = {
        "/start": _help,
        "/help": _help,
        "/news": _news,
        "/news-tech": _news_tech,
        "/news-sport": _news_sport,
        "/sport-football": _sport_football,
        "/sport-motogp": _sport_motogp,
        "/sport-f1": _sport_f1,
        "/gempa": _gempa,
        "/weather": _weather,
        "/whois": _whois,
        "/asn": _asn,
        "/geo-trace": _geo_trace,
        "/ping": _ping,
        "/ipinfo": _ipinfo,
        "/traceroute": _traceroute,
        "/sysinfo": _sysinfo,
        "/network": _network,
        "/status": _status,
        "/alerts": _alerts,
        "/documents": _documents,
        "/crons": _crons,
        "/cron": _cron_cancel,
        "/monitor": _monitor,
        "/memory": _memory,
        "/forget": _forget,
    }
    handler = table.get(cmd)
    if handler is None:
        return (
            f"{bold('?')} Perintah tidak dikenal: {mono(cmd)}\n"
            f"Ketik {mono('/help')} untuk daftar perintah."
        )
    try:
        if handler is _monitor:
            return await handler(session, arg, chat_id=chat_id)
        return await handler(session, arg)
    except Exception as exc:
        return f"{bold('Error')} — {esc(str(exc)[:300])}"


# ---------------------------------------------------------------------------
# Individual handlers
# ---------------------------------------------------------------------------

async def _help(session: AsyncSession, arg: str) -> str:  # noqa: ARG001
    return HELP


async def _news(session: AsyncSession, arg: str) -> str:
    return await _render_news(session, title="Berita terbaru", tech_only=False)


async def _news_tech(session: AsyncSession, arg: str) -> str:  # noqa: ARG001
    from app.config import settings

    keywords = settings.load_fixtures("news_tech_source_keywords")
    if not keywords:
        return f"{bold('Berita Tech')} — filter tech belum dikonfigurasi."
    return await _render_news(session, title="Berita Tech terbaru", tech_only=True, keywords=keywords)


async def _news_sport(session: AsyncSession, arg: str) -> str:  # noqa: ARG001
    from app.config import settings

    keywords = settings.load_fixtures("news_sport_source_keywords")
    if not keywords:
        return f"{bold('Berita Sport')} — filter sport belum dikonfigurasi."
    return await _render_news(session, title="Berita Sport terbaru", tech_only=True, keywords=keywords)


_REPO_FOOTBALL = ["sepak bola", "sepakbola", "football", "soccer", "futsal", "timnas", "liga", "prancis u", "jerman u"]
_REPO_MOTOGP = ["motogp", "moto gp", "moto3", "moto2", "marquez", "bagnaia"]
_REPO_F1 = ["f1", "formula 1", "formula one", "grand prix", "verstappen", "leclerc", "hamilton"]


async def _sport_football(session: AsyncSession, arg: str) -> str:  # noqa: ARG001
    return await _render_news(
        session,
        title="Sepakbola terbaru",
        tech_only=True,
        keywords=_sport_keywords(),
        content=_REPO_FOOTBALL,
    )


async def _sport_motogp(session: AsyncSession, arg: str) -> str:  # noqa: ARG001
    return await _render_news(
        session,
        title="MotoGP terbaru",
        tech_only=True,
        keywords=_sport_keywords(),
        content=_REPO_MOTOGP,
    )


async def _sport_f1(session: AsyncSession, arg: str) -> str:  # noqa: ARG001
    return await _render_news(
        session,
        title="Formula 1 terbaru",
        tech_only=True,
        keywords=_sport_keywords(),
        content=_REPO_F1,
    )


def _sport_keywords() -> list[str]:
    from app.config import settings

    return settings.load_fixtures("news_sport_source_keywords")


async def _render_news(
    session: AsyncSession,
    *,
    title: str,
    tech_only: bool,
    keywords: list[str] | None = None,
    content: list[str] | None = None,
    limit: int = 8,
    max_age_hours: int = 0,
) -> str:
    from app.config import settings

    max_age_hours = max_age_hours or settings.news_max_age_hours
    stmt = (
        select(Article, Source.name.label("source_name"))
        .join(Source, Article.source_id == Source.id, isouter=True)
        .order_by(func.coalesce(Article.published_at, Article.scraped_at).desc())
        .limit(limit)
    )
    filters: list = []
    if max_age_hours > 0:
        cutoff = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None) - _dt.timedelta(hours=max_age_hours)
        filters.append(or_(Article.published_at.is_(None), Article.published_at >= cutoff))
    if tech_only and keywords:
        filters.append(
            or_(
                *[func.lower(Source.name).like(f"%{k.lower()}%") for k in keywords]
            )
        )
    if content:
        filters.append(
            or_(
                *[func.lower(Article.title).like(f"%{k.lower()}%") for k in content]
            )
        )
    if filters:
        stmt = stmt.where(and_(*filters))
    rows = (await session.execute(stmt)).all()
    if not rows:
        return f"{bold(esc(title))} — belum ada data yang cocok."
    lines = [bold(esc(title))]
    for art, src_name in rows:
        title_text = esc(art.title or "(tanpa judul)")
        when = dt(art.published_at or art.scraped_at)
        lines.append(f"\n• <a href=\"{esc(art.url)}\">{title_text}</a>")
        lines.append(f"  {esc(src_name or '?')} — {when}")
    return "\n".join(lines)


async def _gempa(session: AsyncSession, arg: str) -> str:
    min_mag: float | None = None
    limit = 5
    if arg:
        try:
            min_mag = float(arg)
        except ValueError:
            pass
    rows = (
        await session.execute(
            select(Earthquake).order_by(desc(Earthquake.occurred_at)).limit(50)
        )
    ).scalars().all()
    if min_mag is not None:
        rows = [r for r in rows if r.magnitude is not None and float(r.magnitude) >= min_mag]
    rows = rows[:limit]
    if not rows:
        return (
            f"{bold('Gempa')} — tidak ditemukan"
            + (f" (M ≥ {min_mag})" if min_mag else "")
            + "."
        )
    lines = [
        bold("Gempa terkini" + (f" (M ≥ {min_mag})" if min_mag else ""))
    ]
    for eq in rows:
        mag = num(eq.magnitude, 1) if eq.magnitude else "?"
        place = esc(eq.place or "unknown")
        depth = num(eq.depth_km, 0) if eq.depth_km else "?"
        when = dt(eq.occurred_at)
        lines.append(f"\n{bold(f'M {mag}')} — {place}")
        lines.append(f"  {when} · kedalaman {depth} km")
    return "\n".join(lines)


async def _weather(session: AsyncSession, arg: str) -> str:
    q = arg.strip()
    stmt = select(WeatherLocation).where(WeatherLocation.enabled.is_(True))
    if q:
        stmt = stmt.where(WeatherLocation.name.ilike(f"%{q}%"))
    locs = (await session.execute(stmt)).scalars().all()
    if not locs:
        return (
            f"{bold('Cuaca')} — lokasi "
            f"{mono(esc(q)) if q else '(tidak ada watchlist)'} tidak ditemukan."
        )

    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    start = now - _dt.timedelta(hours=6)
    end = now + _dt.timedelta(hours=6)
    rows = (
        await session.execute(
            select(WeatherForecast, WeatherLocation)
            .join(WeatherLocation, WeatherForecast.location_id == WeatherLocation.id)
            .where(WeatherForecast.location_id.in_([l.id for l in locs]))
            .where(WeatherForecast.forecast_datetime >= start)
            .where(WeatherForecast.forecast_datetime <= end)
        )
    ).all()

    nearest: dict[int, tuple[WeatherForecast, WeatherLocation]] = {}
    for f, loc in rows:
        cur = nearest.get(loc.id)
        if cur is None or abs((f.forecast_datetime - now).total_seconds()) < abs(
            (cur[0].forecast_datetime - now).total_seconds()
        ):
            nearest[loc.id] = (f, loc)

    title = "Cuaca sekarang (BMKG)"
    if q:
        title += f" — {esc(q)}"
    lines = [bold(title)]
    for loc in locs:
        pair = nearest.get(loc.id)
        name = esc(loc.name)
        if pair is None:
            lines.append(f"\n• {name} — belum ada data forecast terbaru.")
            continue
        f, _ = pair
        descr = esc(f.weather_description or "n/a")
        temp = f"{num(f.temperature_c, 1)}°C" if f.temperature_c is not None else "?"
        hum = f"RH {f.humidity_pct:g}%" if f.humidity_pct is not None else "RH ?"
        wind = f"angin {f.wind_speed_kmh:g} km/j"
        if f.wind_dir:
            wind += f" ({esc(f.wind_dir)})"
        rain = f"hujan {num(f.precipitation_mm, 1)} mm" if f.precipitation_mm is not None else None
        parts = [descr, temp, hum, wind]
        if rain:
            parts.append(rain)
        lines.append(f"\n• {name}")
        lines.append("  " + " · ".join(parts))
    return "\n".join(lines)


async def _asn(session: AsyncSession, arg: str) -> str:  # noqa: ARG001
    import re as _re

    ip = extract_ip(arg)
    if ip:
        info = await asn_lookup(ip)
        if info is None:
            return f"{bold('ASN')} — {mono(esc(ip))} gagal di-resolve."
        return format_asn(ip, info)
    # Accept AS number: "140444", "AS140444", "as 140444"
    raw = (arg or "").strip()
    m = _re.match(r"^(?:AS)?\s*(\d{1,10})$", raw, _re.IGNORECASE)
    if m:
        from app.netinfo import asn_number_lookup, format_asn_number

        asn_num = int(m.group(1))
        info = await asn_number_lookup(asn_num)
        if info is None:
            return f"{bold('ASN')} — {mono(f'AS{asn_num}')} gagal di-resolve via RDAP."
        return format_asn_number(asn_num, info)
    return (
        f"{bold('ASN')} — kasih IP publik atau AS number dong.\n"
        f"Contoh: {mono('/asn 8.8.8.8')} atau {mono('/asn 140444')}"
    )


async def _geo_trace(session: AsyncSession, arg: str) -> str:  # noqa: ARG001
    target = arg.strip()
    if not target or not parse_server_request(f"traceroute {target}"):
        return (
            f"{bold('Geo-Trace')} — target gak valid.\n"
            f"Contoh: {mono('/geo-trace google.com')} atau {mono('/geo-trace 8.8.8.8')}"
        )
    geo = await geotrace(target)
    return format_geotrace(geo)


async def _ping(session: AsyncSession, arg: str) -> str:
    args = parse_server_request(f"ping {arg}")
    if not args:
        return (
            f"{bold('Ping')} — target gak valid.\n"
            f"Contoh: {mono('/ping 8.8.8.8')} atau {mono('/ping google.com')}"
        )
    result = await run_network_cmd(args)
    return format_server_command(args, result)


async def _ipinfo(session: AsyncSession, arg: str) -> str:  # noqa: ARG001
    args = ["ip", "a"]
    result = await run_network_cmd(args)
    return format_server_command(args, result)


async def _traceroute(session: AsyncSession, arg: str) -> str:
    args = parse_server_request(f"traceroute {arg}")
    if not args:
        return (
            f"{bold('Traceroute')} — target gak valid.\n"
            f"Contoh: {mono('/traceroute 8.8.8.8')} atau {mono('/traceroute google.com')}"
        )
    result = await run_network_cmd(args)
    return format_server_command(args, result)


async def _sysinfo(session: AsyncSession, arg: str) -> str:  # noqa: ARG001
    stats = await collect_host_stats()
    return format_status(stats, None)


async def _whois(session: AsyncSession, arg: str) -> str:  # noqa: ARG001
    ip = extract_ip(arg)
    if ip:
        info = await rdap_lookup(ip)
        if info is None:
            return f"{bold('WHOIS')} — {mono(esc(ip))} tidak ditemukan di RDAP (internet registry)."
        if info.get("error"):
            return f"{bold('WHOIS')} — {esc(info['error'])}"
        return format_whois(ip, info)
    domain = extract_domain(arg)
    if domain:
        info = await rdap_domain_lookup(domain)
        if info is None:
            return f"{bold('WHOIS')} — {mono(esc(domain))} tidak ditemukan di RDAP."
        if info.get("error"):
            return f"{bold('WHOIS')} — {esc(info['error'])}"
        return format_whois_domain(domain, info)
    return (
        f"{bold('WHOIS')} — kasih IP publik atau domain dong.\n"
        f"Contoh: {mono('/whois 114.120.14.5')} atau {mono('/whois cnnindonesia.com')}"
    )


async def _network(session: AsyncSession, arg: str) -> str:  # noqa: ARG001
    return await monitoring_list(session)


async def _status(session: AsyncSession, arg: str) -> str:
    is_ai = "ai" in arg.lower() if arg else False
    if is_ai:
        return await _status_ai(session)
    counts = {}
    model_counts = [
        (Source, "sources"),
        (Article, "articles"),
        (Event, "events"),
        (Earthquake, "gempa"),
        (Alert, "alerts"),
        (Document, "dokumen"),
    ]
    for model, label in model_counts:
        counts[label] = (await session.execute(select(func.count(model.id)))).scalar() or 0
    nc = (
        await session.execute(
            select(func.count(NetworkCheck.id))
        )
    ).scalar() or 0
    counts["network_checks"] = nc

    ai_reqs = (await session.execute(select(func.count(AIRequest.id)))).scalar() or 0
    ai_ok = (await session.execute(
        select(func.count(AIRequest.id)).where(AIRequest.status == "succeeded")
    )).scalar() or 0

    counts["ai_reqs"] = ai_reqs
    counts["ai_ok"] = ai_ok

    stats = await collect_host_stats()
    return format_status(stats, counts)


async def _status_ai(session: AsyncSession) -> str:
    from app.config import settings
    from app.kela_ai.gateway import build_gateway

    gw = build_gateway(settings)
    st = gw.status
    keys_info = st.get("keys", {})
    rl = st.get("rate_limiter", {})
    cb = st.get("circuit_breaker", {})
    configured = gw.configured
    state = "configured" if configured else "no keys"
    lines = [
        bold("AI Gateway"),
        f"  state: {mono(state)}",
        f"  model: {mono(st.get('model', '?'))}",
        f"  keys: {len(keys_info)}",
    ]
    models_info = st.get("models", [])
    if isinstance(models_info, list) and models_info:
        lines.append("  pools:")
        for m in models_info:
            if isinstance(m, dict):
                provider = m.get("provider", "?")
                model = m.get("model", "?")
                akeys = m.get("active_keys", 0)
                cfg = "ok" if m.get("configured") else "no key"
                lines.append(f"    {esc(provider)}/{mono(model)} · {akeys} key · {mono(cfg)}")
    for name, info in (rl if isinstance(rl, dict) else {}).items():
        if isinstance(info, dict):
            win = info.get("window_seconds")
            remaining = info.get("remaining")
            win_s = f"{num(win, 0)}s" if win is not None else "?"
            rem_s = str(remaining) if remaining is not None else "?"
            lines.append(f"  rate limiter [{esc(name)}]: window {mono(win_s)} · remaining {mono(rem_s)}")
    if not rl:
        lines.append("  rate limiter: belum ada request")
    for name, info in (cb if isinstance(cb, dict) else {}).items():
        if isinstance(info, dict):
            cstate = info.get("state", "?")
            fails = info.get("consecutive_failures", 0)
            reopen = info.get("reopens_in_s")
            reopen_s = f"{num(reopen, 0)}s" if reopen is not None else "n/a"
            lines.append(
                f"  circuit breaker [{esc(name)}]: {mono(cstate)} · {fails} gagal · reopen {mono(reopen_s)}"
            )
    if not cb:
        lines.append("  circuit breaker: belum ada request")
    return "\n".join(lines)


async def _alerts(session: AsyncSession, arg: str) -> str:
    from app.db.repo import list_alerts

    rows = await list_alerts(session, limit=8)
    if not rows:
        return f"{bold('Alerts')} — belum ada alert."
    lines = [bold("Alerts terbaru")]
    for a in rows:
        priority = a.priority.value if a.priority else "?"
        status = (getattr(a.status, "value", a.status) if a.status else "?").upper()
        title = esc(a.title or "")
        title_line = f"{bold(priority)} · {bold(status)}" + (f" · {title}" if title else "")
        meta = f"#{a.id}"
        if a.event_id:
            meta += f" · event #{a.event_id}"
        ts = a.created_at or a.updated_at or a.last_sent_at
        meta += f" · {dt_wib(ts)}"
        if getattr(a, "message_count", None) and a.message_count > 1:
            meta += f" · {a.message_count} msg"
        lines.append(f"\n{title_line}")
        lines.append(f"  {esc(meta)}")
    return "\n".join(lines)


async def _documents(session: AsyncSession, arg: str) -> str:  # noqa: ARG001
    rows = (
        await session.execute(
            select(Document).order_by(desc(Document.created_at)).limit(8)
        )
    ).scalars().all()
    if not rows:
        return f"{bold('Dokumen')} — belum ada dokumen."
    lines = [bold("Dokumen terbaru")]
    for d in rows:
        dtype = d.document_type.value if d.document_type else "?"
        lines.append(f"\n{bold(f'#{d.id}')} [{esc(dtype)}] {esc(d.original_filename or d.filename)}")
        lines.append(f"  {d.file_size or 0} byte · {d.text_content and len(d.text_content) or 0} char · {dt(d.created_at)}")
    return "\n".join(lines)


async def _crons(session: AsyncSession, arg: str) -> str:  # noqa: ARG001
    from app.cron import format_jobs_list, list_jobs

    return format_jobs_list(await list_jobs())


async def _cron_cancel(session: AsyncSession, arg: str) -> str:  # noqa: ARG001
    from app.cron import delete_job, edit_job, format_ack, format_jobs_list, list_jobs

    parts = (arg or "").split(maxsplit=2)
    if len(parts) >= 2:
        verb, job_id = parts[0].lower(), parts[1]
        if verb == "cancel":
            if await delete_job(job_id):
                return f"{bold('Cron dihapus')} — {mono(esc(job_id))}.\n\n" + format_jobs_list(await list_jobs())
            return f"{bold('?')} Cron {mono(esc(job_id))} nggak ketemu."
        if verb == "edit":
            payload = parts[2] if len(parts) > 2 else ""
            status, job, hint = await edit_job(job_id, payload, "cli")
            if status == "ok":
                return format_ack(job)
            if status == "missing":
                return f"{bold('?')} Cron {mono(esc(job_id))} nggak ketemu."
            if status == "redis_unavailable":
                return "Redis lagi ngadat, edit cron nggak kesimpen. Coba lagi."
            return hint or f"{bold('Cron edit')} — gak bisa diubah."
    return format_jobs_list(await list_jobs()) + (
        f"\n\nBatal: {mono('/cron cancel <id>')}"
        f"\nEdit: {mono('/cron edit <id> <jadwal/pesan>')}"
    )


# ---------------------------------------------------------------------------
# Chat-driven network monitoring (also used by telegram free-text intercept)
# ---------------------------------------------------------------------------

async def _monitor(session: AsyncSession, arg: str, chat_id: str | None = None) -> str:
    from app.monitoring import resolve_monitor_number

    parts = (arg or "").strip().split(None, 1)
    if not parts:
        return await monitoring_list(session)
    if parts[0].lower() in ("list", "ls"):
        return await monitoring_list(session)
    if parts[0].lower() in ("stop", "off", "hapus", "berhenti", "batalkan", "cancel", "matiin"):
        target = parts[1].strip() if len(parts) > 1 else ""
        # Support "/monitor stop <nomor>" — resolve list index → target first
        if target.isdigit() and (resolved := await resolve_monitor_number(session, target)):
            target = resolved
        return await stop_monitoring(session, target)
    if parts[0].lower() in ("edit", "ubah", "ganti"):
        rest = parts[1].strip() if len(parts) > 1 else ""
        # Support "/monitor edit <nomor> ..." — resolve leading list index → target
        rest_parts = rest.split(None, 1)
        if rest_parts and rest_parts[0].isdigit() and (
            resolved := await resolve_monitor_number(session, rest_parts[0])
        ):
            rest = resolved + (f" {rest_parts[1]}" if len(rest_parts) > 1 else "")
        return await edit_monitor(session, rest)
    return await start_monitoring(session, arg, chat_id=chat_id)


async def start_monitoring(session: AsyncSession, text: str, chat_id: str | None = None) -> str:
    from app.collectors.network.checks import run_check
    from app.db.repo import store_network_check
    from app.memory import remember
    from app.monitoring import (
        is_monitor_target,
        parse_monitor_command,
        set_monitor_name_pending,
        set_monitored,
        set_monitor_status,
        upsert_monitor_target,
    )

    target, label = parse_monitor_command(text or "")
    if not target or not is_monitor_target(target):
        return (
            f"{bold('Monitor')} — target gak valid.\n"
            f"Kirim IP non-loopback atau domain. Contoh: {mono('/monitor 8.8.8.8')}\n"
            f"Nama opsional: {mono('/monitor 114.120.14.5 nama ROUTER RUMAH')}"
        )
    row, _created = await upsert_monitor_target(session, target)
    if label:
        row.name = label
        await session.commit()
        await session.refresh(row)
    await set_monitored(row.id)
    result = await run_check(row)
    await store_network_check(session, row.id, result)
    raw = getattr(result["status"], "value", result["status"])
    status = str(raw).lower()
    await set_monitor_status(row.id, status)
    if status != "up":
        from app.monitoring import format_monitor_down, send_monitor_alert
        body = await format_monitor_down(row, result)
        await send_monitor_alert(body, chat_id=chat_id)
    await remember(
        session,
        f"monitor {target} (status {status})" + (f" nama {label}" if label else ""),
        kind="monitor",
        source="chat",
        meta={"target_id": row.id},
    )
    display = label or target
    lines = [
        f"{bold('Monitor aktif')} — {bold(esc(display))} {mono(f'({esc(target)})')} dicek tiap {mono('60')} detik.",
        "Alert DOWN/UP bakal dikirim otomatis.",
        "",
        f"Cek pertama: {_monitor_check_line(result)}",
    ]
    if not label and chat_id:
        await set_monitor_name_pending(chat_id, target)
        lines.extend(
            [
                "",
                f"{bold('Mau dikasih nama buat monitoring ini?')} (opsional)",
                f"Balas: {mono('Ya, nama RO UNIV')}, {mono('Ngga usah')}, atau langsung ketik namanya.",
            ]
        )
    return "\n".join(lines)


def _monitor_check_line(result: dict) -> str:
    raw = result["status"]
    status = str(getattr(raw, "value", raw)).lower()
    parts = [f"status {mono(status)}"]
    if result.get("latency_ms") is not None:
        parts.append(f"{result['latency_ms']:.0f} ms")
    if result.get("error_message"):
        parts.append(esc(str(result["error_message"])[:140]))
    return " · ".join(parts)


def _parse_monitor_edits(params: str) -> dict:
    import re as _re

    edits: dict = {}
    params = (params or "").strip()
    if not params:
        return edits
    nm = _re.search(r"\bnama\s*[:=]?\s*(.+?)(?:\s+(?:interval|timeout)\b|$)", params, _re.I)
    if nm:
        edits["name"] = nm.group(1).strip().rstrip(".")
    iv = _re.search(r"\binterval\s*[:=]?\s*(\d+)", params, _re.I)
    if iv:
        edits["interval"] = int(iv.group(1))
    tt = _re.search(r"\btimeout\s*[:=]?\s*(\d+)", params, _re.I)
    if tt:
        edits["timeout"] = int(tt.group(1))
    return edits


async def edit_monitor_target(session: AsyncSession, target: str, edits: dict) -> str:
    from app.monitoring import update_monitor_target

    row, detail = await update_monitor_target(
        session,
        target,
        name=edits.get("name"),
        interval=edits.get("interval"),
        timeout=edits.get("timeout"),
    )
    if row is None:
        return f"{bold('Monitor edit')} — {detail}"
    return (
        f"{bold('Monitor diubah')} — {bold(esc(target))}\n"
        f"  {detail}\n"
        f"  stop: {mono(f'/monitor stop {esc(target)}')}"
    )


async def edit_monitor(session: AsyncSession, text: str) -> str:
    from app.monitoring import is_monitor_target

    text = (text or "").strip()
    parts = text.split(None, 1)
    if not parts:
        return (
            f"{bold('Monitor edit')} — pakai:\n"
            f"{mono('/monitor edit <target> nama <label>')} "
            f"{mono('[interval <detik>] [timeout <detik>]')}"
        )
    target = parts[0].lower()
    if not is_monitor_target(target):
        return f"{bold('Monitor edit')} — {mono(esc(target))} bukan IP/domain yang valid."
    edits = _parse_monitor_edits(parts[1] if len(parts) > 1 else "")
    if not edits:
        return (
            f"{bold('Monitor edit')} — gak ada param yang bisa diubah.\n"
            f"Pakai {mono('nama / interval / timeout')}. Contoh: "
            f"{mono('/monitor edit 8.8.8.8 nama DNS GOOGLE interval 120')}"
        )
    return await edit_monitor_target(session, target, edits)


async def stop_monitoring(session: AsyncSession, target_str: str) -> str:
    from app.memory import remember
    from app.monitoring import (
        disable_monitor_target,
        get_target_by_string,
        is_monitor_target,
        remove_monitor_state,
    )

    target_str = (target_str or "").strip().lower()
    if not is_monitor_target(target_str):
        return f"{bold('Monitor')} — target gak valid."
    row = await get_target_by_string(session, target_str)
    if row is None or not row.enabled:
        return f"{bold('Monitor')} — {mono(esc(target_str))} gak lagi dipantau."
    await disable_monitor_target(session, row)
    await remove_monitor_state(row.id)
    await remember(
        session,
        f"monitor {target_str} di-stop",
        kind="monitor",
        source="chat",
        meta={"target_id": row.id},
    )
    return f"{bold('Monitor di-stop')} — {bold(esc(target_str))} gak bakal ditegur lagi."


async def monitoring_list(session: AsyncSession) -> str:
    from app.monitoring import list_monitored_ids

    ids = await list_monitored_ids()
    if not ids:
        return (
            f"{bold('Monitor')} — belum ada target yang dipantau.\n"
            f"Contoh: {mono('/monitor 8.8.8.8')} atau {mono('/monitor 114.120.14.5')}"
        )
    subq = (
        select(NetworkCheck.target_id, func.max(NetworkCheck.id).label("max_id"))
        .where(NetworkCheck.target_id.in_(ids))
        .group_by(NetworkCheck.target_id)
        .subquery()
    )
    latest = (
        await session.execute(select(NetworkCheck).join(subq, NetworkCheck.id == subq.c.max_id))
    ).scalars().all()
    by_tid = {c.target_id: c for c in latest}
    targets = (
        await session.execute(
            select(NetworkTarget).where(NetworkTarget.id.in_(ids)).order_by(NetworkTarget.id)
        )
    ).scalars().all()
    lines = [bold("Monitor aktif")]
    for idx, t in enumerate(targets, 1):
        c = by_tid.get(t.id)
        st = getattr(c.status, "value", c.status) if c else "belum dicek"
        latency = f" · {c.latency_ms:.0f} ms" if c and c.latency_ms is not None else ""
        name = t.name if t.name and t.name != t.target else t.target
        lines.append(f"\n• {bold(esc(name))} — {mono(st)}{latency}")
        lines.append(
            f"  #{idx} · interval {t.interval_seconds}s · stop: /monitor stop {idx} · edit: /monitor edit {idx} nama …"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Jarvis memory (/memory, /forget)
# ---------------------------------------------------------------------------

async def _memory(session: AsyncSession, arg: str) -> str:
    from app.memory import format_memory_list, memory_stats, recall, search_memories

    q = (arg or "").strip()
    stats = await memory_stats(session)
    if q.lower().startswith("cari"):
        term = q[4:].strip().lstrip(":")
        if not term:
            return (
                f"{bold('Memory cari')} — kasih kata kuncinya dong.\n"
                f"Contoh: {mono('/memory cari bios')}"
            )
        return format_memory_list(await search_memories(session, term, limit=10), stats=stats)
    return format_memory_list(await recall(session, limit=10), stats=stats)


async def _forget(session: AsyncSession, arg: str) -> str:
    from app.memory import forget

    mem_id = (arg or "").strip()
    if not mem_id.isdigit():
        return f"{bold('Forget')} — kasih id-nya dong.\nContoh: {mono('/forget 12')}"
    return await forget_memory(session, int(mem_id)) + f"\n\nLihat: {mono('/memory')}"


async def forget_memory(session: AsyncSession, memory_id: int) -> str:
    from app.memory import forget

    if await forget(session, memory_id):
        return f"{bold('Memory dihapus')} — #{memory_id}."
    return f"{bold('?')} Memory #{memory_id} nggak ketemu."