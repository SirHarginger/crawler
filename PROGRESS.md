# Progress

Working log for bringing this repository from foundation (`un_sc_consolidated` only) toward
the full crawler roadmap described in the system documentation. Read this before starting new
work here — it says what's done, what's verified vs. placeholder, and exactly what's left.

**Last updated:** 2026-08-07
**Verification status as of last update:** `python -m pytest tests -q` (135 passed, including a
new schema-sync contract test) all green. A real `build-fixture-release` bundle produced by this
repo's own CLI was independently verified against `screening-engine`'s actual, unmodified
`importer/verifier.py::ReleaseVerifier` — passed. See "Phase 7" below.

---

## 0. Orientation

- This is the **crawler** repo (public, reference-data acquisition/projection/release-bundle).
  It is *not* `screening-engine` (private, application plane — Postgres, API, yente, alerts).
  As of 2026-08-07, `screening-engine` **does** exist as a sibling checkout at
  `/home/eddy/projects/screening-engine` (it didn't in earlier sessions) — see Phase 7.
- Git history is effectively empty (one commit, `LICENSE` only) — everything described below is
  currently **untracked on disk**. Committing is deliberately left to the repo owner; nothing in
  this session has run `git add`/`git commit`. See the end of this file for the exact commit
  command to run once reviewed.
- The original gap analysis and phase plan live in the plan file this session started from
  (`this-repo-is-the-gentle-eclipse.md` in the Claude Code plan history) — this document
  supersedes it as the source of truth for *current* status through Phase 6. Phase 7 came from a
  separate plan run from the `screening-engine` side
  (`this-repo-is-the-hashed-kahan.md`), once that repo's own gaps were closed and the two repos'
  release-bundle contract was checked directly against each other for the first time.

---

## 1. What's done

### Phase 0 — quick fixes
- `docs/architecture.md`: added a decision note on the `zavod` vs. bespoke-toolkit divergence
  (system docs say `zavod`; this repo has no such dependency, by design — flagged for
  compliance/engineering sign-off, not resolved unilaterally).
- `.github/CODEOWNERS` **still has placeholder team names** (`@compliance-lead @eng-lead`) —
  not fixed, because real GitHub handles weren't available and fabricating them would be the
  same mistake this whole effort has tried to avoid. **Action needed from a human:** replace
  with real team/user handles before branch protection is enabled.

### Phase 1 — live fetch transport
- `src/screening_crawler/transport.py`: `HttpFetchTransport`, a stdlib-only `FetchTransport`
  implementation (conditional GET via `If-None-Match`/`If-Modified-Since`, redirect-chain
  capture, bounded reads, retry/backoff). This is the "one safe first deployment" for fetching,
  same role `LocalBlobStore` plays for storage.
- New CLI commands: `crawl-dataset <key>` (live fetch + publish a release; refuses to run
  without a confirmed `source_endpoint`) and `record-fixture <key>` (capture a live response to
  disk for local fixture development; explicitly labelled "not a publishable fixture").
- `un_sc_consolidated/metadata.yml`: `source_endpoint` is now set and **verified live**
  (`https://scsanctions.un.org/resources/xml/en/consolidated.xml`, confirmed via a real fetch
  this session — root element, field names, and `dateGenerated` all checked against the live
  file). Caveat recorded in that file: only `INDIVIDUALS` records were confirmed, not
  `ENTITIES`.
- Fixed a batch of pre-existing, never-actually-run lint/type issues repo-wide (nothing had
  ever been committed, so CI had never executed) — this is why `ruff`/`mypy` are clean now.

### Phase 2 — list-breadth datasets
Four fully onboarded (parser, projection, Layer 1.5 domain interpreter, `fields.yml`, synthetic
fixture, tests), one genuinely blocked:

| Dataset | Endpoint status | Schema verification |
|---|---|---|
| `us_ofac_sdn` | live-confirmed (redirects to signed S3 export) | endpoint live; field names **not** independently re-verified (file too large to fetch this session) |
| `us_ofac_consolidated` | live-confirmed | same pipeline/schema as `us_ofac_sdn`, so higher confidence |
| `eu_fsf` | live-confirmed (hit fetch tool's 10 MB cap, i.e. real data) | endpoint live; schema unconfirmed |
| `gb_ofsi` | live-confirmed | **publisher itself changed mid-session**: the old OFSI Consolidated List closed 28 Jan 2026, replaced by FCDO's "UK Sanctions List" — this is reflected in the dataset's title/metadata |
| `wb_debarred` | **blocked** | No documented public export; underlying API returns `401 Unauthorized`. Only a `metadata.yml` stub exists — **no** `crawler.py`/`fields.yml`/fixture, since building a parser against a schema never actually seen would be fabrication. Needs either World Bank API credentials or a confirmed unauthenticated bulk-export path. |

Every onboarded dataset's `metadata.yml` states plainly what was and wasn't verified this
session — read the `source_format` field before treating any of them as production-ready.

### Phase 3 — Ghana name library (placeholder plumbing)
- `src/screening_crawler/name_library.py`: `GivenNameFrequencyTable` and
  `OrthographyVariantTable`, loading versioned YAML artifacts from `datasets/gh_name_library/`.
- `normalization.py`'s `GhanaianNameRule` is now genuinely data-driven (was a hardcoded no-op).
- **Both shipped artifacts are still empty** (`gh-name-frequency-v0-placeholder`,
  `gh-orthography-v0-placeholder`) — no reviewed Ghanaian name corpus was available. Behaviour
  today is byte-for-byte identical to before this machinery existed (confirmed: same release ID
  from `build-fixture-release` before and after this change).
- `tests/test_name_library.py` includes a regression guard that fails if either shipped file
  ever stops being empty without someone also updating the test — a deliberate tripwire so
  populating real data is a visible, reviewed act, not a silent edit.
- **Action needed from a human:** a reviewed, source-cited Ghanaian name corpus. Until then, do
  not populate these files from general knowledge or guesswork — see the warning headers in
  both YAML files for exactly what's needed.

### Phase 4 — Ghana PEP register
This phase surfaced a real architecture gap before any dataset work started: `assertions.py`
(`PepAssertion` lifecycle) and `pep.py` (`PepOccupancyProjector`) existed and were well-tested
in isolation, but neither was wired into the actual dataset pipeline — `pep_assertions.ndjson`
didn't exist as a release file (despite `schemas/pep-assertion.schema.json` already existing,
orphaned), and `PepOccupancyProjector` produces three entities per record (Person, Position,
Occupancy) where the generic `Projector` produces one, so it couldn't just be dropped into
`DatasetAdapter.projection_profile`.

**What was built** (see `docs/architecture.md`'s "PEP-shaped sources get their own adapter
contract" section for the full rationale):
- `release.py`/`validation.py`/`schemas/release-manifest.schema.json` extended to support
  `pep_assertions.ndjson`: schema-validated, cross-checked for `state: PUBLISHED`, an
  independent reviewer, and traceability to a Layer 1 source record. Fully additive — no
  existing release output changed shape.
- `pep.py`: new `PepSourceAdapter` Protocol (deliberately distinct from `DatasetAdapter`),
  `load_pep_adapter()`, and `assertion_from_parsed()` (reconstructs a `PepAssertion` from a
  `SourceRecord` for serialization).
- `pipeline.py`: new `process_pep_snapshot()`, a parallel pipeline alongside the untouched
  `process_snapshot()` — zero risk to the five already-shipped machine-published datasets.
- New CLI command `build-pep-release <key>`: there's no live crawl for these sources — the
  artifact *is* a reviewed intake file (`datasets/<key>/intake/current.yml`), maintained by an
  external dual-review process, not fetched over HTTP.
- Four datasets built end-to-end: `gh_parliament`, `gh_article71`, `gh_mmdce`,
  `gh_siga_boards`. Each has its own `metadata.yml` (correct publisher/legal framing per
  authority — e.g. `gh_article71` correctly notes it spans multiple constitutional bodies, not
  one publisher), an intake parser that replays and enforces the full
  `DRAFT -> EVIDENCED -> REVIEWED -> PUBLISHED` lifecycle (rejecting self-review), and
  `fields.yml`.
- **Every intake file ships empty.** No real Ghanaian officials' names, positions, or dates
  appear anywhere in this repository — fabricating them would be exactly the defamation risk
  the system documentation warns about (§4.7: "an assertion we cannot evidence is a defamation
  claim we cannot defend"). All tests use obviously synthetic names ("Synthetic Person Alpha").
- Verified two ways: internal test suite (`tests/test_gh_parliament.py`,
  `tests/test_gh_pep_datasets.py`, 33 tests total), *and* two live CLI runs
  (`build-pep-release gh_parliament` and `build-pep-release gh_mmdce` against synthetic intake
  files, followed by `validate-release` on the output) — not just unit-tested in isolation.
- **Action needed from a human:** reviewed, cited, dual-transcribed data for each register. See
  the warning header and worked example in each `datasets/gh_*/intake/current.yml`.

### Phase 7 — release-bundle contract reconciled against screening-engine, for real

Until this phase, this repo's release-manifest schema and screening-engine's had **silently
diverged** — different field names throughout (`release_status`/lowercase vs. `status`/
`PUBLISHED`, `generation_timestamp` vs. `generated_at`, per-dataset version maps vs. singular
strings, `projection_id` vs. `id`, `source_key`/`publisher_native_key` vs. `dataset_key`/
`source_record_key`), no `datasets[]`/`snapshots[]`/`files[]` arrays in the shape
screening-engine's importer requires, single-JSON-document files where screening-engine expects
NDJSON. A bundle built by this repo would have failed screening-engine's import outright. Neither
repo's test suite could catch this alone, because each only validated itself against its own
copy of the schema.

**screening-engine's shape was taken as canonical** (its importer — independent per-file
re-hashing, a checksum-ledger cross-check, per-dataset count verification, hard rejection on any
`quarantined` field — was already built, tested, and re-verified against a live-Postgres-free
harness before this phase started). What changed here to match it:

- `schemas/release-manifest.schema.json`: replaced with screening-engine's copy verbatim (plus
  one addition screening-engine's side needed too: a `pep_assertions` file role, so this repo's
  already-built PEP release support from Phase 4 has somewhere to land in the canonical schema).
- `schemas/source-record.schema.json`, `schemas/projection-statement.schema.json` (this repo's
  own internal per-record schemas, not shared with screening-engine): renamed fields to match —
  `source_key`→`dataset_key`, `publisher_native_key`→`source_record_key`,
  `projection_id`→`id` (+ added `dataset_key`). The Python dataclass field names themselves
  (`SourceRecord.source_key`, `ProjectionStatement.projection_id`, etc.) were deliberately **not**
  renamed — they're used internally as identity/sort keys throughout `pep.py`/`projection.py`/
  `release.py`; only the serialized dict *output* going into the release bundle changes shape.
- `projection.py`: `ProjectedEntity` gained `dataset_key` and a computed `caption` property
  (screening-engine's entity schema requires both a singular `dataset_key` and a `datasets`
  array, plus a caption — this repo's entities previously had neither).
- `release.py` and `validation.py`: substantially rewritten around the new shape — NDJSON output
  for every per-record file, an embedded (not file-per-snapshot) `snapshots[]` array, a
  `files[]` array with per-file role/checksum/size metadata, a `_require_consistent_versions()`
  guard (screening-engine's schema has one `mapping_version`/`normalization_version` per release,
  not per dataset — this repo's CLI only ever builds single-dataset releases today, so this fails
  loudly rather than silently picking one if that's ever violated). Layer 1.5 (`domain_objects`)
  deliberately **not** written into the bundle — no matching role exists in screening-engine's
  schema yet, and Layer 1 retains everything so nothing is lost at the authoritative layer.
- Found by testing a real bundle against screening-engine's real verifier, not by
  inspection: `source_record_extents` had been treated as an *optional* role here (only written
  when a dataset actually uses extents), but screening-engine's importer hard-requires the role
  to be present on every release (an empty NDJSON file satisfies it). Now always written.
- New `tests/test_schema_sync.py`, mirrored in screening-engine as
  `tests/unit/test_schema_sync.py`: asserts the two repos' `release-manifest.schema.json` are
  byte-identical whenever a sibling checkout of the other repo is present locally. Skips
  gracefully otherwise (neither repo's CI checks out the other), so it's a local drift guard, not
  a CI dependency between the two repos.

**Proof this actually closes the gap**, not just that the two schema files match:
`build-fixture-release` (this repo's own CLI, fully offline) produced a real release bundle,
which was then loaded with screening-engine's `importer.store.LocalReleaseStore` and verified
with its actual, unmodified `importer.verifier.ReleaseVerifier` — the same class that gates a
real import into the Postgres reference registry. Result: `VERIFIED OK`, counts
`{'un_sc_consolidated': {'entities': 2, 'source_records': 2, 'projection_statements': 13}}`
matching the bundle exactly.

**What Phase 7 did not close:** the next link in the chain, `importer.service.ReleaseImporter
.import_release()`, requires a live `psycopg` connection to a real Postgres — no live Postgres
has been available in either repo's sandbox this whole engagement. The file-contract layer (the
part that was actually broken) is proven; the DB-load step is unexercised but unchanged by this
phase, and was already a known, pre-existing gap on screening-engine's side.

---

## 2. What's left

### Phase 5 — restricted-distribution sources (`gh_nsc_advisory`)
**Not started, and blocked on something outside this repo's control**, not on effort: the
system documentation itself lists "may restricted-distribution advisory content be
redistributed to accountable institutions" as an open legal question requiring an answer from
the issuing authority (Appendix C, item 2). Building `gh_nsc_advisory` now would mean designing
against an unresolved legal question. When that answer exists:
- Reuse `ManualReceipt` (already in `model.py`) for the chain-of-custody envelope.
- Model OCR as a versioned parser (per doc §13.5) with mandatory dual review — same
  `AssertionState` machinery already built for the Ghana PEP register in Phase 4 likely applies
  directly.
- Keep `gh_nsc_advisory` excluded from any tenant-facing collection until redistribution is
  cleared — this is a hard requirement, not a nice-to-have.

### Phase 6 — scheduled polling infrastructure
**Not started**, deliberately — it only makes sense once real endpoints are actually being
polled in a real deployment. When that's imminent:
- Respect each dataset's cadence from its `metadata.yml` (`expected_cadence`,
  `freshness_slo_hours`) and the doc's §6.1 table (binding/contractual sources: on detected
  change, target under 2 hours; PEP: scheduled daily/weekly; debarment: scheduled daily).
- Coalesce promotions within a window (§11.3) rather than firing one release per detected
  change.
- This is infrastructure/deployment work (cron, GitHub Actions scheduled workflow, or
  equivalent) layered on top of the already-built `crawl-dataset`/`build-pep-release` commands
  — no new crawler-side primitives should be needed.

### Smaller open items, not phase-blocking
- `importer.service.ReleaseImporter.import_release()` (screening-engine side) has never been
  exercised against a real bundle produced by this repo, because it needs a live Postgres — the
  file-contract layer is now proven (Phase 7); the actual COPY-into-database step is not. Needs
  a real (or CI-ephemeral) Postgres in either repo's sandbox to close.
- `.github/CODEOWNERS` placeholders (see Phase 0 above).
- `un_sc_consolidated`'s `ENTITIES` records were never confirmed against `fields.yml`, only
  `INDIVIDUALS` — worth a follow-up live check before relying on entity coverage.
- `us_ofac_sdn`/`us_ofac_consolidated`/`eu_fsf`/`gb_ofsi` all need a `record-fixture` capture +
  review against real live content before a first production crawl — endpoints are confirmed
  live, field-level schemas are not.
- `wb_debarred` needs either API credentials or a confirmed unauthenticated export path before
  any dataset code can be written for it at all.

---

## 3. How to verify the repo at any point

```sh
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src datasets
.venv/bin/python -m pytest tests -q
.venv/bin/python tools/check_public_repo.py
```

All four must stay green. If any is red on pickup, something regressed since this document was
written — don't assume it's pre-existing without checking history first.

Useful one-off checks:

```sh
# Any dataset's metadata is well-formed:
.venv/bin/screening-crawler validate-dataset datasets/<key>/metadata.yml

# Offline synthetic UN release (no network needed):
.venv/bin/screening-crawler build-fixture-release --work /tmp/w --output /tmp/o

# Live crawl (only for datasets with a confirmed source_endpoint):
.venv/bin/screening-crawler crawl-dataset <key> --work /tmp/w --output /tmp/o

# PEP-shaped dataset release from a (real or synthetic) intake file:
.venv/bin/screening-crawler build-pep-release <key> --intake path/to/intake.yml --work /tmp/w --output /tmp/o
```

---

## 4. File map of what changed this session

```text
src/screening_crawler/
  transport.py            new — HttpFetchTransport
  name_library.py         new — GivenNameFrequencyTable, OrthographyVariantTable
  normalization.py        GhanaianNameRule made data-driven (was hardcoded no-op)
  cli.py                  + crawl-dataset, record-fixture, build-pep-release
  datasets.py             + load_adapter() (generic dataset lookup)
  pep.py                  + PepSourceAdapter, load_pep_adapter(), assertion_from_parsed()
  pipeline.py             + process_pep_snapshot() (parallel to process_snapshot, untouched)
  release.py              + pep_assertions support in ReleaseInput/ReleaseBuilder
  validation.py           + pep_assertions.ndjson validation
  errors.py                + FetchError

schemas/
  release-manifest.schema.json   + optional pep_assertion_counts

datasets/
  us_ofac_sdn/, us_ofac_consolidated/, eu_fsf/, gb_ofsi/     new, full datasets
  wb_debarred/                                                metadata.yml stub only (blocked)
  gh_name_library/                                            new — placeholder name artifacts
  gh_parliament/, gh_article71/, gh_mmdce/, gh_siga_boards/   new, full PEP datasets

tests/
  test_transport.py, test_datasets_registry.py, test_cli_crawl.py
  test_ofac_sdn.py, test_eu_fsf.py, test_gb_ofsi.py, test_ofac_consolidated.py
  test_name_library.py
  test_gh_parliament.py, test_gh_pep_datasets.py

docs/
  architecture.md          + zavod divergence note, + PEP adapter contract rationale
  release-contract.md      + pep_assertions.ndjson in the bundle layout
  source-onboarding.md     + note on live transport now existing
  versioning.md            + note on name-library plumbing status

README.md                  + Live acquisition, Name library, Ghana PEP register sections
```

---

## 5. If you're picking this up fresh

1. Run the verification sweep in section 3. If it's not green, stop and figure out why before
   adding anything.
2. Read this file's "What's left" section — Phases 5 and 6 are both externally blocked, not
   just unstarted. Don't try to force them without the missing input (legal answer / real
   deployment).
3. If you have real data for any placeholder (Ghana name corpus, a Ghana PEP register, World
   Bank API access, `gh_nsc_advisory` redistribution clearance), that unblocks real forward
   progress — populate the relevant file(s) following the warning headers already in place, and
   the existing regression-guard tests will tell you if you missed a step.
4. If asked to onboard another machine-published sanctions/debarment source: follow the
   `us_ofac_consolidated` pattern (near-mechanical duplication of `us_ofac_sdn`) if the schema
   is well-documented and stable, or the `eu_fsf`/`gb_ofsi` pattern (build from general
   knowledge, flag verification tier honestly in `metadata.yml`) if it's a source I don't have
   strong prior knowledge of. Always try to verify the live endpoint first; never guess one.
5. If a sibling `screening-engine` checkout is present, run `test_schema_sync.py` on both sides
   before touching either repo's `release-manifest.schema.json` — it'll tell you immediately if
   someone already let them drift again since Phase 7.

---

## 6. File map — Phase 7 (release-bundle contract reconciliation)

```text
schemas/
  release-manifest.schema.json      replaced verbatim with screening-engine's copy
  source-record.schema.json         source_key->dataset_key, publisher_native_key->source_record_key
  projection-statement.schema.json  projection_id->id, + dataset_key

src/screening_crawler/
  projection.py    ProjectedEntity + dataset_key field, + caption property
  pep.py           3 ProjectedEntity(...) call sites updated for the new dataset_key arg
  release.py       rewritten: NDJSON everywhere, files[]/snapshots[]/datasets[] arrays,
                    _require_consistent_versions() guard, source_record_extents now
                    unconditionally written, record/statement rows renamed (not duplicated)
  validation.py    rewritten around REQUIRED_ROLES (incl. source_record_extents) and the
                    new per-role NDJSON shape
  cli.py           + _git_revision(), ReleaseVersions now takes crawler_revision (required)

tests/
  test_release.py       rewritten for the new manifest/row shapes
  test_schema_sync.py   new — cross-repo schema byte-identity guard (skips without a sibling)

(mirrored in screening-engine: schemas/release-manifest.schema.json got the pep_assertions
role added; tests/unit/test_schema_sync.py added as the other half of the drift guard.)
```

Verified end-to-end at the file-contract layer: a real `build-fixture-release` bundle passed
screening-engine's actual `ReleaseVerifier` unmodified. Not yet exercised: the Postgres
COPY-in step (`ReleaseImporter.import_release`), which needs a live database neither repo's
sandbox has had this whole engagement.

**Not committed.** Everything in this repo is still untracked except the original `LICENSE`
commit. To commit everything reviewed above in one commit, from this repo's root:

```sh
git add -A && git commit -m "Reconcile release-bundle contract with screening-engine; full crawler build-out"
```

Review `git status`/`git diff` first if you want to split this into smaller commits instead —
nothing here requires a single commit, that's just the minimum needed to stop being untracked.
