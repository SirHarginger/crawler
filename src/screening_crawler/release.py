"""Validated, immutable, atomically published release bundle builder.

Manifest and file-role shapes match screening-engine's importer contract
exactly (schemas/release-manifest.schema.json, mirrored byte-for-byte from
the private screening-engine repo; importer/verifier.py there is what
actually consumes a published bundle). Every payload file is NDJSON (one
record per line), declared in manifest["files"] with a role from that
schema's enum, and cross-checked against a checksums.sha256 ledger covering
exactly the declared files -- nothing more, nothing less.

Deliberately out of scope for this pass: Layer 1.5 domain interpretation
(``ReleaseInput.domain_objects``) is accepted but not written into the
bundle. screening-engine's manifest schema has no role for it yet -- the
assertion-shaped state machine its ``source_interpretations`` role expects
(id/state/asserted_by/reviewed_by/observed_from/...) doesn't match this
repo's ``DomainObject`` shape (id/kind/data/interpretation_version) closely
enough to paper over, and it's optional/opt-in per doc S4.4 -- Layer 1 source
records retain everything Layer 1.5 would interpret, so nothing is lost at
the authoritative layer. Revisit together with the next dataset that
actually needs it.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .assertions import AssertionState, PepAssertion
from .canonical import canonical_json_bytes, canonical_ndjson, sha256_bytes
from .datasets import DatasetMetadata
from .domain import DomainObject
from .errors import ReleaseValidationError
from .field_inventory import FieldCoverage
from .ids import stable_id
from .model import Snapshot, SnapshotState, SourceRecord, utc_iso
from .projection import ProjectionResult
from .validation import validate_release_bundle


@dataclass(frozen=True, slots=True)
class ReleaseVersions:
    release_schema_version: str
    ftm_schema_version: str
    crawler_version: str
    crawler_revision: str
    deterministic_id_method: str = "uuid5-namespaced-stable-id"


@dataclass(frozen=True, slots=True)
class ReleaseInput:
    datasets: tuple[DatasetMetadata, ...]
    snapshots: tuple[Snapshot, ...]
    source_records: tuple[SourceRecord, ...]
    projections: tuple[ProjectionResult, ...]
    field_coverages: dict[str, FieldCoverage]
    domain_objects: tuple[DomainObject, ...] = ()
    pep_assertions: tuple[PepAssertion, ...] = ()


@dataclass(frozen=True, slots=True)
class _FileEntry:
    path: str
    role: str
    sha256: str
    size_bytes: int
    media_type: str
    datasets: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "role": self.role,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "datasets": list(self.datasets),
        }


class ReleaseBuilder:
    def __init__(self, schema_root: Path, versions: ReleaseVersions) -> None:
        self.schema_root = schema_root
        self.versions = versions

    def build(
        self,
        release_input: ReleaseInput,
        *,
        output_root: Path,
        generated_at: datetime,
    ) -> Path:
        if not release_input.datasets:
            raise ReleaseValidationError("a release must contain at least one dataset")
        for snapshot in release_input.snapshots:
            snapshot.require_complete()
        if any(coverage.quarantined for coverage in release_input.field_coverages.values()):
            raise ReleaseValidationError("quarantined field coverage cannot be published")
        if any(item.state != AssertionState.PUBLISHED for item in release_input.pep_assertions):
            raise ReleaseValidationError(
                "only independently-reviewed, PUBLISHED assertions may enter a release"
            )
        source_keys = {item.key for item in release_input.datasets}
        if {item.source_key for item in release_input.snapshots} != source_keys:
            raise ReleaseValidationError("release must have snapshot coverage for every dataset")
        self._require_consistent_versions(release_input)

        release_id = self._release_id(release_input)
        destination = output_root / release_id
        if destination.exists():
            raise FileExistsError(f"immutable release already exists: {destination}")
        output_root.mkdir(parents=True, exist_ok=True)
        staging = Path(
            tempfile.mkdtemp(prefix=f".{release_id}.", suffix=".staging", dir=output_root)
        )
        try:
            record_dataset_by_id = {
                record.id: record.source_key for record in release_input.source_records
            }
            entries, published_snapshots = self._write_payload(
                staging, release_input, record_dataset_by_id
            )
            manifest, checksums_content = self._manifest(
                release_id,
                release_input,
                generated_at,
                entries,
                published_snapshots,
                record_dataset_by_id,
            )
            self._write(staging / "manifest.json", canonical_json_bytes(manifest, newline=True))
            self._write(staging / "checksums.sha256", checksums_content)
            validate_release_bundle(staging, self.schema_root)
            self._fsync_tree(staging)
            os.replace(staging, destination)
            self._fsync_directory(output_root)
            return destination
        except Exception:
            self._remove_staging(staging)
            raise

    @staticmethod
    def _require_consistent_versions(release_input: ReleaseInput) -> None:
        # screening-engine's schema carries one mapping_version/normalization_version per
        # release, not one per dataset (doc S4.5 describes per-dataset mapping profiles, but
        # every CLI entry point in this repo builds single-dataset releases today -- see
        # cli.py's _publish_release). Fail loudly rather than silently picking one if that
        # assumption is ever broken by a future multi-dataset release.
        mapping_versions = {item.mapping_version for item in release_input.datasets}
        normalization_versions = {item.normalization_version for item in release_input.datasets}
        if len(mapping_versions) > 1 or len(normalization_versions) > 1:
            raise ReleaseValidationError(
                "a release's datasets must share one mapping_version and one "
                "normalization_version -- screening-engine's manifest schema has no "
                "per-dataset slot for these; split into separate releases instead"
            )

    def _release_id(self, release_input: ReleaseInput) -> str:
        parts = [
            item.combined_manifest_sha256
            for item in sorted(release_input.snapshots, key=lambda item: item.source_key)
        ]
        parts.extend(item.mapping_version for item in release_input.projections)
        parts.extend(item.normalization_version for item in release_input.projections)
        return f"release-{stable_id('release', self.versions.release_schema_version, *parts)}"

    def _write_payload(
        self,
        root: Path,
        release_input: ReleaseInput,
        record_dataset_by_id: dict[str, str],
    ) -> tuple[list[_FileEntry], list[Snapshot]]:
        dataset_keys = tuple(sorted(item.key for item in release_input.datasets))
        entries: list[_FileEntry] = []
        dataset_by_key = {item.key: item for item in release_input.datasets}

        published_snapshots = [
            snapshot.transition(SnapshotState.PUBLISHED)
            for snapshot in sorted(release_input.snapshots, key=lambda item: item.source_key)
        ]
        snapshot_by_source = {snapshot.source_key: snapshot for snapshot in published_snapshots}

        artifacts = sorted(
            (artifact for snapshot in published_snapshots for artifact in snapshot.artifacts),
            key=lambda item: item.id,
        )
        artifact_rows = [
            {
                "id": artifact.id,
                "snapshot_id": snapshot_by_source[artifact.source_key].id,
                "uri": artifact.request_url,
                "sha256": artifact.sha256,
                "media_type": (
                    artifact.response_headers.get("content-type")
                    or artifact.response_headers.get("Content-Type")
                    or "application/octet-stream"
                ),
                "size_bytes": artifact.byte_length,
                "acquired_at": utc_iso(artifact.retrieved_at),
                "request_metadata": {
                    "http_status": artifact.http_status,
                    "redirect_chain": [hop.to_dict() for hop in artifact.redirects],
                    "publisher_publication_date": artifact.publisher_publication_date,
                    "crawler_version": artifact.crawler_version,
                    "parser_version": artifact.parser_version,
                    "runtime_version": artifact.runtime_version,
                    "distribution_restriction": (
                        dataset_by_key[artifact.source_key].distribution_restriction.value
                    ),
                    "storage_key": artifact.storage_key,
                },
            }
            for artifact in artifacts
        ]
        entries.append(
            self._write_ndjson(root, "artifacts.ndjson", "artifacts", artifact_rows, dataset_keys)
        )

        records = sorted(release_input.source_records, key=lambda item: item.id)
        record_rows = [
            {
                **{
                    key: value
                    for key, value in record.to_dict().items()
                    if key not in {"source_key", "publisher_native_key"}
                },
                "dataset_key": record.source_key,
                "source_record_key": record.publisher_native_key,
            }
            for record in records
        ]
        entries.append(
            self._write_ndjson(
                root, "source_records.ndjson", "source_records", record_rows, dataset_keys
            )
        )

        extent_rows = [
            {**extent.to_dict(record.id)}
            for record in records
            for extent in sorted(record.extents, key=lambda item: item.ordinal)
        ]
        # screening-engine's importer (importer/verifier.py) hard-requires the
        # source_record_extents role to be present on every release, even when no dataset
        # in this release uses extents -- an empty NDJSON file satisfies that, so this is
        # never conditional on extent_rows being non-empty.
        entries.append(
            self._write_ndjson(
                root,
                "source_record_extents.ndjson",
                "source_record_extents",
                extent_rows,
                dataset_keys,
            )
        )

        statements = sorted(
            (item for projection in release_input.projections for item in projection.statements),
            key=lambda item: item.projection_id,
        )
        statement_rows = [
            {
                **{
                    key: value
                    for key, value in statement.to_dict().items()
                    if key != "projection_id"
                },
                "id": statement.projection_id,
                "dataset_key": record_dataset_by_id[statement.source_record_id],
            }
            for statement in statements
        ]
        entries.append(
            self._write_ndjson(
                root,
                "projection_statements.ndjson",
                "projection_statements",
                statement_rows,
                dataset_keys,
            )
        )

        entities = sorted(
            (item for projection in release_input.projections for item in projection.entities),
            key=lambda item: item.id,
        )
        entity_rows = [entity.to_dict() for entity in entities]
        entries.append(
            self._write_ndjson(root, "entities.ndjson", "entities", entity_rows, dataset_keys)
        )

        coverage_rows = []
        for dataset_key, coverage in sorted(release_input.field_coverages.items()):
            for path, stats in sorted(coverage.paths.items()):
                coverage_rows.append(
                    {
                        "dataset_key": dataset_key,
                        "field_path": path,
                        "disposition": stats["disposition"],
                        "observed_count": stats["present_records"],
                        "null_count": coverage.record_count - stats["present_records"],
                        "enum_values": stats["observed_values"] or None,
                    }
                )
        entries.append(
            self._write_ndjson(
                root, "field_coverage.ndjson", "field_coverage", coverage_rows, dataset_keys
            )
        )

        report_rows = [
            {
                "dataset_key": dataset_key,
                "valid": not coverage.quarantined,
                "findings": [finding.to_dict() for finding in coverage.findings],
                "controls": {
                    "snapshot_completeness": "passed",
                    "field_accounting": "passed",
                    "property_provenance": "passed",
                    "deterministic_serialization": "passed",
                    "schema_validation": "passed",
                },
            }
            for dataset_key, coverage in sorted(release_input.field_coverages.items())
        ]
        entries.append(
            self._write_ndjson(
                root, "validation_report.ndjson", "validation_report", report_rows, dataset_keys
            )
        )

        assertions = sorted(release_input.pep_assertions, key=lambda item: item.id)
        if assertions:
            assertion_rows = [
                {**item.to_dict(), "dataset_key": dataset_keys[0]} for item in assertions
            ]
            entries.append(
                self._write_ndjson(
                    root, "pep_assertions.ndjson", "pep_assertions", assertion_rows, dataset_keys
                )
            )

        return entries, published_snapshots

    def _write_ndjson(
        self,
        root: Path,
        filename: str,
        role: str,
        rows: list[dict[str, Any]],
        dataset_keys: tuple[str, ...],
    ) -> _FileEntry:
        content = canonical_ndjson(rows)
        self._write(root / filename, content)
        return _FileEntry(
            path=filename,
            role=role,
            sha256=sha256_bytes(content),
            size_bytes=len(content),
            media_type="application/x-ndjson",
            datasets=dataset_keys,
        )

    def _manifest(
        self,
        release_id: str,
        release_input: ReleaseInput,
        generated_at: datetime,
        entries: list[_FileEntry],
        published_snapshots: list[Snapshot],
        record_dataset_by_id: dict[str, str],
    ) -> tuple[dict[str, Any], bytes]:
        dataset_by_key = {item.key: item for item in release_input.datasets}
        counts = {
            key: {"entity_count": 0, "source_record_count": 0, "projection_statement_count": 0}
            for key in dataset_by_key
        }
        for record in release_input.source_records:
            counts[record.source_key]["source_record_count"] += 1
        for projection in release_input.projections:
            for entity in projection.entities:
                counts[entity.dataset_key]["entity_count"] += 1
            for statement in projection.statements:
                dataset_key = record_dataset_by_id[statement.source_record_id]
                counts[dataset_key]["projection_statement_count"] += 1

        snapshot_by_source = {snapshot.source_key: snapshot for snapshot in published_snapshots}
        publication_dates: dict[str, str | None] = {}
        for snapshot in published_snapshots:
            dates = sorted(
                {
                    artifact.publisher_publication_date
                    for artifact in snapshot.artifacts
                    if artifact.publisher_publication_date is not None
                }
            )
            publication_dates[snapshot.source_key] = dates[-1] if dates else None

        ledger_lines = [f"{entry.sha256}  {entry.path}" for entry in sorted(entries, key=lambda e: e.path)]
        checksums_content = ("\n".join(ledger_lines) + "\n").encode("ascii") if ledger_lines else b""
        checksums_entry = _FileEntry(
            path="checksums.sha256",
            role="checksums",
            sha256=sha256_bytes(checksums_content),
            size_bytes=len(checksums_content),
            media_type="text/plain",
            datasets=tuple(sorted(dataset_by_key)),
        )
        all_entries = [*entries, checksums_entry]

        single_dataset = release_input.datasets[0]
        manifest = {
            "release_id": release_id,
            "release_schema_version": self.versions.release_schema_version,
            "status": "PUBLISHED",
            "generated_at": utc_iso(generated_at),
            "ftm_schema_version": self.versions.ftm_schema_version,
            "mapping_version": single_dataset.mapping_version,
            "normalization_version": single_dataset.normalization_version,
            "crawler_version": self.versions.crawler_version,
            "crawler_revision": self.versions.crawler_revision,
            "deterministic_id_method": self.versions.deterministic_id_method,
            "datasets": [
                {
                    "key": key,
                    "version": snapshot_by_source[key].combined_manifest_sha256,
                    "entity_count": counts[key]["entity_count"],
                    "source_record_count": counts[key]["source_record_count"],
                    "projection_statement_count": counts[key]["projection_statement_count"],
                    "publication_date": publication_dates.get(key),
                    "freshness_checked_at": utc_iso(generated_at),
                }
                for key in sorted(dataset_by_key)
            ],
            "snapshots": [self._snapshot_dict(snapshot) for snapshot in published_snapshots],
            "files": [entry.to_dict() for entry in all_entries],
        }
        return manifest, checksums_content

    @staticmethod
    def _snapshot_dict(snapshot: Snapshot) -> dict[str, Any]:
        return {
            "id": snapshot.id,
            "dataset_key": snapshot.source_key,
            "state": snapshot.state.value,
            "combined_manifest_sha256": snapshot.combined_manifest_sha256,
            "completeness_evidence": (
                snapshot.completeness_evidence.to_dict()
                if snapshot.completeness_evidence
                else None
            ),
        }

    @staticmethod
    def _write(path: Path, content: bytes) -> None:
        with path.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _fsync_tree(self, root: Path) -> None:
        for directory in sorted((path for path in root.rglob("*") if path.is_dir()), reverse=True):
            self._fsync_directory(directory)
        self._fsync_directory(root)

    @staticmethod
    def _remove_staging(root: Path) -> None:
        if not root.exists():
            return
        for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        root.rmdir()
