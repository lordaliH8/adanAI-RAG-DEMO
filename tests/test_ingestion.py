"""Rebuild travel facts from the committed JSONL inputs, with independent oracles."""
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.config import Settings
from app.etl import IngestionError, initialize_database

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ("alibaba", "eghamat24", "safarmarket")


def read_source_records():
    return [json.loads(line) for source in SOURCES
            for line in (ROOT / "data/raw" / (source + ".jsonl")).read_text().splitlines() if line.strip()]


def independent_id(row):
    return hashlib.sha256(f"{row['source']}:{row['entity_type']}:{row['source_id']}".encode()).hexdigest()[:24]


@pytest.fixture(scope="module")
def populated(tmp_path_factory):
    settings = Settings(_env_file=None, data_dir=ROOT / "data", state_dir=tmp_path_factory.mktemp("travel-state"))
    report = initialize_database(settings)
    return settings, report


def test_actual_jsonl_rebuild_has_source_derived_counts_and_stable_ids(populated):
    settings, report = populated
    raw = read_source_records()
    diagnostic = [r for r in raw if r.get("data", {}).get("content_type") in {"inventory_probe", "collection_status"}]
    business = [r for r in raw if r not in diagnostic]
    expected_ids = {independent_id(r) for r in business}
    coverage_rows = sum(bool(line.strip()) for line in (ROOT / "data/raw/coverage.jsonl").read_text().splitlines())
    assert report["record_source_rows"] == len(raw)
    assert report["source_rows"] == len(raw) + coverage_rows
    assert report["records"] == len(expected_ids)
    assert report["skipped_diagnostic_count"] == len(diagnostic)
    assert report["skipped_diagnostics"] == dict(Counter(r["data"]["content_type"] for r in diagnostic))
    assert report["duplicates_removed"] == 0
    with sqlite3.connect(settings.db_path) as db:
        assert {r[0] for r in db.execute("SELECT id FROM records")} == expected_ids
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("SELECT COUNT(*) FROM coverage").fetchone()[0] == coverage_rows
        assert db.execute("SELECT COUNT(*) FROM stay_rates s LEFT JOIN properties p ON p.record_id=s.property_id WHERE p.record_id IS NULL").fetchone()[0] == 0
    assert initialize_database(settings)["cache_hit"]


def test_every_transport_projection_matches_raw_units_dates_and_availability(populated):
    settings, _ = populated
    expected = []
    for row in read_source_records():
        data = row.get("data", {})
        if row["entity_type"] != "offer" or data.get("offer_type") != "transport":
            continue
        multiplier = {"IRR": 1, "IRT": 10}[data["currency"]]
        amount = int(Decimal(str(data["amount"]))) * multiplier
        mode = "flight" if data["mode"] == "air" else data["mode"]
        timestamps = []
        for key in ("departure_at", "arrival_at"):
            value = data.get(key)
            if value and datetime.fromisoformat(value).tzinfo is None:
                assert data["time_zone"] == "Asia/Tehran"
                value = datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Asia/Tehran")).isoformat()
            timestamps.append(value)
        expected.append((independent_id(row), mode, data["service_date"], *timestamps,
                         amount, data["availability"], data.get("available_seats"), data["offer_kind"]))
    with sqlite3.connect(settings.db_path) as db:
        actual = db.execute("SELECT record_id,mode,service_date,departure_at,arrival_at,amount_irr,availability,available_seats,offer_kind FROM transport_offers ORDER BY record_id").fetchall()
    assert actual == sorted(expected)
    assert sum(r[5] % 10 != 0 for r in actual) == sum(r[5] % 10 != 0 for r in expected)
    assert any(r[5] % 10 for r in actual)  # Fractional-toman fares must not be rounded away.


def test_pinned_real_quotes_preserve_party_size_room_plan_and_missing_arrival(populated):
    settings, _ = populated
    with sqlite3.connect(settings.db_path) as db:
        flight = db.execute("SELECT mode,amount_irr,available_seats,departure_at,arrival_at,flight_number FROM transport_offers WHERE record_id='8ab52f6cc64c3e44f55dd263'").fetchone()
        assert flight == ("flight", 120000000, 9, "2026-11-17T07:30:00+03:30", "2026-11-17T08:30:00+03:30", "3114")
        assert db.execute("SELECT amount_irr,arrival_at FROM transport_offers WHERE record_id='f2339e7abc1356e44b5dd891'").fetchone() == (13130000, None)
        for id in ("f2200987c1bd5e36fa3e4eb4", "6a4cac02999f59172672e65a"):
            row = db.execute("SELECT property_id,room_id,rate_plan_id,amount_irr,occupancy,inventory,min_stay,max_stay,board_type FROM stay_rates WHERE record_id=?", (id,)).fetchone()
            assert row == ("eebaa9ba7ec7123ad48040cc", "282", "184", 10800000, 2, 3, None, None, "room_only")
            payload = json.loads(db.execute("SELECT data_json FROM records WHERE id=?", (id,)).fetchone()[0])
            assert payload["room_type_source_id"] == 282 and isinstance(payload["room_type_source_id"], int)
        assert db.execute("SELECT COUNT(*) FROM transport_offers WHERE origin_fa='مشهد' AND destination_fa='تهران' AND service_date='2026-11-19'").fetchone()[0] == 0
        # There are calendar observations, but none becomes an exact service.
        assert db.execute("SELECT COUNT(*) FROM transport_offers WHERE offer_kind='daily_fare_aggregate' AND departure_at IS NOT NULL").fetchone()[0] == 0


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


@pytest.fixture
def minimal(tmp_path):
    folder = tmp_path / "data/raw"
    folder.mkdir(parents=True)
    def record(source, kind, id, data, name="نمونه"):
        return {"source": source, "entity_type": kind, "source_id": id,
                "source_url": "https://example.test/" + str(id), "name_fa": name,
                "city_fa": "مشهد", "data": data,
                "evidence": {"observed_at": "2026-10-18T09:00:00+00:00"}}
    property = record("eghamat24", "property", 106, {"property_type": "hotel", "stars": 1,
        "rating": 5, "rating_scale": 10, "check_in": "14:00:00", "check_out": "12:00:00",
        "price_from_irr": 10000000, "price_currency": "IRR", "amenities": ["پارکینگ"]})
    stay = record("eghamat24", "offer", "night-1", {"offer_type": "stay", "offer_kind": "room_night",
        "property_source_id": 106, "room_type_source_id": 282, "rate_plan_source_id": 184,
        "service_date": "2026-11-17", "checkin": "2026-11-17", "checkout": "2026-11-18",
        "amount": 10800000, "currency": "IRR", "price_basis": "per_room_per_night",
        "availability": "available", "occupancy": 2, "inventory": 3, "min_stay": None,
        "max_stay": None, "source_rate": {"closed": 0, "close_to_arrival": 0, "close_to_departure": 0}})
    flight = record("alibaba", "offer", "flight-1", {"offer_type": "transport", "offer_kind": "departure",
        "mode": "air", "origin_fa": "تهران", "destination_fa": "مشهد", "service_date": "2026-11-17",
        "departure_at": "2026-11-17T07:30:00", "arrival_at": "2026-11-17T08:30:00",
        "time_zone": "Asia/Tehran", "amount": 123456789, "currency": "IRR",
        "price_basis": "per_adult_one_way", "availability": "available", "available_seats": 9,
        "source_details": {"priceInfant": 0}})
    probe = record("alibaba", "content", "probe", {"content_type": "inventory_probe"})
    write_jsonl(folder / "alibaba.jsonl", [flight, probe])
    write_jsonl(folder / "eghamat24.jsonl", [property, stay])
    write_jsonl(folder / "safarmarket.jsonl", [])
    write_jsonl(folder / "coverage.jsonl", [{"id": "cov-1", "source": "alibaba", "mode": "flight",
        "origin_fa": "تهران", "destination_fa": "مشهد", "service_date": "2026-11-19",
        "status": "planned", "checked_at": None, "result_count": None,
        "reason": "No collection; not absence", "query_url": None}])
    return Settings(_env_file=None, data_dir=tmp_path / "data", state_dir=tmp_path / "state")


def test_null_zero_currency_and_typed_source_ids(minimal):
    initialize_database(minimal)
    with sqlite3.connect(minimal.db_path) as db:
        assert db.execute("SELECT amount_irr,child_price_irr,infant_price_irr FROM transport_offers").fetchone() == (123456789, None, 0)
        assert db.execute("SELECT min_stay,max_stay FROM stay_rates").fetchone() == (None, None)
        assert db.execute("SELECT status,result_count FROM coverage").fetchone() == ("planned", None)
        assert db.execute("SELECT checkin_time,checkout_time FROM properties").fetchone() == ("14:00:00", "12:00:00")


def test_identical_duplicates_are_counted_and_conflicts_preserve_live_database(minimal):
    first = initialize_database(minimal)
    path = minimal.raw_dir / "alibaba.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    write_jsonl(path, rows + [rows[0]])
    assert initialize_database(minimal)["duplicates_removed"] == 1
    before = hashlib.sha256(minimal.db_path.read_bytes()).hexdigest()
    changed = json.loads(json.dumps(rows[0])); changed["data"]["amount"] += 1
    write_jsonl(path, rows + [changed])
    with pytest.raises(IngestionError, match="Source validation failed"):
        initialize_database(minimal)
    assert hashlib.sha256(minimal.db_path.read_bytes()).hexdigest() == before
    assert first["skipped_diagnostics"] == {"inventory_probe": 1}


@pytest.mark.parametrize("change", ["currency", "fraction", "invalid_date", "unknown_timezone", "orphan_property", "duplicate_night"])
def test_bad_units_dates_and_nightly_keys_cannot_replace_valid_database(minimal, change):
    initialize_database(minimal)
    before = hashlib.sha256(minimal.db_path.read_bytes()).hexdigest()
    source = "alibaba" if change in {"currency", "fraction", "invalid_date", "unknown_timezone"} else "eghamat24"
    path = minimal.raw_dir / (source + ".jsonl")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if change == "currency": rows[0]["data"]["currency"] = "USD"
    elif change == "fraction": rows[0]["data"]["amount"] = 12.25
    elif change == "invalid_date": rows[0]["data"]["service_date"] = "2026-02-31"
    elif change == "unknown_timezone": rows[0]["data"].pop("time_zone")
    elif change == "orphan_property": rows[1]["data"]["property_source_id"] = 999
    else:
        duplicate = json.loads(json.dumps(rows[1])); duplicate["source_id"] = "another-night-id"; rows.append(duplicate)
    write_jsonl(path, rows)
    with pytest.raises(IngestionError): initialize_database(minimal)
    assert hashlib.sha256(minimal.db_path.read_bytes()).hexdigest() == before


def test_cache_recovers_and_pipeline_version_invalidates(minimal, monkeypatch):
    initialize_database(minimal)
    assert initialize_database(minimal)["cache_hit"]
    (minimal.state_dir / "database-ingestion.json").write_text("{interrupted")
    assert not initialize_database(minimal)["cache_hit"]
    monkeypatch.setattr("app.etl.PIPELINE_VERSION", "travel-test-new-normalization")
    rebuilt = initialize_database(minimal)
    assert not rebuilt["cache_hit"] and rebuilt["pipeline_version"] == "travel-test-new-normalization"
