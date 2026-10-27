# Persian travel document authoring

The five application PDFs are source-derived compilations of actual Persian
travel text and captured hotel/booking conditions. They contain no fictional
store rules, QA answers, agent instructions, database schema, or model prompts.

`sources/` holds pinned HTML receipts, extracted source text, exact Wikimedia
revision URLs, hashes, and selected provider records captured on 2026-10-18.
The Wikimedia city guides were selected on 2026-10-27. `travel-documents.json`
is a reproducible authoring specification; `manifest.json` records PDF hashes,
page count and measured layout. None is an application ingestion shortcut.

Rebuild offline after installing the authoring-only browser dependency:

```sh
npm --prefix tools/data-authoring install
npm --prefix tools/data-authoring run browser:install
python scripts/build_travel_documents.py
```

The builder writes `output/pdf/`, copies final PDFs to `data/documents/`, and
updates the factual `data/document-sources.json` bibliography. Native Chromium
Arabic shaping preserves readable RTL and the Unicode text layer. Each chapter
flows to continuation pages if required; overflow fails the build.

Wikimedia text is selected, rearranged and lightly edited under CC BY-SA 4.0.
The PDFs retain revision/history URLs, the license URL and a changes notice.
Images are omitted. Commercial source rights are not asserted; selected hotel
conditions and FAQs are short attributed factual compilations, with the source
capture date and hotel-specific scope visible. Exact cancellation charges remain
unknown where the source requires hotel inquiry. General FAQs do not override
ticket-specific conditions.

Cross-checks used for notable facts: UNESCO's Meidan Emam listing
<https://whc.unesco.org/en/list/115/>, its Persian Garden listing
<https://whc.unesco.org/en/list/1372/>, and VisitIran's Ferdowsi tomb description
<https://visitiran.ir/fa/attraction/آرامگاه-حکیم-ابوالقاسم-فردوسی>.
These were factual cross-checks; their photographs and full articles were not
reproduced. Old prices, transport schedules, live access promises and legal
assertions were excluded from the city selections.

The application runs Poppler against the PDFs, normalizes Persian characters,
removes repeated page furniture, and creates bounded page/section chunks.
Provider, hotel, city and capture-date provenance are bibliographic metadata;
no prewritten answers or retrieval plans are supplied.
