"""Execute acceptance cases without supplying their oracle to the application.

Live requests contain only a question and retrieval controls. Security and fault
checks are dependency-injected, offline executions, reported separately from
actual provider/API results. Document wording still requires semantic review.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩يك", "01234567890123456789یک")
NUMBER_WORDS = {"دویست هزار": 200000, "بیست و چهار": 24, "هجده": 18,
                "پانزده": 15, "چهارده": 14, "دوازده": 12, "ده": 10, "هفت": 7, "هفتم": 7, "شش": 6, "پنج": 5,
                "سه": 3, "دو": 2, "یک": 1, "صفر": 0}
OUTCOMES = {"answer": {"answered"}, "not_found": {"insufficient_evidence", "answered"},
            "insufficient_data": {"insufficient_evidence"}, "needs_clarification": {"clarification"},
            "out_of_scope": {"insufficient_evidence"}}
SECURITY_CODES = {"write_operation": {"unsafe_query", "write_operation"},
                  "attach_database": {"unsafe_query", "attach_database"},
                  "multiple_statements": {"multiple_statements"},
                  "disallowed_function": {"disallowed_function"}}


def normalized(value) -> str:
    return " ".join(str(value).translate(DIGITS).replace("\u200c", " ").split())


def as_number(value):
    if isinstance(value, bool) or value is None:
        return None
    text = normalized(value).replace("٬", "").replace(",", "").replace("٫", ".").replace("−", "-")
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        return None
    try:
        number = Decimal(text)
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def text_numbers(text: str) -> set[Decimal]:
    text = normalized(text).replace("٬", "").replace(",", "").replace("٫", ".").replace("−", "-")
    values = {Decimal(match) for match in re.findall(r"(?<![A-Za-z0-9_-])[+-]?\d+(?:\.\d+)?(?![A-Za-z0-9_])", text)}
    for amount, scale in re.findall(r"(?<![A-Za-z0-9_-])([+-]?\d+(?:\.\d+)?)\s*(هزار|میلیون|میلیارد)", text):
        values.add(Decimal(amount) * {"هزار": 1000, "میلیون": 1000000, "میلیارد": 1000000000}[scale])
    for phrase, number in NUMBER_WORDS.items():
        if re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text):
            values.add(Decimal(number))
    return values


def sql_rows(actual):
    return [row for result in actual.get("sql_results", []) if not result.get("evidence_warning")
            for row in result.get("rows", [])]


def row_cells(row):
    return list(row.values()) if isinstance(row, dict) else list(row)


def sql_numbers(actual):
    return {number for row in sql_rows(actual) for cell in row_cells(row)
            if (number := as_number(cell)) is not None}


def calculation_numbers(actual):
    return {number for calculation in actual.get("calculations", [])
            if (number := as_number(calculation.get("value"))) is not None}


def has_number(values, expected, tolerance="0"):
    target = Decimal(str(expected))
    return any(abs(value - target) <= Decimal(tolerance) for value in values)


def gold_pages(case):
    return {(source["document_id"], int(source["page"])) for source in case.get("expected_sources", [])}


def retrieved_pages(sources):
    output = []
    for source in sources:
        try:
            output.append((source["document_id"], int(source["page"])))
        except (KeyError, TypeError, ValueError):
            continue
    return output


def page_metrics(gold, sources, k=6):
    ranked = retrieved_pages(sources[:k])
    hits = set(ranked) & set(gold)
    first = next((index + 1 for index, value in enumerate(ranked) if value in gold), None)
    return {"gold_page_count": len(gold), "hit_page_count": len(hits),
            "recall_at_k": len(hits) / len(gold) if gold else None,
            "any_gold_hit": bool(hits), "all_gold_hit": bool(gold) and len(hits) == len(gold),
            "mrr": 1 / first if first else 0, "k": k,
            "ranked_pages": [{"document_id": doc, "page": page} for doc, page in ranked]}


def referenced_tables(actual):
    import sqlglot
    from sqlglot import exp
    tables = set()
    for result in actual.get("sql_results", []):
        if result.get("evidence_warning"):
            continue
        try:
            tree = sqlglot.parse_one(result.get("sql", ""), read="sqlite")
            tables.update(table.name.lower() for table in tree.find_all(exp.Table))
        except Exception:
            continue
    return tables


def calculation_provenance(case: dict, actual: dict) -> list[dict]:
    """Check literal operand provenance, not the semantic choice of operands.

    This rejects an invented fare even if the final total happens to match.
    SQL values, explicit user numbers/slots, prior tool results and elementary
    unit factors are allowed. A reviewer must still check group/leg selection.
    """
    allowed = sql_numbers(actual) | {Decimal(0), Decimal(1), Decimal(10), Decimal(100)}
    for text in case.get("turns", [case["question_fa"]]):
        allowed |= text_numbers(text)
    for value in actual.get("conversation_state", {}).values():
        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            allowed.add(Decimal(str(value)))
        elif isinstance(value, list):
            allowed |= {number for v in value if (number := as_number(v)) is not None}
    allowed = {abs(value) for value in allowed}
    output = []
    for calculation in actual.get("calculations", []):
        expression = str(calculation.get("expression", "")).translate(DIGITS)
        operands, unknown, valid = [], [], True
        try:
            tree = ast.parse(expression, mode="eval")
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant):
                    if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                        valid = False
                        continue
                    literal = (ast.get_source_segment(expression, node) or "").replace("_", "")
                    value = Decimal(literal)
                    operands.append(str(value))
                    if abs(value) not in allowed:
                        unknown.append(str(value))
        except (SyntaxError, ValueError, InvalidOperation):
            valid = False
        output.append({"expression": expression, "operands": operands,
                       "unsupported_literals": unknown, "passed": valid and bool(operands) and not unknown})
        result = as_number(calculation.get("value"))
        if result is not None:
            allowed.add(abs(result))
    return output


def result_ids(result: dict) -> list[str]:
    return list(dict.fromkeys(str(value) for row in result.get("rows", []) for value in row_cells(row)
                             if re.fullmatch(r"[a-f0-9]{24}", str(value))))


def has_property_record_count(actual: dict) -> bool:
    """A filtered property-record COUNT needs no one-to-one detail-table join."""
    import sqlglot
    from sqlglot import exp
    for result in actual.get("sql_results", []):
        if result.get("evidence_warning"):
            continue
        try:
            tree = sqlglot.parse_one(result.get("sql", ""), read="sqlite")
            for select in tree.find_all(exp.Select):
                if not any(table.name.lower() == "records" for table in select.find_all(exp.Table)):
                    continue
                if not any(expression.find(exp.Count) is not None for expression in select.expressions):
                    continue
                where = select.args.get("where")
                if where is None:
                    continue
                # Only guaranteed AND conjuncts qualify: an OR branch could
                # count other entity types despite mentioning the predicate.
                def conjuncts(node):
                    if isinstance(node, exp.Paren):
                        return conjuncts(node.this)
                    if isinstance(node, exp.And):
                        return conjuncts(node.left) + conjuncts(node.right)
                    return [node]
                for predicate in conjuncts(where.this):
                    if not isinstance(predicate, exp.EQ):
                        continue
                    for column, literal in ((predicate.left, predicate.right), (predicate.right, predicate.left)):
                        if (isinstance(column, exp.Column) and column.name.lower() == "entity_type" and
                            isinstance(literal, exp.Literal) and literal.is_string and literal.this == "property"):
                            return True
        except Exception:
            continue
    return False


def typed_equal(actual, expected):
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(typed_equal(left, right) for left, right in zip(actual, expected))
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(typed_equal(actual[key], value) for key, value in expected.items())
    return actual == expected


def assess_live(case: dict, actual: dict) -> dict:
    """Travel acceptance checks: evidence equivalence, never exact SQL/prose."""
    checks, pending = [], []

    def check(name, passed, **detail):
        checks.append({"criterion": name, "passed": bool(passed), **detail})

    expected, kind = case["expected_result"], case["oracle"]["kind"]
    check("route", actual.get("route") == case["expected_route"], expected=case["expected_route"], actual=actual.get("route"))
    accepted = OUTCOMES.get(case["expected_outcome"], set())
    check("outcome", actual.get("status") in accepted, expected=sorted(accepted), actual=actual.get("status"))
    steps = {(event.get("step"), event.get("status")) for event in actual.get("trace", [])}
    successful_results = [r for r in actual.get("sql_results", []) if not r.get("evidence_warning")]
    executed = {"semantic_retrieval": ("document_specialist", "completed") in steps,
                "sql_query": bool(successful_results) and ("database_specialist", "completed") in steps,
                "calculator": bool(actual.get("calculations")) and ("calculator", "completed") in steps}
    for tool in case.get("expected_tools", []):
        check("tool:" + tool, executed.get(tool, False))
    gold = gold_pages(case)
    if gold:
        citations = page_metrics(gold, actual.get("sources", []), 12)
        check("relevant_source_page", citations["any_gold_hit"], coverage=citations)
        pending.append("Verify every cited claim and cross-property pairing. Any/all gold page coverage is reported; a page hit alone is not entailment.")
    valid_source_ids = {source.get("source_id") for source in actual.get("sources", []) if source.get("source_id")}
    inline_ids = set(re.findall(r"\[(S\d+)\]", actual.get("answer", "")))
    check("inline_sources_resolve", inline_ids.issubset(valid_source_ids), actual=sorted(inline_ids))
    numbers, calc, rows = sql_numbers(actual), calculation_numbers(actual), sql_rows(actual)
    tables = referenced_tables(actual)
    for index, value in enumerate(expected.get("sql_numbers", []) + expected.get("raw_amounts_irr", [])):
        check(f"SQL_numeric_evidence:{index}", has_number(numbers, value), expected=str(value), observed=sorted(map(str, numbers)))
    for index, value in enumerate(expected.get("calculator_values", [])):
        check(f"native_calculator_value:{index}", has_number(calc, value, expected.get("tolerance", "0")),
              expected=str(value), observed=sorted(map(str, calc)))
    for index, value in enumerate(expected.get("answer_numbers", [])):
        check(f"answer_numeric_fact:{index}", has_number(text_numbers(actual.get("answer", "")), value), expected=str(value))
    answer = normalized(actual.get("answer", ""))
    for index, alternatives in enumerate(expected.get("answer_term_groups", [])):
        check(f"answer_term_group:{index}", any(normalized(term) in answer for term in alternatives), acceptable_terms=alternatives)
    required_tables = set(expected.get("required_tables", []))
    if required_tables:
        effective_tables = set(tables)
        equivalents = []
        if kind == "row_equivalence" and "properties" in required_tables and has_property_record_count(actual):
            effective_tables.add("properties")
            equivalents.append("records filtered to entity_type=property with COUNT supplies property-record source counts without the one-to-one detail join")
        check("operational_tables_queried", required_tables.issubset(effective_tables), expected=sorted(required_tables),
              actual=sorted(tables), accepted_equivalences=equivalents)
    if expected.get("required_tables_any"):
        check("operational_entity_lookup", bool(set(expected["required_tables_any"]) & tables), actual=sorted(tables))
    for name, value in expected.get("conversation_state", {}).items():
        observed = actual.get("conversation_state", {}).get(name)
        check("conversation_slot:" + name, typed_equal(observed, value), expected=value, actual=observed)
    if expected.get("no_calculation"):
        check("no_invented_calculation", not actual.get("calculations"))
    if actual.get("calculations"):
        provenance = calculation_provenance(case, actual)
        check("calculator_operand_provenance", all(row["passed"] for row in provenance), expressions=provenance)
        pending.append("Literal provenance is checked; review price basis, selected legs/room plan, occupancy and arithmetic meaning separately.")
    if kind == "row_equivalence":
        def matches(row, target):
            cells = row_cells(row)
            text = normalized(json.dumps(cells, ensure_ascii=False))
            numeric = {value for cell in cells if (value := as_number(cell)) is not None}
            return all(normalized(term) in text for term in target.get("texts", [])) and all(
                has_number(numeric, value) for value in target.get("numbers", []))
        wanted = expected["row_expectations"]
        candidates = [result.get("rows", []) for result in successful_results]
        selected_rows = max(candidates, key=lambda candidate: (
            sum(any(matches(row, target) for row in candidate) for target in wanted),
            len(candidate) == expected.get("exact_row_count", len(candidate))), default=[])
        for index, target in enumerate(wanted):
            check(f"paired_SQL_row:{index}", any(matches(row, target) for row in selected_rows), expected=target)
        if "exact_row_count" in expected:
            check("SQL_result_row_count", len(selected_rows) == expected["exact_row_count"],
                  expected=expected["exact_row_count"], actual=len(selected_rows),
                  scope="one complete resultset; independent auxiliary results are not concatenated")
    elif kind == "ranked_records":
        wanted = expected["record_ids"]
        candidates = [result_ids(result) for result in successful_results]
        observed = max(candidates, key=lambda ids: len(set(ids) & set(wanted)), default=[])
        check("ordered_record_identities", observed == wanted, expected=wanted, actual=observed)
    elif kind == "missing_entity":
        empty = not rows or (len(rows) == 1 and row_cells(rows[0]) and
                            all(value is None or as_number(value) == 0 for value in row_cells(rows[0])))
        check("executed_lookup_confirms_absence", executed["sql_query"] and empty)
    elif kind == "ambiguous_property":
        check("multiple_property_candidates", len(rows) >= expected["minimum_candidates"], actual=len(rows))
        pending.append("Confirm the assistant asks which provider/property the user means and does not silently choose a similar name.")
    elif kind in {"calendar_only", "unknown_arrival"}:
        for value in expected.get("required_row_texts", []):
            check("SQL_preserves:" + value, any(value in row_cells(row) for row in rows))
        field = expected["null_field"]
        nullable = any(field in result.get("sql", "").lower() and any(None in row_cells(row) for row in result.get("rows", []))
                       for result in successful_results)
        check("unknown_field_not_zero:" + field, nullable)
        pending.append("Verify no exact arrival, seat count, booking guarantee or current availability is inferred from NULL/calendar evidence.")
    elif kind == "incomplete_stay":
        stay = actual.get("quote_evidence", {}).get("stay", {})
        check("missing_nights_block_quote", stay.get("blocked") is True)
        for day in expected["known_dates"]:
            check("known_night:" + day, any(day in row_cells(row) for row in rows))
        pending.append("Confirm missing dates are named and neither another room/plan nor repeated known-night prices fill them.")
    elif kind == "unpriced_child":
        check("child_quote_blocked", actual.get("quote_evidence", {}).get("stay", {}).get("blocked") is True)
        pending.append("Check supported adult observations and child conditions are explained without inventing the child tariff or a full-family price.")
    elif kind == "unknown_penalty":
        pending.append("The source requires inquiry; verify the answer gives no fixed penalty from the user-provided24-hour interval.")
    if expected.get("require_compatible_stay"):
        stay = actual.get("quote_evidence", {}).get("stay", {})
        check("compatible_all_night_stay", stay.get("blocked") is False and any(group.get("complete_observed") for group in stay.get("groups", [])))
    if expected.get("require_exact_transport"):
        transport = actual.get("quote_evidence", {}).get("transport", {})
        check("exact_observed_transport", transport.get("blocked") is False and bool(transport.get("eligible_record_ids")))
    if kind in {"quote", "calendar_only", "unpriced_child", "incomplete_stay"}:
        snapshot = actual.get("snapshot", {})
        check("snapshot_not_live_booking", snapshot.get("captured_on") == "2026-10-18" and
              snapshot.get("requires_revalidation") is True and snapshot.get("live_availability_confirmed") is False)
        pending.append("Check the answer labels observed prices as stale snapshot evidence, preserves IRR/Toman and price basis, and never promises a booking.")
    if kind == "policy_fact":
        pending.append("Review semantic meaning, negation, alias identity, age/rate pairing, exceptions and unsupported opening-time claims manually.")
    return {"automatic_pass": all(row["passed"] for row in checks), "checks": checks,
            "semantic_review_required": bool(pending), "semantic_review_notes": list(dict.fromkeys(pending)),
            "scope": "observable route/tools, citations, slots, row/numeric equivalence and literal operands; not full semantic fidelity or live availability"}


def hash_database(path: Path):
    return {file.name: hashlib.sha256(file.read_bytes()).hexdigest()
            for file in (path, Path(str(path) + "-wal")) if file.exists()}


def run_security_case(case, db_path: Path):
    from app.sql import SQLGuardError, SQLService
    if not db_path.is_file():
        return {"case_id": case["id"], "state": "not_run", "reason": "database_missing"}
    started = time.perf_counter()
    before = hash_database(db_path)
    subcases = case.get("malicious_sql_subcases") or [{"sql": case["malicious_sql"],
                "expected_guard_rejection": case["expected_guard_rejection"]}]
    results = []
    for item in subcases:
        rejection, unexpected = None, False
        try:
            SQLService(db_path).execute(item["sql"])
        except SQLGuardError as error:
            rejection = {"code": error.code, "message": error.message}
        except Exception:
            unexpected = True
        accepted = SECURITY_CODES.get(item["expected_guard_rejection"], {item["expected_guard_rejection"]})
        unchanged = before == hash_database(db_path)
        passed = not unexpected and rejection is not None and rejection["code"] in accepted and unchanged
        results.append({"malicious_sql": item["sql"], "expected_guard_code": item["expected_guard_rejection"],
                        "accepted_equivalent_codes": sorted(accepted), "actual_guard_rejection": rejection,
                        "database_unchanged": unchanged, "state": "passed" if passed else "failed"})
    unchanged = before == hash_database(db_path)
    passed = bool(results) and all(row["state"] == "passed" for row in results) and unchanged
    return {"case_id": case["id"], "state": "passed" if passed else "failed",
            "execution": "actual SQLService validation boundary; no model refusal involved",
            "subcases": results, "database_unchanged": unchanged,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2)}


async def run_failure_case(case, db_path: Path):
    from langchain_core.messages import AIMessage
    from app.llm import LLMClient
    from app.sql import SQLService
    from app.workflow import WorkflowService
    marker = "fake-offline-evaluation-private-key"

    class FaultChat:
        attempts = 0

        async def ainvoke(self, *args, **kwargs):
            self.attempts += 1
            if case["injected_failure"]["component"] == "llm_provider":
                request = httpx.Request("POST", "https://offline.invalid/v1/chat/completions")
                response = httpx.Response(429, headers={"retry-after": "0"}, request=request)
                raise httpx.HTTPStatusError(marker, request=request, response=response)
            return AIMessage(content=case["injected_failure"]["raw_output"])

    class RecordingSQL:
        def __init__(self):
            self.service, self.calls = SQLService(db_path), 0

        def schema_context(self):
            return self.service.schema_context()

        def execute(self, query):
            self.calls += 1
            return self.service.execute(query)

    class UnusedRetriever:
        calls = 0

        async def search(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("Fault must stop before retrieval")

    settings = SimpleNamespace(llm_api_key=marker, llm_base_url="https://offline.invalid/v1",
        llm_model="offline-fault-fixture", llm_timeout_seconds=5, llm_max_retries=2,
        llm_min_interval_seconds=0, embedding_model="unused", embedding_batch_size=2,
        langfuse_enabled=False, as_of="2026-10-27", max_history_messages=10)
    stub = FaultChat()
    started = time.perf_counter()
    with patch("app.llm.ChatOpenAI", return_value=stub):
        client = LLMClient(settings)
    sql, retriever = RecordingSQL(), UnusedRetriever()
    try:
        with patch("app.llm.retry_delay", return_value=0):
            actual = await WorkflowService(settings, client, sql, retriever).run(case["question_fa"])
    finally:
        await client.close()
    expected_code = case["expected_result"]["error_code"]
    safe = marker not in json.dumps(actual, ensure_ascii=False)
    attempts_ok = stub.attempts == (3 if expected_code == "provider_rate_limited" else 2)
    passed = actual.get("status") == "error" and (actual.get("error") or {}).get("code") == expected_code
    passed = passed and safe and attempts_ok and sql.calls == 0 and not actual.get("calculations") and not actual.get("sql_results")
    return {"case_id": case["id"], "state": "passed" if passed else "failed",
            "execution": "actual adapter + workflow with injected model transport; no provider network",
            "attempts": stub.attempts, "bounded_attempts_passed": attempts_ok,
            "sql_execution_count": sql.calls, "secrets_exposed": not safe,
            "actual": actual, "duration_ms": round((time.perf_counter() - started) * 1000, 2)}


def flush_report(path: Path, report: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def summarize(report):
    output = {}
    for name in ("live", "offline", "retrieval"):
        section = report[name]
        counts = Counter(row["state"] for row in section["results"])
        output[name] = {"planned": section["planned"], "executed": sum(counts[s] for s in ("passed", "failed")),
                        "passed": counts["passed"], "failed": counts["failed"],
                        "not_run": section["planned"] - sum(counts[s] for s in ("passed", "failed"))}
    report["summary"] = output


async def run_live(cases, client, report, output, limit=None, retrieval_mode="hybrid"):
    for case in cases[:limit] if limit else cases:
        started, turns, history = time.perf_counter(), [], []
        # Only user turns and actual prior assistant output/state enter requests.
        # Never send expected_result, oracle, pass_criteria, gold pages or slots.
        try:
            questions = case.get("turns") or [case["question_fa"]]
            for index, question in enumerate(questions):
                payload = {"question": question, "retrieval_mode": retrieval_mode}
                if case.get("filters"):
                    payload["filters"] = case["filters"]
                if history:
                    payload["history"] = history[-10:]
                turn_started = time.perf_counter()
                response = await client.post("/chat", json=payload)
                response.raise_for_status()
                actual = response.json()
                turns.append({"turn": index + 1, "question_fa": question, "actual": actual,
                              "duration_ms": round((time.perf_counter() - turn_started) * 1000, 2)})
                assistant = str(actual.get("answer", ""))
                if actual.get("conversation_state"):
                    assistant += "\nوضعیت معتبر استخراج‌شده از همین نوبت (داده گفتگو): " + json.dumps(actual["conversation_state"], ensure_ascii=False)
                history.extend([{"role": "user", "content": question}, {"role": "assistant", "content": assistant}])
                if actual.get("status") in {"error", "rejected"} and index < len(questions)-1:
                    break
            assessment = assess_live(case, actual)
            if len(questions) > 1:
                conversation_ok = len(turns) == len(questions) and all(
                    turn["actual"].get("status") not in {"error", "rejected"} for turn in turns[:-1])
                assessment["checks"].append({"criterion": "preceding_conversation_turns_executed", "passed": conversation_ok})
                assessment["automatic_pass"] = assessment["automatic_pass"] and conversation_ok
            record = {"case_id": case["id"], "state": "passed" if assessment["automatic_pass"] else "failed",
                      "question_fa": case["question_fa"], "actual": actual, "assessment": assessment}
            if len(questions) > 1:
                record["turns"] = turns
        except httpx.HTTPStatusError as error:
            record = {"case_id": case["id"], "state": "failed", "reason": "api_http_error", "http_status": error.response.status_code, "turns": turns}
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
            record = {"case_id": case["id"], "state": "failed", "reason": "api_unavailable_or_invalid_response", "turns": turns}
        record["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
        report["live"]["results"].append(record)
        summarize(report)
        flush_report(output, report)
        print(f"live {case['id']}: {record['state']}", flush=True)


def comparison_modes(payload):
    for key in ("results", "comparisons", "modes"):
        candidate = payload.get(key)
        if isinstance(candidate, dict):
            return candidate
        if isinstance(candidate, list):
            return {item["mode"]: item for item in candidate if isinstance(item, dict) and "mode" in item}
    return {name: value for name, value in payload.items() if name in {"dense", "lexical", "hybrid", "hybrid_rerank"}}


async def run_retrieval(cases, client, report, output, limit=None, include_rerank=False):
    for case in cases[:limit] if limit else cases:
        started = time.perf_counter()
        payload = {"query": case.get("retrieval_query", case["question_fa"]), "limit": 6}
        if case.get("filters"):
            payload["filters"] = case["filters"]
        try:
            response = await client.post("/retrieval/compare", params={"include_rerank": "true"} if include_rerank else {}, json=payload)
            response.raise_for_status()
            actual = response.json()
            modes = comparison_modes(actual)
            scores = {}
            for mode, result in modes.items():
                sources = result if isinstance(result, list) else result.get("results", [])
                metrics = page_metrics(gold_pages(case), sources)
                if isinstance(result, dict):
                    metrics["diagnostics"] = result.get("diagnostics", {})
                scores[mode] = metrics
            required = {"dense", "lexical", "hybrid"} | ({"hybrid_rerank"} if include_rerank else set())
            # Retrieval effectiveness is a measurement, not an invented universal pass threshold.
            record = {"case_id": case["id"], "state": "passed" if required.issubset(scores) else "failed",
                      "query": payload["query"], "filters": payload.get("filters", {}), "mode_metrics": scores,
                      "scope": "endpoint/mode execution passed; effectiveness is reported by Recall/MRR, not this state"}
        except httpx.HTTPStatusError as error:
            record = {"case_id": case["id"], "state": "failed", "reason": "retrieval_http_error", "http_status": error.response.status_code}
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
            record = {"case_id": case["id"], "state": "failed", "reason": "retrieval_unavailable_or_invalid_response"}
        record["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
        report["retrieval"]["results"].append(record)
        summarize(report)
        flush_report(output, report)
        print(f"retrieval {case['id']}: {record['state']}", flush=True)
    aggregate = {}
    modes = {mode for row in report["retrieval"]["results"] for mode in row.get("mode_metrics", {})}
    for mode in sorted(modes):
        records = [row["mode_metrics"][mode] for row in report["retrieval"]["results"] if mode in row.get("mode_metrics", {})]
        aggregate[mode] = {"queries": len(records), "mean_recall_at_6": sum(r["recall_at_k"] or 0 for r in records) / len(records),
                           "mean_mrr": sum(r["mrr"] for r in records) / len(records),
                           "any_gold_coverage": sum(r["any_gold_hit"] for r in records) / len(records),
                           "all_gold_coverage": sum(r["all_gold_hit"] for r in records) / len(records)}
    report["retrieval"]["aggregate"] = aggregate


async def main_async(args):
    bundle = json.loads((ROOT / "evaluation/cases.json").read_text(encoding="utf-8"))
    cases = bundle["cases"]
    selected = getattr(args, "case_ids", None)
    if selected:
        unknown = set(selected) - {case["id"] for case in cases}
        if unknown:
            raise ValueError("Unknown case IDs: " + ", ".join(sorted(unknown)))
        cases = [case for case in cases if case["id"] in selected]
    live = [case for case in cases if case["category"] not in {"sql_security", "controlled_failure"}]
    offline = [case for case in cases if case["category"] in {"sql_security", "controlled_failure"}]
    retrieval = [case for case in cases if "retrieval_challenge" in case.get("tags", [])][:12]
    output = Path(args.output)
    report = {"schema_version": 1, "started_at": datetime.now(timezone.utc).isoformat(),
              "as_of": bundle["as_of"], "mode": args.mode, "base_url": args.base_url,
              "selected_case_ids": selected,
              "cases_sha256": hashlib.sha256((ROOT / "evaluation/cases.json").read_bytes()).hexdigest(),
              "notes": ["Expected answers and oracle files are never supplied to application requests.",
                        "Each selected live case is attempted once; transport/model retries inside the app remain bounded.",
                        "Automatic pass covers stated mechanical/numeric checks, not full semantic answer quality.",
                        "Retrieval uses the same question for every mode; without explicit filters, context-dependent followups may lack their preceding travel slots.",
                        "Guard/failure executions are offline injections, never presented as live model behavior."],
              "live": {"planned": len(live), "results": []},
              "offline": {"planned": len(offline), "results": []},
              "retrieval": {"planned": len(retrieval), "results": []}}
    summarize(report)
    flush_report(output, report)
    if args.mode in {"offline", "all"}:
        for case in offline[:args.max_cases] if args.max_cases else offline:
            result = run_security_case(case, Path(args.db)) if case["category"] == "sql_security" else await run_failure_case(case, Path(args.db))
            report["offline"]["results"].append(result)
            summarize(report)
            flush_report(output, report)
            print(f"offline {case['id']}: {result['state']}", flush=True)
    if args.mode in {"live", "retrieval", "all"}:
        async with httpx.AsyncClient(base_url=args.base_url.rstrip("/"), timeout=httpx.Timeout(args.timeout, connect=5)) as client:
            if args.mode in {"live", "all"}:
                await run_live(live, client, report, output, args.max_cases, args.retrieval_mode)
            if args.mode in {"retrieval", "all"}:
                await run_retrieval(retrieval, client, report, output, args.max_cases, args.include_rerank)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    summarize(report)
    flush_report(output, report)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 1 if any(section["failed"] for section in report["summary"].values()) else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--mode", choices=("live", "offline", "retrieval", "all"), default="offline")
    parser.add_argument("--output", default="reports/evaluation.json")
    parser.add_argument("--db", default="var/travel.sqlite")
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--case-ids", nargs="+", help="Run only these acceptance case IDs, retaining their original questions and oracles")
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--retrieval-mode", choices=("dense", "lexical", "hybrid", "hybrid_rerank"), default="hybrid")
    parser.add_argument("--include-rerank", action="store_true", help="Extra live model cost; disabled by default")
    args = parser.parse_args()
    if args.max_cases is not None and args.max_cases < 1:
        parser.error("--max-cases must be positive")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
