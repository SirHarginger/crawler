# Source onboarding checklist

## Authority and legal review

- Confirm the primary publisher, authority, jurisdiction, legal basis, risk semantics, and plain-language treatment.
- Confirm the current official endpoint, format, redirect behavior, cadence, publication-date semantics, and redistribution basis.
- Classify distribution as public, restricted, or internal-only. Record tenant entitlement requirements outside this public repository.
- Define freshness SLO, anomaly threshold, expected completeness signal, and failure escalation owner.

## Acquisition

- Define single-file, cursor, page-count, index-link, multipart, archive, HTML, PDF, or manual-receipt behavior.
- Retain raw bytes exactly, including compressed archives and scans.
- Capture request/response metadata and publisher date separately from retrieval time.
- Define retry idempotency, conditional request behavior, duplicate suppression, maximum sizes, and archive expansion bounds.
- Prove a partial acquisition cannot produce `COMPLETE`.

## Extraction and accounting

- Implement a versioned, preferably streaming parser with stable native keys and locators.
- Record reliable raw or decompressed extents only when byte fidelity can be proved; otherwise retain the complete artifact and stable locator.
- Preserve unexpected fields so field accounting can quarantine them.
- Add `fields.yml` covering all paths, types, cardinalities, enumerations, dispositions, reasons, and coverage expectations.
- Add source-specific validation, duplicate-key conflict handling, volume anomaly checks, and format-drift fixtures.

## Interpretation and projection

- Document whether Layer 1.5 is justified. Keep it off for flat sources.
- Map every alias independently and retain original-script names.
- Derive IDs from publisher identifiers, never random values or name similarity.
- Label published, normalized, derived, and inferred values and emit property provenance.
- Model PEPs as Person, Position, and Occupancy with bitemporal dates and precision. Model relatives and close associates separately as RCA and never infer family links from surnames.
- Prove projection has no network, clock, environment, database, or unstable iteration dependency.

## Test and release

- Use only synthetic or legally safe fixtures.
- Cover single-file, paginated, incomplete, multipart, duplicate, conflicting, unknown path/enum, type/cardinality/coverage drift, aliases, normalization, provenance, regeneration, restriction, checksums, atomicity, and schema validation.
- Build a non-active release and review the snapshot manifest, field coverage, domain objects, entities, provenance, validation report, and checksum inventory.
- Add a new collection version in screening-engine. Never silently modify an existing tenant collection.

## Current operational blockers

The attached system specification leaves the current official endpoint and format for each source unresolved. Restricted-advisory redistribution and tenant entitlement are also unresolved. An adapter is not operationally complete until those facts are confirmed from primary authorities.

`un_sc_consolidated`'s endpoint is a partial exception: `source_endpoint` in its `metadata.yml` is now set to a URL verified live (element names matching this dataset's `fields.yml`, including `DATAID`, `UN_LIST_TYPE`, and the `INDIVIDUAL_*` group). That verification only observed `INDIVIDUALS` records; `ENTITIES` records were not confirmed against `fields.yml` and should be checked before assuming entity coverage is complete. Every other dataset's endpoint remains unconfirmed, and `un_sc_consolidated` otherwise still relies on its synthetic fixture for day-to-day development.

A live HTTP transport (`screening_crawler.transport.HttpFetchTransport`) and a `crawl-dataset` CLI command now exist, so once an endpoint is confirmed, wiring it in is a one-line `metadata.yml` change rather than new acquisition code. The blocker was never the transport; it is, and remains, confirming each publisher's current official endpoint and format.

