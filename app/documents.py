"""Extract raw PDFs with Poppler and build bounded, page-aware section chunks."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

import tiktoken

from app.normalization import normalize_text

PIPELINE_VERSION = "travel-pdf-v2-source-scoped-timestamps"
DOC_ID = re.compile(r"\b[A-Z]{2,}-\d{4}-V\d+\b")
SECTION = re.compile(r"\b[A-Z]{2,}-\d{2}(?!\d)\b")
DOCUMENT_TYPES = {"MASHHAD": "city_guide", "SHIRAZ": "city_guide", "ISFAHAN": "city_guide",
                  "STAY": "accommodation_policies", "FAQ": "booking_faq"}
CITY_BY_PREFIX = {"MASHHAD": "مشهد", "SHIRAZ": "شیراز", "ISFAHAN": "اصفهان", "STAY": "مشهد"}
PUBLISHERS = {"ویکی سفر": "wikimedia", "ویکی پدیا": "wikimedia", "اقامت بیست و چهار": "eghamat24", "علی بابا": "alibaba"}


class DocumentError(RuntimeError):
    pass


def extract_pdf(path: Path, executable: str) -> list[str]:
    try:
        result = subprocess.run([executable, "-enc", "UTF-8", str(path), "-"], capture_output=True,
                                text=True, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        raise DocumentError("PDF extraction failed; install Poppler and verify the PDF input.") from None
    pages = result.stdout.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    if not pages or any(len(page.strip()) < 100 for page in pages):
        raise DocumentError("A PDF page has no useful text layer; scanned PDFs need OCR before ingestion.")
    return pages


def clean_lines(page: str) -> list[str]:
    lines = [normalize_text(line, indexing=True) for line in page.splitlines()]
    # Chromium's lam-alef glyph order maps narrowly to these forms in the
    # otherwise readable Poppler text layer. Preserve Unicode word order.
    repairs = {"استعالم": "استعلام", "معموال": "معمولا"}
    for wrong, right in repairs.items():
        lines = [re.sub(r"\b" + wrong + r"\b", right, line) for line in lines]
    return lines


def metadata(path: Path, pages: list[str]) -> dict:
    lines = [line for line in clean_lines(pages[0]) if line]
    first = "\n".join(lines)
    match = DOC_ID.search(first[:800])
    document_id = match.group() if match else path.stem
    prefix = document_id.split("-")[0]
    # The sidecar contains factual bibliographic metadata only. Searchable text
    # always comes from Poppler, never the authoring sources or stored chunks.
    source_manifest = path.parent.parent / "document-sources.json"
    provenance = {}
    if source_manifest.exists():
        try:
            documents = json.loads(source_manifest.read_text(encoding="utf-8"))["documents"]
            provenance = next((d for d in documents if d.get("filename") == path.name), {})
        except (OSError, ValueError, KeyError, TypeError):
            raise DocumentError("Invalid document provenance manifest.") from None
        if provenance and provenance.get("id") != document_id:
            raise DocumentError("PDF identity disagrees with the document provenance manifest.")
    titles = [line for line in lines[:16] if len(line) > 12 and not DOC_ID.fullmatch(line)
              and not any(word in line for word in ["گردآوری", "منبع", "صفحه", "گزیده"])
              and not SECTION.search(line) and not re.search(r"[A-Za-z]", line)]
    title = titles[0] if titles else path.stem
    observed_at = provenance.get("observed_at")
    return {"document_id": document_id, "title": provenance.get("title", title), "filename": path.name,
            "document_type": provenance.get("document_type", DOCUMENT_TYPES.get(prefix, "other")),
            "city": provenance.get("city", CITY_BY_PREFIX.get(prefix)),
            "source": provenance.get("source", "unknown"), "source_urls": provenance.get("source_urls", []),
            "source_url": next(iter(provenance.get("source_urls", [])), None),
            "property_ids": [str(v) for v in provenance.get("property_ids", [])],
            "property_id": None, "observed_at": observed_at,
            "capture_date": observed_at[:10] if observed_at else None,
            "compiled_at": provenance.get("compiled_at"), "license": provenance.get("license"),
            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def chunk_pdf(path: Path, settings) -> tuple[list[dict], dict]:
    pages = extract_pdf(path, settings.pdftotext_bin)
    info = metadata(path, pages)
    per_page_lines = [clean_lines(page) for page in pages]
    frequencies = Counter(line for lines in per_page_lines for line in set(lines) if line)
    repeated = {line for line, count in frequencies.items() if count >= max(2, len(pages) - 1)
                and len(line) < 180 and not SECTION.search(line)}
    encoding = tiktoken.get_encoding("cl100k_base")
    chunks, removed = [], 0
    for page_number, lines in enumerate(per_page_lines, 1):
        page_info = dict(info)
        heading_index = next((i for i, line in enumerate(lines[:18])
                              if SECTION.search(line) and "|" in line), 12)
        page_context = " ".join(lines[:heading_index])
        property_match = re.search(r"شناسه هتل\s*[:|]?\s*(\d+)", page_context)
        page_info["property_id"] = property_match.group(1) if property_match else None
        for printed, provider in PUBLISHERS.items():
            if printed in page_context:
                page_info["source"] = provider
                break
        capture_match = re.search(r"ثبت منبع\s*:\s*(\d{4}-\d{2}-\d{2})", page_context)
        if capture_match:
            page_info["capture_date"] = capture_match.group(1)
        # Bibliographic sources distinguish provider profiles of the same hotel.
        manifest = path.parent.parent / "document-sources.json"
        if manifest.exists():
            doc = next((d for d in json.loads(manifest.read_text())["documents"] if d["filename"] == path.name), {})
            selected_source = None
            candidates = [s for s in doc.get("sources", [])
                          if s.get("provider") == page_info["source"]
                          and (not page_info["property_id"] or str(s.get("property_id")) == page_info["property_id"])]
            if candidates:
                selected_source = candidates[0]
            candidates = [s for s in doc.get("sources", [])
                          if ("؛ " + normalize_text(s.get("title", ""))) in page_context]
            if candidates:
                selected_source = candidates[0]
            if selected_source:
                page_info["source_url"] = selected_source.get("revision_url", selected_source["source_url"])
                observed_at = selected_source.get("observed_at") or selected_source.get("retrieved_at")
                if observed_at:
                    page_info["observed_at"] = observed_at
                    page_info["capture_date"] = observed_at[:10]
        kept = []
        for line in lines:
            if not line:
                kept.append("")
            elif line in repeated or re.match(r"^صفحه \d+ از \d+$", line):
                removed += 1
            else:
                kept.append(line)
        text = "\n".join(kept).strip()
        if "منابع و حقوق استفاده" in text[:200]:
            # Bibliography is preserved in the PDF and provenance manifest, but
            # a credits-only page is not business evidence for answer retrieval.
            continue
        sections = [(index, SECTION.search(line).group()) for index, line in enumerate(kept)
                    if SECTION.search(line) and "|" in line and len(line) < 160]
        if not sections:
            found = SECTION.search(text)
            sections = [(0, found.group() if found else f"PAGE-{page_number}")]
        elif sections[0][0] > 0:
            sections[0] = (0, sections[0][1])
        for section_index, (start, section) in enumerate(sections):
            end = sections[section_index + 1][0] if section_index + 1 < len(sections) else len(kept)
            body = "\n".join(kept[start:end]).strip()
            # Whole extracted lines preserve table-cell adjacency and Unicode
            # characters. Long individual lines are split on word boundaries.
            units = []
            for line in body.splitlines():
                if len(encoding.encode(line)) <= settings.chunk_max_tokens:
                    units.append(line)
                else:
                    current = []
                    for word in line.split():
                        if current and len(encoding.encode(" ".join(current + [word]))) > settings.chunk_max_tokens:
                            units.append(" ".join(current))
                            current = []
                        current.append(word)
                    if current:
                        units.append(" ".join(current))
            max_tokens = settings.chunk_max_tokens
            unit_start = 0
            while unit_start < len(units):
                unit_end = unit_start
                while unit_end < len(units) and len(encoding.encode("\n".join(units[unit_start:unit_end + 1]))) <= max_tokens:
                    unit_end += 1
                unit_end = max(unit_end, unit_start + 1)
                piece = "\n".join(units[unit_start:unit_end]).strip()
                if len(piece) < 25:
                    unit_start = unit_end
                    continue
                identity = hashlib.sha256(f'{info["source_sha256"]}:{page_number}:{section}:{unit_start}'.encode()).hexdigest()
                chunks.append({**page_info, "chunk_id": identity, "page": page_number, "section": section,
                               "text": piece, "token_count": len(encoding.encode(piece)),
                               "embedding_text": f'{info["title"]}\n{section}\n{piece}'})
                if unit_end >= len(units):
                    break
                overlap_start = unit_end
                while overlap_start > unit_start + 1 and len(encoding.encode("\n".join(units[overlap_start:unit_end]))) < settings.chunk_overlap_tokens:
                    overlap_start -= 1
                unit_start = max(unit_start + 1, overlap_start)
    return chunks, {"filename": path.name, "document_id": info["document_id"], "pages": len(pages),
                    "chunks": len(chunks), "repeated_lines_removed": removed,
                    "raw_characters": sum(len(page) for page in pages),
                    "city": info["city"], "document_type": info["document_type"],
                    "source": info["source"], "capture_date": info["capture_date"]}
