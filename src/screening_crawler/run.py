"""The common live-crawler engine: run lifecycle/state, duplicate-run locking, conditional
HTTP reuse, raw artifact persistence, release-to-release diffing, statistical validation, and
release construction -- all wired together once here so that adding a new machine-crawled
source is "point a new adapter/metadata.yml at this engine", not "reimplement a run loop".

Everything this module orchestrates already exists and is reused as-is: `HttpFetchTransport`
(transport.py), `Acquirer`/`SnapshotAssembler` (acquisition.py), `process_snapshot`
(pipeline.py), `ReleaseBuilder` (release.py, via the same `_publish_release` helper cli.py's
existing `crawl-dataset` command uses), `diff_releases`/`evaluate_statistical_gate` (diff.py,
new this milestone but reused by both the run engine and the CLI's standalone
`compare-releases` command). This module adds only what didn't already exist: the run
lifecycle state machine, local run-record persistence, duplicate-run locking, and the
release-to-release diff/statistical gates.

State machine (module docstring, not enforced by an ORM/DB -- run records are plain JSON,
matching this repo's "no database driver in this repo" boundary):

    SCHEDULED -> FETCHING -> FETCHED -> PARSING -> DIFFING -> READY -> PUBLISHED
                          |-> UNCHANGED (304, terminal, no release built)
              -> FETCH_FAILED (terminal)
    FETCHED   -> PARSE_FAILED | VALIDATION_FAILED (terminal; see _run_pipeline's docstring
                 for why parse/normalize/validate aren't independently catchable stages)
    DIFFING   -> QUARANTINED (terminal; catastrophic release-over-release change)

NORMALIZING/VALIDATING transitions are recorded (the task's required state vocabulary
includes them), but -- since `pipeline.process_snapshot` performs extraction, field-inventory
validation, and FollowTheMoney projection as one call, with no seam between "parsing
finished" and "normalizing started" that this module could observe without forking that
function -- both are recorded together, immediately after PARSING succeeds, rather than as
independently timed stages. This is documented here rather than silently pretended away.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from . import __version__
from .acquisition import Acquirer, FetchRequest, SnapshotAssembler
from .canonical import canonical_json_bytes
from .datasets import DatasetAdapter, load_adapter
from .diff import CatastrophicChangeDetected, diff_releases, evaluate_statistical_gate
from .errors import FetchError, QuarantineRequired
from .locking import run_lock
from .model import CompletenessEvidence, CompletenessKind
from .name_library import OrthographyVariantTable
from .normalization import AdditiveNormalizer, GhanaianNameRule
from .pipeline import process_snapshot
from .release import ReleaseBuilder, ReleaseInput, ReleaseVersions
from .release_reader import dataset_entry, read_entities, read_manifest
from .storage import LocalBlobStore
from .transport import HttpFetchTransport

RunState = Literal[
    "SCHEDULED",
    "FETCHING",
    "FETCHED",
    "UNCHANGED",
    "PARSING",
    "NORMALIZING",
    "VALIDATING",
    "DIFFING",
    "READY",
    "PUBLISHED",
    "FETCH_FAILED",
    "PARSE_FAILED",
    "NORMALIZATION_FAILED",
    "VALIDATION_FAILED",
    "QUARANTINED",
]

TERMINAL_STATES: frozenset[RunState] = frozenset(
    {
        "UNCHANGED",
        "PUBLISHED",
        "FETCH_FAILED",
        "PARSE_FAILED",
        "NORMALIZATION_FAILED",
        "VALIDATION_FAILED",
        "QUARANTINED",
    }
)
FAILURE_STATES: frozenset[RunState] = frozenset(
    {"FETCH_FAILED", "PARSE_FAILED", "NORMALIZATION_FAILED", "VALIDATION_FAILED", "QUARANTINED"}
)

# Deterministic process exit codes (task requirement: "distinguish success, unchanged,
# validation quarantine and hard failure"). Never reuse 1 for more than one meaning -- a
# systemd timer/cron caller needs these to branch on, not just "did it print an error".
EXIT_PUBLISHED = 0
EXIT_UNCHANGED = 0  # "nothing to do" is success, not failure, for a scheduled caller
EXIT_QUARANTINED = 3
EXIT_VALIDATION_FAILED = 4
EXIT_PARSE_FAILED = 5
EXIT_FETCH_FAILED = 6
EXIT_LOCKED = 7
EXIT_UNEXPECTED = 1


def exit_code_for(run: CrawlRun) -> int:
    return {
        "PUBLISHED": EXIT_PUBLISHED,
        "UNCHANGED": EXIT_UNCHANGED,
        "QUARANTINED": EXIT_QUARANTINED,
        "VALIDATION_FAILED": EXIT_VALIDATION_FAILED,
        "PARSE_FAILED": EXIT_PARSE_FAILED,
        "NORMALIZATION_FAILED": EXIT_PARSE_FAILED,
        "FETCH_FAILED": EXIT_FETCH_FAILED,
    }.get(run.state, EXIT_UNEXPECTED)


_SECRET_LIKE = re.compile(
    r"(?i)(token|sig|signature|authorization|password|secret|api[_-]?key)=[^&\s\"']+"
)


def sanitize_error(message: str, *, max_length: int = 500) -> str:
    """Strip query-string-embedded credentials (eu_fsf's source_endpoint carries a `token=`
    query parameter; other sources may carry signed-URL `sig=`/`signature=` parameters the
    same way un_sc_consolidated's redirect target does) before a raw exception message is
    ever written to a run record. Bounded length so a pathological error (e.g. an entire
    malformed response body echoed into an exception) can't blow up run-record storage.
    """
    redacted = _SECRET_LIKE.sub(r"\1=REDACTED", str(message))
    if len(redacted) > max_length:
        redacted = redacted[: max_length - 3] + "..."
    return redacted


@dataclass(slots=True)
class RunTransition:
    state: RunState
    at: str
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "at": self.at, "detail": self.detail}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunTransition:
        return cls(state=data["state"], at=data["at"], detail=data.get("detail"))


@dataclass(slots=True)
class CrawlRun:
    run_id: str
    source_key: str
    trigger: str
    started_at: str
    transitions: list[RunTransition] = field(default_factory=list)
    finished_at: str | None = None
    http_status: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    content_type: str | None = None
    byte_size: int | None = None
    raw_sha256: str | None = None
    artifact_storage_key: str | None = None
    records_discovered: int | None = None
    records_parsed: int | None = None
    entities_produced: int | None = None
    warnings: list[str] = field(default_factory=list)
    validation_result: dict[str, Any] | None = None
    diff_summary: dict[str, Any] | None = None
    previous_release_id: str | None = None
    release_id: str | None = None
    # The SHA-256 of the published release's own on-disk manifest.json, hex-encoded --
    # computed from the real bytes after ReleaseBuilder writes them, the same way
    # screening-engine's importer.verifier.ReleaseVerifier independently computes
    # `releases.manifest_sha256` at import time. This is the sidecar-binding mechanism (doc:
    # crawl_run.json's release_id check alone only proves the sidecar *claims* to belong to
    # this directory, not that it actually observed this release's real content -- a sidecar
    # copied or hand-edited to claim a different release_id would pass that check trivially).
    # A sidecar can only present the correct value here by having genuinely built this exact
    # release; screening-engine cross-checks it against the manifest hash it already computed
    # independently at import time (reference_ops/ingest.py), not against anything the
    # sidecar alone asserts.
    release_manifest_sha256: str | None = None
    parser_version: str | None = None
    normalizer_version: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    @property
    def state(self) -> RunState:
        return self.transitions[-1].state if self.transitions else "SCHEDULED"

    def transition(self, state: RunState, *, detail: str | None = None, now: datetime | None = None) -> None:
        self.transitions.append(RunTransition(state=state, at=(now or datetime.now(UTC)).isoformat(), detail=detail))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source_key": self.source_key,
            "trigger": self.trigger,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "state": self.state,
            "transitions": [t.to_dict() for t in self.transitions],
            "http_status": self.http_status,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "content_type": self.content_type,
            "byte_size": self.byte_size,
            "raw_sha256": self.raw_sha256,
            "artifact_storage_key": self.artifact_storage_key,
            "records_discovered": self.records_discovered,
            "records_parsed": self.records_parsed,
            "entities_produced": self.entities_produced,
            "warnings": self.warnings,
            "validation_result": self.validation_result,
            "diff_summary": self.diff_summary,
            "previous_release_id": self.previous_release_id,
            "release_id": self.release_id,
            "release_manifest_sha256": self.release_manifest_sha256,
            "parser_version": self.parser_version,
            "normalizer_version": self.normalizer_version,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CrawlRun:
        run = cls(
            run_id=data["run_id"],
            source_key=data["source_key"],
            trigger=data["trigger"],
            started_at=data["started_at"],
            finished_at=data.get("finished_at"),
            http_status=data.get("http_status"),
            etag=data.get("etag"),
            last_modified=data.get("last_modified"),
            content_type=data.get("content_type"),
            byte_size=data.get("byte_size"),
            raw_sha256=data.get("raw_sha256"),
            artifact_storage_key=data.get("artifact_storage_key"),
            records_discovered=data.get("records_discovered"),
            records_parsed=data.get("records_parsed"),
            entities_produced=data.get("entities_produced"),
            warnings=list(data.get("warnings", [])),
            validation_result=data.get("validation_result"),
            diff_summary=data.get("diff_summary"),
            previous_release_id=data.get("previous_release_id"),
            release_id=data.get("release_id"),
            release_manifest_sha256=data.get("release_manifest_sha256"),
            parser_version=data.get("parser_version"),
            normalizer_version=data.get("normalizer_version"),
            error_code=data.get("error_code"),
            error_message=data.get("error_message"),
        )
        run.transitions = [RunTransition.from_dict(t) for t in data.get("transitions", [])]
        return run


class CrawlRunStore:
    """Local JSON run-record persistence -- `runs/<source_key>/<run_id>.json`. This is the
    crawler's own operational history, independent of whether a run ever produces a release
    that crosses into screening-engine; a FETCH_FAILED run is never transferred anywhere
    (there is nothing to transfer), so it must remain inspectable here or nowhere at all.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def _dir(self, source_key: str) -> Path:
        return self.root / source_key

    def _path(self, source_key: str, run_id: str) -> Path:
        return self._dir(source_key) / f"{run_id}.json"

    def save(self, run: CrawlRun) -> None:
        directory = self._dir(run.source_key)
        directory.mkdir(parents=True, exist_ok=True)
        path = self._path(run.source_key, run.run_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_bytes(canonical_json_bytes(run.to_dict(), newline=True))
        tmp.replace(path)

    def load(self, source_key: str, run_id: str) -> CrawlRun:
        data = self._path(source_key, run_id).read_text(encoding="utf-8")
        return CrawlRun.from_dict(json.loads(data))

    def list_runs(self, source_key: str | None = None) -> list[CrawlRun]:
        runs: list[CrawlRun] = []
        sources = [source_key] if source_key else [p.name for p in self.root.iterdir() if p.is_dir()] if self.root.is_dir() else []
        for key in sources:
            directory = self._dir(key)
            if not directory.is_dir():
                continue
            for run_file in directory.glob("*.json"):
                runs.append(CrawlRun.from_dict(json.loads(run_file.read_text(encoding="utf-8"))))
        return sorted(runs, key=lambda r: r.started_at, reverse=True)

    def last_successful(self, source_key: str) -> CrawlRun | None:
        for run in self.list_runs(source_key):
            if run.state == "PUBLISHED":
                return run
        return None


def _release_pointer_path(output: Path, source_key: str) -> Path:
    return output / ".pointers" / f"{source_key}.json"


def _read_release_pointer(output: Path, source_key: str) -> Path | None:
    path = _release_pointer_path(output, source_key)
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    release_dir = output / data["release_id"]
    return release_dir if release_dir.is_dir() else None


def _write_release_pointer(output: Path, source_key: str, release_dir: Path) -> None:
    pointer_dir = _release_pointer_path(output, source_key).parent
    pointer_dir.mkdir(parents=True, exist_ok=True)
    path = _release_pointer_path(output, source_key)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_bytes(canonical_json_bytes({"release_id": release_dir.name}, newline=True))
    tmp.replace(path)


def _ghanaian_name_rule(repository_root: Path) -> GhanaianNameRule:
    path = repository_root / "datasets/gh_name_library/orthography_variants.yml"
    return OrthographyVariantTable.from_path(path).to_rule()


def _git_revision(repository_root: Path) -> str:
    import subprocess

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository_root, capture_output=True, text=True, check=True, timeout=5
    )
    revision = result.stdout.strip()
    if not revision:
        raise RuntimeError("git rev-parse HEAD returned no revision")
    return revision


def run_source(
    dataset_key: str,
    *,
    work: Path,
    output: Path,
    schemas: Path,
    repository_root: Path,
    run_store: CrawlRunStore,
    trigger: str = "cli",
    timeout: float = 30.0,
    retries: int = 3,
    now: Callable[[], datetime] | None = None,
) -> CrawlRun:
    """Run one full crawl of `dataset_key` end to end: acquire, parse/normalize/validate
    (via the existing `process_snapshot`), diff against the dataset's previous release,
    gate on catastrophic change, and -- only if every gate passes -- publish an immutable
    release via the existing `ReleaseBuilder`. Returns the finished `CrawlRun` record
    (already persisted) whether the run succeeded, was unchanged, or failed/quarantined;
    raises only for a duplicate-run lock conflict or a genuinely unexpected bug (anything
    else is captured as a terminal run state instead of an exception, by design -- a
    scheduler driving many sources must be able to move on to the next one).
    """
    clock = now or (lambda: datetime.now(UTC))
    adapter: DatasetAdapter = load_adapter(dataset_key)
    metadata = adapter.metadata
    metadata.validate()
    if not metadata.source_endpoint:
        raise ValueError(
            f"datasets/{dataset_key}/metadata.yml has no source_endpoint configured; "
            "confirm the current publisher URL before enabling live acquisition."
        )

    run = CrawlRun(run_id=str(uuid.uuid4()), source_key=dataset_key, trigger=trigger, started_at=clock().isoformat())
    run.transition("SCHEDULED", now=clock())
    run_store.save(run)

    with run_lock(work / "locks", dataset_key):
        try:
            return _run_pipeline(
                run,
                adapter=adapter,
                work=work,
                output=output,
                schemas=schemas,
                repository_root=repository_root,
                run_store=run_store,
                timeout=timeout,
                retries=retries,
                clock=clock,
            )
        except Exception as exc:  # last-resort: an unanticipated bug, not a typed gate failure
            run.transition("VALIDATION_FAILED", detail=sanitize_error(str(exc)), now=clock())
            run.error_code = "UNEXPECTED_ERROR"
            run.error_message = sanitize_error(str(exc))
            run.finished_at = clock().isoformat()
            run_store.save(run)
            raise


def _run_pipeline(
    run: CrawlRun,
    *,
    adapter: DatasetAdapter,
    work: Path,
    output: Path,
    schemas: Path,
    repository_root: Path,
    run_store: CrawlRunStore,
    timeout: float,
    retries: int,
    clock: Callable[[], datetime],
) -> CrawlRun:
    metadata = adapter.metadata
    assert metadata.source_endpoint is not None

    previous_release_dir = _read_release_pointer(output, metadata.key)
    previous_run = run_store.last_successful(metadata.key)

    run.transition("FETCHING", now=clock())
    run_store.save(run)

    conditional_headers: dict[str, str] = {}
    if previous_run and previous_run.etag:
        conditional_headers["If-None-Match"] = previous_run.etag
    if previous_run and previous_run.last_modified:
        conditional_headers["If-Modified-Since"] = previous_run.last_modified

    transport = HttpFetchTransport(timeout=timeout, max_retries=retries)
    try:
        response = transport.fetch(FetchRequest(url=metadata.source_endpoint, headers=conditional_headers))
    except FetchError as exc:
        run.transition("FETCH_FAILED", detail=sanitize_error(str(exc)), now=clock())
        run.error_code = "FETCH_FAILED"
        run.error_message = sanitize_error(str(exc))
        run.finished_at = clock().isoformat()
        run_store.save(run)
        return run

    run.http_status = response.status
    run.etag = response.headers.get("etag") or response.headers.get("ETag")
    run.last_modified = response.headers.get("last-modified") or response.headers.get("Last-Modified")
    run.content_type = response.headers.get("content-type") or response.headers.get("Content-Type")

    if response.status == 304:
        run.transition("UNCHANGED", now=clock())
        run.finished_at = clock().isoformat()
        run_store.save(run)
        return run

    run.transition("FETCHED", now=clock())
    run.byte_size = len(response.body)
    run_store.save(run)

    retrieved_at = clock()
    store = LocalBlobStore(work / "capture_store")
    acquirer = Acquirer(store, crawler_version=__version__, parser_version=adapter.parser.version)
    artifact = acquirer.capture_response(
        source_key=metadata.key, response=response, retrieved_at=retrieved_at, restriction=metadata.distribution_restriction
    )
    run.raw_sha256 = artifact.sha256
    run.artifact_storage_key = artifact.storage_key

    assembler = SnapshotAssembler(metadata.key, expected_parts=1)
    assembler.add(artifact)
    snapshot = assembler.finalize(CompletenessEvidence(CompletenessKind.SINGLE_FILE, {"http_status": response.status}))

    run.transition("PARSING", now=clock())
    run_store.save(run)

    previous_source_record_count: int | None = None
    if previous_release_dir is not None:
        try:
            previous_manifest = read_manifest(previous_release_dir)
            entry = dataset_entry(previous_manifest, metadata.key)
            if entry is not None:
                previous_source_record_count = int(entry["source_record_count"])
        except Exception:  # best-effort enrichment; never block a run over a stale pointer
            previous_source_record_count = None

    try:
        build = process_snapshot(
            adapter,
            snapshot,
            store,
            observed_at=retrieved_at,
            normalizer=AdditiveNormalizer(metadata.normalization_version, (_ghanaian_name_rule(repository_root),)),
            previous_record_count=previous_source_record_count,
        )
    except QuarantineRequired as exc:
        run.transition("VALIDATION_FAILED", detail=sanitize_error(str(exc)), now=clock())
        run.error_code = "VALIDATION_FAILED"
        run.error_message = sanitize_error(str(exc))
        run.finished_at = clock().isoformat()
        run_store.save(run)
        return run
    except Exception as exc:
        run.transition("PARSE_FAILED", detail=sanitize_error(str(exc)), now=clock())
        run.error_code = "PARSE_FAILED"
        run.error_message = sanitize_error(str(exc))
        run.finished_at = clock().isoformat()
        run_store.save(run)
        return run

    run.records_discovered = len(build.source_records)
    run.records_parsed = len(build.source_records)
    run.entities_produced = len(build.projection.entities)
    run.parser_version = adapter.parser.version
    run.normalizer_version = metadata.normalization_version
    # See module docstring: normalize/validate already ran inside process_snapshot above --
    # these two transitions document that those stages completed, not that they're starting.
    run.transition("NORMALIZING", now=clock())
    run.transition("VALIDATING", now=clock())
    run.validation_result = {
        "technical": {"http_status": response.status, "byte_size": run.byte_size, "raw_sha256": run.raw_sha256},
        "structural": {"records_parsed": run.records_parsed, "entities_produced": run.entities_produced},
        "field_coverage_quarantined": build.field_coverage.quarantined,
    }
    run_store.save(run)

    run.transition("DIFFING", now=clock())
    is_first_release = previous_release_dir is None
    previous_entities = read_entities(previous_release_dir) if previous_release_dir is not None else []
    new_entities = [entity.to_dict() for entity in build.projection.entities]
    diff = diff_releases(previous_entities, new_entities)
    run.diff_summary = diff.as_summary_dict()
    run.previous_release_id = previous_release_dir.name if previous_release_dir is not None else None
    run_store.save(run)

    try:
        evaluate_statistical_gate(diff, anomaly_threshold=metadata.anomaly_threshold, is_first_release=is_first_release)
    except CatastrophicChangeDetected as exc:
        run.transition("QUARANTINED", detail=sanitize_error(str(exc)), now=clock())
        run.error_code = "QUARANTINED"
        run.error_message = sanitize_error(str(exc))
        run.finished_at = clock().isoformat()
        run_store.save(run)
        return run

    run.transition("READY", now=clock())
    run_store.save(run)

    release_input = ReleaseInput(
        datasets=(metadata,),
        snapshots=(build.snapshot,),
        source_records=build.source_records,
        projections=(build.projection,),
        field_coverages={metadata.key: build.field_coverage},
        domain_objects=build.domain_objects,
        pep_assertions=build.pep_assertions,
    )
    builder = ReleaseBuilder(
        schemas,
        ReleaseVersions(
            release_schema_version="1.0",
            ftm_schema_version="followthemoney-pinned-by-deployment",
            crawler_version=__version__,
            crawler_revision=_git_revision(repository_root),
        ),
    )
    destination = builder.build(release_input, output_root=output, generated_at=retrieved_at)

    run.release_id = destination.name
    # Computed from the real, just-written manifest.json bytes -- not copied from any
    # in-memory value -- so this can only be correct if the sidecar genuinely observed this
    # release's actual content. See CrawlRun.release_manifest_sha256's field comment.
    run.release_manifest_sha256 = hashlib.sha256(
        (destination / "manifest.json").read_bytes()
    ).hexdigest()
    run.transition("PUBLISHED", now=clock())
    run.finished_at = clock().isoformat()
    run_store.save(run)

    # Sidecar, not a manifest["files"] entry: informational operational provenance for
    # screening-engine's optional reference-ops ingestion step, deliberately outside the
    # strict, schema-validated, checksummed release contract (see docs/release-contract.md
    # and this module's own docstring) -- the existing importer/verifier never looks at it,
    # so this cannot break or even touch the existing release consumer.
    (destination / "crawl_run.json").write_bytes(canonical_json_bytes(run.to_dict(), newline=True))

    _write_release_pointer(output, metadata.key, destination)
    return run
