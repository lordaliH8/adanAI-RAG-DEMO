"""Travel graph tests with isolated fake services; no source inventory fabrication."""
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.llm import ProviderError
from app.workflow import TripContext, WorkflowService, history_messages, stay_quote_evidence, transport_quote_evidence, append_quote_context, format_chosen_citations, Answer, factual_text, calculator_tool_for_question


def completion(content=None, *, tools=None):
    message = {"role": "assistant", "content": json.dumps(content, ensure_ascii=False) if isinstance(content, dict) else content}
    if tools:
        message["tool_calls"] = tools
    return {"choices": [{"message": message}], "usage": {"prompt_tokens": 3, "completion_tokens": 5}}


def trip(**changes):
    fields = {name: [] if name in {"selected_transport_ids", "selected_stay_ids"} else None for name in TripContext.model_fields}
    fields.update(changes)
    return fields


def plan(route="documents", *, sql=None, docs=True, calc=False, clarification=None,
         context=None, exact=False, stay=False, reject=None, query=None):
    return completion({"route": route, "needs_documents": docs, "document_query": query or ("قاعده اقامت" if docs else None),
        "sql": sql, "requires_calculator": calc, "clarification": clarification, "reject_reason": reject,
        "trip_context": context or trip(), "requires_exact_transport": exact, "requires_complete_stay": stay})


def final(text="پاسخ مستند [S1]", status="answered", sources=None):
    return completion({"status": status, "answer": text, "source_ids": ["S1"] if sources is None else sources})


def tool(expression="501/10", identity="native-call-1", precision=1):
    return completion(tools=[{"id": identity, "type": "function", "function": {"name": "calculate",
        "arguments": json.dumps({"expression": expression, "precision": precision, "unit": "تومان"})}}])


class FakeLLM:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    async def complete(self, messages, **kwargs):
        self.calls.append((copy.deepcopy(messages), kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class SafeSQLError(Exception):
    def __init__(self, code):
        self.code, self.message = code, "پرس‌وجو با قاعده مجاز سازگار نیست."


class FakeSQL:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes if outcomes is not None else [{"columns": ["amount_irr"], "rows": [[501]], "row_count": 1}])
        self.calls = []

    def schema_context(self):
        return "schema آزمایشی سفر؛ بدون جواب مرجع"

    def execute(self, query):
        self.calls.append(query)
        result = self.outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return {"sql": query, "truncated": False, **result}


class FakeRetriever:
    def __init__(self, results=None):
        self.results = results if results is not None else [{"chunk_id": "travel:1:a", "document_id": "STAY-2026-V1",
            "title": "راهنمای اقامت", "page": 1, "section": "ورود", "text": "ساعت ورود ساعت چهارده است؛ نرخ مشاهده تاریخی است.",
            "score": 0.8, "filename": "stay.pdf", "city": "مشهد", "source": "eghamat24"}]
        self.calls = []

    async def search(self, query, **kwargs):
        self.calls.append((query, kwargs))
        return {"results": self.results, "diagnostics": {"mode": kwargs["mode"]}}


def night_rows(second_room=False, occupancy=2):
    return [{"record_id": f"night-{index}", "property_id": "hotel-A", "room_id": "room-B" if second_room and index else "room-A",
             "rate_plan_id": "rate-A", "service_date": day, "amount_irr": amount,
             "price_basis": "per_room_per_night", "availability": "available", "occupancy": occupancy,
             "inventory": 4, "min_stay": None, "max_stay": None, "closed": 0, "close_to_arrival": 0, "close_to_departure": 0}
            for index, (day, amount) in enumerate([("2026-11-17", 100), ("2026-11-18", 150)])]


def row_result(rows):
    return {"columns": list(rows[0]), "rows": [list(row.values()) for row in rows], "row_count": len(rows)}


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    def build(self, responses, sql=None, retriever=None):
        config = SimpleNamespace(as_of="2026-10-27", max_history_messages=10,
            langfuse_enabled=False, llm_api_key="test-secret-value")
        llm, sql, retriever = FakeLLM(responses), sql or FakeSQL(), retriever or FakeRetriever()
        return WorkflowService(config, llm, sql, retriever), llm, sql, retriever

    async def test_document_branch_returns_real_citations_without_sql(self):
        service, _, sql, retrieval = self.build([plan(), final()])
        result = await service.run("قانون ورود اقامتگاه چیست؟")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["sources"][0]["chunk_id"], "travel:1:a")
        self.assertFalse(sql.calls)
        self.assertTrue(retrieval.calls[0][1]["expand_context"])
        self.assertFalse(result["snapshot"]["live_availability_confirmed"])

    async def test_database_branch_executes_generated_sql(self):
        query = "SELECT COUNT(*) FROM properties"
        service, _, sql, retrieval = self.build([plan("database", sql=query, docs=False), final("سه رکورد.", sources=[])])
        result = await service.run("چند رکورد اقامتگاه وجود دارد؟")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(sql.calls, [query])
        self.assertFalse(retrieval.calls)

    async def test_native_calculator_round_trip_preserves_fractional_toman(self):
        service, llm, sql, _ = self.build([plan("combined", sql="SELECT amount_irr FROM transport_offers", docs=False, calc=True),
            tool(), final("قیمت مشاهده‌شده ۵۰٫۱ تومان است.", sources=[])])
        result = await service.run("قیمت را از ریال به تومان تبدیل کن.")
        self.assertEqual(result["calculations"][0]["value"], "50.1")
        self.assertEqual(len(sql.calls), 1)
        messages = llm.calls[-1][0]
        self.assertEqual(messages[-1]["tool_call_id"], messages[-2]["tool_calls"][0]["id"])

    async def test_native_currency_error_then_corrected_call_returns_actual_toman_total(self):
        service, llm, sql, _ = self.build([
            plan("combined", sql="SELECT amount_irr FROM stay_rates", docs=False, calc=True),
            tool("10800000+10800000", identity="wrong-currency", precision=0),
            tool("(10800000+10800000)/10", identity="correct-currency", precision=0),
            final("جمع مشاهده‌شده ۲٬۱۶۰٬۰۰۰ تومان است.", sources=[])],
            sql=FakeSQL([{"columns": ["amount_irr"], "rows": [[10800000], [10800000]]}]))
        result = await service.run("جمع دو شب را به تومان بده.")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(len(sql.calls), 1)
        self.assertEqual(len(result["calculations"]), 1)
        self.assertEqual(result["calculations"][0]["value"], "2160000")
        self.assertEqual(result["calculations"][0]["expression"], "(10800000+10800000)/10")
        failed_reply = llm.calls[2][0][-1]
        self.assertEqual(failed_reply["role"], "tool")
        self.assertEqual(failed_reply["tool_call_id"], "wrong-currency")
        self.assertEqual(json.loads(failed_reply["content"])["error"]["code"], "currency_unit_mismatch")
        self.assertEqual(len(llm.calls), 4)
        units = llm.calls[1][1]["tools"][0]["function"]["parameters"]["properties"]["unit"]["enum"]
        self.assertEqual(units, ["تومان", "IRT", "درصد", "%", ""])

    def test_requested_toman_tool_schema_is_a_copy_and_both_currencies_remain_flexible(self):
        from app.calculator import CALCULATOR_TOOL
        expected = ["تومان", "IRT", "درصد", "%", ""]
        tool_definition = calculator_tool_for_question("دو شب را به تومان حساب کن.")
        self.assertEqual(tool_definition["function"]["parameters"]["properties"]["unit"]["enum"], expected)
        self.assertNotIn("enum", CALCULATOR_TOOL["function"]["parameters"]["properties"]["unit"])
        for question in ("قیمت را به ریال و تومان بده.", "قیمت به IRR و IRT", "نسبت را محاسبه کن"):
            self.assertNotIn("enum", calculator_tool_for_question(question)["function"]["parameters"]["properties"]["unit"])

    async def test_compatible_nights_use_same_room_and_keep_unknown_restrictions(self):
        context = trip(destination="مشهد", checkin="2026-11-17", checkout="2026-11-19", adults=2, rooms=1)
        service, _, _, _ = self.build([plan("combined", sql="SELECT nightly_evidence FROM stay_rates", calc=True, stay=True, context=context),
            tool("(100+150)/10", precision=0), final("برآورد مشاهده‌شده ۲۵ تومان است. [S1]")], sql=FakeSQL([row_result(night_rows())]))
        result = await service.run("برای دو شب و دو بزرگسال طبق راهنمای اقامت قیمت بده.")
        self.assertEqual(result["status"], "answered")
        group = result["quote_evidence"]["stay"]["groups"][0]
        self.assertTrue(group["complete_observed"])
        self.assertIn("min_stay", group["unknown_restrictions"])
        self.assertEqual(group["nightly_amounts_irr"], [100, 150])

    async def test_bundle_executes_independent_generated_queries_and_partitions_evidence(self):
        context = trip(origin="تهران", destination="مشهد", departure_date="2026-11-17",
                       checkin="2026-11-17", checkout="2026-11-19", adults=2, rooms=1)
        fare = {"record_id": "fare-A", "offer_kind": "departure", "availability": "available",
                "departure_at": "2026-11-17T10:00:00+03:30", "amount_irr": 300,
                "price_basis": "per_adult_one_way", "available_seats": 5, "mode": "flight",
                "origin_fa": "تهران", "destination_fa": "مشهد", "service_date": "2026-11-17", "arrival_at": None, "search_completed": 1}
        queries = ["SELECT fare_evidence FROM transport_offers", "SELECT nightly_evidence FROM stay_rates"]
        service, llm, sql, _ = self.build([plan("combined", sql=queries, docs=False, calc=True,
            exact=True, stay=True, context=context), tool("(300*2+100+150)/10", precision=0),
            final("برآورد مشاهده‌شده ۸۵ تومان است.", sources=[])],
            sql=FakeSQL([row_result([fare]), row_result(night_rows())]))
        result = await service.run("برآورد پرواز و دو شب هتل برای دو نفر را بده.")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(sql.calls, queries)
        self.assertEqual(len(result["sql_results"]), 2)
        self.assertEqual(result["quote_evidence"]["transport"]["eligible_record_ids"], ["fare-A"])
        self.assertEqual(result["quote_evidence"]["stay"]["groups"][0]["nightly_amounts_irr"], [100, 150])
        self.assertEqual(result["calculations"][0]["value"], "85")
        self.assertEqual(len(llm.calls), 3)

    async def test_bundle_missing_one_component_cannot_quote_other_as_complete(self):
        context = trip(checkin="2026-11-17", checkout="2026-11-19", adults=2, rooms=1)
        service, _, sql, _ = self.build([plan("combined", sql=["SELECT nightly_evidence FROM stay_rates",
            "SELECT fare_evidence FROM transport_offers"], docs=False, calc=True, exact=True, stay=True, context=context),
            final("قیمت کامل ۲۵ تومان است.", sources=[])],
            sql=FakeSQL([row_result(night_rows()), {"columns": ["record_id"], "rows": []}]))
        result = await service.run("هزینه کامل پرواز و اقامت را بده.")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertFalse(result["calculations"])
        self.assertFalse(result["quote_evidence"]["stay"]["blocked"])
        self.assertTrue(result["quote_evidence"]["transport"]["blocked"])
        self.assertEqual(len(sql.calls), 2)

    async def test_more_than_three_component_queries_never_reach_database(self):
        batch = ["SELECT 1", "SELECT 2", "SELECT 3", "SELECT 4"]
        service, _, sql, _ = self.build([plan("database", sql=batch, docs=False), plan("database", sql=batch, docs=False)])
        result = await service.run("پرسش چندبخشی")
        self.assertEqual(result["error"]["code"], "invalid_model_output")
        self.assertFalse(sql.calls)

    async def test_different_rooms_across_nights_cannot_form_a_quote(self):
        context = trip(checkin="2026-11-17", checkout="2026-11-19", adults=2, rooms=1)
        service, llm, _, _ = self.build([plan("combined", sql="SELECT nightly_evidence FROM stay_rates", docs=False, calc=True, stay=True, context=context),
            final("قیمت ساختگی ۲۵ تومان است.", sources=[])], sql=FakeSQL([row_result(night_rows(second_room=True))]))
        result = await service.run("قیمت کل اقامت دوشبه؟")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertNotIn("۲۵", result["answer"])
        self.assertFalse(result["calculations"])
        self.assertNotIn("tools", llm.calls[-1][1])

    def test_occupancy_and_children_do_not_become_free_capacity(self):
        context = TripContext(**trip(checkin="2026-11-17", checkout="2026-11-19", adults=3, rooms=1))
        self.assertTrue(stay_quote_evidence(night_rows(), context)["blocked"])
        context.adults, context.children_ages = 2, [5]
        self.assertTrue(stay_quote_evidence(night_rows(), context)["blocked"])

    def test_transport_dates_directions_modes_and_selected_ids_are_enforced(self):
        context = TripContext(**trip(origin="تهران", destination="مشهد", departure_date="2026-11-17", adults=2,
                                     mode="flight", selected_transport_ids=["fare-A"]))
        fare = {"record_id": "fare-A", "offer_kind": "departure", "availability": "available", "mode": "flight",
                "origin_fa": "تهران", "destination_fa": "مشهد", "service_date": "2026-11-17",
                "departure_at": "2026-11-17T07:30:00+03:30", "available_seats": 9,
                "price_basis": "per_adult_one_way", "search_completed": 1}
        self.assertFalse(transport_quote_evidence([fare], context)["blocked"])
        for change in [{"service_date": "2026-11-01"}, {"destination_fa": "کیش"}, {"mode": "bus"},
                       {"record_id": "different-fare"}, {"search_completed": None}]:
            with self.subTest(change=change):
                self.assertTrue(transport_quote_evidence([{**fare, **change}], context)["blocked"])
        context.return_date = "2026-11-19"
        self.assertTrue(transport_quote_evidence([fare], context)["blocked"])
        back = {**fare, "record_id": "return-A", "origin_fa": "مشهد", "destination_fa": "تهران", "service_date": "2026-11-19"}
        self.assertFalse(transport_quote_evidence([fare, back], context)["blocked"])

    async def test_dropped_known_stay_restriction_requires_repair_then_blocks_quote(self):
        incomplete = night_rows()
        for row in incomplete:
            row.pop("max_stay")
        restricted = [{**row, "max_stay": 1} for row in night_rows()]
        sql = FakeSQL([row_result(incomplete), row_result(restricted)])
        context = trip(checkin="2026-11-17", checkout="2026-11-19", adults=2, rooms=1)
        service, _, _, _ = self.build([
            plan("combined", sql="SELECT partial FROM stay_rates", docs=False, calc=True, stay=True, context=context),
            completion({"sql": "SELECT complete FROM stay_rates"}), final("قیمت قطعی ۲۵ تومان است.", sources=[])], sql=sql)
        result = await service.run("قیمت دو شب همان اقامتگاه را بده.")
        self.assertEqual(len(sql.calls), 2)
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertFalse(result["calculations"])
        self.assertTrue(result["quote_evidence"]["stay"]["blocked"])

    async def test_calendar_fares_cannot_be_quoted_as_exact_departures(self):
        row = {"offer_kind": "daily_fare_aggregate", "availability": "unknown", "departure_at": None,
               "amount_irr": 100, "price_basis": "per_adult_one_way_from", "available_seats": None,
               "record_id": "calendar", "mode": "flight", "origin_fa": "تهران", "destination_fa": "مشهد",
               "service_date": "2026-11-17", "arrival_at": None, "search_completed": None}
        service, _, _, _ = self.build([plan("combined", sql="SELECT offer_evidence FROM transport_offers", docs=False, calc=True, exact=True),
            final("پرواز قطعی است.", sources=[])], sql=FakeSQL([row_result([row])]))
        result = await service.run("این نرخ تقویمی را به عنوان پرواز دقیق قیمت بده.")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertFalse(result["calculations"])

    async def test_four_native_calculations_can_share_one_assistant_message(self):
        calls = [tool(str(index+1), identity=f"call-{index}")["choices"][0]["message"]["tool_calls"][0] for index in range(4)]
        service, llm, _, _ = self.build([plan("combined", sql="SELECT amount_irr FROM stay_rates", docs=False, calc=True),
            completion(tools=calls), final("چهار محاسبه اجرا شد.", sources=[])])
        result = await service.run("دو گزینه و اختلاف و مانده بودجه را محاسبه کن.")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(len(result["calculations"]), 4)
        self.assertEqual([m["tool_call_id"] for m in llm.calls[-1][0] if m["role"] == "tool"], [f"call-{i}" for i in range(4)])

    async def test_five_calls_and_reused_tool_identity_fail_closed(self):
        for count, duplicate, expected in [(5, False, "tool_call_limit"), (2, True, "invalid_tool_call")]:
            calls = [tool("1", identity="same" if duplicate else f"call-{index}")["choices"][0]["message"]["tool_calls"][0] for index in range(count)]
            service, _, _, _ = self.build([plan("combined", sql="SELECT amount_irr FROM stay_rates", docs=False, calc=True), completion(tools=calls)])
            result = await service.run("محاسبه کن.")
            self.assertEqual(result["error"]["code"], expected)
            self.assertNotEqual(result["status"], "answered")

    async def test_conversation_context_is_explicit_and_history_is_used(self):
        context = trip(origin="تهران", destination="مشهد", checkin="2026-11-17", checkout="2026-11-19", adults=2, rooms=1)
        history = [{"role": "user", "content": "از تهران به مشهد؛ 2026-11-17 تا 2026-11-19؛ دو بزرگسال و یک اتاق."}]
        service, llm, _, _ = self.build([plan(context=context), final()])
        result = await service.run("قواعد ورود همان اقامت را بگو.", history=history)
        self.assertIn(history[0], llm.calls[0][0])
        self.assertEqual(result["conversation_state"]["destination"], "مشهد")
        self.assertEqual(result["conversation_state"]["adults"], 2)
        self.assertEqual(result["conversation_state"]["checkout"], "2026-11-19")

    async def test_compatible_document_and_database_plan_flags_execute_both_sources(self):
        query = "SELECT amount_irr FROM stay_rates"
        for route, docs in (("documents", False), ("database", True)):
            with self.subTest(route=route):
                service, llm, sql, retrieval = self.build([
                    plan(route, sql=query, docs=docs), final("ساعت تحویل چهارده است. [S1]")])
                result = await service.run("هتل چه ساعتی اتاق را تحویل می‌دهد؟")
                self.assertEqual(result["status"], "answered")
                self.assertEqual(result["route"], "combined")
                self.assertEqual(sql.calls, [query])
                self.assertEqual(result["sql_results"][0]["rows"], [[501]])
                self.assertEqual(len(retrieval.calls), 1)
                self.assertEqual(result["sources"][0]["chunk_id"], "travel:1:a")
                self.assertEqual(len(llm.calls), 2)
                completed = {event["step"] for event in result["trace"] if event["status"] == "completed"}
                self.assertTrue({"database_specialist", "document_specialist"}.issubset(completed))
                self.assertTrue(any(event["step"] == "coordinator" and event["status"] == "normalized" for event in result["trace"]))

    async def test_policy_with_sql_gets_document_component(self):
        service, _, _, retrieval = self.build([plan("database", sql="SELECT amount_irr FROM stay_rates", docs=False), final()])
        result = await service.run("طبق راهنمای اقامت، این نرخ چه محدودیتی دارد؟")
        self.assertEqual(result["route"], "combined")
        self.assertEqual(len(retrieval.calls), 1)

    async def test_exact_entity_absence_requires_a_bounded_lookup_correction(self):
        identifier = "abcdef0123456789abcdef01"
        service, llm, sql, _ = self.build([plan("clarification", docs=False, clarification="شناسه را بگویید."),
            plan("database", sql="SELECT record_id FROM properties WHERE record_id='"+identifier+"'", docs=False),
            final("شناسه در snapshot پیدا نشد.", "insufficient_evidence", sources=[])],
            sql=FakeSQL([{"columns": ["record_id"], "rows": [], "row_count": 0}]))
        result = await service.run("اطلاعات شناسه "+identifier+" چیست؟")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(len(sql.calls), 1)
        self.assertEqual(len(llm.calls), 3)

    async def test_lookup_correction_fails_closed_when_model_still_supplies_no_sql(self):
        service, llm, sql, _ = self.build([plan("clarification", docs=False), plan("clarification", docs=False)])
        result = await service.run("شناسه abcdef0123456789abcdef01 را بررسی کن.")
        self.assertEqual(result["error"]["code"], "missing_entity_lookup")
        self.assertFalse(sql.calls)
        self.assertEqual(len(llm.calls), 2)

    async def test_write_rejection_precedes_entity_lookup(self):
        service, llm, sql, _ = self.build([plan("clarification", docs=False, reject="تغییر داده مجاز نیست.")])
        result = await service.run("شناسه abcdef0123456789abcdef01 را حذف کن.")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(len(llm.calls), 1)
        self.assertFalse(sql.calls)

    async def test_security_guard_is_never_repaired(self):
        service, llm, sql, _ = self.build([plan("database", sql="SELECT forbidden()", docs=False)],
            sql=FakeSQL([SafeSQLError("disallowed_function")]))
        result = await service.run("درخواست خطرناک")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(len(sql.calls), 1)

    async def test_safe_sql_error_gets_at_most_one_repair(self):
        service, _, sql, _ = self.build([plan("database", sql="SELECT wrong FROM properties", docs=False),
            completion({"sql": "SELECT record_id FROM properties"}), final("یک رکورد.", sources=[])],
            sql=FakeSQL([SafeSQLError("unknown_column"), {"columns": ["record_id"], "rows": [["hotel-A"]]}]))
        self.assertEqual((await service.run("رکورد را بده."))["status"], "answered")
        self.assertEqual(len(sql.calls), 2)

    async def test_malformed_json_never_reaches_sql(self):
        service, llm, sql, _ = self.build([completion("{invalid"), completion("{invalid")])
        result = await service.run("پرسش")
        self.assertEqual(result["error"]["code"], "invalid_model_output")
        self.assertFalse(sql.calls)
        self.assertEqual(len(llm.calls), 2)

    async def test_document_source_list_without_inline_is_formatted_without_another_model_call(self):
        original = "ساعت ورود چهارده است."
        service, llm, sql, _ = self.build([plan(), final(original, sources=["S1"])])
        result = await service.run("ساعت ورود طبق راهنما چیست؟")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["answer"], original + " [S1]")
        self.assertEqual(len(llm.calls), 2)
        self.assertFalse(sql.calls)
        self.assertFalse(any(event["step"] == "source_validation" and event["status"] == "repair" for event in result["trace"]))
        self.assertTrue(any(event["step"] == "source_validation" and event["status"] == "formatted" for event in result["trace"]))

    async def test_sql_quote_with_extraneous_retrieval_can_answer_without_pdf_attribution(self):
        for empty in (False, True):
            with self.subTest(empty_retrieval=empty):
                service, llm, sql, retrieval = self.build([
                    plan("combined", sql="SELECT amount_irr FROM stay_rates", docs=True, calc=True),
                    tool("501/10"), final("مبلغ مشاهده‌شده ۵۰٫۱ تومان است.", sources=[])],
                    retriever=FakeRetriever([]) if empty else FakeRetriever())
                result = await service.run("نرخ را به تومان بده.")
                self.assertEqual(result["status"], "answered")
                self.assertEqual(len(sql.calls), 1)
                self.assertEqual(len(retrieval.calls), 1)
                self.assertEqual(result["sources"], [])
                self.assertNotIn("[S", result["answer"])
                self.assertEqual(result["calculations"][0]["value"], "50.1")
                self.assertEqual(len(llm.calls), 3)
                self.assertFalse(any(event["step"] == "source_validation" and event["status"] == "repair" for event in result["trace"]))

    async def test_combined_sql_still_rejects_unknown_pdf_markers(self):
        service, llm, _, _ = self.build([
            plan("combined", sql="SELECT amount_irr FROM stay_rates", docs=True),
            final("ادعای منبع [S99]", sources=["S99"]), final("ادعای منبع [S99]", sources=["S99"])])
        result = await service.run("مقدار مشاهده‌شده را بگو.")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "invalid_source_reference")
        self.assertEqual(len(llm.calls), 3)

    async def test_answered_document_still_requires_nonempty_model_chosen_source_ids(self):
        service, llm, _, _ = self.build([plan(), final("ساعت چهارده است.", sources=[]),
                                        final("ساعت چهارده است.", sources=[])])
        result = await service.run("طبق سند ساعت چیست؟")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "missing_source_reference")
        self.assertEqual(len(llm.calls), 3)

    def test_citation_formatting_preserves_facts_and_uses_only_model_chosen_valid_ids(self):
        answer = Answer(status="answered", answer="ساعت ۱۴ است و مبلغ ۵۰٫۱ تومان است.", source_ids=["S2"])
        formatted = format_chosen_citations(answer, {"S1", "S2"}, True)
        self.assertEqual(formatted.answer, answer.answer + " [S2]")
        self.assertNotIn("[S1]", formatted.answer)
        self.assertEqual(factual_text(formatted.answer), factual_text(answer.answer))
        self.assertEqual(format_chosen_citations(formatted, {"S1", "S2"}, True), formatted)
        self.assertEqual(format_chosen_citations(answer, {"S1", "S2"}, False).answer, formatted.answer)
        with self.assertRaises(ProviderError):
            format_chosen_citations(answer.model_copy(update={"source_ids": ["S99"]}), {"S1", "S2"}, True)
        with self.assertRaises(ProviderError):
            format_chosen_citations(answer.model_copy(update={"answer": "ادعا [S99]"}), {"S1", "S2"}, True)

    async def test_invalid_typed_literals_repair_before_actual_sql_execution(self):
        from app.schema import SCHEMA_SQL
        from app.sql import SQLService
        prefix = "SELECT t.* FROM transport_offers t JOIN records r ON r.id=t.record_id WHERE "
        cases = [
            (prefix + "r.entity_type='transport'", prefix + "r.entity_type='offer'", "offer"),
            (prefix + "r.entity_type='offer' AND t.availability=1",
             prefix + "r.entity_type='offer' AND t.availability='available'", "availability"),
            (prefix + "r.entity_type='offer' AND t.availability=TRUE",
             prefix + "r.entity_type='offer' AND t.availability='available'", "availability"),
        ]
        class RecordingSQL(SQLService):
            def __init__(self, path):
                super().__init__(path)
                self.calls = []
            def execute(self, query):
                self.calls.append(query)
                return super().execute(query)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "travel-test.sqlite"
            with sqlite3.connect(path) as connection:
                connection.executescript(SCHEMA_SQL)
                connection.execute("INSERT INTO sources VALUES (?,?)", ("alibaba", "https://travel-test.invalid"))
                connection.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?)", (
                    "a"*24, "alibaba", "test-offer", "offer", "https://travel-test.invalid/offer", "پرواز آزمایشی", "مشهد", "2026-10-18T09:00:00+00:00", "{}", "{}"))
                connection.execute("INSERT INTO transport_offers(record_id,mode,origin_fa,destination_fa,service_date,departure_at,amount_irr,availability,available_seats,offer_kind,price_basis,search_completed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
                    "a"*24, "flight", "تهران", "مشهد", "2026-11-17", "2026-11-17T10:00:00+03:30", 501, "available", 3, "departure", "per_adult_one_way", 1))
            for bad, corrected, expected_guidance in cases:
                with self.subTest(bad=bad):
                    sql = RecordingSQL(path)
                    service, llm, _, _ = self.build([plan("database", sql=bad, docs=False), completion({"sql": corrected}),
                        final("یک رکورد سرویس در داده موجود است.", sources=[])], sql=sql)
                    result = await service.run("رکوردهای سرویس موجود در snapshot را بده.")
                    self.assertEqual(result["status"], "answered")
                    self.assertEqual(sql.calls, [corrected])
                    self.assertEqual(result["sql_results"][0]["row_count"], 1)
                    self.assertIn(501, result["sql_results"][0]["rows"][0])
                    self.assertEqual(len(llm.calls), 3)
                    repair_text = llm.calls[1][0][-1]["content"]
                    self.assertIn(expected_guidance, repair_text)
                    self.assertIn("SELECT t.*", repair_text)

    async def test_empty_stay_wrong_parent_join_repairs_once_and_preserves_genuine_absence(self):
        from app.schema import SCHEMA_SQL
        from app.sql import SQLService
        class RecordingSQL(SQLService):
            def __init__(self, path):
                super().__init__(path)
                self.calls = []
            def execute(self, query):
                self.calls.append(query)
                return super().execute(query)
        property_id = "b"*24
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "parent-link.sqlite"
            with sqlite3.connect(path) as connection:
                connection.executescript(SCHEMA_SQL)
                connection.execute("INSERT INTO sources VALUES (?,?)", ("eghamat24", "https://travel-test.invalid"))
                def record(identity, source_id, kind):
                    connection.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?,?)", (identity, "eghamat24", source_id,
                        kind, "https://travel-test.invalid/"+source_id, "هتل آزمایشی", "مشهد", "2026-10-18T09:00:00+00:00", "{}", "{}"))
                record(property_id, "parent-fixture", "property")
                connection.execute("INSERT INTO properties(record_id,city_fa,price_kind,amenities_json) VALUES (?,?,?,?)",
                                   (property_id, "مشهد", "undated_headline", "[]"))
                for index, (day, next_day, amount) in enumerate([( "2026-11-17", "2026-11-18", 100), ("2026-11-18", "2026-11-19", 150)]):
                    identity = str(index+1)*24
                    record(identity, "parent-fixture:room-A:plan-A:"+day, "offer")
                    connection.execute("INSERT INTO stay_rates(record_id,property_id,room_id,rate_plan_id,service_date,checkin,checkout,amount_irr,availability,occupancy,inventory,closed,close_to_arrival,close_to_departure,price_basis) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (identity, property_id, "room-A", "plan-A", day, day, next_day, amount, "available", 2, 3, 0, 0, 0, "per_room_per_night"))
            for begin, end, found in [("2026-11-17", "2026-11-19", True), ("2026-12-01", "2026-12-03", False)]:
                with self.subTest(found=found):
                    tail = f" WHERE r.source='eghamat24' AND r.source_id='parent-fixture' AND sr.room_id='room-A' AND sr.rate_plan_id='plan-A' AND sr.service_date>='{begin}' AND sr.service_date<'{end}' ORDER BY sr.service_date"
                    bad = "SELECT sr.* FROM stay_rates sr JOIN records r ON sr.record_id=r.id" + tail
                    corrected = "SELECT sr.* FROM stay_rates sr JOIN properties p ON sr.property_id=p.record_id JOIN records r ON r.id=p.record_id" + tail
                    context = trip(destination="مشهد", checkin=begin, checkout=end, adults=2, rooms=1,
                                   property_id=property_id, room_id="room-A", rate_plan_id="plan-A")
                    responses = [plan("combined", sql=bad, docs=False, calc=True, stay=True, context=context), completion({"sql": corrected})]
                    responses += [tool("(100+150)/10", precision=0), final("جمع مشاهده‌شده ۲۵ تومان است.", sources=[])] if found else [final("پس از بررسی پیوند والد، برای تاریخ درخواستی شاهد نرخ موجود نیست.", "insufficient_evidence", sources=[])]
                    sql = RecordingSQL(path)
                    service, llm, _, _ = self.build(responses, sql=sql)
                    result = await service.run(f"هتل آزمایشی برای دو نفر و یک اتاق از {begin} تا {end} را قیمت بده.")
                    self.assertEqual(sql.calls, [bad, corrected])
                    self.assertTrue(result["sql_results"][0]["evidence_warning"])
                    self.assertEqual(result["sql_results"][1]["row_count"], 2 if found else 0)
                    self.assertEqual(result["status"], "answered" if found else "insufficient_evidence")
                    self.assertEqual(len(result["calculations"]), 1 if found else 0)
                    self.assertEqual(result["quote_evidence"]["stay"]["blocked"], not found)
                    self.assertEqual(len(llm.calls), 4 if found else 3)
                    repair_messages = llm.calls[1][0]
                    self.assertTrue(any("کاندیدهای واقعی کاتالوگ" in message["content"] and "parent-fixture" in message["content"] for message in repair_messages))
                    self.assertIn("sr.property_id", repair_messages[-1]["content"])

    def test_quote_context_uses_actual_dates_sources_and_is_idempotent(self):
        arguments = {"snapshot": {"captured_on": "2026-10-18"},
            "trip_context": {"departure_date": "2026-11-17", "checkin": "2026-11-17", "checkout": "2026-11-19"},
            "sql_results": [{"record_sources": [
                {"source": "captured-source-A", "observed_at": "2026-10-18T09:00:00+00:00"},
                {"source": "captured-source-A", "observed_at": "2026-10-18T09:00:00+00:00"},
                {"source": "captured-source-A", "observed_at": "2026-10-18T09:30:00+00:00"},
                {"source": "captured-source-B", "observed_at": "2026-10-18T10:00:00+00:00"}]},
                {"evidence_warning": "not used", "record_sources": [{"source": "discarded-source"}]}],
            "calculations": [{"value": "50.1"}]}
        text = append_quote_context("مبلغ مشاهده‌شده ۵۰٫۱ تومان است. [S1]", **arguments)
        self.assertIn("snapshot ثبت‌شده در 2026-10-18", text)
        self.assertIn("رفت: 2026-11-17", text)
        self.assertIn("خروج: 2026-11-19", text)
        self.assertEqual(text.count("captured-source-A"), 2)
        self.assertEqual(text.count("2026-10-18T09:00:00+00:00"), 1)
        self.assertNotIn("discarded-source", text)
        self.assertIn("موجودی فعلی یا امکان رزرو امروز تأیید نشده", text)
        self.assertEqual(append_quote_context(text, **arguments), text)
        arguments["calculations"] = []
        self.assertEqual(append_quote_context("فقط راهنما", **arguments), "فقط راهنما")

    async def test_validated_native_quote_receives_context_without_changing_status_or_citations(self):
        context = trip(checkin="2026-11-17", checkout="2026-11-19", adults=2, rooms=1)
        evidence = {"columns": ["amount_irr"], "rows": [[501]], "record_sources": [
            {"source": "actual-supplier", "observed_at": "2026-10-18T09:00:00+00:00"}]}
        service, _, _, _ = self.build([plan("combined", sql="SELECT amount_irr FROM stay_rates", calc=True, context=context),
            tool(), final("مبلغ ۵۰٫۱ تومان است. [S1]")], sql=FakeSQL([evidence]))
        result = await service.run("تبدیل نرخ و راهنمای اقامت را بده.")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["sources"][0]["source_id"], "S1")
        self.assertIn("[S1]", result["answer"])
        self.assertIn("actual-supplier (مشاهده: 2026-10-18T09:00:00+00:00)", result["answer"])
        self.assertEqual(result["answer"].count("زمینه نقل‌قول:"), 1)
        self.assertEqual(result["calculations"][0]["value"], "50.1")

    async def test_bad_citations_still_fail_after_one_repair(self):
        service, llm, _, _ = self.build([plan(), final("ادعا [S99]", sources=["S99"]), final("ادعا [S99]", sources=["S99"])])
        result = await service.run("قاعده چیست؟")
        self.assertEqual(result["error"]["code"], "invalid_source_reference")
        self.assertEqual(len(llm.calls), 3)

    async def test_citation_repair_never_reruns_successful_sql_or_calculator(self):
        service, llm, sql, _ = self.build([plan("combined", sql="SELECT amount_irr FROM transport_offers", docs=False, calc=True),
            tool(), final("مبلغ ۵۰٫۱ تومان است. [S99]", sources=["S99"]), final("مبلغ ۵۰٫۱ تومان است.", sources=[])])
        result = await service.run("تبدیل قیمت به تومان")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(len(sql.calls), 1)
        self.assertEqual(len(result["calculations"]), 1)
        self.assertNotIn("tools", llm.calls[-1][1])
        field = llm.calls[-1][1]["response_format"]["json_schema"]["schema"]["properties"]["source_ids"]
        self.assertEqual(field["maxItems"], 0)
        self.assertNotIn("enum", field["items"])

    async def test_citation_only_repair_cannot_change_price(self):
        service, _, _, _ = self.build([plan(), final("ساعت ۱۴ است. [S99]", sources=["S99"]), final("ساعت ۱۵ است. [S1]")])
        self.assertEqual((await service.run("ساعت ورود؟"))["error"]["code"], "invalid_source_reference")

    async def test_missing_calculator_cannot_publish_model_price(self):
        service, llm, _, _ = self.build([plan("combined", sql="SELECT amount_irr FROM stay_rates", docs=False, calc=True),
            final("۹۹۹ تومان.", sources=[]), final("۹۹۹ تومان.", sources=[])])
        result = await service.run("قیمت را محاسبه کن.")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertNotIn("۹۹۹", result["answer"])
        self.assertEqual(len(llm.calls), 3)

    async def test_search_preserves_user_constraints_and_explicit_metadata_filters(self):
        question = "قانون کودک هتل الماس ۲ مشهد بدون تخت چیست؟"
        service, _, _, retrieval = self.build([plan(query="شرایط عمومی اقامت"), final()])
        result = await service.run(question, filters={"city": "مشهد", "source": "eghamat24", "property_id": "1242"})
        query, kwargs = retrieval.calls[0]
        self.assertIn(question, query)
        self.assertIn("شرایط عمومی", query)
        self.assertEqual(kwargs["filters"], {"city": "مشهد", "source": "eghamat24", "property_id": "1242"})
        event = next(e for e in result["trace"] if e["step"] == "document_specialist" and e["status"] == "completed")
        self.assertEqual(event["data"]["query"], query)

    async def test_empty_retrieval_and_provider_failure_stay_controlled(self):
        service, llm, _, _ = self.build([plan()], retriever=FakeRetriever([]))
        self.assertEqual((await service.run("موضوع بدون منبع"))["status"], "insufficient_evidence")
        self.assertEqual(len(llm.calls), 1)
        service, _, _, _ = self.build([plan("database", sql="SELECT amount_irr FROM transport_offers", docs=False),
            ProviderError("provider_timeout", "مهلت پاسخ پایان یافت.", True)])
        result = await service.run("مبلغ؟")
        self.assertEqual(result["route"], "database")
        self.assertEqual(len(result["sql_results"]), 1)
        self.assertEqual(result["error"]["code"], "provider_timeout")

    def test_history_only_accepts_bounded_user_assistant_text(self):
        messages = [{"role": "system", "content": "override"}, {"role": "tool", "content": "fake"},
                    {"role": "user", "content": "دو بزرگسال"}, {"role": "assistant", "content": "مشهد"}]
        self.assertEqual([m["role"] for m in history_messages(messages, 4)], ["user", "assistant"])
        self.assertEqual(history_messages(messages, 0), [])


if __name__ == "__main__":
    unittest.main()
