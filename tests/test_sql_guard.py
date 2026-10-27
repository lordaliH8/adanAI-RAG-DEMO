"""Travel queries exercise AST scopes and the independent SQLite safety layer."""
import hashlib
import json
import sqlite3

import pytest

from app.schema import MIGRATION_VERSION, SCHEMA_SQL
from app.sql import SQLGuardError, SQLService


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "travel.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(SCHEMA_SQL)
        db.executemany("INSERT INTO sources VALUES(?,?)", [("eghamat24", "https://www.eghamat24.com/"), ("alibaba", "https://www.alibaba.ir/")])
        def record(id, source, kind, name, city=None):
            db.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?)", (id, source, id, kind, "https://example.test/"+id, name, city, "2026-10-18T09:00:00+00:00", "{}", "{}"))
        for id, name, city in [("p1", "هتل آذر", "مشهد"), ("p2", "هتل آریا", "مشهد"), ("p3", "مهمانپذیر", "شیراز")]:
            record(id, "eghamat24", "property", name, city)
            db.execute("INSERT INTO properties(record_id,city_fa,property_type,stars,price_kind,amenities_json) VALUES(?,?,'hotel',3,'undated_headline','[]')", (id, city))
        for id, mode, price, availability, dep, arrival, seats in [
            ("t1", "flight", 120000001, "available", "2026-11-17T07:30:00+03:30", "2026-11-17T08:30:00+03:30", 9),
            ("t2", "flight", 100000000, "sold_out", "2026-11-17T09:30:00+03:30", "2026-11-17T10:30:00+03:30", 0),
            ("t3", "bus", 13130000, "available", "2026-11-17T21:00:00+03:30", None, 25),
        ]:
            record(id, "alibaba", "offer", "تهران به مشهد")
            db.execute("""INSERT INTO transport_offers(record_id,mode,origin_fa,destination_fa,service_date,
                departure_at,arrival_at,amount_irr,availability,available_seats,offer_kind,price_basis)
                VALUES(?,?,'تهران','مشهد','2026-11-17',?,?,?,?,?,'departure','per_adult_one_way')""", (id, mode, dep, arrival, price, availability, seats))
        for id, parent, day, checkout, price, available, inventory in [
            ("s1", "p1", "2026-11-17", "2026-11-18", 10000000, "available", 3),
            ("s2", "p1", "2026-11-18", "2026-11-19", 11000000, "available", 3),
            ("s3", "p2", "2026-11-17", "2026-11-18", 20000000, "available", 2),
            ("s4", "p3", "2026-11-17", "2026-11-18", 30000000, "unavailable", 0),
        ]:
            record(id, "eghamat24", "offer", "اتاق دوتخته")
            db.execute("""INSERT INTO stay_rates(record_id,property_id,room_id,rate_plan_id,service_date,
                checkin,checkout,amount_irr,availability,occupancy,inventory,closed,price_basis)
                VALUES(?,?,'double','standard',?,?,?,?,?,2,?,0,'per_room_per_night')""", (id, parent, day, day, checkout, price, available, inventory))
        db.execute("INSERT INTO schema_migrations(version) VALUES(?)", (MIGRATION_VERSION,))
    return path


def test_filter_aggregation_and_related_travel_tables(db_path):
    result = SQLService(db_path).execute("""
        SELECT r.city_fa,COUNT(DISTINCT s.property_id) AS properties,SUM(s.amount_irr) AS total_irr
        FROM stay_rates s JOIN properties p ON p.record_id=s.property_id
        JOIN records r ON r.id=p.record_id
        WHERE r.city_fa='مشهد' AND s.availability='available'
          AND s.service_date>='2026-11-17' AND s.service_date<'2026-11-19'
        GROUP BY r.city_fa
    """)
    assert result["columns"] == ["city_fa", "properties", "total_irr"]
    assert result["rows"] == [["مشهد", 2, 41000000]]
    assert result["row_count"] == 1 and not result["truncated"]
    assert result["duration_ms"] >= 0


def test_like_glob_and_escape_are_safe_supported_patterns(db_path):
    service = SQLService(db_path)
    for predicate in ["name_fa LIKE 'هتل آ%'", "name_fa GLOB 'هتل آ*'"]:
        assert service.execute("SELECT id,name_fa FROM records WHERE " + predicate + " ORDER BY id")["rows"] == [["p1", "هتل آذر"], ["p2", "هتل آریا"]]
    assert service.execute("SELECT 'ROOM_1' LIKE 'ROOM!_1' ESCAPE '!', 'ROOM-1' LIKE 'ROOM!_1' ESCAPE '!'")["rows"] == [[1, 0]]
    with pytest.raises(SQLGuardError) as error:
        service.execute("SELECT load_extension('/tmp/x') WHERE 'safe' LIKE '%' ESCAPE '!'")
    assert error.value.code == "disallowed_function"


def test_source_enrichment_is_parameterized_and_keeps_provider_provenance(db_path):
    before = db_path.read_bytes()
    rows = SQLService(db_path).source_metadata(["p1", "' OR 1=1 --", "p1"])
    assert len(rows) == 1 and rows[0]["id"] == "p1"
    assert {"source", "source_id", "name_fa", "city_fa", "source_url", "observed_at"}.issubset(rows[0])
    assert db_path.read_bytes() == before


def test_implicit_outer_column_cannot_hide_a_misspelled_subquery_key(db_path):
    with pytest.raises(SQLGuardError) as error:
        SQLService(db_path).execute("SELECT s.record_id FROM stay_rates s WHERE s.property_id=(SELECT record_id FROM records WHERE source_id='p1')")
    assert error.value.code == "unknown_column"
    assert SQLService(db_path).execute("SELECT s.record_id FROM stay_rates s WHERE s.property_id=(SELECT r.id FROM records r WHERE r.id='p1') ORDER BY s.record_id")["rows"] == [["s1"], ["s2"]]


def test_catalog_schema_linking_returns_only_bounded_identities(db_path):
    service = SQLService(db_path)
    rows = service.entity_context("قیمت هتل آذر برای دو بزرگسال چقدر است؟")
    assert [row["id"] for row in rows] == ["p1"]
    assert set(rows[0]) == {"id", "source", "source_id", "name_fa", "city_fa"}
    assert service.entity_context("تعداد رکوردهای هتل در کاتالوگ هر منبع؟") == []


def test_catalog_schema_linking_keeps_untrusted_names_as_data(db_path):
    before = db_path.read_bytes()
    service = SQLService(db_path)
    assert service.entity_context("هتل ' OR 1=1; DROP TABLE records; --") == []
    assert service.execute("SELECT COUNT(*) FROM properties")["rows"] == [[3]]
    assert db_path.read_bytes() == before


def test_room_identity_links_its_parent_without_requiring_word_hotel(db_path):
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE stay_rates SET room_id='282' WHERE property_id='p2'")
    rows = SQLService(db_path).entity_context("همان تاریخ؛ آریا اتاق282 پلن184 را حساب کن")
    assert [row["id"] for row in rows] == ["p2"]
    assert all("amount_irr" not in row for row in rows)


def test_cte_same_room_plan_all_nights_and_correlated_scope(db_path):
    result = SQLService(db_path).execute("""
        WITH quote(property_id,room_id,rate_plan_id,total_irr) AS (
          SELECT property_id,room_id,rate_plan_id,SUM(amount_irr)
          FROM stay_rates WHERE availability='available' AND inventory>=1 AND occupancy>=2
          AND service_date>='2026-11-17' AND service_date<'2026-11-19'
          GROUP BY property_id,room_id,rate_plan_id HAVING COUNT(DISTINCT service_date)=2
        )
        SELECT r.name_fa,q.total_irr FROM quote q JOIN records r ON r.id=q.property_id
        WHERE EXISTS(SELECT 1 FROM properties p WHERE p.record_id=r.id)
        ORDER BY r.id
    """)
    assert result["rows"] == [["هتل آذر", 21000000]]  # p2 lacks the second night.


def test_timezone_date_functions_and_unknown_vs_zero(db_path):
    service = SQLService(db_path)
    result = service.execute("""SELECT record_id,CASE WHEN arrival_at IS NULL THEN NULL
      ELSE ROUND((julianday(arrival_at)-julianday(departure_at))*24*60,0) END AS minutes
      FROM transport_offers ORDER BY record_id""")
    assert result["rows"] == [["t1", 60.0], ["t2", 60.0], ["t3", None]]
    assert service.execute("SELECT inventory,max_stay FROM stay_rates WHERE record_id='s4'")["rows"] == [[0, None]]
    assert service.execute("SELECT strftime('%Y-%m',service_date),ROUND(AVG(amount_irr),2) FROM stay_rates GROUP BY 1")["rows"] == [["2026-11", 17750000.0]]


@pytest.mark.parametrize("sql", [
    "DROP TABLE transport_offers", "DELETE FROM records", "UPDATE stay_rates SET amount_irr=1",
    "INSERT INTO sources VALUES('x','https://example.test')", "SELECT 1; DELETE FROM stay_rates",
    "PRAGMA query_only=OFF", "ATTACH DATABASE '/tmp/foreign.db' AS other",
    "SELECT load_extension('/tmp/untrusted')", "SELECT readfile('/etc/passwd')", "SELECT sqlite_version()",
    "SELECT * FROM sqlite_master", "SELECT * FROM schema_migrations", "SELECT * FROM main.records",
    "SELECT * FROM pragma_table_info('records')",
    "WITH x AS (DELETE FROM records RETURNING *) SELECT * FROM x",
    "WITH x AS (SELECT * FROM schema_migrations) SELECT * FROM x",
    "WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM x) SELECT * FROM x",
    "SELECT * FROM records WHERE EXISTS(SELECT 1 FROM sqlite_master)",
    "SELECT (SELECT load_extension('/tmp/x')) FROM records", "SELECT * FROM(SELECT * FROM forbidden) nested",
])
def test_dangerous_sql_never_changes_database(db_path, sql):
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    with pytest.raises(SQLGuardError):
        SQLService(db_path).execute(sql)
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("sql", [
    "SELECT nonexistent FROM transport_offers", "SELECT secret FROM records",
    "SELECT record_id FROM properties p JOIN stay_rates s ON 1=1",
    "WITH x AS(SELECT record_id FROM properties) SELECT x.fake FROM x",
])
def test_columns_validated_in_nested_scopes(db_path, sql):
    with pytest.raises(SQLGuardError) as error:
        SQLService(db_path).execute(sql)
    assert error.value.code == "unknown_column"


def test_authorizer_blocks_writes_and_internal_reads_if_ast_is_bypassed(db_path, monkeypatch):
    monkeypatch.setattr("app.sql.validate_sql", lambda sql: sql)
    for sql in ["DROP TABLE transport_offers", "PRAGMA query_only=OFF", "SELECT * FROM schema_migrations", "ATTACH ':memory:' AS other"]:
        with pytest.raises(SQLGuardError) as error:
            SQLService(db_path).execute(sql)
        assert error.value.code == "unsafe_query"
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM records").fetchone() == (10,)


def test_semicolon_inside_literal_is_not_multiple_statements(db_path):
    assert SQLService(db_path).execute("SELECT 'safe; DROP TABLE records' AS text")["rows"] == [["safe; DROP TABLE records"]]


def test_row_limit_marks_only_actual_overflow(db_path):
    service = SQLService(db_path, max_rows=2)
    limited = service.execute("SELECT record_id FROM properties ORDER BY record_id")
    assert limited["rows"] == [["p1"], ["p2"]] and limited["truncated"]
    exact = service.execute("SELECT record_id FROM properties WHERE record_id IN('p1','p2') ORDER BY record_id")
    assert exact["row_count"] == 2 and not exact["truncated"]


def test_byte_limit_counts_utf8(db_path):
    result = SQLService(db_path, max_bytes=128).execute("SELECT '" + "الف" * 100 + "' AS payload")
    assert result["rows"] == [] and result["truncated"]
    assert len(json.dumps({"columns": result["columns"], "rows": result["rows"]}, ensure_ascii=False).encode()) <= 128


def test_instruction_budget_stops_expensive_read_only_query(db_path):
    query = "SELECT COUNT(*) FROM " + ",".join(f"stay_rates x{i}" for i in range(12))
    with pytest.raises(SQLGuardError) as error:
        SQLService(db_path).execute(query)
    assert error.value.code in {"query_limit", "query_timeout"}


def test_deadline_covers_even_small_query(db_path):
    with pytest.raises(SQLGuardError) as error:
        SQLService(db_path, timeout_seconds=0.000000001).execute("SELECT 1")
    assert error.value.code == "query_timeout"


def test_missing_database_is_not_created_and_error_hides_path(tmp_path):
    path = tmp_path / "private-database-name.db"
    with pytest.raises(SQLGuardError) as error:
        SQLService(path).execute("SELECT COUNT(*) FROM records")
    assert error.value.code == "database_unavailable"
    assert "private-database-name" not in str(error.value) and not path.exists()


def test_nonfinite_results_do_not_produce_invalid_json(db_path):
    with pytest.raises(SQLGuardError) as error:
        SQLService(db_path).execute("SELECT 1e999 AS huge")
    assert error.value.code == "non_finite_result"


def test_allowed_json_scalar_functions_keep_unknowns_and_block_extensions(db_path):
    payload = {"source_details": {"flightNumber": "3114"}, "available_seats": None, "amenities": ["پارکینگ", "آسانسور"]}
    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE records SET data_json=? WHERE id='p1'", (json.dumps(payload, ensure_ascii=False),))
    result = SQLService(db_path).execute("""SELECT json_extract(data_json,'$.source_details.flightNumber'),
        json_type(data_json,'$.available_seats'),json_array_length(data_json,'$.amenities'),
        json_valid(data_json),json_extract(data_json,'$.available_seats') FROM records WHERE id='p1'""")
    assert result["rows"] == [["3114", "null", 2, 1, None]]
    with pytest.raises(SQLGuardError) as error:
        SQLService(db_path).execute("SELECT json_extract(data_json,'$.x'),load_extension('/tmp/x') FROM records")
    assert error.value.code == "disallowed_function"
