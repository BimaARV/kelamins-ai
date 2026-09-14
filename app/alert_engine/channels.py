"""Alert delivery channels (Telegram Bot API + Discord webhooks).

Fact-driven, async, and failure-tolerant: a failing channel never breaks the
alert engine. No tokens are stored in the database - configuration comes from
the environment only (config/env.settings), and delivery rows keep just the
channel + external id + error string.
"""

from __future__ import annotations

import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import AlertDeliveryStatus, AlertMessage

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class ChannelRegistry:
    def __init__(self, telegram_token: str | None, telegram_chat_id: str | None, discord_webhook_url: str | None):
        self.telegram_token = telegram_token
        self.telegram_chat_id = telegram_chat_id
        self.discord_webhook_url = discord_webhook_url

    @classmethod
    def from_settings(cls, settings: Settings) -> "ChannelRegistry":
        return cls(
            telegram_token=settings.telegram_bot_token and settings.telegram_bot_token.strip() or None,
            telegram_chat_id=settings.telegram_chat_id and settings.telegram_chat_id.strip() or None,
            discord_webhook_url=settings.discord_webhook_url and settings.discord_webhook_url.strip() or None,
        )

    def channels(self) -> list[str]:
        available = []
        if self.telegram_token and self.telegram_chat_id:
            available.append("telegram")
        if self.discord_webhook_url:
            available.append("discord")
        return available or ["none"]

    def enabled(self) -> bool:
        return bool(self.channels() and self.channels() != ["none"])

    def summary(self) -> dict:
        return {
            "telegram_enabled": bool(self.telegram_token and self.telegram_chat_id),
            "discord_enabled": bool(self.discord_webhook_url),
        }


async def deliver_message(
    session: AsyncSession, message: AlertMessage, registry: ChannelRegistry
) -> list[dict]:
    """Return a list of {channel, status, external_id?, error?} per channel."""
    _ = session  # deliveries are fire-and-forget after the body is built
    channels = registry.channels()
    results: list[dict] = []
    text = f"<b>{message.title}</b>\n\n{message.body}"
    for channel in channels:
        try:
            if channel == "telegram":
                external_id = await _send_telegram(
                    registry.telegram_token, registry.telegram_chat_id, text
                )
                results.append(
                    {"channel": channel, "status": AlertDeliveryStatus.delivered, "external_id": external_id}
                )
            elif channel == "discord":
                external_id = await _send_discord(registry.discord_webhook_url, text)
                results.append(
                    {"channel": channel, "status": AlertDeliveryStatus.delivered, "external_id": external_id}
                )
            else:
                results.append({"channel": channel, "status": AlertDeliveryStatus.skipped, "error": "no channel configured"})
        except Exception as exc:  # noqa: BLE001
            logger.warning("alert channel %s failed: %s", channel, exc)
            results.append(
                {"channel": channel, "status": AlertDeliveryStatus.failed, "error": str(exc)[:2000]}
            )
    return results


async def _send_telegram(token: str, chat_id: str, text: str) -> str:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{TELEGRAM_API}/bot{token}/sendMessage", json=payload)
        data = resp.json()
    if resp.status_code != 200 or not data.get("ok"):
        raise ValueError(data.get("description") or f"telegram http {resp.status_code}")
    return str(data.get("result", {}).get("message_id") or "")


async def _send_discord(webhook_url: str, text: str, username: str = "KELA Alert") -> str:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(webhook_url, json={"username": username, "content": text})
    if resp.status_code not in (200, 204):
        raise ValueError(f"discord http {resp.status_code}")
    return ""