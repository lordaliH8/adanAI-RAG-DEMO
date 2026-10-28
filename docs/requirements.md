# Task requirements and implementation evidence

This mapping covers the private hiring brief. The original brief is intentionally excluded from the repository.

| Requirement | Implementation | Verification |
| --- | --- | --- |
| Small PDFs and meaningful preprocessing | Five attributed PDFs; Poppler extraction, Persian normalization, repeated header/footer removal, page/section chunking | `tests/test_documents.py`, startup report |
| Executable semantic RAG and source attribution | Qdrant cosine embeddings, BM25/RRF, page context, PDF/original-source links in Details | `tests/test_retrieval.py`, `reports/conversation-v2-clarification.json`, `reports/release-scenarios.json` |
| Honest insufficient evidence | Model-authored absence/clarification, bounded correction of unsupported claims; infrastructure errors remain technical | `tests/test_workflow.py`, conversation regression report |
| Related relational tables | Records, properties, dated stay rates, transport offers, coverage and sources; raw-source ingestion on first run | `app/schema.py`, `app/etl.py`, `tests/test_ingestion.py` |
| LLM-generated filtering, aggregation and joins | Schema-grounded coordinator SQL; no question-to-SQL mapping | `reports/conversation-v2-live.json`, `reports/release-scenarios.json` |
| Read-only SQL and controlled errors | AST allowlist, read-only URI/query_only/authorizer, resource bounds, one safe repair | `tests/test_sql_guard.py`, `reports/evaluation-v2-offline.json` |
| Coordinator and independent specialists | LangGraph document and database nodes with separate retrieval/SQL services | `app/workflow.py`, per-response trace |
| Real calculator and multi-step example | Native model tool calls to bounded Decimal AST calculator using SQL outputs | `tests/test_calculator.py`, `reports/conversation-v2-live.json` |
| Inspect SQL, result, agents and tools | Persisted SSE events, actual prompts/calls/results, per-message English inspector | `frontend/src/Inspector.jsx`, `tests/test_conversations.py` |
| Lightweight API and input validation | FastAPI/Pydantic, chat/SSE and persistent conversation endpoints, Swagger | `tests/test_api.py` |
| Docker with reproducible inputs/setup | Locked dependencies, pinned base images, raw files/PDFs in runtime, automatic ETL/indexing, persistent volumes | Dockerfiles, `compose.yaml`, clean-clone release report |
| Examples and tests | Two RAG questions, SQL filter/join and aggregate questions, calculator follow-up, unavailable information and unsafe-write examples | README, `evaluation/cases.json`, Docker-only test script |
| Model/API setup and documented limits | OpenAI/AvalAI/custom profiles, placeholders, provider-safe HTTP contract tests | `.env.example`, `tests/test_config.py`, `tests/test_provider_http.py` |

Live provider conversations, mocked HTTP contract checks and injected guard/failure checks are distinct evidence. Official OpenAI transport is covered by installed-SDK HTTP tests; no live official OpenAI credential was available. Passing a mechanical check is not a claim of perfect semantic answer correctness. See the release verification report for the exact executed scope.
