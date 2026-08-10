# Operations

Operator reference for the `screening-crawler` CLI. All commands verified against this
repo's actual `--help` output and real command execution, not assumed.

## List registered sources

```sh
screening-crawler list-sources
```

Lists every machine-crawled dataset's registry metadata (`source_endpoint`, cadence,
freshness SLO, anomaly threshold, mapping/parser version, `live_crawl_enabled`). PEP-shaped
datasets (no `source_endpoint`) are excluded.

## Run a live crawl

```sh
screening-crawler crawl un_sc_consolidated --work /srv/crawler/work --output /srv/crawler/releases --timeout 60 --retries 3
```

Or every dataset with a confirmed endpoint in one call:

```sh
screening-crawler crawl --all --work /srv/crawler/work --output /srv/crawler/releases
```

Prints `{"dataset", "release_id", "run_id", "state"}` and exits with a code a scheduler can
branch on: `0` published or unchanged, `3` quarantined, `4` validation failed, `5` parse
failed, `6` fetch failed, `7` a run for that dataset was already in progress. Suitable for a
systemd timer or cron entry — duplicate-run locking makes concurrent invocations for the same
dataset safe.

## Inspect crawl runs

```sh
screening-crawler list-runs --work /srv/crawler/work --source un_sc_consolidated
screening-crawler inspect-run --work /srv/crawler/work RUN_ID
```

`inspect-run` shows the full state-transition sequence, HTTP status/ETag/Last-Modified, raw
byte size and SHA-256, records discovered/parsed, entities produced, and (on failure) a
sanitized error code/message. `--source` is optional on `inspect-run` and only speeds up
lookup when omitted.

## List and inspect releases

```sh
screening-crawler list-releases --output /srv/crawler/releases --dataset un_sc_consolidated
screening-crawler validate-release /srv/crawler/releases/RELEASE_ID
```

`validate-release` independently re-verifies schema, checksums, and stream counts.

## Compare two releases

```sh
screening-crawler compare-releases /srv/crawler/releases/OLD_RELEASE /srv/crawler/releases/NEW_RELEASE --detail
```

Deterministic entity-level diff (added/modified/removed/unchanged), keyed by stable entity ID.
`--detail` includes field-level before/after for every modified entity.

## Offline/synthetic commands (no network, no real data)

```sh
screening-crawler build-fixture-release --work /tmp/w --output /tmp/o
screening-crawler validate-dataset datasets/un_sc_consolidated/metadata.yml
```

## Verification

```sh
python -m ruff check .
python -m mypy src datasets
python -m pytest tests -q
python tools/check_public_repo.py
```

All four are green as of this writing (158 tests, `ruff`/`mypy` clean). Tests run only against
committed synthetic fixtures — no live network access is exercised by the ordinary suite.

## Troubleshooting

| Symptom | Do this |
|---|---|
| Snapshot stuck `INCOMPLETE` | Record explicit completeness evidence; never override the state. |
| Unknown path/enum quarantines | Compare to the previous artifact, classify with compliance, update `fields.yml`, add a regression fixture. [Runbook](runbooks/quarantine-and-recovery.md). |
| Release checksum fails | Bundle is compromised or incomplete — never edit it. Rebuild into a clean output root. |
| Fixture release already exists | Release IDs are content-derived and immutable. Use a clean output root. |
| `crawl`/`crawl-dataset` refuses to run | `source_endpoint` is `null` or unconfirmed in `metadata.yml` — confirm it from a primary source first. |
| `crawl` exits `7` | Another run for that dataset holds the lock. Wait for it or confirm it's actually dead before removing `work/locks/<dataset>.lock`. |
