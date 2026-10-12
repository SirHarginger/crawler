# ADR: Crawler/engine responsibility boundary

## Context

Earlier revisions of this repository ran a pure `Projector` step that turned each parsed
`SourceRecord` into a FollowTheMoney entity with property-level provenance
(`ProjectionStatement`), via per-dataset `*ProjectionProfileV1` classes. That logic lived in the
same repository as acquisition and parsing, and had no access to any other source's data, so every
dataset's projection rules were necessarily local and non-shareable — even though PEP
classification, sanctions interpretation, and entity resolution are fundamentally cross-source
concerns.

## Decision

This repository stops at `RawRecord -> Release`. It answers exactly one question — "what did a
source publish, and when" — and never classifies PEP status, interprets sanctions designations,
does entity resolution, calls Yente, or owns any part of the Knowledge Layer. `screening-engine`
owns everything from ingestion through the Knowledge Layer: identity, PEP, sanctions, evidence,
provenance, indexing, Yente, screening.

## Why

- Semantic interpretation (is this person a PEP, is this designation active, is this the same
  entity as an existing record) requires cross-source context this repository, by design, never
  has — one source at a time, no database.
- Keeping the crawler acquisition-only means one broken interpretation rule can never block a
  publisher's data from being captured and preserved; interpretation can be re-run without
  re-crawling.
- A `RawRecord`'s `payload` carries the full native dict, so nothing is lost by deferring
  interpretation — it can be added, fixed, or extended in the engine without touching this repo.

## Alternatives considered

- **Keep projection in the crawler, move only classification to the engine.** Rejected: this
  still means "interpretation" is split across a repo boundary partway through, and the crawler
  would still need FollowTheMoney/entity-schema knowledge it has no legitimate use for otherwise
  (see [docs/architecture.md](../architecture.md)'s system invariants — this repo holds no
  database or search-index credentials by design, and entity-schema modeling only matters once
  something is prepared to store and query entities).
- **A shared library for projection, imported by both repos.** Rejected for this pass: it
  reintroduces exactly the coupling this boundary is meant to remove, and the two repos already
  have a well-defined contract (`RawRecord`) that doesn't require sharing code, only a schema.

## Amendment (2026-10-11): the research egress

Owner decision. Live web access for an investigation is approved, and it belongs on this side
of the boundary: `screening_crawler.research` ([docs/research-egress.md](../research-egress.md))
is the only place such search and fetches happen. It is acquisition only. It holds no database
or search-index credentials, classifies nothing and resolves no identity; it returns bounded
plain text with provenance to the engine, which owns interpretation. It does not produce
`RawRecord`s or releases, and it is optional and off by default.

## Consequences

- Every dataset's `crawler.py` lost its `*ProjectionProfileV1` class; only the parser and
  `SourceConfig` remain.
- `screening-engine` must independently implement a normalizer per source to go from `RawRecord`
  to Knowledge Layer facts — this is real, non-duplicated work, not eliminated by this decision,
  just relocated to where cross-source context actually exists.
- Only `un_sc_consolidated` and `gh_parliament` have an engine-side normalizer as of this pass;
  every other dataset produces valid `RawRecord`s with no consumer yet wired up. This is a known,
  explicitly tracked limitation, not a silent gap.
