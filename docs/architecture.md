# Architecture and decisions

## Responsibility boundary

The crawler terminates at a validated immutable release bundle. `screening-engine` verifies and imports that bundle, owns reference-registry PostgreSQL, builds Elasticsearch indexes, runs yente, and stores tenant screening activity. There is no database client dependency or registry connection configuration in this repository.

## Data and release flow

1. A deployment-supplied transport returns a response envelope and exact body bytes.
2. `Acquirer` calculates SHA-256, writes the body to content-addressed storage, and records transport, version, time, and distribution metadata.
3. `SnapshotAssembler` binds one or more artifacts and requires explicit completeness evidence.
4. A versioned parser reads retained artifacts and emits source-native Layer 1 envelopes. It never claims reserialized XML is byte-identical.
5. Field accounting compares every observed leaf path, type, enumeration, cardinality, and coverage with `fields.yml`. Safety findings quarantine.
6. An optional deterministic interpreter emits source-scoped Layer 1.5 objects with Layer 1 referents.
7. A pure projector emits deterministic FollowTheMoney entities and property-level statements. Values are labelled `published`, `normalized`, `derived`, or `inferred`.
8. The release builder stages all files, writes checksums, validates the bundle, and atomically promotes one immutable directory.

## Architecture decisions

### Layer 0 remains authoritative

Publishers issue bytes, not parsed dictionaries. The raw body is retained without newline normalization, decompression replacement, XML reserialization, or character conversion. Parsed representations can be regenerated when parser knowledge improves.

### Content-addressed storage is abstract

Core code depends on `BlobStore`. `LocalBlobStore` supplies one safe first deployment and `MemoryBlobStore` supplies tests. A private object-store backend can implement the same verified put/get/exists contract without changing acquisition, parsing, or releases.

### Completeness is evidence, not inference

Parse success and record count do not prove a paginated or multipart source is complete. State transition guards enforce this independently of individual adapters.

### Publisher keys define source identity

Source record and entity IDs derive from source key, record kind, publisher-native key, and snapshot where appropriate. Random IDs are not used for stable publisher records. Name similarity never deduplicates or merges source data.

### Layer 1.5 is opt-in

An interpreter must declare a version and written justification. It remains scoped to one source and every object resolves to Layer 1. This preserves meaningful legal structure without creating a cross-source identity registry.

### Projection is pure and lossy by design

Projection accepts stored records and pinned rules. It has no network, clock, database, or environment lookup. Full source semantics remain below it. Generated values cannot masquerade as publisher values because provenance records the native-value hash and derivation metadata.

### Publication is atomic and immutable

The final release path appears only after validation, file and directory fsync, and same-filesystem rename. Existing release IDs are never overwritten. Screening-engine must still verify checksums and schema before import.

### Restricted data stays restricted

Manual receipts create restricted artifacts. Distribution restrictions propagate to the release manifest. Entitlement and redistribution enforcement belong to the private importer and runtime; public CI prevents generated or restricted data from entering this repository.

### Crawler toolkit is bespoke, not `zavod`

The system documentation's technology inventory and repository layout describe the crawler toolkit as `zavod`, adopted as a framework, with source modules written against it. No `zavod` dependency exists in this repository; `pyproject.toml` declares only `jsonschema` and `PyYAML`, and every acquisition, parsing, field-accounting, projection, and release-building primitive is implemented directly in `screening_crawler`.

This is a deliberate divergence, not an oversight. The property-level provenance (`projection_statements`), field-disposition quarantine gates, and atomic immutable release bundle this platform requires go beyond what `zavod` provides out of the box, and building them directly avoided adapting or forking an external framework to get there. The resulting behaviour matches, and in places exceeds, what the documentation promises.

The divergence needs one of two resolutions before the documentation is next revised, because a regulator or auditor may be shown the documentation's claim that `zavod` is in use:

1. Update the system documentation to describe the bespoke toolkit actually in use, or
2. Adopt `zavod` as a dependency and re-platform the existing modules onto it.

(1) is recommended: re-platforming onto `zavod` would mean giving up functionality — quarantine gating, property-level provenance, the PEP assertion lifecycle — that this repository already has and `zavod` does not provide natively. This decision is flagged here for compliance/engineering sign-off rather than made unilaterally.

### PEP-shaped sources get their own adapter contract, not `DatasetAdapter`

A human-assembled PEP source (see `docs/source-onboarding.md` and the Ghana PEP register datasets) is a genuinely different shape from every machine-published source: `PepOccupancyProjector` produces three linked entities per record (Person, Position, Occupancy) via `PepProjectionInput`, where the generic `Projector`/`ProjectionProfile` pair produces exactly one entity per record. Forcing a PEP source through `ProjectionProfile.properties_for()` would be a false uniformity, not a simplification.

PEP-shaped datasets therefore implement `pep.PepSourceAdapter` instead of `datasets.DatasetAdapter`, loaded through `pep.load_pep_adapter` instead of `datasets.load_adapter`, and run through `pipeline.process_pep_snapshot` instead of `pipeline.process_snapshot`. Capture, snapshot assembly, field accounting, and release publication are exactly the same machinery either way — `process_snapshot` and every already-shipped machine-published dataset are untouched by this. Only the parser and projection step differ, because the underlying data genuinely does.

A PEP source's "artifact" is not fetched over HTTP: it is a reviewed intake file (`datasets/<key>/intake/current.yml`) maintained through an external dual-review process, captured and built into a release with `screening-crawler build-pep-release <key>`. The parser is the enforcement point for the review gate — it replays each entry's `DRAFT -> EVIDENCED -> REVIEWED -> PUBLISHED` transition sequence (`assertions.PepAssertion`) and only ever emits a record for an entry that reaches `PUBLISHED` with a reviewer distinct from the asserter; anything else is left in the intake file for a future release rather than quarantining the whole crawl, since an unpublished entry isn't a data-quality problem, just not ready yet.

## Scaling properties

Parsers receive artifacts independently and can use streaming APIs. Local storage hashes and paths support idempotent retries and byte-level duplicate suppression. Record content hashes support incremental comparisons. Deterministic sorting occurs only at release serialization boundaries, so acquisition and parsing can later run horizontally while output remains stable. ZIP and gzip decoding is bounded. Object storage and worker scheduling are interfaces rather than mandatory distributed infrastructure.

Operational metrics should include per-source last successful observation, snapshot state, artifact bytes and hash reuse, fetch/parse duration, record counts and deltas, field findings, quarantine count and age, projection duration, release validation duration, and publication delay. Metrics must use source and snapshot identifiers, never personal names or identifiers.

