from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import FIXED_TIME

from datasets.un_sc_consolidated.crawler import adapter
from screening_crawler.errors import ReleaseValidationError
from screening_crawler.model import DistributionRestriction
from screening_crawler.release import ReleaseBuilder, ReleaseInput, ReleaseVersions
from screening_crawler.validation import validate_release_bundle

_TEST_REVISION = "0123456789abcdef0123456789abcdef01234567"


def _input(dataset_build, *, metadata=adapter.metadata) -> ReleaseInput:
    return ReleaseInput(
        datasets=(metadata,),
        snapshots=(dataset_build.snapshot,),
        source_records=dataset_build.source_records,
        projections=(dataset_build.projection,),
        field_coverages={metadata.key: dataset_build.field_coverage},
        domain_objects=dataset_build.domain_objects,
    )


def _builder() -> ReleaseBuilder:
    return ReleaseBuilder(
        Path("schemas"),
        ReleaseVersions(
            release_schema_version="1.0",
            ftm_schema_version="synthetic-ftm-v1",
            crawler_version="test-v1",
            crawler_revision=_TEST_REVISION,
        ),
    )


def test_release_contains_contract_files_and_valid_checksums(tmp_path: Path, dataset_build) -> None:
    release = _builder().build(_input(dataset_build), output_root=tmp_path, generated_at=FIXED_TIME)
    validate_release_bundle(release, Path("schemas"))
    expected = {
        "manifest.json",
        "artifacts.ndjson",
        "source_records.ndjson",
        "projection_statements.ndjson",
        "entities.ndjson",
        "field_coverage.ndjson",
        "validation_report.ndjson",
        "checksums.sha256",
    }
    assert expected.issubset({item.name for item in release.iterdir()})


def test_release_publication_is_immutable(tmp_path: Path, dataset_build) -> None:
    builder = _builder()
    builder.build(_input(dataset_build), output_root=tmp_path, generated_at=FIXED_TIME)
    with pytest.raises(FileExistsError):
        builder.build(_input(dataset_build), output_root=tmp_path, generated_at=FIXED_TIME)
    assert not list(tmp_path.glob("*.staging"))


def test_checksum_tampering_is_rejected(tmp_path: Path, dataset_build) -> None:
    release = _builder().build(_input(dataset_build), output_root=tmp_path, generated_at=FIXED_TIME)
    with (release / "entities.ndjson").open("ab") as handle:
        handle.write(b" ")
    # Appending a byte changes size first; screening-engine's own importer/verifier.py
    # checks size_bytes before re-hashing too (see importer/verifier.py), so this is the
    # expected first failure mode, not a weaker check than "checksum mismatch".
    with pytest.raises(ReleaseValidationError, match="size mismatch"):
        validate_release_bundle(release, Path("schemas"))


def test_manifest_counts_match_payload(tmp_path: Path, dataset_build) -> None:
    release = _builder().build(_input(dataset_build), output_root=tmp_path, generated_at=FIXED_TIME)
    manifest = json.loads((release / "manifest.json").read_text())
    [dataset_entry] = manifest["datasets"]
    assert dataset_entry["key"] == "un_sc_consolidated"
    assert dataset_entry["source_record_count"] == 2
    assert dataset_entry["entity_count"] == 2
    assert manifest["status"] == "PUBLISHED"


def test_restricted_dataset_is_explicit_in_manifest(tmp_path: Path, dataset_build) -> None:
    restricted = replace(
        adapter.metadata,
        distribution_restriction=DistributionRestriction.RESTRICTED,
    )
    release = _builder().build(
        _input(dataset_build, metadata=restricted), output_root=tmp_path, generated_at=FIXED_TIME
    )
    manifest = json.loads((release / "manifest.json").read_text())
    artifacts_file = next(item for item in manifest["files"] if item["role"] == "artifacts")
    artifact_rows = [
        json.loads(line)
        for line in (release / artifacts_file["path"]).read_text().splitlines()
        if line.strip()
    ]
    assert all(
        row["request_metadata"]["distribution_restriction"] == "restricted" for row in artifact_rows
    )


def test_schema_failure_never_exposes_a_published_path(tmp_path: Path, dataset_build) -> None:
    builder = ReleaseBuilder(
        Path("schemas"),
        ReleaseVersions(
            release_schema_version="unsupported-v2",
            ftm_schema_version="synthetic-ftm-v1",
            crawler_version="test-v1",
            crawler_revision=_TEST_REVISION,
        ),
    )
    with pytest.raises(ReleaseValidationError, match="manifest schema validation failed"):
        builder.build(_input(dataset_build), output_root=tmp_path, generated_at=FIXED_TIME)
    assert list(tmp_path.iterdir()) == []
