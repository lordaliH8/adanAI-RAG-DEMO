"""Idempotent raw-source initialization: migrations, ETL, PDF processing and indexing."""
from __future__ import annotations

import asyncio
import json
import logging
import time

from app.config import get_settings
from app.etl import initialize_database
from app.llm import LLMClient
from app.retrieval import Retriever
from app.storage import write_json

LOG = logging.getLogger("hamsafar.startup")


async def initialize(settings, llm, retriever, *, force=False):
    started = time.perf_counter()
    LOG.info("stage=relational_ingestion status=started")
    database = await asyncio.to_thread(initialize_database, settings, force=force)
    LOG.info("stage=relational_ingestion status=completed records=%s duplicates=%s cache_hit=%s",
             database["records"], database["duplicates_removed"], database["cache_hit"])
    LOG.info("stage=document_ingestion status=started")
    documents = await retriever.initialize(force=force)
    LOG.info("stage=document_ingestion status=completed pages=%s chunks=%s cache_hit=%s",
             documents["pages"], documents["chunks"], documents["cache_hit"])
    report = {"ready": True, "as_of": settings.as_of, "database": database, "documents": documents,
              "duration_ms": round((time.perf_counter() - started) * 1000, 2)}
    write_json(settings.state_dir / "startup.json", report)
    return report


async def main():
    settings = get_settings()
    client = LLMClient(settings)
    retriever = Retriever(settings, client)
    try:
        await initialize(settings, client, retriever)
    finally:
        retriever.close()
        await client.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    try:
        asyncio.run(main())
    except Exception as error:
        # Deliberately do not print provider bodies, request objects or credentials.
        LOG.error("Initialization failed: %s. Verify inputs, provider configuration and service availability.", type(error).__name__)
        raise SystemExit(1)
