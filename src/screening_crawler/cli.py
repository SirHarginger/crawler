"""CLI commands for fixture development, live acquisition, and bundle verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import __version__
from .acquisition import Acquirer, FetchRequest, FetchResponse, SnapshotAssembler
from .datasets import DatasetAdapter, DatasetMetadata, load_adapter
from .diff import diff_releases
from .locking import RunAlreadyInProgress
from .model import CompletenessEvidence, CompletenessKind, DistributionRestriction
from .name_library import OrthographyVariantTable
from .normalization import AdditiveNormalizer, GhanaianNameRule
from .pep import PepSourceAdapter, load_pep_adapter
from .pipeline import DatasetBuild, process_pep_snapshot, process_snapshot
from .release import ReleaseBuilder, ReleaseInput, ReleaseVersions
from .release_reader import read_entities, read_manifest
from .run import CrawlRunStore, exit_code_for, run_source
from .storage import LocalBlobStore
from .transport import HttpFetchTransport
from .validation import validate_release_bundle

_FIXED_FIXTURE_TIME = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _git_revision() -> str:
    """screening-engine's manifest schema requires a real (non-null) crawler_revision,
    7-64 hex characters. Read the checked-out commit rather than pin a stale constant, so a
    release always names the exact revision that produced it."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_repository_root(),
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            "could not determine the checked-out git revision; a release manifest requires "
            "one (crawler_revision) -- run from within the repository's git checkout"
        ) from exc
    revision = result.stdout.strip()
    if not revision:
        raise RuntimeError("git rev-parse HEAD returned no revision")
    return revision


def _ghanaian_name_rule() -> GhanaianNameRule:
    # Still an empty placeholder pending a reviewed corpus (see
    # datasets/gh_name_library/orthography_variants.yml); loading it here
    # rather than constructing a bare GhanaianNameRule() makes the artifact
    # -- not this function -- the single source of truth for the version
    # stamped onto every projection statement.
    path = _repository_root() / "datasets/gh_name_library/orthography_variants.yml"
    return OrthographyVariantTable.from_path(path).to_rule()


def _validate_release(args: argparse.Namespace) -> int:
    validate_release_bundle(args.bundle.resolve(), args.schemas.resolve())
    print(json.dumps({"bundle": str(args.bundle), "status": "valid"}, sort_keys=True))
    return 0


def _publish_release(
    adapter: DatasetAdapter | PepSourceAdapter,
    build: DatasetBuild,
    *,
    output: Path,
    schemas: Path,
    generated_at: datetime,
) -> Path:
    release_input = ReleaseInput(
        datasets=(adapter.metadata,),
        snapshots=(build.snapshot,),
        source_records=build.source_records,
        projections=(build.projection,),
        field_coverages={adapter.metadata.key: build.field_coverage},
        domain_objects=build.domain_objects,
        pep_assertions=build.pep_assertions,
    )
    builder = ReleaseBuilder(
        schemas,
        ReleaseVersions(
            release_schema_version="1.0",
            ftm_schema_version="followthemoney-pinned-by-deployment",
            crawler_version=__version__,
            crawler_revision=_git_revision(),
        ),
    )
    return builder.build(release_input, output_root=output, generated_at=generated_at)


def _fixture_release(args: argparse.Namespace) -> int:
    from datasets.un_sc_consolidated.crawler import adapter

    fixture = args.fixture.read_bytes()
    store = LocalBlobStore(args.work / "capture_store")
    acquirer = Acquirer(
        store,
        crawler_version=__version__,
        parser_version=adapter.parser.version,
        runtime_version="fixture-runtime-v1",
    )
    artifact = acquirer.capture_response(
        source_key=adapter.metadata.key,
        response=FetchResponse(
            request_url="fixture://un_sc_consolidated/synthetic.xml",
            status=200,
            headers={"content-type": "application/xml"},
            body=fixture,
        ),
        retrieved_at=_FIXED_FIXTURE_TIME,
        restriction=DistributionRestriction.PUBLIC,
    )
    assembler = SnapshotAssembler(adapter.metadata.key, expected_parts=1)
    assembler.add(artifact)
    snapshot = assembler.finalize(
        CompletenessEvidence(CompletenessKind.SINGLE_FILE, {"fixture": True})
    )
    build = process_snapshot(
        adapter,
        snapshot,
        store,
        observed_at=_FIXED_FIXTURE_TIME,
        normalizer=AdditiveNormalizer(adapter.metadata.normalization_version, (_ghanaian_name_rule(),)),
    )
    destination = _publish_release(
        adapter, build, output=args.output, schemas=args.schemas, generated_at=_FIXED_FIXTURE_TIME
    )
    print(json.dumps({"release": str(destination), "status": "published"}, sort_keys=True))
    return 0


def _crawl_dataset(args: argparse.Namespace) -> int:
    """Fetch a dataset's live source endpoint and publish a release from it.

    Requires ``metadata.yml``'s ``source_endpoint`` to already be set to a
    confirmed publisher URL — this command never guesses one (see the
    README's "Live crawl is unavailable" troubleshooting entry). Only the
    single-file case is handled; multi-part/paginated sources need an
    adapter-specific acquisition loop, not yet wired here.
    """

    adapter = load_adapter(args.dataset)
    metadata = adapter.metadata
    metadata.validate()
    if not metadata.source_endpoint:
        raise ValueError(
            f"datasets/{args.dataset}/metadata.yml has no source_endpoint configured. "
            "Confirm the current publisher URL and format from a primary source before "
            "enabling live acquisition, then set source_endpoint explicitly."
        )

    conditional_headers: dict[str, str] = {}
    if args.if_none_match:
        conditional_headers["If-None-Match"] = args.if_none_match
    if args.if_modified_since:
        conditional_headers["If-Modified-Since"] = args.if_modified_since

    transport = HttpFetchTransport(timeout=args.timeout, max_retries=args.retries)
    response = transport.fetch(
        FetchRequest(url=metadata.source_endpoint, headers=conditional_headers)
    )

    if response.status == 304:
        print(
            json.dumps(
                {
                    "dataset": metadata.key,
                    "status": "not_modified",
                    "url": metadata.source_endpoint,
                },
                sort_keys=True,
            )
        )
        return 0

    retrieved_at = datetime.now(UTC)
    store = LocalBlobStore(args.work / "capture_store")
    acquirer = Acquirer(
        store,
        crawler_version=__version__,
        parser_version=adapter.parser.version,
    )
    artifact = acquirer.capture_response(
        source_key=metadata.key,
        response=response,
        retrieved_at=retrieved_at,
        restriction=metadata.distribution_restriction,
    )
    assembler = SnapshotAssembler(metadata.key, expected_parts=1)
    assembler.add(artifact)
    snapshot = assembler.finalize(
        CompletenessEvidence(CompletenessKind.SINGLE_FILE, {"http_status": response.status})
    )
    build = process_snapshot(
        adapter,
        snapshot,
        store,
        observed_at=retrieved_at,
        normalizer=AdditiveNormalizer(metadata.normalization_version, (_ghanaian_name_rule(),)),
    )
    destination = _publish_release(
        adapter, build, output=args.output, schemas=args.schemas, generated_at=retrieved_at
    )
    print(
        json.dumps(
            {"dataset": metadata.key, "status": "published", "release": str(destination)},
            sort_keys=True,
        )
    )
    return 0


def _build_pep_release(args: argparse.Namespace) -> int:
    """Build a release from a PEP-shaped dataset's reviewed intake file.

    There is no live crawl for these sources (see README, "Name library" and
    the Ghana PEP sections): a human-maintained, dual-reviewed intake file
    *is* the artifact, not something fetched over HTTP. Only entries already
    marked ``state: PUBLISHED`` in the intake file are ever built into a
    release -- the parser enforces this, not this command.
    """

    adapter = load_pep_adapter(args.dataset)
    intake_path = args.intake or (
        _repository_root() / f"datasets/{args.dataset}/intake/current.yml"
    )
    body = intake_path.read_bytes()
    store = LocalBlobStore(args.work / "capture_store")
    acquirer = Acquirer(
        store,
        crawler_version=__version__,
        parser_version=adapter.parser.version,
    )
    retrieved_at = datetime.now(UTC)
    artifact = acquirer.capture_response(
        source_key=adapter.metadata.key,
        response=FetchResponse(
            request_url=f"local-intake://{args.dataset}/{intake_path.name}",
            status=200,
            headers={"content-type": "application/yaml"},
            body=body,
        ),
        retrieved_at=retrieved_at,
        restriction=adapter.metadata.distribution_restriction,
    )
    assembler = SnapshotAssembler(adapter.metadata.key, expected_parts=1)
    assembler.add(artifact)
    snapshot = assembler.finalize(
        CompletenessEvidence(CompletenessKind.SINGLE_FILE, {"intake_file": True})
    )
    build = process_pep_snapshot(
        adapter,
        snapshot,
        store,
        observed_at=retrieved_at,
        normalizer=AdditiveNormalizer(adapter.metadata.normalization_version, (_ghanaian_name_rule(),)),
    )
    destination = _publish_release(
        adapter, build, output=args.output, schemas=args.schemas, generated_at=retrieved_at
    )
    print(
        json.dumps(
            {
                "dataset": adapter.metadata.key,
                "status": "published",
                "release": str(destination),
                "published_assertions": len(build.pep_assertions),
            },
            sort_keys=True,
        )
    )
    return 0


def _record_fixture(args: argparse.Namespace) -> int:
    """Capture one live response to disk for local fixture development.

    This is the offline-development counterpart described in the system
    documentation's local-development guidance: a stable recorded input, not
    a publishable fixture. The output is a live capture and may contain real
    personal data (e.g. real sanctioned or PEP names); per
    ``docs/public-private-boundary.md`` it must never be committed as-is —
    review it, replace real values with synthetic ones, and only the
    resulting synthetic file belongs under ``tests/fixtures/``.
    """

    adapter = load_adapter(args.dataset)
    url = args.url or adapter.metadata.source_endpoint
    if not url:
        raise ValueError(
            f"no --url given and datasets/{args.dataset}/metadata.yml has no source_endpoint"
        )
    transport = HttpFetchTransport(timeout=args.timeout, max_retries=args.retries)
    response = transport.fetch(FetchRequest(url=url, headers={}))

    args.output.mkdir(parents=True, exist_ok=True)
    body_path = args.output / args.filename
    body_path.write_bytes(response.body)
    manifest = {
        "recorded_from": url,
        "status": response.status,
        "sha256": hashlib.sha256(response.body).hexdigest(),
        "byte_length": len(response.body),
        "headers": response.headers,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    (args.output / f"{args.filename}.manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "recorded": str(body_path),
                "warning": (
                    "live capture, not a synthetic fixture -- do not commit as-is; "
                    "see docs/public-private-boundary.md"
                ),
            },
            sort_keys=True,
        )
    )
    return 0


def _validate_dataset(args: argparse.Namespace) -> int:
    metadata = DatasetMetadata.from_path(args.metadata)
    metadata.validate()
    print(json.dumps({"dataset": metadata.key, "status": "valid"}, sort_keys=True))
    return 0


def _iter_machine_dataset_keys(root: Path) -> list[str]:
    """Every `datasets/<key>/` directory that has a `metadata.yml` -- excludes
    non-dataset directories like `gh_name_library` (a shared data artifact, no
    metadata.yml of its own) automatically, by the same criterion `load_adapter`
    itself depends on existing.
    """
    keys = []
    for entry in sorted((root / "datasets").iterdir()):
        if entry.is_dir() and (entry / "metadata.yml").is_file():
            keys.append(entry.name)
    return keys


def _list_sources(args: argparse.Namespace) -> int:
    root = _repository_root()
    rows = []
    for key in _iter_machine_dataset_keys(root):
        metadata = DatasetMetadata.from_path(root / "datasets" / key / "metadata.yml")
        rows.append(
            {
                "source_key": metadata.key,
                "title": metadata.title,
                "publisher_authority": metadata.publisher_authority,
                "risk_semantics": metadata.risk_semantics,
                "legal_basis": metadata.legal_basis,
                "expected_cadence": metadata.expected_cadence,
                "freshness_slo_hours": metadata.freshness_slo_hours,
                "anomaly_threshold": metadata.anomaly_threshold,
                "distribution_restriction": metadata.distribution_restriction.value,
                "source_endpoint": metadata.source_endpoint,
                "live_crawl_enabled": metadata.source_endpoint is not None,
                "parser_version": metadata.parser_version,
                "mapping_version": metadata.mapping_version,
            }
        )
    print(json.dumps(rows, sort_keys=True, indent=2))
    return 0


def _crawl(args: argparse.Namespace) -> int:
    root = _repository_root()
    run_store = CrawlRunStore(args.work / "runs")
    dataset_keys = (
        [key for key in _iter_machine_dataset_keys(root) if _has_live_endpoint(root, key)]
        if args.all
        else [args.dataset]
    )
    results = []
    exit_codes = []
    for dataset_key in dataset_keys:
        try:
            run = run_source(
                dataset_key,
                work=args.work,
                output=args.output,
                schemas=args.schemas,
                repository_root=root,
                run_store=run_store,
                trigger="cli",
                timeout=args.timeout,
                retries=args.retries,
            )
        except RunAlreadyInProgress as exc:
            results.append({"dataset": dataset_key, "error": "RunAlreadyInProgress", "message": str(exc)})
            exit_codes.append(7)
            continue
        results.append({"dataset": dataset_key, "run_id": run.run_id, "state": run.state, "release_id": run.release_id})
        exit_codes.append(exit_code_for(run))
    print(json.dumps(results if args.all else results[0], sort_keys=True, indent=2 if args.all else None))
    if not exit_codes:
        return 0
    # --all: report the single worst outcome as the process exit code (a scheduler wrapper
    # checks this to decide whether to alert; per-source detail is in the JSON above and in
    # each source's own run records, not lost by this aggregation).
    return max(exit_codes)


def _has_live_endpoint(root: Path, dataset_key: str) -> bool:
    metadata = DatasetMetadata.from_path(root / "datasets" / dataset_key / "metadata.yml")
    return metadata.source_endpoint is not None


def _list_runs(args: argparse.Namespace) -> int:
    run_store = CrawlRunStore(args.work / "runs")
    runs = run_store.list_runs(args.source)
    print(
        json.dumps(
            [
                {
                    "run_id": run.run_id,
                    "source_key": run.source_key,
                    "state": run.state,
                    "trigger": run.trigger,
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                    "entities_produced": run.entities_produced,
                    "diff_summary": run.diff_summary,
                    "release_id": run.release_id,
                    "error_code": run.error_code,
                }
                for run in runs
            ],
            sort_keys=True,
            indent=2,
        )
    )
    return 0


def _inspect_run(args: argparse.Namespace) -> int:
    run_store = CrawlRunStore(args.work / "runs")
    if args.source:
        run = run_store.load(args.source, args.run_id)
    else:
        candidates = [run for run in run_store.list_runs() if run.run_id == args.run_id]
        if not candidates:
            raise ValueError(f"no run {args.run_id!r} found under {args.work / 'runs'}")
        run = candidates[0]
    print(json.dumps(run.to_dict(), sort_keys=True, indent=2))
    return 0


def _list_releases(args: argparse.Namespace) -> int:
    rows = []
    for entry in sorted(args.output.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        manifest_path = entry / "manifest.json"
        if not manifest_path.is_file():
            continue
        manifest = read_manifest(entry)
        if args.dataset and args.dataset not in {item["key"] for item in manifest["datasets"]}:
            continue
        rows.append(
            {
                "release_id": manifest["release_id"],
                "generated_at": manifest["generated_at"],
                "status": manifest["status"],
                "datasets": [item["key"] for item in manifest["datasets"]],
                "entity_count": sum(int(item["entity_count"]) for item in manifest["datasets"]),
                "crawler_revision": manifest["crawler_revision"],
                "path": str(entry),
            }
        )
    print(json.dumps(rows, sort_keys=True, indent=2))
    return 0


def _compare_releases(args: argparse.Namespace) -> int:
    old_entities = read_entities(args.old_release)
    new_entities = read_entities(args.new_release)
    diff = diff_releases(old_entities, new_entities)
    summary = diff.as_summary_dict()
    output: dict[str, object] = {
        "old_release": str(args.old_release),
        "new_release": str(args.new_release),
        "summary": summary,
    }
    if args.detail:
        output["added"] = [
            {"entity_id": c.entity_id, "schema": c.schema, "caption": c.caption} for c in diff.added
        ]
        output["removed"] = [
            {"entity_id": c.entity_id, "schema": c.schema, "caption": c.caption} for c in diff.removed
        ]
        output["modified"] = [
            {
                "entity_id": c.entity_id,
                "schema": c.schema,
                "caption": c.caption,
                "property_changes": [
                    {"property": p.property, "before": list(p.before), "after": list(p.after)}
                    for p in c.property_changes
                ],
            }
            for c in diff.modified
        ]
    print(json.dumps(output, sort_keys=True, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    root = _repository_root()
    parser = argparse.ArgumentParser(prog="screening-crawler")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(required=True)

    validate = commands.add_parser(
        "validate-release",
        help="verify schema, checksums, counts, and provenance",
    )
    validate.add_argument("bundle", type=Path)
    validate.add_argument("--schemas", type=Path, default=root / "schemas")
    validate.set_defaults(handler=_validate_release)

    fixture = commands.add_parser(
        "build-fixture-release",
        help="build an offline synthetic UN release",
    )
    fixture.add_argument(
        "--fixture",
        type=Path,
        default=root / "tests/fixtures/un_sc_consolidated/synthetic.xml",
    )
    fixture.add_argument("--work", type=Path, required=True)
    fixture.add_argument("--output", type=Path, required=True)
    fixture.add_argument("--schemas", type=Path, default=root / "schemas")
    fixture.set_defaults(handler=_fixture_release)

    crawl = commands.add_parser(
        "crawl-dataset",
        help="fetch a dataset's confirmed live source endpoint and publish a release",
    )
    crawl.add_argument("dataset", help="dataset key, e.g. un_sc_consolidated")
    crawl.add_argument("--work", type=Path, required=True)
    crawl.add_argument("--output", type=Path, required=True)
    crawl.add_argument("--schemas", type=Path, default=root / "schemas")
    crawl.add_argument("--timeout", type=float, default=30.0)
    crawl.add_argument("--retries", type=int, default=3)
    crawl.add_argument("--if-none-match", dest="if_none_match", default=None)
    crawl.add_argument("--if-modified-since", dest="if_modified_since", default=None)
    crawl.set_defaults(handler=_crawl_dataset)

    pep = commands.add_parser(
        "build-pep-release",
        help="build a release from a PEP-shaped dataset's reviewed intake file",
    )
    pep.add_argument("dataset", help="dataset key, e.g. gh_parliament")
    pep.add_argument(
        "--intake", type=Path, default=None, help="defaults to datasets/<dataset>/intake/current.yml"
    )
    pep.add_argument("--work", type=Path, required=True)
    pep.add_argument("--output", type=Path, required=True)
    pep.add_argument("--schemas", type=Path, default=root / "schemas")
    pep.set_defaults(handler=_build_pep_release)

    record = commands.add_parser(
        "record-fixture",
        help="capture a live response to disk for local fixture development",
    )
    record.add_argument("dataset", help="dataset key, e.g. un_sc_consolidated")
    record.add_argument("--url", default=None, help="override metadata.yml's source_endpoint")
    record.add_argument("--output", type=Path, required=True)
    record.add_argument("--filename", default="capture.bin")
    record.add_argument("--timeout", type=float, default=30.0)
    record.add_argument("--retries", type=int, default=3)
    record.set_defaults(handler=_record_fixture)

    dataset = commands.add_parser(
        "validate-dataset",
        help="validate dataset metadata without acquisition",
    )
    dataset.add_argument("metadata", type=Path)
    dataset.set_defaults(handler=_validate_dataset)

    sources = commands.add_parser(
        "list-sources",
        help="list every machine-crawled dataset's registry metadata",
    )
    sources.set_defaults(handler=_list_sources)

    live_crawl = commands.add_parser(
        "crawl",
        help=(
            "orchestrated live crawl: acquire, parse/normalize/validate, diff against the "
            "dataset's previous release, gate on catastrophic change, and publish -- "
            "suitable for a systemd timer/cron entry"
        ),
    )
    live_crawl.add_argument(
        "dataset", nargs="?", default=None, help="dataset key, e.g. un_sc_consolidated (omit with --all)"
    )
    live_crawl.add_argument(
        "--all", action="store_true", help="crawl every dataset with a configured source_endpoint"
    )
    live_crawl.add_argument("--work", type=Path, required=True)
    live_crawl.add_argument("--output", type=Path, required=True)
    live_crawl.add_argument("--schemas", type=Path, default=root / "schemas")
    live_crawl.add_argument("--timeout", type=float, default=30.0)
    live_crawl.add_argument("--retries", type=int, default=3)
    live_crawl.set_defaults(handler=_crawl)

    runs = commands.add_parser("list-runs", help="list recorded crawl runs")
    runs.add_argument("--work", type=Path, required=True)
    runs.add_argument("--source", default=None, help="restrict to one dataset key")
    runs.set_defaults(handler=_list_runs)

    inspect_run = commands.add_parser("inspect-run", help="show one crawl run's full record")
    inspect_run.add_argument("run_id")
    inspect_run.add_argument("--work", type=Path, required=True)
    inspect_run.add_argument("--source", default=None, help="dataset key, speeds up lookup")
    inspect_run.set_defaults(handler=_inspect_run)

    releases = commands.add_parser("list-releases", help="list published releases under an output root")
    releases.add_argument("--output", type=Path, required=True)
    releases.add_argument("--dataset", default=None, help="restrict to releases containing this dataset key")
    releases.set_defaults(handler=_list_releases)

    compare = commands.add_parser(
        "compare-releases", help="deterministic entity-level diff between two release directories"
    )
    compare.add_argument("old_release", type=Path)
    compare.add_argument("new_release", type=Path)
    compare.add_argument("--detail", action="store_true", help="include per-entity added/modified/removed rows")
    compare.set_defaults(handler=_compare_releases)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except Exception as exc:
        print(
            json.dumps({"error": type(exc).__name__, "message": str(exc)}),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
