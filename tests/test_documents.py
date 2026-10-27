import json
import os
import shutil
from pathlib import Path

import pytest

from app.config import Settings
from app.documents import DocumentError, chunk_pdf, metadata
from app.normalization import normalize_text

ROOT = Path(__file__).resolve().parents[1]
EXECUTABLE = os.getenv("PDFTOTEXT_BIN") or shutil.which("pdftotext")
EXPECTED = {
    "mashhad-culture.pdf": ("MASHHAD-2026-V1", "مشهد", "city_guide"),
    "shiraz-culture.pdf": ("SHIRAZ-2026-V1", "شیراز", "city_guide"),
    "isfahan-culture.pdf": ("ISFAHAN-2026-V1", "اصفهان", "city_guide"),
    "mashhad-stay-conditions.pdf": ("STAY-2026-V1", "مشهد", "accommodation_policies"),
    "booking-faq.pdf": ("FAQ-2026-V1", None, "booking_faq"),
}


def test_pdf_normalization_preserves_code_and_date_boundaries():
    assert normalize_text("STAY-01ورود", indexing=True) == "STAY-01 ورود"
    assert normalize_text("2026-10-18ثبت", indexing=True) == "2026-10-18 ثبت"
    assert normalize_text("علي‌بابا كودك ۱۲", indexing=True) == "علی بابا کودک 12"


@pytest.mark.skipif(not EXECUTABLE, reason="Poppler required for actual PDF extraction")
@pytest.mark.parametrize("filename,expected", EXPECTED.items())
def test_actual_travel_pdf_has_bounded_text_and_factual_provenance(filename, expected):
    settings = Settings(_env_file=None, pdftotext_bin=EXECUTABLE)
    chunks, report = chunk_pdf(ROOT / "data/documents" / filename, settings)
    assert chunks and report["pages"] >= 2
    assert report["repeated_lines_removed"] > 0
    assert (report["document_id"], report["city"], report["document_type"]) == expected
    assert all(c["token_count"] <= settings.chunk_max_tokens and "�" not in c["text"] for c in chunks)
    assert all(c["section"] != "PAGE-1" for c in chunks)
    assert all(c["source_url"] and c["capture_date"] for c in chunks)
    assert len({c["chunk_id"] for c in chunks}) == len(chunks)
    obsolete_fields = {"categories", "effective_from", "effective_to", "effective_from_day", "effective_to_day"}
    assert not obsolete_fields.intersection(report)
    assert all(not obsolete_fields.intersection(c) for c in chunks)
    assert not any("منابع و حقوق استفاده" in c["text"][:100] for c in chunks)


@pytest.mark.skipif(not EXECUTABLE, reason="Poppler required for actual PDF extraction")
def test_hotel_context_does_not_merge_properties_or_provider_profiles():
    chunks, _ = chunk_pdf(ROOT / "data/documents/mashhad-stay-conditions.pdf",
                          Settings(_env_file=None, pdftotext_bin=EXECUTABLE))
    for chunk in chunks:
        expected = {"STAY-01": ("1242", "eghamat24", "Almas_2Hotel", "2026-10-18T09:48:50+00:00"),
                    "STAY-02": ("106", "eghamat24", "ArmaqanHotel", "2026-10-18T09:57:48+00:00"),
                    "STAY-03": ("1242", "alibaba", "mashhad-almas2", "2026-10-18T09:49:18+00:00")}[chunk["section"]]
        assert (chunk["property_id"], chunk["source"]) == expected[:2]
        assert expected[2] in chunk["source_url"]
        assert chunk["observed_at"] == expected[3]
        assert chunk["capture_date"] == "2026-10-18"
    joined = normalize_text(" ".join(c["text"] for c in chunks), indexing=True)
    assert "15:00" in joined and "14:00" in joined and "12:00" in joined
    assert "استعلام از هتل" in joined
    assert "2026-10-18" in joined
    assert "18-10-2026" not in joined


@pytest.mark.skipif(not EXECUTABLE, reason="Poppler required for actual PDF extraction")
def test_bus_faq_cites_bus_page_not_generic_flight_page():
    chunks, _ = chunk_pdf(ROOT / "data/documents/booking-faq.pdf",
                          Settings(_env_file=None, pdftotext_bin=EXECUTABLE))
    assert all(c["source_url"].endswith("/bus-ticket") for c in chunks if c["section"] == "FAQ-02")


def test_provenance_manifest_cannot_change_pdf_identity(tmp_path):
    folder = tmp_path / "documents"
    folder.mkdir()
    (tmp_path / "document-sources.json").write_text(json.dumps({"documents": [
        {"filename": "travel.pdf", "id": "SHIRAZ-2026-V1"}]}))
    with pytest.raises(DocumentError, match="identity"):
        metadata(folder / "travel.pdf", ["MASHHAD-2026-V1\nمشهد؛ راهنمای میراث فرهنگی"])
