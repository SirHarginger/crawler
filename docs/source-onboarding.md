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

`un_sc_consolidated` is no longer a partial exception: as of 2026-08-07 a full, real live crawl against `https://scsanctions.un.org/resources/xml/en/consolidated.xml` has been executed end-to-end (not just the endpoint reachability check recorded earlier) and independently verified against `screening-engine`'s own `ReleaseVerifier` -- 736 `INDIVIDUALS` and 275 `ENTITIES` records both confirmed against a rewritten `fields.yml`, zero quarantine. Every other dataset's endpoint remains unconfirmed, and every dataset other than `un_sc_consolidated` still relies on its synthetic fixture for day-to-day development.

A live HTTP transport (`screening_crawler.transport.HttpFetchTransport`) and a `crawl-dataset` CLI command exist for a single ad-hoc live fetch. **For any source with a confirmed `source_endpoint`, the standard operational entrypoint is now the common run engine** (`screening_crawler.run.run_source`, invoked via `crawler crawl <key>` or `crawler crawl --all`) documented in `docs/architecture.md`'s "Live crawler run engine" section -- it is what actually owns run lifecycle state, conditional-HTTP reuse, duplicate-run locking, release-to-release diffing, and the statistical quarantine gate. `crawl-dataset` remains available underneath as the lower-level one-shot primitive `run_source` itself is built on, not a competing path. The blocker for onboarding a new source was never the transport or the run engine; it is, and remains, confirming each publisher's current official endpoint and format.

