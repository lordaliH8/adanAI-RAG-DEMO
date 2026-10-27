"""Travel scoring/execution regressions; no provider network or environment reads."""
import copy
import json
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import httpx

from app.schema import SCHEMA_SQL
from evaluation.run import (ROOT, as_number, assess_live, calculation_provenance,
    page_metrics, run_failure_case, run_live, run_retrieval, run_security_case,
    text_numbers)


def sample_case(kind="numeric_evidence", result=None, **changes):
    case = {"id": "TEST-01", "category": "sql", "question_fa": "تعداد گزینه‌های سفر را گزارش کن.",
            "expected_route": "database", "expected_outcome": "answer", "expected_sources": [],
            "expected_tools": ["sql_query"], "expected_result": result or {"sql_numbers": [3, 100]},
            "oracle": {"kind": kind}, "tags": [], "pass_criteria": ["Never send this oracle"]}
    case.update(changes)
    return case


def actual_sql(rows=None, columns=None, sql=None):
    return {"route": "database", "status": "answered", "answer": "نتیجه از snapshot استخراج شد.",
            "sources": [], "calculations": [], "conversation_state": {}, "quote_evidence": {},
            "snapshot": {"captured_on": "2026-10-18", "requires_revalidation": True, "live_availability_confirmed": False},
            "sql_results": [{"sql": sql or "SELECT COUNT(*),MIN(amount_irr) FROM transport_offers",
                             "columns": columns or ["some_price", "some_count"],
                             "rows": rows if rows is not None else [["100.00", 3.0]],
                             "row_count": len(rows) if rows is not None else 1}],
            "trace": [{"step": "database_specialist", "status": "completed"}]}


def report_template(planned=1):
    return {"live": {"planned": planned, "results": []}, "offline": {"planned": 0, "results": []},
            "retrieval": {"planned": planned, "results": []}}


def add_calculation(actual, expression, value):
    actual["calculations"].append({"tool": "calculate", "tool_call_id": "actual-tool-1", "expression": expression,
                                   "value": value, "unit": "تومان", "precision": 1})
    actual["trace"].append({"step": "calculator", "status": "completed"})


class EvaluationTests(unittest.IsolatedAsyncioTestCase):
    def test_decimal_parsing_preserves_fractional_toman_sign_and_excludes_ids(self):
        self.assertEqual(as_number("−۱۲٬۴۹۹٬۹۹۹٫۹"), Decimal("-12499999.9"))
        self.assertEqual(as_number("۱۲۴۹۹۹۹۹۹"), Decimal(124999999))
        self.assertIsNone(as_number("58c461e96d91a4ad873bd44d"))
        self.assertIsNone(as_number(None))
        self.assertIsNone(as_number(True))
        self.assertTrue({Decimal(14), Decimal(15), Decimal(25000000)}.issubset(
            text_numbers("چهارده، پانزده و ۲۵ میلیون تومان")))

    def test_page_recall_deduplicates_gold_pages_and_preserves_rank(self):
        sources = [{"document_id": "noise", "page": 1}, {"document_id": "STAY", "page": 1},
                   {"document_id": "STAY", "page": 1}, {"document_id": "STAY", "page": 3}]
        metrics = page_metrics({("STAY", 1), ("STAY", 3)}, sources)
        self.assertEqual(metrics["recall_at_k"], 1)
        self.assertEqual(metrics["mrr"], 0.5)
        self.assertEqual(metrics["hit_page_count"], 2)
        limited = page_metrics({("STAY", 1), ("STAY", 3)}, sources, k=2)
        self.assertEqual(limited["recall_at_k"], 0.5)
        self.assertTrue(limited["any_gold_hit"])
        self.assertFalse(limited["all_gold_hit"])

    def test_equivalent_sql_aliases_and_numeric_types_pass_but_wrong_values_fail(self):
        case = sample_case(result={"sql_numbers": [3, 100], "required_tables": ["transport_offers"]})
        self.assertTrue(assess_live(case, actual_sql())["automatic_pass"])
        actual = actual_sql(rows=[[101, 3]])
        assessment = assess_live(case, actual)
        self.assertFalse(assessment["automatic_pass"])
        self.assertIn("SQL_numeric_evidence:1", [c["criterion"] for c in assessment["checks"] if not c["passed"]])

    def test_source_count_rows_require_source_number_pairing(self):
        case = sample_case("row_equivalence", {"row_expectations": [
            {"texts": ["source-A"], "numbers": [8]}, {"texts": ["source-B"], "numbers": [255]}], "exact_row_count": 2})
        actual = actual_sql(rows=[["source-A", 8], ["source-B", 255]],
                            sql="SELECT source,COUNT(*) FROM records GROUP BY source")
        self.assertTrue(assess_live(case, actual)["automatic_pass"])
        actual["sql_results"][0]["rows"] = [["source-A", 255], ["source-B", 8]]
        self.assertFalse(assess_live(case, actual)["automatic_pass"])

    def test_property_source_count_accepts_filtered_records_and_auxiliary_resultsets(self):
        case = sample_case("row_equivalence", {"row_expectations": [
            {"texts": ["source-A"], "numbers": [8]}, {"texts": ["source-B"], "numbers": [255]}],
            "exact_row_count": 2, "required_tables": ["records", "properties"]})
        actual = actual_sql(rows=[["source-A", 8], ["source-B", 255]],
            sql="SELECT source,COUNT(*) FROM records WHERE entity_type='property' AND city_fa='مشهد' GROUP BY source")
        actual["sql_results"].append(copy.deepcopy(actual["sql_results"][0]))
        assessment = assess_live(case, actual)
        self.assertTrue(assessment["automatic_pass"], assessment)
        count_check = next(item for item in assessment["checks"] if item["criterion"] == "SQL_result_row_count")
        self.assertEqual(count_check["actual"], 2)
        # A second result does not rescue the wrong count if neither has the correct complete rows.
        for result in actual["sql_results"]:
            result["rows"][1][1] = 254
        self.assertFalse(assess_live(case, actual)["automatic_pass"])

    def test_source_pairs_must_coexist_in_one_complete_resultset(self):
        case = sample_case("row_equivalence", {"row_expectations": [
            {"texts": ["source-A"], "numbers": [8]}, {"texts": ["source-B"], "numbers": [255]}], "exact_row_count": 2})
        actual = actual_sql(rows=[["source-A", 8]])
        actual["sql_results"].append({"sql": "SELECT 255 FROM records", "rows": [["source-B", 255]], "columns": ["source", "n"]})
        self.assertFalse(assess_live(case, actual)["automatic_pass"])

    def test_property_count_equivalence_rejects_or_bypass(self):
        case = sample_case("row_equivalence", {"row_expectations": [{"texts": ["source-A"], "numbers": [8]}],
            "exact_row_count": 1, "required_tables": ["records", "properties"]})
        actual = actual_sql(rows=[["source-A", 8]],
            sql="SELECT source,COUNT(*) FROM records WHERE entity_type='property' OR city_fa='مشهد' GROUP BY source")
        self.assertFalse(assess_live(case, actual)["automatic_pass"])

    def test_ranked_identity_order_cannot_be_replaced_with_same_numbers(self):
        identities = ["a"*24, "b"*24]
        case = sample_case("ranked_records", {"record_ids": identities})
        actual = actual_sql(rows=[[identities[0], 100], [identities[1], 200]])
        self.assertTrue(assess_live(case, actual)["automatic_pass"])
        actual["sql_results"][0]["rows"].reverse()
        self.assertFalse(assess_live(case, actual)["automatic_pass"])

    def test_native_calculator_result_required_and_operand_provenance_checked(self):
        case = sample_case("quote", {"raw_amounts_irr": [1234], "calculator_values": ["123.4"]},
                           expected_route="combined", expected_tools=["sql_query", "calculator"])
        actual = actual_sql(rows=[[1234]], columns=["amount_irr"])
        actual.update(route="combined", answer="۱۲۳٫۴ تومان.")
        self.assertFalse(assess_live(case, actual)["automatic_pass"])
        add_calculation(actual, "1234/10", "123.4")
        self.assertTrue(assess_live(case, actual)["automatic_pass"])
        actual["calculations"][0]["expression"] = "999+0"
        # Correct-looking output is insufficient when the price input was invented.
        self.assertFalse(assess_live(case, actual)["automatic_pass"])

    def test_fractional_toman_is_not_rounded_to_integer_by_evaluator(self):
        case = sample_case("quote", {"calculator_values": ["12499999.9"]}, expected_route="combined")
        actual = actual_sql(rows=[[124999999]])
        actual["route"] = "combined"
        add_calculation(actual, "124999999/10", "12499999.9")
        self.assertTrue(assess_live(case, actual)["automatic_pass"])
        actual["calculations"][0]["value"] = "12500000"
        self.assertFalse(assess_live(case, actual)["automatic_pass"])

    def test_valid_user_budget_and_prior_native_result_are_allowed_operands(self):
        case = sample_case(question_fa="از بودجه ۲۵ میلیون تومان کم کن.")
        actual = actual_sql(rows=[[104500000]])
        add_calculation(actual, "(104500000+104500000)/10", "20900000")
        add_calculation(actual, "25000000-20900000", "4100000")
        self.assertTrue(all(row["passed"] for row in calculation_provenance(case, actual)))

    def test_unknown_arrival_cannot_be_replaced_by_zero(self):
        case = sample_case("unknown_arrival", {"null_field": "arrival_at", "no_calculation": True},
                           expected_outcome="insufficient_data")
        actual = actual_sql(rows=[["2026-11-17T06:00:00+03:30", None]],
                            columns=["departure_at", "unknown_arrival"],
                            sql="SELECT departure_at,arrival_at AS unknown_arrival FROM transport_offers")
        actual["status"] = "insufficient_evidence"
        self.assertTrue(assess_live(case, actual)["automatic_pass"])
        actual["sql_results"][0]["rows"][0][1] = 0
        self.assertFalse(assess_live(case, actual)["automatic_pass"])

    def test_document_source_and_numeric_presence_remain_semantic_review_pending(self):
        case = sample_case("policy_fact", {"answer_numbers": [14, 15]}, category="document", expected_route="documents",
                           expected_tools=["semantic_retrieval"], expected_sources=[{"document_id": "STAY", "page": 1}])
        actual = {"route": "documents", "status": "answered", "answer": "چهارده و پانزده. [S1]",
                  "sources": [{"source_id": "S1", "document_id": "STAY", "page": 1}], "sql_results": [],
                  "calculations": [], "trace": [{"step": "document_specialist", "status": "completed"}]}
        assessment = assess_live(case, actual)
        self.assertTrue(assessment["automatic_pass"])
        self.assertTrue(assessment["semantic_review_required"])
        actual["answer"] += " [S99]"
        self.assertFalse(assess_live(case, actual)["automatic_pass"])
        actual["answer"] = "چهارده و پانزده. [S1]"
        actual["sources"][0]["page"] = 99
        self.assertFalse(assess_live(case, actual)["automatic_pass"])

    def test_required_conversation_slots_are_checked_not_inferred_from_prose(self):
        case = sample_case(result={"conversation_state": {"adults": 2, "rooms": 1, "checkin": "2026-11-17"}})
        actual = actual_sql()
        actual["answer"] = "دو بزرگسال و یک اتاق در تاریخ درست"
        self.assertFalse(assess_live(case, actual)["automatic_pass"])
        actual["conversation_state"] = {"adults": 2, "rooms": 1, "checkin": "2026-11-17"}
        self.assertTrue(assess_live(case, actual)["automatic_pass"])
        for wrong_type in ("2", 2.0, True):
            actual["conversation_state"]["adults"] = wrong_type
            self.assertFalse(assess_live(case, actual)["automatic_pass"])

    def test_incomplete_stay_requires_structural_block_and_no_calculation(self):
        case = sample_case("incomplete_stay", {"known_dates": ["2026-12-20", "2026-12-21"], "no_calculation": True},
                           expected_route="combined", expected_outcome="insufficient_data")
        actual = actual_sql(rows=[["2026-12-20", 100], ["2026-12-21", 100]])
        actual.update(route="combined", status="insufficient_evidence", quote_evidence={"stay": {"blocked": True}})
        self.assertTrue(assess_live(case, actual)["automatic_pass"])
        add_calculation(actual, "100*4/10", "40")
        self.assertFalse(assess_live(case, actual)["automatic_pass"])

    def test_actual_sql_guards_execute_four_injections_in_two_groups(self):
        cases = json.loads((ROOT / "evaluation/cases.json").read_text())["cases"]
        groups = [case for case in cases if case["category"] == "sql_security"]
        self.assertEqual(sum(len(case["malicious_sql_subcases"]) for case in groups), 4)
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "test.sqlite"
            with sqlite3.connect(database) as connection:
                connection.executescript(SCHEMA_SQL)
            for case in groups:
                result = run_security_case(case, database)
                self.assertEqual(result["state"], "passed", result)
                self.assertTrue(result["database_unchanged"])
                self.assertEqual(len(result["subcases"]), 2)
                self.assertTrue(all(row["actual_guard_rejection"] for row in result["subcases"]))
            self.assertEqual(run_security_case(groups[0], Path(folder) / "missing.sqlite")["state"], "not_run")

    async def test_two_fault_cases_execute_actual_adapter_and_workflow_without_network(self):
        cases = json.loads((ROOT / "evaluation/cases.json").read_text())["cases"]
        with tempfile.TemporaryDirectory() as folder:
            for case in [case for case in cases if case["category"] == "controlled_failure"]:
                result = await run_failure_case(case, Path(folder) / "unused.sqlite")
                self.assertEqual(result["state"], "passed", result)
                self.assertEqual(result["sql_execution_count"], 0)
                self.assertFalse(result["secrets_exposed"])
                self.assertTrue(result["bounded_attempts_passed"])

    async def test_multiturn_payload_uses_actual_history_and_never_expected_slots_or_oracle(self):
        captured = []
        case = sample_case(result={"sql_numbers": [3, 100], "conversation_state": {"adults": 2}},
                           turns=["دو بزرگسال برای سفر در نظر دارم.", "همان سفر را بررسی کن."])

        def transport(request):
            captured.append(json.loads(request.content))
            response = actual_sql()
            response["conversation_state"] = {"adults": 3 if len(captured) == 1 else 2, "destination": "شیراز"}
            response["answer"] = "پاسخ واقعی سرویس " + str(len(captured))
            return httpx.Response(200, json=response)

        report = report_template()
        with tempfile.TemporaryDirectory() as folder:
            async with httpx.AsyncClient(base_url="http://offline.invalid", transport=httpx.MockTransport(transport)) as client:
                await run_live([case], client, report, Path(folder) / "report.json")
        self.assertEqual(set(captured[0]), {"question", "retrieval_mode"})
        self.assertEqual(set(captured[1]), {"question", "retrieval_mode", "history"})
        self.assertEqual(captured[1]["history"][0], {"role": "user", "content": case["turns"][0]})
        self.assertIn('"adults": 3', captured[1]["history"][1]["content"])
        self.assertIn("پاسخ واقعی سرویس 1", captured[1]["history"][1]["content"])
        self.assertNotIn('"adults": 2', captured[1]["history"][1]["content"])
        self.assertNotIn("oracle", str(captured))
        self.assertNotIn("expected_result", str(captured))
        self.assertEqual(len(report["live"]["results"][0]["turns"]), 2)
        self.assertEqual(report["summary"]["live"]["passed"], 1)

    async def test_failed_preceding_turn_stops_conversation_without_fabricating_history(self):
        captured = []
        case = sample_case(turns=["شروع سفر", "ادامه"])
        def transport(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"route": "clarification", "status": "error", "answer": "خطا"})
        report = report_template()
        with tempfile.TemporaryDirectory() as folder:
            async with httpx.AsyncClient(base_url="http://offline.invalid", transport=httpx.MockTransport(transport)) as client:
                await run_live([case], client, report, Path(folder) / "report.json")
        self.assertEqual(len(captured), 1)
        self.assertEqual(report["summary"]["live"]["failed"], 1)

    async def test_case_limit_preserves_honest_not_run_counts(self):
        case = sample_case()
        report = report_template(planned=2)
        with tempfile.TemporaryDirectory() as folder:
            async with httpx.AsyncClient(base_url="http://offline.invalid", transport=httpx.MockTransport(lambda r:httpx.Response(200,json=actual_sql()))) as client:
                await run_live([case, {**case, "id": "TEST-02"}], client, report, Path(folder) / "report.json", limit=1)
        self.assertEqual(report["summary"]["live"], {"planned": 2, "executed": 1, "passed": 1, "failed": 0, "not_run": 1})

    async def test_retrieval_modes_share_query_and_rerank_is_explicit_query_parameter(self):
        captured = []
        case = sample_case(expected_sources=[{"document_id": "STAY", "page": 1}])
        def transport(request):
            captured.append((dict(request.url.params), json.loads(request.content)))
            entry = {"results": [{"document_id": "STAY", "page": 1}], "diagnostics": {"duration_ms": 1}}
            return httpx.Response(200, json={"query": case["question_fa"], "modes": {name:entry for name in
                ("dense", "lexical", "hybrid", "hybrid_rerank")}})
        report = report_template()
        with tempfile.TemporaryDirectory() as folder:
            async with httpx.AsyncClient(base_url="http://offline.invalid", transport=httpx.MockTransport(transport)) as client:
                await run_retrieval([case], client, report, Path(folder) / "report.json", include_rerank=True)
        self.assertEqual(captured[0][0], {"include_rerank": "true"})
        self.assertEqual(captured[0][1], {"query": case["question_fa"], "limit": 6})
        self.assertEqual(report["retrieval"]["aggregate"]["hybrid"]["mean_recall_at_6"], 1)

    def test_bundle_has_thirty_real_snapshot_cases_and_twelve_retrieval_challenges(self):
        bundle = json.loads((ROOT / "evaluation/cases.json").read_text())
        cases = bundle["cases"]
        self.assertEqual(len(cases), 30)
        self.assertEqual(len({case["id"] for case in cases}), 30)
        self.assertFalse(bundle["synthetic"])
        self.assertEqual(bundle["as_of"], "2026-10-27")
        self.assertEqual(sum("retrieval_challenge" in case["tags"] for case in cases), 12)
        self.assertEqual(sum("turns" in case for case in cases), 4)
        self.assertEqual(sum(case["category"] == "controlled_failure" for case in cases), 2)
        for case in cases:
            if case.get("turns"):
                self.assertEqual(case["turns"][-1], case["question_fa"])


if __name__ == "__main__":
    unittest.main()
