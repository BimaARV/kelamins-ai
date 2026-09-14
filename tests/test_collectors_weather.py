"""BMKG weather collector offline tests (fixture payload, no network)."""

import json

from app.collectors.weather.bmkg import parse_weather_payload

FIXTURE = {
    "lokasi": {
        "adm1": "32",
        "adm2": "32.76",
        "adm3": "32.76.06",
        "adm4": "32.76.06.1001",
        "provinsi": "Jawa Barat",
        "kotkab": "Kota Depok",
        "kecamatan": "Beji",
        "desa": "Beji",
        "lon": 106.82308,
        "lat": -6.37805,
        "timezone": "Asia/Jakarta",
    },
    "data": [
        {
            "lokasi": {"adm4": "32.76.06.1001"},
            "cuaca": [
                [
                    {
                        "datetime": "2026-09-10T23:00:00Z",
                        "t": 26,
                        "tcc": 5,
                        "tp": 0,
                        "weather": 0,
                        "weather_desc": "Cerah",
                        "weather_desc_en": "Sunny",
                        "wd_deg": 219,
                        "wd": "S",
                        "wd_to": "N",
                        "ws": 2.7,
                        "hu": 86,
                        "vs": 7008,
                        "vs_text": "< 8 km",
                        "time_index": "10-11",
                        "analysis_date": "2026-09-10T12:00:00",
                        "image": "https://example.invalid/icon.png",
                    }
                ]
            ],
        }
    ],
}


def test_parse_weather_payload_extracts_location():
    info, rows = parse_weather_payload(FIXTURE)
    assert info["adm4"] == "32.76.06.1001"
    assert info["kotkab"] == "Kota Depok"
    assert info["kecamatan"] == "Beji"
    assert info["latitude"] == -6.37805
    assert info["timezone"] == "Asia/Jakarta"


def test_parse_weather_payload_normalizes_rows():
    info, rows = parse_weather_payload(FIXTURE)
    assert len(rows) == 1
    row = rows[0]
    assert row["temperature_c"] == 26.0
    assert row["humidity_pct"] == 86.0
    assert row["precipitation_mm"] == 0.0
    assert row["weather_description"] == "Cerah"
    assert row["weather_description_en"] == "Sunny"
    assert row["wind_dir"] == "S"
    assert row["wind_speed_kmh"] == 2.7
    assert row["visibility_text"] == "< 8 km"
    assert row["forecast_datetime"] is not None
    assert row["analysis_date"] is not None
    assert row["raw_data"]["datetime"] == "2026-09-10T23:00:00Z"


def test_parse_weather_payload_empty():
    assert parse_weather_payload({}) == ({}, [])
    assert parse_weather_payload(None) == ({}, [])