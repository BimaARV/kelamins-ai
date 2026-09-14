"""Pytest fixtures shared across KELA AI tests.

Also patches SQLite so BigInteger primary keys alias rowid (SQLite only gives
autoincrement to INTEGER PRIMARY KEY; MariaDB keeps BIGINT in production).
"""

from sqlalchemy.ext.compiler import compiles
from sqlalchemy.types import BigInteger


@compiles(BigInteger, "sqlite")
def _compile_bigint_sqlite(type_, compiler, **kw):
    return "INTEGER"


import pytest


@pytest.fixture
def sample_rss_bytes() -> bytes:
    return b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <link>https://example.com</link>
    <description>Fixture feed</description>
    <item>
      <title>Gempa guncang Maluku Barat Daya</title>
      <link>https://example.com/a/gempa-maluku</link>
      <description>Gempa Magnitudo 5.2 mengguncang Maluku Barat Daya.</description>
      <author>Reporter</author>
      <pubDate>Tue, 10 Sep 2026 01:02:03 GMT</pubDate>
    </item>
    <item>
      <title>Network provider alami gangguan</title>
      <link>https://example.com/b/network-gangguan</link>
      <description>Gangguan koneksi dilaporkan di beberapa wilayah.</description>
      <pubDate>Tue, 10 Sep 2026 02:03:04 GMT</pubDate>
    </item>
    <item>
      <title>Invalid URL entry</title>
      <link>javascript:alert(1)</link>
      <description>should be dropped</description>
      <pubDate>Tue, 10 Sep 2026 03:04:05 GMT</pubDate>
    </item>
  </channel>
</rss>
"""