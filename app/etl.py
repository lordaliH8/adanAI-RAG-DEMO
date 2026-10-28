"""Build the travel database from raw JSONL facts, never a prebuilt database."""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import tempfile
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

from app.normalization import normalize_text
from app.schema import MIGRATION_VERSION, SCHEMA_SQL, TABLE_COLUMNS
from app.storage import read_json, write_json

PIPELINE_VERSION = "travel-jsonl-v2-flight-number-explicit-irr-nightly-fk"
SOURCE_URLS = {"alibaba": "https://www.alibaba.ir/", "eghamat24": "https://www.eghamat24.com/", "safarmarket": "https://safarmarket.com/"}
DIAGNOSTICS = {"inventory_probe", "collection_status"}


class IngestionError(RuntimeError):
    pass


def stable_id(source, entity_type, source_id):
    return hashlib.sha256(":".join((source, entity_type, str(source_id))).encode()).hexdigest()[:24]


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_fingerprint(folder: Path, pattern: str) -> tuple[str, dict]:
    paths = sorted(folder.glob(pattern))
    if not paths:
        raise IngestionError("No input files found")
    hashes = {p.name: file_hash(p) for p in paths}
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(), hashes


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _first(data, *keys):
    return next((data[key] for key in keys if data.get(key) is not None and data[key] != ""), None)


def _integer(value, *, nullable=True, minimum=None):
    if value is None or value == "":
        if nullable:
            return None
        raise ValueError("Missing integer")
    if isinstance(value, bool):
        raise ValueError("Boolean is not an integer amount")
    try:
        numeric = Decimal(normalize_text(value).replace(",", "").replace("٬", "").replace(" ", ""))
    except InvalidOperation:
        raise ValueError("Invalid integer") from None
    if not numeric.is_finite() or numeric != numeric.to_integral_value():
        raise ValueError("Non-integral amount")
    result = int(numeric)
    if minimum is not None and result < minimum:
        raise ValueError("Integer outside domain")
    return result


def _number(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("Invalid numeric field")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite numeric field")
    return result


def _flag(value):
    if value is None or value == "":
        return None
    if value is True or value == 1 or value == "1":
        return 1
    if value is False or value == 0 or value == "0":
        return 0
    raise ValueError("Invalid flag")


def _irr(amount, currency, *, required=False):
    value = _integer(amount, nullable=not required, minimum=0)
    if value is None:
        return None
    if currency in ("IRR", "rial", "ریال"):
        return value
    if currency in ("IRT", "toman", "تومان"):
        return value * 10
    raise ValueError("Explicit known currency required")


def _day(value):
    text = normalize_text(value)
    parsed = date.fromisoformat(text)
    if parsed.isoformat() != text:
        raise ValueError("Expected YYYY-MM-DD Gregorian date")
    return text


def _timestamp(value, *, nullable=False):
    if nullable and not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Timestamp needs explicit timezone")
    return value


def _city(value):
    if value is None:
        return None
    text = normalize_text(value)
    return {"بندر عباس": "بندرعباس", "خرم آباد": "خرم‌آباد"}.get(text, text)


def _clock(value):
    if value is None or value == "":
        return None
    return time.fromisoformat(str(value)).isoformat()


def _lines(path):
    if not path.is_file():
        raise IngestionError(f"Required source file missing: {path.name}")
    with path.open(encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("Object required")
            except (ValueError, TypeError):
                raise IngestionError(f"Invalid JSON object: {path.name}:{line_number}") from None
            yield line_number, value


def _record(raw, source):
    if raw.get("source") != source or raw.get("entity_type") not in {"property", "route", "offer", "content"}:
        raise ValueError("Unknown source or entity type")
    if raw.get("source_id") is None or isinstance(raw["source_id"], bool):
        raise ValueError("Missing source identifier")
    source_id, kind = str(raw["source_id"]), raw["entity_type"]
    if not source_id:
        raise ValueError("Empty source identifier")
    data, evidence = raw.get("data") or {}, raw.get("evidence") or {}
    if not isinstance(data, dict) or not isinstance(evidence, dict):
        raise ValueError("Data and evidence must be objects")
    data = dict(data)
    city = _city(raw.get("city_fa"))
    if raw.get("city_fa") and city != raw["city_fa"]:
        data["source_city_fa"] = raw["city_fa"]
    for field in ("origin_fa", "destination_fa"):
        if data.get(field):
            normalized = _city(data[field])
            if normalized != data[field]:
                data["source_" + field] = data[field]
            data[field] = normalized
    if data.get("mode") == "air":
        data["source_mode"], data["mode"] = "air", "flight"
    for field in ("departure_at", "arrival_at"):
        if data.get(field):
            parsed = datetime.fromisoformat(data[field])
            if parsed.tzinfo is None and data.get("time_zone") == "Asia/Tehran":
                data["source_" + field] = data[field]
                data[field] = parsed.replace(tzinfo=ZoneInfo("Asia/Tehran")).isoformat()
            _timestamp(data[field])
    observed = _timestamp(raw.get("observed_at") or evidence.get("observed_at"))
    url = raw.get("source_url")
    if not isinstance(url, str) or not url.startswith(("https://", "http://")):
        raise ValueError("Missing source URL")
    return {"id": stable_id(source, kind, source_id), "source": source, "source_id": source_id,
            "entity_type": kind, "source_url": url, "name_fa": normalize_text(raw.get("name_fa") or source_id),
            "city_fa": city, "observed_at": observed, "data_json": _dumps(data), "evidence_json": _dumps(evidence)}


def read_tables(folder: Path) -> tuple[dict, dict]:
    """Typed source projections; duplicate IDs must agree, never last-write-wins."""
    tables = {name: [] for name in TABLE_COLUMNS}
    tables["sources"] = [{"id": source, "homepage": url} for source, url in SOURCE_URLS.items()]
    rows, coverage_rows = {}, {}
    skipped, raw_counts = Counter(), Counter()
    duplicate_counts = Counter()
    for source in SOURCE_URLS:
        path = folder / f"{source}.jsonl"
        for line_number, raw in _lines(path):
            raw_counts[source] += 1
            data = raw.get("data")
            if isinstance(data, dict) and data.get("content_type") in DIAGNOSTICS:
                skipped[data["content_type"]] += 1
                continue
            try:
                row = _record(raw, source)
                key = row["id"]
                if key in rows:
                    if rows[key] != row:
                        raise ValueError("Conflicting duplicate source entity")
                    duplicate_counts["records"] += 1
                else:
                    rows[key] = row
            except (ValueError, KeyError, TypeError, OverflowError):
                raise IngestionError(f"Source validation failed: {path.name}:{line_number}") from None
    tables["records"] = list(rows.values())
    for row in rows.values():
        try:
            data, rid = json.loads(row["data_json"]), row["id"]
            if row["entity_type"] == "property":
                amount = _first(data, "headline_price_amount", "price_from_irr", "min_price", "price_amount", "price")
                currency = _first(data, "headline_price_currency", "price_currency", "currency")
                headline = _irr(amount, currency) if amount is not None and currency is not None else None
                if headline is not None and headline <= 0:
                    headline = None
                address = _first(data, "address_fa", "address")
                if isinstance(address, dict):
                    address = address.get("streetAddress") or _dumps(address)
                tables["properties"].append({"record_id": rid, "city_fa": row["city_fa"],
                    "property_type": _first(data, "property_type", "type"), "stars": _number(_first(data, "stars", "star_rating")),
                    "rating": _number(_first(data, "rating_out_of_10", "rating", "rating_value")),
                    "rating_scale": 10 if data.get("rating_out_of_10") is not None else _number(data.get("rating_scale")),
                    "review_count": _integer(_first(data, "review_count", "rating_count", "comment_count"), minimum=0),
                    "headline_price_irr": headline, "price_kind": "undated_headline",
                    "checkin_time": _clock(_first(data, "checkin_time", "check_in")),
                    "checkout_time": _clock(_first(data, "checkout_time", "check_out")),
                    "address_fa": normalize_text(address) if address else None, "amenities_json": _dumps(data.get("amenities") or [])})
            elif row["entity_type"] == "offer":
                amount = _irr(data.get("amount"), data.get("currency"), required=True)
                if not amount:
                    raise ValueError("Positive offer amount required")
                service_date = _day(data["service_date"])
                if data.get("offer_type") == "transport":
                    kind = data["offer_kind"]
                    departure, arrival = data.get("departure_at"), data.get("arrival_at")
                    if kind == "departure":
                        if _timestamp(departure)[:10] != service_date:
                            raise ValueError("Departure/service date mismatch")
                        if arrival and datetime.fromisoformat(_timestamp(arrival)) < datetime.fromisoformat(departure):
                            raise ValueError("Arrival before departure")
                    elif kind != "daily_fare_aggregate" or departure is not None or arrival is not None:
                        raise ValueError("Calendar has no exact departure/arrival")
                    details = data.get("source_details") or {}
                    tables["transport_offers"].append({"record_id": rid, "mode": data["mode"],
                        "origin_fa": data["origin_fa"], "destination_fa": data["destination_fa"],
                        "service_date": service_date, "departure_at": departure, "arrival_at": arrival,
                        "amount_irr": amount, "availability": data["availability"],
                        "available_seats": _integer(data.get("available_seats"), minimum=0),
                        "operator_name": data.get("operator_name"), "offer_kind": kind, "price_basis": data["price_basis"],
                        "search_completed": _flag(data.get("search_completed")),
                        "child_price_irr": _irr(details.get("priceChild"), data.get("currency")),
                        "infant_price_irr": _irr(details.get("priceInfant"), data.get("currency")),
                        "baggage_kg": _integer(details.get("maxAllowedBaggage"), minimum=0),
                        "flight_number": str(details["flightNumber"]) if details.get("flightNumber") is not None else None})
                elif data.get("offer_type") == "stay":
                    checkin, checkout = _day(data["checkin"]), _day(data["checkout"])
                    if checkin != service_date or date.fromisoformat(checkout) != date.fromisoformat(checkin) + timedelta(days=1):
                        raise ValueError("Expected one explicit room-night")
                    if data.get("room_type_source_id") is None or data.get("rate_plan_source_id") is None:
                        raise ValueError("Room and rate-plan identifiers required")
                    raw_rate = data.get("source_rate") or {}
                    tables["stay_rates"].append({"record_id": rid,
                        "property_id": stable_id(row["source"], "property", data["property_source_id"]),
                        "room_id": str(data["room_type_source_id"]), "room_name": data.get("room_name_fa"),
                        "rate_plan_id": str(data["rate_plan_source_id"]), "rate_plan_name": data.get("rate_plan_name_fa"),
                        "service_date": service_date, "checkin": checkin, "checkout": checkout, "amount_irr": amount,
                        "availability": data["availability"], "occupancy": _integer(data.get("occupancy"), minimum=1),
                        "inventory": _integer(data.get("inventory"), minimum=0), "min_stay": _integer(data.get("min_stay"), minimum=0),
                        "max_stay": _integer(data.get("max_stay"), minimum=0), "closed": _flag(raw_rate.get("closed")),
                        "close_to_arrival": _flag(raw_rate.get("close_to_arrival")),
                        "close_to_departure": _flag(raw_rate.get("close_to_departure")), "board_type": data.get("board_type"),
                        "price_basis": data["price_basis"], "extra_capacity": _integer(data.get("extra_capacity"), minimum=0),
                        "cancelable": _flag(data.get("cancelable"))})
                else:
                    raise ValueError("Unknown offer type")
        except (ValueError, KeyError, TypeError, OverflowError):
            raise IngestionError(f"Record projection failed: {row['source']}:{row['source_id']}") from None
    for line_number, raw in _lines(folder / "coverage.jsonl"):
        raw_counts["coverage"] += 1
        try:
            row = {"id": str(raw["id"]), "source": raw["source"], "mode": raw["mode"],
                   "origin_fa": _city(raw.get("origin_fa")) or "", "destination_fa": _city(raw.get("destination_fa")) or "",
                   "service_date": _day(raw["service_date"]), "status": raw["status"],
                   "checked_at": _timestamp(raw.get("checked_at"), nullable=True),
                   "result_count": _integer(raw.get("result_count"), minimum=0), "reason": raw.get("reason"),
                   "query_url": raw.get("query_url")}
            if row["id"] in coverage_rows:
                if coverage_rows[row["id"]] != row:
                    raise ValueError("Conflicting coverage observation")
                duplicate_counts["coverage"] += 1
            else:
                coverage_rows[row["id"]] = row
        except (ValueError, KeyError, TypeError, OverflowError):
            raise IngestionError(f"Coverage validation failed: coverage.jsonl:{line_number}") from None
    tables["coverage"] = list(coverage_rows.values())
    report = {"source_rows": sum(raw_counts.values()), "record_source_rows": sum(raw_counts[s] for s in SOURCE_URLS),
              "coverage_source_rows": raw_counts["coverage"], "raw_counts": dict(raw_counts),
              "skipped_diagnostics": dict(skipped), "skipped_diagnostic_count": sum(skipped.values()),
              "duplicates_removed": sum(duplicate_counts.values()),
              "tables": {name: {"records": len(values), "duplicates_removed": duplicate_counts[name]} for name, values in tables.items()}}
    return tables, report


def initialize_database(settings, *, force=False) -> dict:
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    raw_fingerprint, hashes = source_fingerprint(settings.raw_dir, "*.jsonl")
    fingerprint = hashlib.sha256(_dumps([raw_fingerprint, PIPELINE_VERSION, MIGRATION_VERSION]).encode()).hexdigest()
    report_path = settings.state_dir / "database-ingestion.json"
    if not force and settings.db_path.is_file():
        previous = read_json(report_path, {})
        try:
            with sqlite3.connect(settings.db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
                version = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
                counts = {name: db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in TABLE_COLUMNS}
                consistent = db.execute("PRAGMA quick_check").fetchone()[0] == "ok" and not db.execute("PRAGMA foreign_key_check").fetchall()
            if isinstance(previous, dict) and previous.get("fingerprint") == fingerprint and version == MIGRATION_VERSION \
                    and consistent and all(counts[k] == previous["tables"][k]["records"] for k in counts):
                return {**previous, "cache_hit": True, "as_of": settings.as_of}
        except (sqlite3.Error, KeyError, TypeError):
            pass
    tables, report = read_tables(settings.raw_dir)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    handle, filename = tempfile.mkstemp(prefix=".travel-", suffix=".sqlite", dir=settings.db_path.parent)
    os.close(handle)
    temporary = Path(filename)
    try:
        with sqlite3.connect(temporary) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.executescript(SCHEMA_SQL)
            with db:
                for table, records in tables.items():
                    columns = list(TABLE_COLUMNS[table])
                    marks = ",".join("?" for _ in columns)
                    db.executemany(f'INSERT INTO "{table}" ({",".join(columns)}) VALUES ({marks})',
                                   [tuple(row[col] for col in columns) for row in records])
                db.execute("INSERT INTO schema_migrations(version) VALUES (?)", (MIGRATION_VERSION,))
            if db.execute("PRAGMA foreign_key_check").fetchall() or db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise IngestionError("Relational integrity check failed")
        os.replace(temporary, settings.db_path)
    except (sqlite3.Error, IngestionError):
        raise IngestionError("Travel source population failed; validate source keys, dates, units and nightly uniqueness.") from None
    finally:
        temporary.unlink(missing_ok=True)
    report.update({"stage": "relational_ingestion", "migration_version": MIGRATION_VERSION,
                   "pipeline_version": PIPELINE_VERSION, "fingerprint": fingerprint, "source_hashes": hashes,
                   "records": len(tables["records"]), "relational_rows": sum(len(v) for v in tables.values()),
                   "canonical_currency": "IRR",
                   "observed_date": min((row["observed_at"][:10] for row in tables["records"] if row.get("observed_at")), default=None),
                   "as_of": settings.as_of,
                   "normalization": ["explicit IRR/IRT→integer IRR", "typed Gregorian dates and zoned departures",
                                     "source-qualified stable IDs", "verified duplicate equality", "FK and unique room/plan/date checks",
                                     "diagnostic records separated from business facts"],
                   "finished_at": datetime.now(timezone.utc).isoformat(), "cache_hit": False})
    write_json(report_path, report)
    return report
