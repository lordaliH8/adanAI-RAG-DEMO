import json
from pathlib import Path

import pytest

from app.config import Settings
from app.retrieval import Retriever


class Embeddings:
    def __init__(self):
        self.calls = []
        self.dimensions = 3

    async def embed(self, texts):
        self.calls.append(list(texts))
        return [[1.0] + [0.0] * (self.dimensions - 1) for _ in texts]


def make_chunks(path, settings):
    first = path.stem == "mashhad"
    row = {"chunk_id": path.stem, "document_id": "MASHHAD" if first else "SHIRAZ", "title": "راهنمای شهر",
           "filename": path.name, "document_type": "city_guide", "source_sha256": "test",
           "page": 1, "section": "GUIDE-01", "text": "دیدنی مشهد" if first else "دیدنی شیراز",
           "embedding_text": "راهنمای شهر مشهد" if first else "راهنمای شهر شیراز",
           "city": "مشهد" if first else "شیراز", "source": "wikivoyage", "property_id": None,
           "token_count": 10}
    return [row], {"pages": 1, "filename": path.name}


@pytest.mark.asyncio
async def test_vector_population_caching_and_city_prefilter(tmp_path, monkeypatch):
    inputs = tmp_path / "data/documents"
    inputs.mkdir(parents=True)
    (inputs / "mashhad.pdf").write_bytes(b"Mashhad test source")
    (inputs / "shiraz.pdf").write_bytes(b"Shiraz test source")
    settings = Settings(_env_file=None, data_dir=tmp_path / "data", state_dir=tmp_path / "state", embedding_dimensions=3)
    monkeypatch.setattr("app.retrieval.chunk_pdf", make_chunks)
    llm = Embeddings()
    service = Retriever(settings, llm)
    try:
        first = await service.initialize()
        assert first["chunks"] == 2 and first["embeddings_created"] == 2
        warm = await service.initialize()
        assert warm["cache_hit"] and warm["embeddings_created"] == 0 and warm["embedding_cache_hits"] == 2
        assert len(llm.calls) == 1
        results = await service.search("راهنما", mode="dense", filters={"city": "مشهد"})
        assert [r["document_id"] for r in results["results"]] == ["MASHHAD"]
        results = await service.search("راهنما", mode="dense", filters={"city": "شیراز"})
        assert [r["document_id"] for r in results["results"]] == ["SHIRAZ"]
        # Cache fingerprints change on raw source edits, but unchanged text does
        # not need a second embedding provider call.
        (inputs / "shiraz.pdf").write_bytes(b"Shiraz edited test source")
        calls = len(llm.calls)
        rebuilt = await service.initialize()
        assert not rebuilt["cache_hit"] and rebuilt["embeddings_created"] == 0
        assert len(llm.calls) == calls
        old_target = service.client.get_aliases().aliases[0].collection_name
        # A failed forced rebuild must not delete the currently aliased index.
        original = service.client.upsert
        def broken_upsert(*args, **kwargs):
            raise RuntimeError("simulated index write failure")
        monkeypatch.setattr(service.client, "upsert", broken_upsert)
        with pytest.raises(RuntimeError):
            await service.initialize(force=True)
        assert service.client.get_aliases().aliases[0].collection_name == old_target
        assert service.client.get_collection(old_target).points_count == 2
        monkeypatch.setattr(service.client, "upsert", original)
        (settings.state_dir / "document-ingestion.json").write_text("{interrupted")
        settings.chunks_path.write_text("[interrupted")
        (settings.state_dir / "embedding-cache.json").write_text("{interrupted")
        recovered = await service.initialize()
        assert recovered["embeddings_created"] == 2
        assert service.client.get_collection(settings.qdrant_collection).points_count == 2
        # Changing embedding configuration must not silently reuse old vectors.
        settings.embedding_dimensions = llm.dimensions = 4
        changed = await service.initialize()
        assert changed["embeddings_created"] == 2
        assert service.client.get_collection(settings.qdrant_collection).config.params.vectors.size == 4
    finally:
        service.close()


def test_filter_rules_are_shared_with_lexical_candidates():
    mashhad, _ = make_chunks(Path("mashhad.pdf"), None)
    shiraz, _ = make_chunks(Path("shiraz.pdf"), None)
    filters = {"city": "مشهد", "document_type": "city_guide", "source": "wikivoyage"}
    assert Retriever._matches(mashhad[0], filters)
    assert not Retriever._matches(shiraz[0], filters)
    assert not Retriever._matches(mashhad[0], {"property_id": "1242"})


def test_page_expansion_keeps_table_context_and_provenance_without_crossing_pages():
    service = object.__new__(Retriever)
    base = make_chunks(Path("shiraz.pdf"), None)[0][0]
    first = {**base, "chunk_id": "part-a", "text": "عنوان\nنرخ روزانه 2 درصد\nسقف 10 درصد"}
    second = {**base, "chunk_id": "part-b", "text": "سقف 10 درصد\nلغو و تغییر زمان مستثناست"}
    service.chunks = [first, second, {**base, "page": 2, "chunk_id": "other-page", "text": "صفحه دیگر"},
                      {**base, "document_id": "OTHER", "chunk_id": "other-doc", "text": "سند دیگر"}]
    context = service._page_context(second, {"city": "شیراز"})
    assert context["chunk_id"] == "part-b" and context["matched_text"] == second["text"]
    assert context["context_chunk_ids"] == ["part-a", "part-b"]
    assert context["text"] == "عنوان\nنرخ روزانه 2 درصد\nسقف 10 درصد\nلغو و تغییر زمان مستثناست"
    assert context["context_scope"] == "physical_page"
