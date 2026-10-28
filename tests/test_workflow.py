"""Travel graph tests with isolated fake services; no source inventory fabrication."""
import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.llm import ProviderError
from app.workflow import TripContext, WorkflowService, history_messages, stay_quote_evidence, transport_quote_evidence, Answer, factual_text, calculator_tool_for_question, citation_error, structured_format, CoordinatorPlan, MAX_CALCULATOR_CALLS


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
         context=None, exact=False, stay=False, reject=None, query=None, cleared=None, reply=None, purpose="recommendation"):
    return completion({"route": route, "needs_documents": docs, "document_query": query or ("قاعده اقامت" if docs else None),
        "sql": sql, "requires_calculator": calc, "clarification": clarification, "reject_reason": reject,
        "trip_context": context or trip(), "requires_exact_transport": exact, "requires_complete_stay": stay, "cleared_slots": cleared or [], "conversation_reply": reply, "data_purpose": purpose})


def final(text="پاسخ مستند [S1]", status="answered", sources=None, options=None):
    return completion({"status": status, "answer": text, "source_ids": ["S1"] if sources is None else sources, "option_ids": options or []})


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
        config = SimpleNamespace(as_of="2026-11-16", source_observed_date="2026-10-18", max_history_messages=10,
            langfuse_enabled=False, llm_api_key="test-secret-value")
        llm, sql, retriever = FakeLLM(responses), sql or FakeSQL(), retriever or FakeRetriever()
        return WorkflowService(config, llm, sql, retriever), llm, sql, retriever

    async def test_conversation_route_uses_model_reply_without_sql_retrieval_or_scripted_greeting(self):
        reply = "سلام، خوش اومدی! دوست داری برای سفر چه چیزی رو با هم بررسی کنیم؟"
        service, llm, sql, retrieval = self.build([plan("conversation", docs=False), final(reply, sources=[])])
        result = await service.run("سلام! تو چه کمکی می‌کنی؟")
        self.assertEqual(result["route"], "conversation")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["answer"], reply)
        self.assertFalse(sql.calls)
        self.assertFalse(retrieval.calls)
        self.assertEqual(len(llm.calls), 2)
        starts = [event for event in result["trace"] if event["step"] == "model_call" and event["status"] == "started"]
        finishes = [event for event in result["trace"] if event["step"] == "model_call" and event["status"] == "completed"]
        self.assertEqual(len(starts), 2)
        self.assertEqual({event["data"]["call_id"] for event in starts}, {event["data"]["call_id"] for event in finishes})
        self.assertIn("messages", starts[0]["data"])
        self.assertIn("tool_choice", starts[0]["data"])
        self.assertEqual(starts[0]["data"]["temperature"], 0)
        self.assertEqual(starts[0]["data"]["max_tokens"], 2200)
        self.assertIn("response", finishes[0]["data"])

    async def test_coordinator_authored_conversation_reply_avoids_a_second_model_call(self):
        authored = "سلام! خوشحالم اینجایی؛ اگر دوست داری، با هم سفر بعدی‌ات را بررسی کنیم."
        service, llm, sql, retrieval = self.build([plan("conversation", docs=False, reply=authored)])
        result = await service.run("سلام، خوبی؟")
        self.assertEqual(result["answer"], authored)
        self.assertEqual(result["route"], "conversation")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["source_ids"], [])
        self.assertEqual(result["offered_options"], [])
        self.assertEqual(len(llm.calls), 1)
        self.assertFalse(sql.calls or retrieval.calls)
        self.assertEqual(sum(event["step"] == "model_call" and event["status"] == "completed" for event in result["trace"]), 1)

    async def test_iterative_clarification_preserves_known_slots_and_deterministic_tomorrow(self):
        service, llm, sql, retrieval = self.build([plan("clarification", docs=False,
            context=trip(checkin="2026-11-17"), clarification="چند شب می‌خواهی بمانی؟")])
        previous = {"origin": "تهران", "destination": "مشهد", "adults": 2}
        first = await service.run("برای فردا یک اقامت می‌خواهم.", conversation_state=previous)
        self.assertEqual(first["status"], "clarification")
        self.assertEqual(first["conversation_state"]["origin"], "تهران")
        self.assertEqual(first["conversation_state"]["adults"], 2)
        self.assertEqual(first["calendar_context"]["relative_dates"]["فردا"]["gregorian"], "2026-11-17")
        self.assertEqual(first["answer"], "چند شب می‌خواهی بمانی؟")
        service2, _, sql2, retrieval2 = self.build([plan("clarification", docs=False,
            context=trip(checkout="2026-11-19"), clarification="یک اتاق می‌خواهید یا دو اتاق جدا؟")])
        second = await service2.run("دو شب می‌مانیم.", conversation_state=first["conversation_state"])
        self.assertEqual(second["conversation_state"]["checkin"], "2026-11-17")
        self.assertEqual(second["conversation_state"]["checkout"], "2026-11-19")
        self.assertEqual(second["conversation_state"]["destination"], "مشهد")
        self.assertFalse(sql.calls or sql2.calls or retrieval.calls or retrieval2.calls)
        self.assertIn("2026-11-17", llm.calls[0][0][1]["content"])

    async def test_explicit_slot_clear_is_not_refilled_from_previous_state(self):
        service, _, _, _ = self.build([plan("conversation", docs=False, cleared=["budget_amount", "budget_currency"]),
                                      final("بودجه قبلی را کنار می‌گذاریم.", sources=[])])
        result = await service.run("بودجه قبلی را فراموش کن.", conversation_state={"budget_amount": 5000000, "budget_currency": "IRT", "destination": "مشهد"})
        self.assertNotIn("budget_amount", result["conversation_state"])
        self.assertEqual(result["conversation_state"]["destination"], "مشهد")

    async def test_actual_options_bind_to_model_display_order_and_second_option_context(self):
        identities = ["a"*24, "b"*24]
        def fare(identity, amount, operator):
            return {"record_id": identity, "mode": "flight", "origin_fa": "تهران", "destination_fa": "مشهد",
                "service_date": "2026-11-17", "departure_at": "2026-11-17T16:30:00+03:30", "arrival_at": None,
                "amount_irr": amount, "availability": "available", "available_seats": 5, "search_completed": 1,
                "offer_kind": "departure", "price_basis": "per_adult_one_way", "operator_name": operator}
        fares = [fare(identities[0], 200, "شرکت الف"), fare(identities[1], 100, "شرکت ب")]
        context = trip(origin="تهران", destination="مشهد", departure_date="2026-11-17", adults=2)
        service, _, _, _ = self.build([plan("database", sql="SELECT * FROM transport_offers", docs=False, context=context),
            final("اول شرکت ب و دوم شرکت الف را داریم؛ حرکت ساعت 16:30 است.", sources=[], options=[identities[1], identities[0]])], sql=FakeSQL([row_result(fares)]))
        first = await service.run("دو گزینه برای فردا با قیمت خام ریالی معرفی کن.")
        self.assertEqual([option["option_id"] for option in first["offered_options"]], [identities[1], identities[0]])
        self.assertEqual([option["ordinal"] for option in first["offered_options"]], [1, 2])
        self.assertIn("۴:۳۰ عصر", first["answer"])
        follow_context = trip(selected_transport_ids=[identities[0]])
        service2, llm2, sql2, _ = self.build([plan("database", sql="SELECT * FROM transport_offers WHERE record_id='"+identities[0]+"'", docs=False, exact=True, context=follow_context),
            final("گزینه دوم همان پرواز شرکت الف است.", sources=[], options=[identities[0]])], sql=FakeSQL([row_result([fares[0]])]))
        second = await service2.run("همان گزینه دوم را با قیمت خام ریالی بررسی کن.", conversation_state=first["conversation_state"], offered_options=first["offered_options"])
        self.assertEqual(second["status"], "answered")
        self.assertEqual(second["offered_options"][0]["option_id"], identities[0])
        self.assertEqual(second["conversation_state"]["origin"], "تهران")
        self.assertEqual(len(sql2.calls), 1)
        context_message = llm2.calls[0][0][2]["content"]
        self.assertLess(context_message.index(identities[1]), context_message.index(identities[0]))

    async def test_model_cannot_publish_option_identity_absent_from_actual_sql(self):
        row = {"record_id": "a"*24, "offer_kind": "departure", "price_basis": "per_adult_one_way", "amount_irr": 100}
        service, _, _, _ = self.build([plan("database", sql="SELECT * FROM transport_offers", docs=False),
            final("گزینه ساختگی", sources=[], options=["b"*24])], sql=FakeSQL([row_result([row])]))
        result = await service.run("یک گزینه با قیمت خام ریالی نشان بده.")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "invalid_option_reference")
        self.assertEqual(result["offered_options"], [])

    async def test_provider_failure_before_plan_preserves_state_for_next_turn(self):
        known = {"origin": "تهران", "destination": "مشهد", "adults": 2, "checkin": "2026-11-17"}
        options = [{"option_id": "a"*24, "record_id": "a"*24, "kind": "transport", "ordinal": 1}]
        failed, _, _, _ = self.build([ProviderError("provider_timeout", "مهلت پاسخ تمام شد.", True)])
        interrupted = await failed.run("ادامه بده.", conversation_state=known, offered_options=options)
        self.assertEqual(interrupted["status"], "error")
        for key, value in known.items():
            self.assertEqual(interrupted["conversation_state"][key], value)
        self.assertEqual(interrupted["offered_options"], options)
        resumed, _, _, _ = self.build([plan("conversation", docs=False, reply="بله، می‌توانیم ادامه بدهیم.")])
        result = await resumed.run("دوباره ادامه بده.", conversation_state=interrupted["conversation_state"], offered_options=interrupted["offered_options"])
        self.assertEqual(result["status"], "answered")
        for key, value in known.items():
            self.assertEqual(result["conversation_state"][key], value)
        self.assertEqual(result["offered_options"], options)

    async def test_snapshot_date_is_unknown_when_metadata_setting_is_unavailable(self):
        service, _, _, _ = self.build([plan("conversation", docs=False), final("سلام!", sources=[])])
        service.settings.source_observed_date = None
        self.assertIsNone((await service.run("سلام"))["snapshot"]["captured_on"])

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
        service, _, sql, retrieval = self.build([plan("database", sql=query, docs=False), final("سه رکورد.", sources=[])],
            sql=FakeSQL([{"columns": ["record_count"], "rows": [[3]]}]))
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

    async def test_monetary_listing_forces_native_default_toman_despite_planner_flag(self):
        service, llm, sql, _ = self.build([
            plan("database", sql="SELECT amount_irr FROM transport_offers", docs=False, calc=False),
            tool("501/10", identity="native-display"), final("قیمت هر نفر ۵۰٫۱ تومان است.", sources=[])])
        result = await service.run("گزینه‌های موجود را با قیمت معرفی کن.")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["route"], "combined")
        self.assertEqual(result["calculations"][0]["value"], "50.1")
        self.assertEqual(len(sql.calls), 1)
        self.assertIn("ابزار واقعی", llm.calls[1][0][1]["content"])
        self.assertIn("تومان", llm.calls[1][1]["tools"][0]["function"]["parameters"]["properties"]["unit"]["enum"])

    async def test_explicit_raw_rial_listing_does_not_force_conversion(self):
        service, _, _, _ = self.build([
            plan("database", sql="SELECT amount_irr FROM transport_offers", docs=False, calc=False),
            final("قیمت ثبت‌شده ۵۰۱ ریال است.", sources=[])])
        result = await service.run("فقط قیمت خام ریالی را بده.")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["route"], "database")
        self.assertEqual(result["calculations"], [])

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
            final("قیمت کامل ۲۵ تومان است.", sources=[]), final("قیمت کامل ۲۵ تومان است.", sources=[])],
            sql=FakeSQL([row_result(night_rows()), {"columns": ["record_id"], "rows": []}]))
        result = await service.run("هزینه کامل پرواز و اقامت را بده.")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "invalid_model_output")
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
            final("قیمت ساختگی ۲۵ تومان است.", sources=[]), final("قیمت ساختگی ۲۵ تومان است.", sources=[])], sql=FakeSQL([row_result(night_rows(second_room=True))]))
        result = await service.run("قیمت کل اقامت دوشبه؟")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "invalid_model_output")
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
            completion({"sql": "SELECT complete FROM stay_rates"}), final("قیمت قطعی ۲۵ تومان است.", sources=[]), final("قیمت قطعی ۲۵ تومان است.", sources=[])], sql=sql)
        result = await service.run("قیمت دو شب همان اقامتگاه را بده.")
        self.assertEqual(len(sql.calls), 2)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "invalid_model_output")
        self.assertFalse(result["calculations"])
        self.assertTrue(result["quote_evidence"]["stay"]["blocked"])

    async def test_calendar_fares_cannot_be_quoted_as_exact_departures(self):
        row = {"offer_kind": "daily_fare_aggregate", "availability": "unknown", "departure_at": None,
               "amount_irr": 100, "price_basis": "per_adult_one_way_from", "available_seats": None,
               "record_id": "calendar", "mode": "flight", "origin_fa": "تهران", "destination_fa": "مشهد",
               "service_date": "2026-11-17", "arrival_at": None, "search_completed": None}
        service, _, _, _ = self.build([plan("combined", sql="SELECT offer_evidence FROM transport_offers", docs=False, calc=True, exact=True),
            final("پرواز قطعی است.", sources=[]), final("پرواز قطعی است.", sources=[])], sql=FakeSQL([row_result([row])]))
        result = await service.run("این نرخ تقویمی را به عنوان پرواز دقیق قیمت بده.")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "invalid_model_output")
        self.assertFalse(result["calculations"])

    async def test_four_native_calculations_can_share_one_assistant_message(self):
        calls = [tool(str(index+1), identity=f"call-{index}")["choices"][0]["message"]["tool_calls"][0] for index in range(4)]
        service, llm, _, _ = self.build([plan("combined", sql="SELECT amount_irr FROM stay_rates", docs=False, calc=True),
            completion(tools=calls), final("چهار محاسبه اجرا شد.", sources=[])])
        result = await service.run("دو گزینه و اختلاف و مانده بودجه را محاسبه کن.")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(len(result["calculations"]), 4)
        self.assertEqual([m["tool_call_id"] for m in llm.calls[-1][0] if m["role"] == "tool"], [f"call-{i}" for i in range(4)])

    async def test_over_budget_calls_and_reused_tool_identity_fail_closed(self):
        for count, duplicate, expected in [(MAX_CALCULATOR_CALLS+1, False, "tool_call_limit"), (2, True, "invalid_tool_call")]:
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
        query = "SELECT checkin_time FROM properties"
        for route, docs in (("documents", False), ("database", True)):
            with self.subTest(route=route):
                service, llm, sql, retrieval = self.build([
                    plan(route, sql=query, docs=docs), final("ساعت تحویل چهارده است. [S1]")],
                    sql=FakeSQL([{"columns": ["checkin_time"], "rows": [["14:00"]]}]))
                result = await service.run("هتل چه ساعتی اتاق را تحویل می‌دهد؟")
                self.assertEqual(result["status"], "answered")
                self.assertEqual(result["route"], "combined")
                self.assertEqual(sql.calls, [query])
                self.assertEqual(result["sql_results"][0]["rows"], [["14:00"]])
                self.assertEqual(len(retrieval.calls), 1)
                self.assertEqual(result["sources"][0]["chunk_id"], "travel:1:a")
                self.assertEqual(len(llm.calls), 2)
                completed = {event["step"] for event in result["trace"] if event["status"] == "completed"}
                self.assertTrue({"database_specialist", "document_specialist"}.issubset(completed))
                self.assertTrue(any(event["step"] == "coordinator" and event["status"] == "normalized" for event in result["trace"]))

    async def test_a_keyword_does_not_override_the_model_selected_capability_plan(self):
        service, _, _, retrieval = self.build([plan("database", sql="SELECT room_id FROM stay_rates", docs=False),
                                               final("یک اتاق در نتیجه داده وجود دارد.", sources=[])],
            sql=FakeSQL([{"columns": ["room_id"], "rows": [["room-A"]]}]))
        result = await service.run("یک رکورد داده برای نوشتن راهنما بده.")
        self.assertEqual(result["route"], "database")
        self.assertEqual(result["status"], "answered")
        self.assertFalse(retrieval.calls)

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

    async def test_document_source_ids_stay_metadata_without_visible_marker_or_extra_call(self):
        original = "ساعت ورود چهارده است."
        service, llm, sql, _ = self.build([plan(), final(original, sources=["S1"])])
        result = await service.run("ساعت ورود طبق راهنما چیست؟")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["answer"], original)
        self.assertEqual(result["source_ids"], ["S1"])
        self.assertEqual(len(llm.calls), 2)
        self.assertFalse(sql.calls)
        self.assertFalse(any(event["step"] == "source_validation" and event["status"] == "repair" for event in result["trace"]))
        self.assertFalse(any(event["step"] == "source_validation" and event["status"] == "formatted" for event in result["trace"]))

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
            plan("combined", sql="SELECT room_id FROM stay_rates", docs=True),
            final("ادعای منبع [S99]", sources=["S99"]), final("ادعای منبع [S99]", sources=["S99"])],
            sql=FakeSQL([{"columns": ["room_id"], "rows": [["room-A"]]}]))
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

    def test_source_selection_is_validated_without_requiring_a_visible_marker(self):
        answer = Answer(status="answered", answer="ساعت ۱۴ است.", source_ids=["S2"])
        self.assertIsNone(citation_error(answer, {"S1", "S2"}, True))
        self.assertEqual(answer.answer, "ساعت ۱۴ است.")
        self.assertEqual(citation_error(answer.model_copy(update={"source_ids": ["S99"]}), {"S1", "S2"}, True), "invalid_source_reference")
        self.assertEqual(citation_error(answer.model_copy(update={"answer": "ادعا [S99]"}), {"S1", "S2"}, True), "invalid_source_reference")

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
                    result = await service.run("رکوردهای سرویس را با قیمت خام ریالی بده.")
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

    def test_model_output_schema_has_persian_semantics_without_english_titles(self):
        schema = structured_format("coordinator_plan", CoordinatorPlan)["json_schema"]["schema"]
        self.assertNotIn("title", schema)
        self.assertEqual(schema["properties"]["clarification"]["description"], "پرسش طبیعی برای ابهام ضروری")
        self.assertIn("conversation", schema["properties"]["route"]["enum"])
        self.assertTrue(schema["additionalProperties"] is False)
        self.assertEqual(set(schema["required"]), set(schema["properties"]))

    def test_exported_coordinator_schema_has_standalone_refs_and_strict_persian_definitions(self):
        schema = structured_format("coordinator_plan", CoordinatorPlan)["json_schema"]["schema"]
        def inspect(node):
            if isinstance(node, dict):
                if "$ref" in node:
                    self.assertEqual(set(node), {"$ref"})
                self.assertNotIn("default", node)
                if node.get("type") == "object":
                    self.assertFalse(node["additionalProperties"])
                    self.assertEqual(set(node["required"]), set(node["properties"]))
                if "description" in node:
                    self.assertTrue(any("آ" <= character <= "ی" for character in node["description"]), node["description"])
                for value in node.values():
                    inspect(value)
            elif isinstance(node, list):
                for value in node:
                    inspect(value)
        inspect(schema)
        self.assertEqual(schema["properties"]["trip_context"], {"$ref": "#/$defs/TripContext"})
        self.assertIn("وضعیت معلوم سفر", schema["$defs"]["TripContext"]["description"])

    async def test_validated_native_quote_keeps_technical_context_out_of_natural_answer(self):
        context = trip(checkin="2026-11-17", checkout="2026-11-19", adults=2, rooms=1)
        evidence = {"columns": ["amount_irr"], "rows": [[501]], "record_sources": [
            {"source": "actual-supplier", "observed_at": "2026-10-18T09:00:00+00:00"}]}
        service, _, _, _ = self.build([plan("combined", sql="SELECT amount_irr FROM stay_rates", calc=True, context=context, purpose="raw_facts"),
            tool(), final("مبلغ ۵۰٫۱ تومان است. [S1]")], sql=FakeSQL([evidence]))
        result = await service.run("تبدیل نرخ و راهنمای اقامت را بده.")
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["sources"][0]["source_id"], "S1")
        self.assertNotIn("[S1]", result["answer"])
        self.assertNotIn("actual-supplier", result["answer"])
        self.assertNotIn("زمینه نقل‌قول:", result["answer"])
        self.assertEqual(result["snapshot"]["captured_on"], "2026-10-18")
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
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "invalid_model_output")
        self.assertNotIn("۹۹۹", result["answer"])
        self.assertEqual(len(llm.calls), 3)

    async def test_clarification_without_model_authored_question_fails_before_tools(self):
        for missing in (None, "   "):
            with self.subTest(clarification=missing):
                service, llm, sql, retrieval = self.build([plan("clarification", docs=False, clarification=missing), plan("clarification", docs=False, clarification=missing)])
                result = await service.run("می‌خواهم سفرم را برنامه‌ریزی کنم.")
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["error"]["code"], "invalid_model_output")
                self.assertEqual(len(llm.calls), 2)
                self.assertFalse(sql.calls or retrieval.calls)
                self.assertNotIn("برای ادامه،", result["answer"])
                self.assertNotIn("منظور دقیق درخواست", result["answer"])

    async def test_lookup_clarification_uses_only_model_authored_questions(self):
        hint, authored = "کدام شهر را برای این هتل مدنظر داری؟", "این دو نام به کدام مقصد سفرت مربوط‌اند؟"
        for status, expected in (("answered", hint), ("clarification", authored)):
            with self.subTest(status=status):
                service, llm, sql, _ = self.build([
                    plan("clarification", docs=False, sql="SELECT name_fa,city_fa FROM records", clarification=hint),
                    final(authored, status=status, sources=[])],
                    sql=FakeSQL([{"columns": ["name_fa", "city_fa"], "rows": [["نام همسان", "مشهد"], ["نام همسان", "شیراز"]]}]))
                result = await service.run("اطلاعات هتل هم‌نام را می‌خواهم.")
                self.assertEqual(result["status"], "clarification")
                self.assertEqual(result["answer"], expected)
                self.assertEqual(len(sql.calls), 1)
                self.assertEqual(len(llm.calls), 2)

    async def test_model_authored_insufficient_evidence_passes_without_required_calculation(self):
        authored = "با این داده‌ها هنوز نمی‌توانم جمع معتبری بدهم؛ مبلغ مسیر برگشت در نتیجه موجود نیست."
        service, llm, _, _ = self.build([
            plan("combined", sql="SELECT amount_irr FROM transport_offers", docs=False, calc=True),
            final(authored, status="insufficient_evidence", sources=[])])
        result = await service.run("هزینه سفر را حساب کن.")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["answer"], authored)
        self.assertEqual(result["calculations"], [])
        self.assertEqual(len(llm.calls), 2)
        self.assertNotIn("error", result)

    async def test_model_authored_insufficient_evidence_survives_a_blocked_stay_quote(self):
        authored = "اتاق یکسانی برای هر دو شب نداریم؛ نرخ‌های این دو اتاق را نمی‌توانم یک اقامت پیوسته معرفی کنم."
        context = trip(checkin="2026-11-17", checkout="2026-11-19", adults=2, rooms=1)
        service, llm, _, _ = self.build([
            plan("combined", sql="SELECT * FROM stay_rates", docs=False, calc=True, stay=True, context=context),
            final(authored, status="insufficient_evidence", sources=[])], sql=FakeSQL([row_result(night_rows(second_room=True))]))
        result = await service.run("قیمت اقامت دوشبه را بده.")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["answer"], authored)
        self.assertTrue(result["quote_evidence"]["stay"]["blocked"])
        self.assertEqual(result["offered_options"], [])
        self.assertFalse(result["calculations"])
        self.assertNotIn("tools", llm.calls[-1][1])

    def test_selected_invalid_stay_cannot_be_rescued_by_an_unselected_valid_property(self):
        valid = [{**row, "property_id": "other-property", "record_id": "valid-"+row["record_id"]} for row in night_rows()]
        context = TripContext(**trip(checkin="2026-11-17", checkout="2026-11-19", adults=2, rooms=1,
                                     selected_stay_ids=["night-0"]))
        invalid_groups = [
            [{**row, "closed": 1} for row in night_rows()],
            night_rows()[:1],
            [{**row, "inventory": 0} for row in night_rows()],
        ]
        for invalid in invalid_groups:
            with self.subTest(invalid=invalid):
                evidence = stay_quote_evidence([*invalid, *valid], context)
                self.assertTrue(evidence["blocked"])
                self.assertTrue(any(group["complete_observed"] for group in evidence["groups"]))
                self.assertEqual(evidence["eligible_record_ids"], ["valid-night-0", "valid-night-1"])
                self.assertNotIn("night-0", evidence["eligible_record_ids"])

    async def test_selected_closed_stay_keeps_tools_disabled_even_with_a_valid_alternative(self):
        valid = [{**row, "property_id": "other-property", "record_id": "valid-"+row["record_id"]} for row in night_rows()]
        closed = [{**row, "closed": 1} for row in night_rows()]
        context = trip(checkin="2026-11-17", checkout="2026-11-19", adults=2, rooms=1, selected_stay_ids=["night-0"])
        authored = "اقامت انتخابی بسته است؛ یک گزینه دیگر در داده هست، اما هزینه انتخاب بسته را تأیید نمی‌کنم."
        service, llm, _, _ = self.build([
            plan("combined", sql="SELECT * FROM stay_rates", docs=False, calc=True, stay=True, context=context),
            final(authored, status="insufficient_evidence", sources=[])], sql=FakeSQL([row_result([*closed, *valid])]))
        result = await service.run("همان اقامت انتخابی را قیمت بده.")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["answer"], authored)
        self.assertTrue(result["quote_evidence"]["stay"]["blocked"])
        self.assertFalse(result["calculations"])
        self.assertNotIn("tools", llm.calls[-1][1])

    async def test_stay_quote_options_exclude_closed_groups_but_raw_listing_retains_them(self):
        closed = [{**row, "closed": 1} for row in night_rows()]
        valid = [{**row, "property_id": "other-property", "record_id": "valid-"+row["record_id"]} for row in night_rows()]
        context = trip(checkin="2026-11-17", checkout="2026-11-19", adults=2, rooms=1)
        for quoting, selected, expected_status in ((True, "valid-night-0", "answered"),
                                                   (True, "night-0", "error"), (False, "night-0", "answered")):
            with self.subTest(quoting=quoting, selected=selected):
                responses = [plan("combined" if quoting else "database", sql="SELECT * FROM stay_rates",
                                  docs=False, stay=quoting, context=context, purpose="recommendation" if quoting else "raw_facts")]
                if quoting:
                    responses.append(tool("(100+150)/10", precision=0))
                responses.append(final("گزینه‌ای از نتیجه واقعی معرفی شد.", sources=[], options=[selected]))
                service, llm, _, _ = self.build(responses, sql=FakeSQL([row_result([*closed, *valid])]))
                result = await service.run("قیمت اقامت دوشبه را بده." if quoting else "ردیف‌های خام ریالی، حتی بسته‌ها را نشان بده.")
                self.assertEqual(result["status"], expected_status)
                option_schema = llm.calls[1][1]["response_format"]["json_schema"]["schema"]["properties"]["option_ids"]
                self.assertEqual(set(option_schema["items"]["enum"]), {"valid-night-0"} if quoting else {"night-0", "valid-night-0"})
                if expected_status == "error":
                    self.assertEqual(result["error"]["code"], "invalid_option_reference")
                    self.assertEqual(result["offered_options"], [])
                else:
                    self.assertEqual(result["offered_options"][0]["option_id"], selected)

    async def test_exact_transport_options_exclude_calendars_without_hiding_raw_facts(self):
        fare = {"record_id": "actual-departure", "mode": "flight", "origin_fa": "تهران", "destination_fa": "مشهد",
                "service_date": "2026-11-17", "departure_at": "2026-11-17T10:00:00+03:30", "arrival_at": None,
                "amount_irr": 100, "availability": "available", "available_seats": 3, "search_completed": 1,
                "offer_kind": "departure", "price_basis": "per_adult_one_way"}
        calendar = {**fare, "record_id": "calendar-floor", "departure_at": None, "available_seats": None,
                    "availability": "calendar_indicated", "search_completed": None,
                    "offer_kind": "daily_fare_aggregate", "price_basis": "per_adult_one_way_from"}
        context = trip(origin="تهران", destination="مشهد", departure_date="2026-11-17", adults=2)
        for exact, selected, expected_status in ((True, "actual-departure", "answered"),
                                                 (True, "calendar-floor", "error"), (False, "calendar-floor", "answered")):
            with self.subTest(exact=exact, selected=selected):
                service, llm, _, _ = self.build([
                    plan("database", sql="SELECT * FROM transport_offers", docs=False, exact=exact, context=context, purpose="recommendation" if exact else "raw_facts"),
                    final("ردیف واقعی داده بررسی شد.", sources=[], options=[selected])], sql=FakeSQL([row_result([fare, calendar])]))
                result = await service.run("داده را با قیمت خام ریالی بده.")
                self.assertEqual(result["status"], expected_status)
                option_schema = llm.calls[1][1]["response_format"]["json_schema"]["schema"]["properties"]["option_ids"]
                self.assertEqual(set(option_schema["items"]["enum"]), {"actual-departure"} if exact else {"actual-departure", "calendar-floor"})
                if expected_status == "error":
                    self.assertEqual(result["error"]["code"], "invalid_option_reference")
                else:
                    self.assertEqual(result["offered_options"][0]["option_id"], selected)

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
        authored = "در اسنادی که جستجو کردم، اطلاعاتی درباره این موضوع پیدا نشد و نمی‌توانم پاسخ مستندی بدهم."
        service, llm, _, _ = self.build([plan(), final(authored, "insufficient_evidence", sources=[])], retriever=FakeRetriever([]))
        result = await service.run("موضوع بدون منبع")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["answer"], authored)
        self.assertEqual(len(llm.calls), 2)
        evidence = next(json.loads(m["content"]) for m in llm.calls[1][0] if m["role"] == "user")
        self.assertEqual(evidence["documents"], [])
        service, _, _, _ = self.build([plan("database", sql="SELECT amount_irr FROM transport_offers", docs=False),
            ProviderError("provider_timeout", "مهلت پاسخ پایان یافت.", True)])
        result = await service.run("مبلغ؟")
        self.assertEqual(result["route"], "combined")
        self.assertEqual(len(result["sql_results"]), 1)
        self.assertEqual(result["error"]["code"], "provider_timeout")

    async def test_empty_document_search_cannot_become_an_answered_factual_reply(self):
        service, llm, _, _ = self.build([plan(), final("ادعای بدون شاهد", sources=[]), final("ادعای بدون شاهد", sources=[])], retriever=FakeRetriever([]))
        result = await service.run("موضوع بدون منبع")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "invalid_model_output")
        self.assertNotEqual(result["answer"], "ادعای بدون شاهد")
        self.assertEqual(len(llm.calls), 3)

    async def test_blocked_quote_is_repaired_into_a_natural_model_authored_clarification(self):
        authored = "نرخ اتاق‌های دونفره برای دو شب پیدا شد. برای دو نفرتان یک اتاق مشترک می‌خواهی؟ تا این مورد مشخص نشود، هزینه کامل را قطعی حساب نمی‌کنم."
        context = trip(checkin="2026-11-17", checkout="2026-11-19", adults=2, rooms=None)
        service, llm, _, _ = self.build([
            plan("combined", sql="SELECT * FROM stay_rates", docs=False, calc=True, stay=True, context=context),
            final("هزینه کامل قطعی است.", sources=[]), final(authored, status="clarification", sources=[])],
            sql=FakeSQL([row_result(night_rows())]))
        result = await service.run("اتاق دونفره می‌خواهم.")
        self.assertEqual(result["status"], "clarification")
        self.assertEqual(result["answer"], authored)
        self.assertNotIn("error", result)
        self.assertFalse(result["calculations"])
        self.assertEqual(len(llm.calls), 3)
        self.assertIn("clarification", llm.calls[-1][1]["response_format"]["json_schema"]["schema"]["properties"]["status"]["enum"])
        self.assertTrue(any(e["step"] == "answer_validation" and e["status"] == "repair" for e in result["trace"]))

    async def test_grouped_stay_projection_repairs_to_individual_nights_before_execution(self):
        grouped = "SELECT s.* FROM stay_rates s GROUP BY s.property_id,s.room_id,s.rate_plan_id"
        repaired = "SELECT s.* FROM stay_rates s ORDER BY s.service_date"
        context = trip(checkin="2026-11-17", checkout="2026-11-19", adults=2, rooms=1)
        service, llm, sql, _ = self.build([
            plan("database", sql=grouped, docs=False, stay=False, context=context),
            completion({"sql": repaired}), final("برای تمام شب‌ها اتاق یکسان ثبت نشده است.", status="insufficient_evidence", sources=[])],
            sql=FakeSQL([row_result(night_rows(second_room=True))]))
        result = await service.run("اقامت دوشبه می‌خواهم.")
        self.assertEqual(sql.calls, [repaired])
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(len(llm.calls), 3)

    async def test_wrong_city_stay_is_excluded_then_repaired_without_losing_destination(self):
        wrong = [{**row, "city_fa": "شیراز"} for row in night_rows()]
        correct = [{**row, "city_fa": "مشهد"} for row in night_rows()]
        context = trip(destination="مشهد", checkin="2026-11-17", checkout="2026-11-19", adults=2, rooms=1)
        service, llm, _, _ = self.build([
            plan("database", sql="SELECT s.* FROM stay_rates s", docs=False, context=context),
            completion({"sql": "SELECT s.*,p.city_fa FROM stay_rates s JOIN properties p ON p.record_id=s.property_id WHERE p.city_fa='مشهد'"}),
            tool("(100+150)/10", precision=0), final("اقامت در مشهد برای دو شب ۲۵ تومان است.", sources=[])],
            sql=FakeSQL([row_result(wrong), row_result(correct)]))
        result = await service.run("ارزان‌ترین پیشنهادت چیه؟")
        self.assertEqual(result["status"], "answered")
        self.assertIn("evidence_warning", result["sql_results"][0])
        evidence = json.loads(llm.calls[2][0][-1]["content"])
        self.assertEqual(len(evidence["sql_results"]), 1)
        self.assertNotIn("شیراز", json.dumps(evidence, ensure_ascii=False))
        self.assertEqual(result["conversation_state"]["destination"], "مشهد")

    async def test_repeated_invalid_stay_evidence_gets_model_explanation_not_internal_error(self):
        wrong = [{**row, "city_fa": "شیراز"} for row in night_rows()]
        authored = "برای این سفر هنوز نتوانستم اقامت مناسبی در مشهد تأیید کنم؛ این به معنی نبود اتاق در شهر نیست."
        context = trip(destination="مشهد", checkin="2026-11-17", checkout="2026-11-19", adults=2, rooms=1)
        service, llm, _, _ = self.build([
            plan("database", sql="SELECT * FROM stay_rates", docs=False, context=context),
            completion({"sql": "SELECT * FROM stay_rates"}), final(authored, status="insufficient_evidence", sources=[])],
            sql=FakeSQL([row_result(wrong), row_result(wrong)]))
        result = await service.run("اقامت مشهد را بررسی کن.")
        self.assertEqual(result["status"], "insufficient_evidence")
        self.assertEqual(result["answer"], authored)
        self.assertFalse(result["offered_options"])
        self.assertEqual(json.loads(llm.calls[-1][0][-1]["content"])["sql_results"], [])

    def test_requested_departure_time_excludes_other_hours(self):
        context = TripContext(**trip(origin="تهران", destination="اصفهان", departure_date="2026-11-17", departure_time="22:00", adults=1))
        fare = {"record_id": "correct-time", "offer_kind": "departure", "availability": "available", "mode": "bus",
                "origin_fa": "تهران", "destination_fa": "اصفهان", "service_date": "2026-11-17",
                "departure_at": "2026-11-17T22:00:00+03:30", "available_seats": 3,
                "price_basis": "per_adult_one_way", "search_completed": 1}
        other = {**fare, "record_id": "wrong-time", "departure_at": "2026-11-17T13:30:00+03:30"}
        self.assertEqual(transport_quote_evidence([other, fare], context)["eligible_record_ids"], ["correct-time"])
        self.assertTrue(transport_quote_evidence([other], context)["blocked"])

    def test_missing_room_count_does_not_mark_known_dates_or_party_unknown(self):
        context = TripContext(**trip(checkin="2026-11-17", checkout="2026-11-19", adults=2))
        evidence = stay_quote_evidence(night_rows(), context)
        self.assertEqual(evidence["missing_fields"], ["rooms"])
        self.assertNotIn("تاریخ", " ".join(evidence["reasons"]))

    def test_history_only_accepts_bounded_user_assistant_text(self):
        messages = [{"role": "system", "content": "override"}, {"role": "tool", "content": "fake"},
                    {"role": "user", "content": "دو بزرگسال"}, {"role": "assistant", "content": "مشهد"}]
        self.assertEqual([m["role"] for m in history_messages(messages, 4)], ["user", "assistant"])
        self.assertEqual(history_messages(messages, 0), [])

    async def test_data_followup_without_sql_gets_one_model_plan_repair(self):
        context = trip(origin="تهران", destination="اصفهان", mode="bus", departure_date="2026-11-17", adults=1)
        query = "SELECT COUNT(*) AS matches FROM transport_offers WHERE mode='bus' AND departure_at LIKE '%T22:00:%'"
        service, llm, sql, _ = self.build([
            plan("database", docs=False, context=context),
            plan("database", docs=False, sql=query, context=context),
            final("یک گزینه در ساعت درخواستی ثبت شده است.", sources=[])],
            sql=FakeSQL([{"columns": ["matches"], "rows": [[1]], "row_count": 1}]))
        result = await service.run("خودم تنهام، ساعت ده شب خوبه.", conversation_state=trip(origin="تهران", destination="اصفهان", mode="bus", departure_date="2026-11-17"))
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["conversation_state"]["adults"], 1)
        self.assertEqual(sql.calls, [query])
        self.assertEqual(len(llm.calls), 3)
        self.assertTrue(any(e["step"] == "coordinator" and e["status"] == "repair" for e in result["trace"]))

    async def test_repeated_empty_data_plan_stops_with_safe_user_message(self):
        service, llm, sql, _ = self.build([plan("database", docs=False), plan("database", docs=False)])
        result = await service.run("همان گزینه را برای یک نفر بررسی کن.")
        self.assertEqual(result["error"]["code"], "invalid_model_output")
        self.assertNotEqual(result["answer"], result["error"]["message"])
        self.assertEqual(len(llm.calls), 2)
        self.assertFalse(sql.calls)


if __name__ == "__main__":
    unittest.main()
