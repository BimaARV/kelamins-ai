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
    f"  /network    {esc('Status jaringan terkini')}\n"
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
    f"  /alerts     {esc('Alert terbaru')}\n"
    f"  /documents  {esc('Daftar dokumen')}\n"
    f"  /crons      {esc('Daftar cron/reminder aktif')}\n"
    f"  /cron cancel {esc('<id> — batalin cron (contoh: /cron cancel a1b2c3)')}\n"
    f"\n"
    f"Ketik pesan biasa untuk chat natural language (perlu AI key aktif).\n"
    f"Contoh cron: 'ingetin pulang jam 17.00', 'cron besok jam 7 pagi ingetin meeting'."
)


async def dispatch(text: str, session: AsyncSession) -> str:
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
    }
    handler = table.get(cmd)
    if handler is None:
        return (
            f"{bold('?')} Perintah tidak dikenal: {mono(cmd)}\n"
            f"Ketik {mono('/help')} untuk daftar perintah."
        )
    try:
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
) -> str:
    stmt = (
        select(Article, Source.name.label("source_name"))
        .join(Source, Article.source_id == Source.id, isouter=True)
        .order_by(desc(Article.scraped_at))
        .limit(limit)
    )
    filters: list = []
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
        when = dt(art.scraped_at)
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
    ip = extract_ip(arg)
    if not ip:
        return (
            f"{bold('ASN')} — kasih IP publik dong.\n"
            f"Contoh: {mono('/asn 8.8.8.8')}"
        )
    info = await asn_lookup(ip)
    if info is None:
        return f"{bold('ASN')} — {mono(esc(ip))} gagal di-resolve."
    return format_asn(ip, info)


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
    subq = (
        select(NetworkCheck.target_id, func.max(NetworkCheck.id).label("max_id"))
        .group_by(NetworkCheck.target_id)
        .subquery()
    )
    latest = (
        await session.execute(
            select(NetworkCheck).join(subq, NetworkCheck.id == subq.c.max_id).order_by(NetworkCheck.target_id)
        )
    ).scalars().all()
    targets = (
        await session.execute(select(NetworkTarget).order_by(NetworkTarget.id))
    ).scalars().all()
    if not targets:
        return f"{bold('Network')} — tidak ada target."
    by_tid = {c.target_id: c for c in latest}
    lines = [bold("Status jaringan")]
    up_count = down_count = 0
    for t in targets:
        c = by_tid.get(t.id)
        st = getattr(c.status, "value", c.status) if c else " belum dicek"
        up_count += 1 if st == "up" else 0
        down_count += 1 if st in ("down", "timeout", "error") else 0
        latency = f" — {num(c.latency_ms, 0)} ms" if c and c.latency_ms is not None else ""
        lines.append(
            f"\n{bold(esc(t.name))} [{esc(getattr(t.target_type, 'value', t.target_type))}]"
            f" {mono(st)}{latency}"
        )
    if up_count or down_count:
        lines.append(f"\nRingkasan: {up_count} up · {down_count} down · {len(targets) - up_count - down_count} lainnya")
    return "\n".join(lines)


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
    from app.cron import delete_job, format_jobs_list, list_jobs

    parts = (arg or "").split()
    if len(parts) == 2 and parts[0].lower() == "cancel":
        job_id = parts[1]
        if await delete_job(job_id):
            return f"{bold('Cron dihapus')} — {mono(esc(job_id))}.\n\n" + format_jobs_list(await list_jobs())
        return f"{bold('?')} Cron {mono(esc(job_id))} nggak ketemu."
    return format_jobs_list(await list_jobs()) + f"\n\nBatal: {mono('/cron cancel <id>')}"