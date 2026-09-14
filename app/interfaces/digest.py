"""Morning digest builder — a compact daily summary pushed to Telegram at
``TELEGRAM_DIGEST_HOUR`` WIB (default 06:00). Combines significant
earthquakes, network summary, top news and watchlist weather.
"""

from __future__ import annotations

import datetime as _dt
from zoneinfo import ZoneInfo

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Earthquake,
    NetworkCheck,
    NetworkTarget,
    WeatherForecast,
    WeatherLocation,
)
from app.interfaces.formatter import bold, dt_wib, esc, mono, num

DIGEST_MIN_MAGNITUDE = 4.5
DIGEST_QUAKE_LIMIT = 5
DIGEST_NEWS_LIMIT = 3
DIGEST_WEATHER_LIMIT = 5
DIGEST_CUTOFF_HOURS = 24


async def build_digest(session: AsyncSession) -> str:
    now = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
    lines = [
        bold(f"KELA Morning Digest — {dt_wib(now)}\n"),
    ]

    # 1) Significant earthquakes in the last 24h
    quake_start = now - _dt.timedelta(hours=DIGEST_CUTOFF_HOURS)
    quakes = (
        await session.execute(
            select(Earthquake)
            .where(Earthquake.occurred_at >= quake_start)
            .where(Earthquake.magnitude >= DIGEST_MIN_MAGNITUDE)
            .order_by(desc(Earthquake.occurred_at))
            .limit(DIGEST_QUAKE_LIMIT)
        )
    ).scalars().all()
    lines.append(bold("Gempa 24 jam (M ≥ 4.5)"))
    if quakes:
        for eq in quakes:
            mag = num(eq.magnitude, 1) if eq.magnitude is not None else "?"
            when = dt_wib(eq.occurred_at)
            lines.append(f"  • M{mag} — {esc(eq.place or '?')} ({when})")
    else:
        lines.append("  (tidak ada gempa signifikan)")

    # 2) Network summary
    lines.append(bold("\nJaringan"))
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
        lines.append("  (tidak ada target)")
    else:
        by_tid = {c.target_id: c for c in latest}
        up = down = other = 0
        rows = []
        for t in targets:
            c = by_tid.get(t.id)
            st = getattr(c.status, "value", c.status) if c else "belum dicek"
            if st == "up":
                up += 1
            elif st in ("down", "timeout", "error"):
                down += 1
            else:
                other += 1
            lat = f", {num(c.latency_ms, 0)} ms" if c and c.latency_ms else ""
            rows.append(f"  • {mono(st)} {esc(t.name)}{lat}")
        lines.append(f"  {up} up · {down} down · {other} lainnya")
        lines.extend(rows[:8])

    # 3) Top news
    from app.interfaces.commands import _render_news

    news = await _render_news(
        session, title="Berita teratas", tech_only=False, limit=DIGEST_NEWS_LIMIT
    )
    lines.append("\n" + news)

    # 4) Weather watchlist (top N enabled locations, nearest forecast)
    lines.append(bold("\nCuaca watchlist"))
    locs = (
        await session.execute(
            select(WeatherLocation).where(WeatherLocation.enabled.is_(True)).limit(DIGEST_WEATHER_LIMIT)
        )
    ).scalars().all()
    if not locs:
        lines.append("  (watchlist kosong)")
    else:
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
        for loc in locs:
            pair = nearest.get(loc.id)
            if pair is None:
                lines.append(f"  • {esc(loc.name)} — belum ada data forecast")
                continue
            f, _ = pair
            descr = esc(f.weather_description or "n/a")
            temp = f"{num(f.temperature_c, 1)}°C" if f.temperature_c is not None else "?"
            lines.append(f"  • {esc(loc.name)}: {descr}, {temp}")

    lines.append(f"\n— dari {bold('The KELAMINS Project')}")
    return "\n".join(lines)