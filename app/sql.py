"""Bounded read-only execution of untrusted model-generated SQLite queries."""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, OptimizeError, ParseError, SqlglotError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

from app.schema import SCHEMA_CONTEXT, TABLE_COLUMNS


class SQLGuardError(Exception):
    """A controlled public error; messages never include database internals."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


SAFE_FUNCTIONS = frozenset({
    "abs", "avg", "count", "sum", "total", "min", "max", "round",
    "coalesce", "ifnull", "nullif", "length", "lower", "upper", "trim",
    "ltrim", "rtrim", "substr", "substring", "replace", "instr",
    # SQLite authorizes these deterministic operators as function calls too,
    # including the three-argument LIKE implementation used with ESCAPE.
    "like", "glob",
    "date", "datetime", "time", "julianday", "strftime", "unixepoch",
    "typeof", "cast", "iif", "if", "group_concat",
    "json_extract", "json_type", "json_array_length", "json_valid",
    "row_number", "rank", "dense_rank", "lag", "lead", "first_value",
    "last_value", "nth_value", "ntile", "percent_rank", "cume_dist",
})
FORBIDDEN_NODES = frozenset({
    "Insert", "Update", "Delete", "Drop", "Create", "Alter", "Command",
    "Pragma", "Attach", "Detach", "Merge", "Copy", "Grant", "Revoke",
    "Transaction", "Commit", "Rollback", "TruncateTable", "Into", "Lock",
    "Execute", "Use", "Set", "Cache", "Uncache", "Analyze", "Vacuum",
    "Show", "LoadData", "Export", "Import", "Placeholder", "Parameter",
})
MAX_SQL_CHARS = 16000
MAX_AST_NODES = 2000
MAX_VM_INSTRUCTIONS = 1_000_000


def _safe_function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return node.name.lower()
    # SQLGlot normalizes some date functions into internal expression names;
    # inspect the SQLite spelling, rather than permitting all internal funcs.
    rendered = node.sql(dialect="sqlite")
    match = re.match(r"\s*([A-Za-z_][A-Za-z_0-9]*)\s*\(", rendered)
    return match.group(1).lower() if match else node.sql_name().lower()


def validate_sql(sql: str) -> str:
    """Validate all scopes and AST nodes, returning the original single query."""
    if not isinstance(sql, str) or not sql.strip() or len(sql) > MAX_SQL_CHARS or "\x00" in sql:
        raise SQLGuardError("invalid_sql", "متن پرس‌وجو نامعتبر یا بیش از حد طولانی است.")
    try:
        statements = sqlglot.parse(sql, read="sqlite", error_level=ErrorLevel.RAISE)
    except (SqlglotError, ValueError, RecursionError):
        raise SQLGuardError("invalid_sql", "ساختار پرس‌وجوی تولیدشده معتبر نیست.") from None
    if len(statements) != 1:
        raise SQLGuardError("multiple_statements", "فقط یک پرس‌وجوی خواندنی مجاز است.")
    query = statements[0]
    if query is None or not isinstance(query, (exp.Select, exp.SetOperation, exp.Subquery)):
        raise SQLGuardError("unsafe_query", "فقط پرس‌وجوی SELECT خواندنی مجاز است.")
    nodes = list(query.walk())
    if len(nodes) > MAX_AST_NODES:
        raise SQLGuardError("query_limit", "پیچیدگی پرس‌وجو از حد مجاز بیشتر است.")
    for node in nodes:
        if type(node).__name__ in FORBIDDEN_NODES:
            raise SQLGuardError("unsafe_query", "این عملیات در پرس‌وجوی خواندنی مجاز نیست.")
        if isinstance(node, exp.With) and node.args.get("recursive"):
            raise SQLGuardError("recursive_cte", "پرس‌وجوی بازگشتی برای این دمو مجاز نیست.")
        if isinstance(node, exp.Func) and not isinstance(node, (exp.Case, exp.If, exp.And, exp.Or, exp.Exists)):
            # SQLite STRFTIME parsing adds an internal timestamp coercion that
            # serializes to its input unchanged; it is not an SQL function call.
            if isinstance(node, exp.TsOrDsToTimestamp) and node.sql(dialect="sqlite") == node.this.sql(dialect="sqlite"):
                continue
            if _safe_function_name(node) not in SAFE_FUNCTIONS:
                raise SQLGuardError("disallowed_function", "تابع درخواستی در فهرست توابع مجاز نیست.")
        if isinstance(node, exp.Table):
            if not isinstance(node.this, exp.Identifier) or node.db or node.catalog:
                raise SQLGuardError("unknown_table", "فقط جدول‌های عملیاتی همین پایگاه داده قابل پرس‌وجو هستند.")
    try:
        checked = normalize_identifiers(query.copy(), dialect="sqlite")
        # Scope resolution distinguishes an approved CTE/derived alias from an
        # unknown physical table, including alias shadowing and nested queries.
        scopes = traverse_scope(checked)
        for scope in scopes:
            for table in scope.tables:
                source = scope.sources.get(table.alias_or_name)
                if isinstance(source, Scope):
                    continue
                if not isinstance(source, exp.Table) or source.name.lower() not in TABLE_COLUMNS:
                    raise SQLGuardError("unknown_table", "جدول درخواستی در فهرست جدول‌های مجاز نیست.")
            if scope.is_subquery:
                local_columns = set()
                for source in scope.sources.values():
                    if isinstance(source, exp.Table):
                        local_columns.update(TABLE_COLUMNS.get(source.name.lower(), {}))
                    elif isinstance(source, Scope):
                        local_columns.update(source.expression.named_selects)
                for column in scope.columns:
                    if not column.table and column.name not in local_columns:
                        raise SQLGuardError("unknown_column", "ستون زیرپرس‌وجو در جدول محلی آن نیست؛ ارجاع بیرونی باید alias صریح داشته باشد. کلید اصلی records ستون id است، نه record_id.")
        qualify(checked, dialect="sqlite", schema=TABLE_COLUMNS,
                expand_stars=True, identify=False,
                quote_identifiers=False, validate_qualify_columns=True,
                allow_partial_qualification=False)
    except SQLGuardError:
        raise
    except (OptimizeError, ParseError, SqlglotError, ValueError, RecursionError):
        raise SQLGuardError("unknown_column", "ستون یا نام مستعار پرس‌وجو معتبر و روشن نیست.") from None
    return sql.strip()


class SQLService:
    def __init__(self, db_path, max_rows: int = 100, timeout_seconds: float = 2,
                 max_bytes: int = 50000):
        self.db_path = Path(db_path).resolve()
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= 10000:
            raise ValueError("max_rows must be an integer between 1 and 10000")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 64 <= max_bytes <= 5_000_000:
            raise ValueError("max_bytes must be an integer between 64 and 5000000")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise ValueError("timeout_seconds must be finite and in (0, 30]")
        self.max_rows = max_rows
        self.timeout_seconds = float(timeout_seconds)
        self.max_bytes = max_bytes

    def schema_context(self) -> str:
        return SCHEMA_CONTEXT

    def source_metadata(self, record_ids) -> list[dict]:
        """Bounded PK enrichment of executed rows; not a question-to-SQL lookup."""
        ids = list(dict.fromkeys(value for value in record_ids if isinstance(value, str) and 0 < len(value) <= 100))[:40]
        if not ids:
            return []
        with sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=self.timeout_seconds) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            db.set_authorizer(self._authorizer)
            rows = db.execute("SELECT id,source,source_id,name_fa,city_fa,source_url,observed_at FROM records WHERE id IN (" +
                              ",".join("?" for _ in ids) + ")", ids).fetchall()
        return [dict(row) for row in rows]

    def entity_context(self, text: str) -> list[dict]:
        """Resolve catalog identities for schema linking; never returns prices."""
        from app.normalization import normalize_text
        latest = normalize_text(text.split("\n", 1)[0])
        text = normalize_text(text)
        if not re.search(r"هتل|اقامتگاه|مهمانپذیر|ملک|اتاق|پلن|property_id|source_id|room_id", text):
            return []
        hashes = re.findall(r"\b[a-fA-F0-9]{24}\b", text)[:12]
        source_ids = re.findall(r"(?:ملک|source_id|شناسه هتل)\s*[:=]?\s*(\d+)", text)[:12]
        room_ids = re.findall(r"(?:اتاق|room_id)\s*[:=]?\s*(\d+)", text)[:12]
        if not hashes and not source_ids and not room_ids and re.search(r"کاتالوگ|چند رکورد|تعداد رکورد|آمار", latest):
            return []
        stop = set("هتل اقامتگاه مهمانپذیر آپارتمان اقامت برای از در به و یا با یک دو سه نفر بزرگسال اتاق شب ورود خروج تاریخ قیمت هزینه تومان ریال موجودی ظرفیت نرخ پلن شرایط همان تهران مشهد شیراز اصفهان لطفا من را این آن حساب کاتالوگ منبع چند بده کن طبق جمع هر تا سفر بررسی مقایسه source_id property_id".split())
        words = list(dict.fromkeys(w for w in re.findall(r"[\u0621-\u06ff]+", text) if len(w) >= 3 and w not in stop))[:16]
        clauses, params = [], []
        if hashes:
            clauses.append("id IN (" + ",".join("?" for _ in hashes) + ")"); params.extend(hashes)
        if source_ids:
            clauses.append("source_id IN (" + ",".join("?" for _ in source_ids) + ")"); params.extend(source_ids)
        if room_ids:
            clauses.append("id IN (SELECT property_id FROM stay_rates WHERE room_id IN (" +
                           ",".join("?" for _ in room_ids) + "))"); params.extend(room_ids)
        if not clauses and words:
            clauses.append("(" + " OR ".join("name_fa LIKE ?" for _ in words) + ")")
            params.extend("%" + word + "%" for word in words)
        if not clauses:
            return []
        with sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=self.timeout_seconds) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON"); db.set_authorizer(self._authorizer)
            rows = db.execute("SELECT id,source,source_id,name_fa,city_fa FROM records WHERE entity_type='property' AND (" +
                              " OR ".join(clauses) + ") ORDER BY source,id LIMIT 12", params).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _authorizer(action, arg1, arg2, database, trigger):
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            table = (arg1 or "").lower()
            column = (arg2 or "").lower()
            # SQLite reports database=None for the empty-column READ used by
            # COUNT(*); ordinary column reads still require the main database.
            permitted_database = database == "main" or (database is None and not column)
            if permitted_database and table in TABLE_COLUMNS and (not column or column in TABLE_COLUMNS[table]):
                return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in SAFE_FUNCTIONS:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def execute(self, sql: str) -> dict:
        started = time.monotonic()
        checked = validate_sql(sql)
        connection = None
        reason = None
        instructions = 0

        def progress():
            nonlocal reason, instructions
            instructions += 1000
            if time.monotonic() - started >= self.timeout_seconds:
                reason = "query_timeout"
                return 1
            if instructions >= MAX_VM_INSTRUCTIONS:
                reason = "query_limit"
                return 1
            return 0

        try:
            remaining = self.timeout_seconds - (time.monotonic() - started)
            if remaining <= 0:
                raise SQLGuardError("query_timeout", "زمان مجاز اجرای پرس‌وجو به پایان رسید.")
            connection = sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True,
                                         timeout=min(remaining, 1))
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            disable_extensions = getattr(connection, "enable_load_extension", None)
            if disable_extensions is not None:
                disable_extensions(False)
            connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_CHARS)
            connection.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 100)
            connection.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 20)
            connection.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 64)
            # This limits SQLite source values/intermediates, including raw JSON and
            # sqlite_schema entries, separately from the strict response-byte cap.
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, max(self.max_bytes, 1_000_000))
            connection.set_authorizer(self._authorizer)
            connection.set_progress_handler(progress, 1000)
            if time.monotonic() - started >= self.timeout_seconds:
                raise SQLGuardError("query_timeout", "زمان مجاز اجرای پرس‌وجو به پایان رسید.")
            cursor = connection.execute(checked)
            columns = [col[0] for col in cursor.description or []]
            if len(json.dumps({"columns": columns, "rows": []}, ensure_ascii=False).encode("utf-8")) > self.max_bytes:
                raise SQLGuardError("result_too_large", "اندازه نتیجه از حد مجاز بیشتر است.")
            rows = []
            truncated = False
            while True:
                if time.monotonic() - started >= self.timeout_seconds:
                    raise SQLGuardError("query_timeout", "زمان مجاز اجرای پرس‌وجو به پایان رسید.")
                row = cursor.fetchone()
                if row is None:
                    break
                if len(rows) >= self.max_rows:
                    truncated = True
                    break
                if any(isinstance(cell, bytes) for cell in row):
                    raise SQLGuardError("result_type", "نمایش نتیجه دودویی در این دمو پشتیبانی نمی‌شود.")
                if any(isinstance(cell, float) and not math.isfinite(cell) for cell in row):
                    raise SQLGuardError("non_finite_result", "نتیجه عددی پرس‌وجو معتبر و متناهی نیست.")
                candidate = rows + [list(row)]
                size = len(json.dumps({"columns": columns, "rows": candidate}, ensure_ascii=False, allow_nan=False).encode("utf-8"))
                if size > self.max_bytes:
                    truncated = True
                    break
                rows = candidate
            return {"sql": checked, "columns": columns, "rows": rows,
                    "row_count": len(rows), "truncated": truncated,
                    "duration_ms": round((time.monotonic() - started) * 1000, 3)}
        except SQLGuardError:
            raise
        except sqlite3.Error as exc:
            if reason:
                message = "زمان مجاز اجرای پرس‌وجو به پایان رسید." if reason == "query_timeout" else "هزینه اجرای پرس‌وجو از حد مجاز بیشتر است."
                raise SQLGuardError(reason, message) from None
            if isinstance(exc, sqlite3.OperationalError) and connection is None:
                raise SQLGuardError("database_unavailable", "پایگاه داده فعلاً در دسترس نیست.") from None
            if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_TOOBIG:
                raise SQLGuardError("result_too_large", "اندازه نتیجه از حد مجاز بیشتر است.") from None
            if getattr(exc, "sqlite_errorcode", None) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                raise SQLGuardError("database_unavailable", "پایگاه داده فعلاً در دسترس نیست.") from None
            if getattr(exc, "sqlite_errorcode", None) in {sqlite3.SQLITE_AUTH, sqlite3.SQLITE_READONLY}:
                raise SQLGuardError("unsafe_query", "اجرای این عملیات در پایگاه داده مجاز نیست.") from None
            raise SQLGuardError("sql_execution_error", "پرس‌وجوی تولیدشده قابل اجرا نبود؛ ساختار و ستون‌ها را بررسی کنید.") from None
        finally:
            if connection is not None:
                connection.close()
