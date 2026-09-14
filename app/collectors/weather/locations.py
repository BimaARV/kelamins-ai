"""BMKG location resolver based on the Kemendagri administrative index.

The index (SK Mendagri 100.1.1-6117/2022) ships with the image as
``data/kemendagri_adm.csv`` (91k rows, one per admin area). Free-text queries
such as "Cimanggis", "Tapos", "Kota Depok" or raw adm4 codes are resolved to a
village-level (adm4) code accepted by the BMKG weather API. Facts about where a
code points to always come from this official index, never from AI.
"""

from __future__ import annotations

import csv
import logging
import re
import threading
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

_ADM4 = re.compile(r"^\d{2}(\.\d{2}){2}\.\d{4}$")
_WORD = re.compile(r"[a-z0-9]+")
_PREFIXES = ("kota ", "kab. ", "kabupaten ", "kec. ", "kecamatan ", "kel. ", "kelurahan ", "desa ")


class LocationIndex:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.codes: dict[str, str] = {}
        self.labels: dict[str, str] = {}
        self.children: dict[str, list[str]] = {}
        self._rows: list[tuple[str, str]] = []

    def load(self) -> None:
        if self.path.exists():
            with self.path.open(newline="", encoding="utf-8") as handle:
                for row in csv.reader(handle):
                    if len(row) != 2:
                        continue
                    code = row[0].strip()
                    name = row[1].strip()
                    if not code or not name:
                        continue
                    self.codes[code] = name
                    self._rows.append((code, name))
        else:
            logger.warning("weather location index not found at %s", self.path)
        self._rows.sort(key=lambda item: item[0])
        labels = {}
        for code, name in self._rows:
            parts = code.split(".")
            chain = []
            for i in range(1, len(parts)):
                parent = ".".join(parts[:i])
                parent_name = self.codes.get(parent)
                if parent_name:
                    chain.append(parent_name)
            chain.append(name)
            labels[code] = " › ".join(chain)
        self.labels = labels
        for code, _name in self._rows:
            parent = code.rsplit(".", 1)[0] if "." in code else ""
            if parent != code and parent in self.codes:
                self.children.setdefault(parent, []).append(code)
        for child in self.children.values():
            child.sort()
        logger.info("weather location index loaded: %d areas", len(self._rows))

    @staticmethod
    def normalize(text: str) -> str:
        lowered = text.lower()
        for prefix in _PREFIXES:
            if lowered.startswith(prefix):
                lowered = lowered[len(prefix):]
                break
        return " ".join(_WORD.findall(lowered))

    def _is_village(self, code: str) -> bool:
        return code.count(".") == 3

    def first_village(self, code: str) -> str | None:
        cursor = code
        while cursor in self.children and self.children[cursor]:
            cursor = self.children[cursor][0]
        return cursor if self._is_village(cursor) else None

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        if _ADM4.match(query.strip()):
            name = self.codes.get(query.strip())
            if name:
                return [self._entry(query.strip(), name)]
            return []
        tokens = set(self.normalize(query).split())
        query_norm = self.normalize(query)
        if not tokens:
            return []
        scored: list[tuple[tuple[int, int], tuple[str, str]]] = []
        for code, name in self._rows:
            name_norm = self.normalize(name)
            label = self._label_norm(code)
            exact = name_norm == query_norm
            whole = query_norm in label
            matched = sum(1 for token in tokens if token in label)
            if not exact and matched == 0:
                continue
            base = 100 if exact else 0
            base += 50 if whole else 0
            base += matched * 10
            if self.labels[code].count(name) >= 2:
                base += 20
            if ">kota " in self._label_lower(code):
                base += 25
            scored.append(((base, -len(code)), (code, name)))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [self._entry(code, name) for _score, (code, name) in scored[:limit]]

    def _label_norm(self, code: str) -> str:
        parts = [self.normalize(segment.strip()) for segment in self.labels[code].split("›")]
        return ">".join(part for part in parts if part)

    def _label_lower(self, code: str) -> str:
        parts = [" ".join(_WORD.findall(segment.lower())) for segment in self.labels[code].split("›")]
        return ">".join(part for part in parts if part)

    def _entry(self, code: str, name: str) -> dict[str, Any]:
        return {
            "code": code,
            "name": name,
            "level": code.count(".") + 1,
            "label": self.labels.get(code, name),
            "village": self.first_village(code),
        }

    def match(self, query: str, limit: int = 10) -> dict[str, Any] | None:
        hits = self.search(query, limit=limit)
        if not hits:
            return None
        return hits[0]


_index: LocationIndex | None = None
_index_lock = threading.Lock()


def get_index() -> LocationIndex:
    global _index
    with _index_lock:
        if _index is None:
            _index = LocationIndex(settings.weather_location_index_path)
            _index.load()
    return _index


def resolve_location(query: str) -> dict[str, Any] | None:
    if not query or not query.strip():
        return None
    return get_index().match(query)


def search_locations(query: str, limit: int = 10) -> list[dict[str, Any]]:
    if not query or not query.strip():
        return []
    return get_index().search(query, limit=limit)