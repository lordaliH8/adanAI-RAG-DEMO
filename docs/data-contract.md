# Source and calculation contract

## Inputs and provenance

The original `torob-trip-demo` project is not a runtime dependency. This project
contains its own copy of three provider JSONL exports, coverage observations and
460 response bodies named by SHA-256. The [source manifest](../data/source-manifest.json)
records their file hashes and row counts; the [offline audit](../reports/source-audit.json)
records response-hash verification. URLs and observation times survive ingestion.
The application rebuilds SQLite from these files. It does not consume a
prepopulated database or evaluation answers.

Ingestion separates `inventory_probe` and `collection_status` diagnostics from
business records. Source records contain prices observed on **2026-10-18**;
service dates can be later. The default simulated conversation date is **2026-11-16**. It is independent of the observation date.
`available` describes the earlier observation, not a held room or seat today.
Quotes require source revalidation before booking; the assistant cannot reserve,
pay for or guarantee inventory.

The five application PDFs are source-derived compilations. Cultural text was
captured on October 27; hotel conditions and booking FAQs retain their October 18
provider observations. PDF text is extracted and indexed at startup. The
[bibliography](../data/document-sources.json) supplies factual attribution and
capture metadata, without prewritten answers, query plans or chunks. Cultural
selections omit old quoted prices and schedules; they cannot establish current
travel costs. Provider/property conditions remain specific to their sources.

## Identity and joins

`records` retains `source`, `source_id`, `source_url`, `observed_at`, `data_json`
and `evidence_json`. Internal IDs are the first 24 hexadecimal characters of
`SHA256(source + ':' + entity_type + ':' + source_id)`. A provider's numeric hotel
ID is not the internal hash, and the same source ID on two providers is not an
identity match. Physical-hotel deduplication is not claimed.

`properties.record_id` and `transport_offers.record_id` reference `records.id`.
A nightly rate has two distinct links: `stay_rates.record_id` identifies its own
source offer, while `stay_rates.property_id` references `properties.record_id`.
Room and rate-plan source IDs are stored as strings in the projections; original
JSON values retain their types. Document property filters use the provider
property ID and source. They are not interchangeable with internal SQL IDs.

`coverage` stores collection outcomes separately from inventory. `planned` means
unqueried, `failed` means collection failed and `not_exposed` means the inspected
surface did not expose the requested inventory. `empty` records an empty result
for that source/query, not proof that no real service exists. `observed` can
include sold-out departures or calendar prices; check offer availability
separately. Missing dates, return legs or arrival times are not extrapolated.

## Quote arithmetic and eligibility

All canonical prices are integer **IRR**. Ten rials equal one toman. Some fares
are not divisible by ten; use Decimal arithmetic and avoid early rounding or
integer division. SQL retrieves the relevant facts; the native calculator
performs unit conversion, group totals, savings and budget arithmetic.

A transport price is for one adult and one direction. Exact-service quotes use
`offer_kind='departure'`, match the requested route/date/selected identity and
check observed availability and seats. Required return legs need their own
matching evidence. `daily_fare_aggregate` is a daily floor price without a
confirmed departure time or seat count. Missing child/infant fares are unknown,
not free or proportional to adult fares. A missing bus arrival cannot establish
compatibility with hotel check-in or a connection.

A stay price is for one room and one night. A two-person shared double room is
not charged twice merely because two adults occupy it. A continuous quote needs
every date in `[checkin, checkout)` for the same property, room and rate plan;
the checkout date is not another charged night. Different rooms' nightly minima
do not establish one continuous stay. `inventory` counts rooms and `occupancy`
is base capacity; `extra_capacity` does not establish a free extra bed.

Quote evidence checks returned nightly availability, inventory and base capacity,
known closure/arrival restrictions and known minimum/maximum stay. Checkout-day
restrictions are checked when supplied as evidence. NULL or missing restriction
information remains unspecified; it is not proof of unrestricted booking.
`properties.headline_price_irr` is explicitly undated and cannot replace nightly
rates. Breakfast comes from the selected rate plan, not merely a hotel's
restaurant/amenity list. `cancelable` does not mean cancellation is free; exact
charges may require hotel inquiry. General FAQs do not override selected-fare or
property conditions.

Quote subtotals state their scope: one-way/return transport, selected room nights
and any allowance explicitly provided by the user. Unknown meals, transfers,
return fares and other expenses are excluded with that limitation stated. A
missing component cannot support a complete-trip price. Snapshot price arithmetic
does not establish that a booking can now be completed.

## Attribution and publication

The bibliography identifies Wikimedia-derived cultural selections as
**CC BY-SA 4.0** and retains revision URLs, source/history links, license links
and a changes notice. Preserve those attributions and notices when sharing the
adapted selections. No source images are reproduced in the application PDFs.

Commercial hotel conditions, FAQs and raw provider responses have separate
provenance. Their metadata explicitly says commercial source rights are not
asserted. Public accessibility and attribution do not establish an open license
for the complete provider archive. No blanket redistribution permission is
claimed for those files; review that scope separately before public publication.
This repository's attribution metadata is not a grant of rights over third-party
material.

The Docker allowlist excludes `.env`, credentials, Git history, the private hiring
brief, authoring receipts/tools, evaluation material, reports and generated
state. It **does include** the raw provider JSONL, response bodies and final PDFs
needed for reproducible ingestion. Docker exclusion is therefore not a substitute
for reviewing what a public repository or distributed image contains. Keep
credentials private and retain the distinction between evaluator access and
public redistribution.
