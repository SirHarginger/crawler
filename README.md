# Screening reference-data crawler

Acquires sanctions/PEP/debarment source material, preserves it byte-for-byte, extracts
records, projects deterministic FollowTheMoney entities, and publishes an immutable,
checksummed **release bundle**. That bundle is the only thing this repo produces or exports —
it never touches a database, Elasticsearch, or yente, and holds no production credentials
(that's the private `screening-engine` repo's job).

## Architecture

```text
official source -> acquisition -> raw artifact -> parser -> FollowTheMoney projection ->
validation -> diff -> immutable release bundle
```

See [docs/architecture.md](docs/architecture.md) for the actual implementation of every stage,
and [docs/public-private-boundary.md](docs/public-private-boundary.md) for what this repo
deliberately does not own.

## Development

Requires Python 3.11+.

```sh
python -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
pre-commit install && chmod +x .githooks/pre-commit && git config core.hooksPath .githooks
```

## Running

```sh
screening-crawler build-fixture-release --work /tmp/w --output /tmp/o
screening-crawler crawl un_sc_consolidated --work /tmp/w --output /tmp/o
```

Full command reference (list sources, run a crawl, inspect runs, list/validate/compare
releases): [docs/operations.md](docs/operations.md). Adding a new source:
[docs/source-onboarding.md](docs/source-onboarding.md).

## Testing

```sh
python -m ruff check .
python -m mypy src datasets
python -m pytest tests -q
python tools/check_public_repo.py
```

All four must be green. Tests run only against committed synthetic fixtures — no network, no
real data.

## Documentation

- [Architecture and decisions](docs/architecture.md)
- [Operations](docs/operations.md)
- [Source onboarding](docs/source-onboarding.md)
- [Release contract](docs/release-contract.md)
- [Field inventory](docs/field-inventory.md) · [Versioning](docs/versioning.md)
- [Public/private data boundary](docs/public-private-boundary.md)
- [Quarantine and recovery runbook](docs/runbooks/quarantine-and-recovery.md)
- [Progress: what's done, verified vs. placeholder, what's left](PROGRESS.md)
