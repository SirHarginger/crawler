# Release bundle contract

The release bundle is the only supported interface between this public crawler and the private
`screening-engine`. This document describes what `ReleaseBuilder` (`src/screening_crawler/
release.py`) actually emits and `validate_release_bundle` (`src/screening_crawler/validation.py`)
actually checks -- both were reconciled against `screening-engine`'s real importer contract and
proven against its real verifier; see `PROGRESS.md`'s "Phase 7" section for the full history of
what changed and why. Someone reading only this document should be able to predict every file in
a real bundle correctly.

## Bundle layout

```text
release-<content-derived-id>/
  manifest.json
  artifacts.ndjson
  source_records.ndjson
  source_record_extents.ndjson
  projection_statements.ndjson
  entities.ndjson
  field_coverage.ndjson
  validation_report.ndjson
  pep_assertions.ndjson          only when the release includes PUBLISHED PEP assertions
  checksums.sha256
```

Every file except `manifest.json` and `checksums.sha256` is NDJSON: one canonical JSON object
per line, sorted keys, no insignificant whitespace, one LF per row (`canonical_ndjson`).

**Every file above `pep_assertions.ndjson` is written unconditionally, every time**, even when
empty -- including `source_record_extents.ndjson`: screening-engine's importer hard-requires the
`source_record_extents` role to be present on every release regardless of whether any dataset in
it actually uses extents. An empty NDJSON file satisfies that. `pep_assertions.ndjson` is the one
genuinely conditional file, written only when `ReleaseInput.pep_assertions` is non-empty.

**Not part of the bundle**, despite what an earlier version of this document said:
- `domain_objects.ndjson` -- Layer 1.5 domain interpretation is accepted by `ReleaseInput` but
  deliberately not written into the bundle. screening-engine's manifest schema has no role for
  it yet, and the assertion-shaped state machine its closest candidate role
  (`source_interpretations`, present in the schema's `files[].role` enum but never emitted by
  this repo) expects doesn't match this repo's `DomainObject` shape closely enough to paper
  over. Layer 1 source records retain everything Layer 1.5 would interpret, so nothing is lost
  at the authoritative layer -- this is a genuine gap, not silent data loss. Revisit together
  with the next dataset that actually needs Layer 1.5 published.
- `snapshots/<source-key>.json` as separate files -- snapshot data is an embedded array,
  `manifest["snapshots"]`, not separate files. See "Manifest" below.

## Per-file roles and row shapes

Every file is declared in `manifest["files"]` with a `role` (from
`schemas/release-manifest.schema.json`'s `files[].role` enum), a `sha256`, `size_bytes`, a
`media_type`, and the `datasets` it covers. The role, not the filename, is what a consumer
should key off of -- `validate_release_bundle` does.

| File | Role | Row shape (NDJSON fields) |
|---|---|---|
| `artifacts.ndjson` | `artifacts` | `id`, `snapshot_id`, `uri`, `sha256`, `media_type`, `size_bytes`, `acquired_at`, `request_metadata` (`http_status`, `redirect_chain`, `publisher_publication_date`, `crawler_version`, `parser_version`, `runtime_version`, `distribution_restriction`, `storage_key`) |
| `source_records.ndjson` | `source_records` | `SourceRecord.to_dict()` fields, with two renames from the Python attribute names: `source_key` -> **`dataset_key`**, `publisher_native_key` -> **`source_record_key`** |
| `source_record_extents.ndjson` | `source_record_extents` | `SourceRecordExtent.to_dict()` fields, unchanged |
| `projection_statements.ndjson` | `projection_statements` | `ProjectionStatement.to_dict()` fields, with `projection_id` renamed to **`id`**, plus an added **`dataset_key`** (looked up from the statement's source record) |
| `entities.ndjson` | `entities` | `id`, `schema`, `caption` (computed -- first `name`/`alias`/`weakAlias` property value, falling back to `id`), `dataset_key`, `datasets` (array, currently always `[dataset_key]`), `properties`, `referents` |
| `field_coverage.ndjson` | `field_coverage` | `dataset_key`, `field_path`, `disposition`, `observed_count`, `null_count`, `enum_values` |
| `validation_report.ndjson` | `validation_report` | `dataset_key`, `valid` (bool), `findings`, `controls` |
| `pep_assertions.ndjson` | `pep_assertions` | `PepAssertion.to_dict()` fields (unchanged), plus an added `dataset_key` |
| `checksums.sha256` | `checksums` | not NDJSON -- see below |

All renames are in the **serialized dict output only**. The Python dataclass field names
(`SourceRecord.source_key`, `ProjectionStatement.projection_id`, etc.) are unchanged and still
used internally as identity/sort keys throughout `pep.py`/`projection.py`/`release.py` -- do not
"fix" those to match the file output, they're not the same thing.

`pep_assertions.ndjson` rows: every row must have `state: PUBLISHED` with `reviewed_by` distinct
from `asserted_by`, and its `id` must equal some `source_records.ndjson` row's
**`source_record_key`** (not `publisher_native_key` -- that field no longer exists in the
serialized output). `validate_release_bundle` enforces all three, plus schema conformance against
`schemas/pep-assertion.schema.json` (validated with `dataset_key` stripped first, since that key
isn't part of the shared schema). This file carries evidentiary/review provenance for a
human-assembled source (see `docs/architecture.md`'s note on `PepSourceAdapter`), not parsed
publisher bytes -- a genuinely different shape from everything else in the bundle.

## Manifest

`manifest.json` conforms to `schemas/release-manifest.schema.json`. Top-level fields:

- `release_id`, `release_schema_version` (e.g. `"1.0"`), `status` (always the literal
  `"PUBLISHED"` -- an unpublished bundle never exists on disk, per the atomic-publish design),
  `generated_at`
- `ftm_schema_version`
- `mapping_version`, `normalization_version` -- **one of each per release, not one per
  dataset.** `ReleaseBuilder._require_consistent_versions()` fails loudly if the datasets in one
  release disagree, rather than silently picking one -- screening-engine's schema has no
  per-dataset slot for these. Every CLI entry point in this repo builds single-dataset releases
  today, so this has never actually fired; see `docs/versioning.md`.
- `crawler_version`, `crawler_revision` (the real checked-out `git rev-parse HEAD`, 7-64 hex
  characters -- `cli.py`'s `_git_revision()` reads it fresh per release rather than pinning a
  stale constant), `deterministic_id_method` (currently always
  `"uuid5-namespaced-stable-id"`)
- `datasets[]`: one entry per dataset in the release -- `key`, `version` (the dataset's
  snapshot's `combined_manifest_sha256`), `entity_count`, `source_record_count`,
  `projection_statement_count`, `publication_date`, `freshness_checked_at`
- `snapshots[]`: the embedded snapshot data mentioned above -- `id`, `dataset_key`, `state`,
  `combined_manifest_sha256`, `completeness_evidence`
- `files[]`: one entry per file in the bundle (including `checksums.sha256` itself) -- `path`,
  `role`, `sha256`, `size_bytes`, `media_type`, `datasets`

An earlier version of this document described a different, larger field list
(`dataset_scopes`, `parser_versions`/`mapping_versions`/`normalization_versions` as per-dataset
maps, `relationship_counts`, `distribution_restrictions`, separate
`completeness_status`/`validation_status`/`release_status` fields). None of that exists in the
current schema -- it was replaced wholesale when the manifest was reconciled against
screening-engine's actual contract (Phase 7).

`checksums.sha256` covers every file in `files[]` except itself, one line each: lowercase
SHA-256, two spaces, a relative POSIX path, LF-terminated, ASCII. `validate_release_bundle`
rejects missing, additional, duplicated, or mismatched entries, and independently re-hashes
every declared file rather than trusting the manifest's own `sha256`/`size_bytes` fields.

## Canonical form

- encoding: UTF-8
- JSON key order: lexicographic
- entity order: entity ID
- property order: property name
- property values: unique, lexicographically sorted
- NDJSON row order: stable object ID
- whitespace: no insignificant whitespace
- newline: one LF after `manifest.json` and after each NDJSON row
- numbers: finite JSON numbers only
- versions: schema and serializer dependencies pinned by the build environment

Regeneration is accepted only when deleting Layer 2 output and recreating it from the same
Layer 1 records at the same pinned mapping/normalization versions yields an identical
`entities.ndjson` canonical hash (`ProjectionResult.canonical_hash`) -- this is what every
dataset's `test_regeneration_is_deterministic` test actually asserts.

## Verifying against screening-engine

This is now a proven, standing check, not an aspiration -- a real `build-fixture-release` bundle
from this repo has been loaded and verified against screening-engine's actual, unmodified
importer code (Phase 7). To repeat it:

```sh
# 1. Build a release from this repo (offline, no network needed):
.venv/bin/screening-crawler build-fixture-release --work /tmp/w --output /tmp/o

# 2. From a sibling screening-engine checkout, verify the file contract only
#    (bypasses importer/cli.py, which forces a live Postgres connection):
python -c "
from pathlib import Path
from importer.store import LocalReleaseStore
from importer.verifier import ReleaseVerifier
store = LocalReleaseStore(root=Path('/tmp/o'))
result = ReleaseVerifier(Path('schemas/release-manifest.schema.json')).verify(store, '<release-id-printed-in-step-1>')
print(result.counts)
"
```

This requires a local sibling checkout of `screening-engine` (expected dev layout: both repos
cloned side by side, e.g. `../screening-engine` relative to this repo) -- it is not something
this repo's own test suite or CI can do alone, since screening-engine is a separate, private
repository. It is also a **file-level check only** -- see "Importer obligations" below for
exactly what it does and doesn't verify, since it's a different (and in one respect narrower)
set of checks than this repo's own `validate_release_bundle`. What neither layer exercises is
`ReleaseImporter.import_release()`, the actual Postgres COPY step, which needs a live database
neither repo's development sandbox has had.

## Changing the contract

`schemas/release-manifest.schema.json` (and the other three shared-shape schemas) are not
imported or submoduled between the two repos -- each repo carries its own copy of the file, and
nothing stops them from silently drifting apart. That already happened once (Phase 7 was the
fix). **A change to this schema is never safe to land in only one repo.** If you change it here:

1. Edit `schemas/release-manifest.schema.json` (or the affected schema) in this repo.
2. Run `pytest tests/test_schema_sync.py`. It compares this repo's copy byte-for-byte against a
   sibling `screening-engine` checkout's `schemas/release-manifest.schema.json`, and skips
   gracefully if no sibling checkout is present locally -- it is a local drift guard, not a CI
   dependency between the repos, so a green CI run in *this* repo alone proves nothing about
   whether the two are still in sync.
3. Copy the changed file into `screening-engine`'s `schemas/` directory (or hand-apply the
   equivalent change if the two have diverged for a legitimate reason -- but check first whether
   that divergence is intentional or the same silent-drift bug happening again).
4. Open a note or PR in `screening-engine` too. Neither repo's CI checks out the other, so
   nothing will catch a one-sided change automatically except the local `test_schema_sync.py`
   guard above, and only for whoever happens to run it with both repos checked out.

## Importer obligations

Two layers check a release before it's ever imported, and they check different things -- do not
assume screening-engine's importer repeats everything this repo already checked, or vice versa.

**This repo's own `validate_release_bundle`** (`src/screening_crawler/validation.py`) runs
before every release is published, and goes further than screening-engine's importer in one
specific respect (per that module's own docstring: it treats `parsed` content as convenience,
not authority, so it checks things a generic importer reasonably wouldn't): every entity's
`referents` resolves to a real, declared source record; every projection statement references a
known source record and entity; and every entity property that was actually projected has a
matching projection statement backing it.

**screening-engine's `importer.verifier.ReleaseVerifier`** (`importer/verifier.py`, read directly
and proven against a real bundle from this repo, Phase 7) independently re-verifies the same
bundle before any database write, but does **not** re-check entity/statement provenance the way
this repo's own validator does. What it does check: manifest schema conformance; `release_id`
and `release_schema_version` match and are supported; dataset keys are unique; every snapshot
references a declared dataset and every dataset has at least one snapshot; every snapshot is
`COMPLETE` or `PUBLISHED` with non-empty `completeness_evidence`; every required file role is
present; every declared file's size and SHA-256 are independently re-verified against the actual
bytes on disk, and its declared `datasets` are a subset of the release's declared datasets; the
checksum ledger has exactly one file and matches every non-ledger file's checksum exactly;
per-dataset `entity_count`/`source_record_count`/`projection_statement_count` match what's
actually in the NDJSON files; every entity has a non-empty, non-duplicate ID and the required
fields `schema`/`caption`/`datasets`/`properties`, with its own dataset present in its `datasets`
array; every `field_coverage` row has a recognized disposition and the release is rejected
outright if any row is `quarantined`; every declared dataset has both field-coverage rows and a
passing (`valid: true`) validation-report row; `generated_at` is not in the future.

Import itself must be transactional, and must never treat a filesystem path as permission for
direct database access from this repo's side -- the two repos share a contract, not a
connection.
