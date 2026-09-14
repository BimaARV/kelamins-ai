"""Earthquake collectors."""

from app.collectors.earthquake.bmkg import fetch_latest_earthquake, parse_bmkg_gempa

__all__ = ["fetch_latest_earthquake", "parse_bmkg_gempa"]