# Screening reference-data crawler

Acquires sanctions/PEP/debarment source material, preserves it byte-for-byte, extracts records,
projects deterministic FollowTheMoney entities, and publishes an immutable, checksummed
**release bundle**. That bundle is the only thing this repo produces or exports.

This repo never touches a database, Elasticsearch, or yente, and holds no production
credentials — that's the private `screening-engine` repo's job. Code is public; crawled data,
secrets, and production data are not (see [public/private boundary](docs/public-private-boundary.md)).

## Requirements

Python 3.11+.

## 1. Install

```sh
python -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
pre-commit install && chmod +x .githooks/pre-commit && git config core.hooksPath .githooks
```

## 2. Inputs — where a dataset's URL (or data) actually lives

| Dataset type | Input | Location |
|---|---|---|
| Machine-crawled (sanctions/debarment lists) | a publisher URL | `datasets/<key>/metadata.yml` → `source_endpoint:` |
| Human-assembled (Ghana PEP registers) | a reviewed intake file | `datasets/<key>/intake/current.yml` |

`crawl-dataset` only ever reads `source_endpoint` from `metadata.yml` — there is **no `--url`
flag** — so no unconfirmed URL can enter a published release. It stays `null` until confirmed
against the actual publisher; never guess one.

| Dataset | `source_endpoint` |
|---|---|
| `un_sc_consolidated`, `us_ofac_sdn`, `us_ofac_consolidated`, `eu_fsf`, `gb_ofsi` | set, live-confirmed |
| `wb_debarred` | `null` — blocked, no public export exists ([PROGRESS.md](PROGRESS.md)) |
| `gh_parliament`, `gh_article71`, `gh_mmdce`, `gh_siga_boards` | none — human intake file, ships empty |

To override a URL for a one-off local capture only (never a published release):
`record-fixture <key> --url <url>`.

## 3. Run

**Offline, no network, no real data — safe to run first:**
```sh
.venv/bin/screening-crawler build-fixture-release --work /tmp/w --output /tmp/o
```

**Live crawl a machine-published dataset** (needs a confirmed `source_endpoint`):
```sh
.venv/bin/screening-crawler crawl-dataset un_sc_consolidated --work /tmp/w --output /tmp/o
```

**Build a release from a PEP-shaped dataset's reviewed intake file:**
```sh
.venv/bin/screening-crawler build-pep-release gh_parliament --work /tmp/w --output /tmp/o
```

**Capture a live response to disk without publishing** (review before ever committing; never
commit a real capture as-is):
```sh
.venv/bin/screening-crawler record-fixture un_sc_consolidated --output /tmp/captures
```

**Validate an existing release or a dataset's metadata:**
```sh
.venv/bin/screening-crawler validate-release /tmp/o/<release-id>
.venv/bin/screening-crawler validate-dataset datasets/<key>/metadata.yml
```

## 4. Output

Every command above that builds a release produces one immutable directory:

```text
<output>/release-<content-derived-id>/
  manifest.json
  artifacts.ndjson  source_records.ndjson  source_record_extents.ndjson
  projection_statements.ndjson  entities.ndjson
  field_coverage.ndjson  validation_report.ndjson
  pep_assertions.ndjson          (PEP-shaped datasets only)
  checksums.sha256
```

Full per-file shape and field names: [release contract](docs/release-contract.md). This bundle
is the *only* interface to `screening-engine` — nothing else leaves this repo.

## 5. Verify

```sh
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy src datasets
.venv/bin/python -m pytest tests -q
.venv/bin/python tools/check_public_repo.py
```

All four must be green. Tests run only against synthetic fixtures — no network, no real data.

## Adding a dataset

1. `datasets/<key>/metadata.yml` — legal basis, risk semantics, cadence, distribution
   restriction, pinned versions. Confirm the endpoint from a primary source before setting it.
2. Parser + projection profile in `datasets/<key>/crawler.py`.
3. `fields.yml` covering every field path the parser emits.
4. Synthetic fixtures under `tests/fixtures/<key>/`.
5. Prove regeneration: delete Layer 2 output, rebuild from Layer 1, compare the canonical hash.

Full checklist: [source onboarding](docs/source-onboarding.md).

## Deliberately out of scope

Database persistence, Elasticsearch/yente, screening decisions/alerts, cross-source identity
resolution, unvalidated matching heuristics, unconfirmed endpoints, redistribution without
authority. All of that belongs to `screening-engine`.

## Troubleshooting

| Symptom | Do this |
|---|---|
| Snapshot stuck `INCOMPLETE` | Record explicit completeness evidence; never override the state. |
| Unknown path/enum quarantines | Compare to the previous artifact, classify with compliance, update `fields.yml`, add a regression fixture. [Runbook](docs/runbooks/quarantine-and-recovery.md). |
| Release checksum fails | Bundle is compromised or incomplete — never edit it. Rebuild into a clean output root. |
| Fixture release already exists | Release IDs are content-derived and immutable. Use a clean output root. |
| `crawl-dataset` refuses to run | `source_endpoint` is `null` or unconfirmed. Confirm it from a primary source first — see section 2. |

## Further documentation

- [Progress: what's done, verified vs. placeholder, what's left](PROGRESS.md)
- [Architecture and decisions](docs/architecture.md)
- [Release contract](docs/release-contract.md)
- [Field inventory](docs/field-inventory.md)
- [Versioning](docs/versioning.md)
- [Source onboarding](docs/source-onboarding.md)
- [Public/private data boundary](docs/public-private-boundary.md)
- [Quarantine and recovery](docs/runbooks/quarantine-and-recovery.md)
