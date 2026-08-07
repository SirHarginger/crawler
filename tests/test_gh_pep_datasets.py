"""Parametrized end-to-end coverage for the PEP-shaped datasets that reuse
gh_parliament's pattern: gh_article71, gh_mmdce, gh_siga_boards.

tests/test_gh_parliament.py already exercises the shared mechanism in depth
(11 tests covering the full DRAFT->PUBLISHED lifecycle, quarantine paths,
and determinism); this file confirms the same mechanism holds for the other
three datasets without re-deriving every case, and separately guards that
each of their shipped placeholder intake files is still empty. Every name
used is obviously synthetic, never a real person.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from screening_crawler.acquisition import Acquirer, FetchResponse, SnapshotAssembler
from screening_crawler.errors import QuarantineRequired
from screening_crawler.model import CompletenessEvidence, CompletenessKind, DistributionRestriction
from screening_crawler.normalization import AdditiveNormalizer
from screening_crawler.pep import load_pep_adapter
from screening_crawler.pipeline import process_pep_snapshot
from screening_crawler.storage import MemoryBlobStore

FIXED_TIME = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
DATASET_KEYS = ["gh_article71", "gh_mmdce", "gh_siga_boards"]


def _synthetic_entry(key: str) -> dict[str, Any]:
    return {
        "subject_key": f"synthetic-subject-{key}",
        "assertion_kind": "occupancy",
        "source_document_id": f"synthetic-document-{key}",
        "source_url_or_receipt": f"fixture://{key}/synthetic",
        "retrieval_timestamp": "2024-01-01T00:00:00+00:00",
        "locator": "page 1, synthetic table, row 1",
        "supporting_passage_or_region": "region:synthetic-001",
        "asserted_by": "synthetic-transcriber",
        "reviewed_by": "synthetic-reviewer",
        "state": "PUBLISHED",
        "valid_from": "2024",
        "valid_to": None,
        "precision": "year",
        "role_classification": "pep",
        "person_native_key": f"synthetic-person-{key}",
        "published_name": "Synthetic Person Alpha",
        "position_native_key": f"synthetic-office-{key}",
        "published_position": "Synthetic Public Office",
    }


def _build(key: str, entries: list[dict[str, Any]]):
    adapter = load_pep_adapter(key)
    body = yaml.safe_dump({"version": adapter.parser.version, "assertions": entries}).encode("utf-8")
    store = MemoryBlobStore()
    artifact = Acquirer(
        store,
        crawler_version="test-v1",
        parser_version=adapter.parser.version,
        runtime_version="test-runtime",
    ).capture_response(
        source_key=adapter.metadata.key,
        response=FetchResponse(
            request_url=f"fixture://{key}/intake.yml",
            status=200,
            headers={"content-type": "application/yaml"},
            body=body,
        ),
        retrieved_at=FIXED_TIME,
        restriction=DistributionRestriction.PUBLIC,
    )
    assembler = SnapshotAssembler(adapter.metadata.key, expected_parts=1)
    assembler.add(artifact)
    snapshot = assembler.finalize(CompletenessEvidence(CompletenessKind.SINGLE_FILE, {"fixture": True}))
    build = process_pep_snapshot(
        adapter,
        snapshot,
        store,
        observed_at=FIXED_TIME,
        normalizer=AdditiveNormalizer(adapter.metadata.normalization_version),
    )
    return build


@pytest.mark.parametrize("key", DATASET_KEYS)
def test_shipped_intake_file_is_still_an_empty_placeholder(key: str) -> None:
    raw = yaml.safe_load(Path(f"datasets/{key}/intake/current.yml").read_text())
    assert raw["assertions"] == []


@pytest.mark.parametrize("key", DATASET_KEYS)
def test_published_entry_projects_to_person_position_occupancy(key: str) -> None:
    build = _build(key, [_synthetic_entry(key)])
    schemas = {entity.schema for entity in build.projection.entities}
    assert schemas == {"Person", "Position", "Occupancy"}
    assert len(build.pep_assertions) == 1
    assert build.pep_assertions[0].state.value == "PUBLISHED"


@pytest.mark.parametrize("key", DATASET_KEYS)
def test_assertion_id_matches_source_record_publisher_native_key(key: str) -> None:
    build = _build(key, [_synthetic_entry(key)])
    assert build.pep_assertions[0].id == build.source_records[0].publisher_native_key


@pytest.mark.parametrize("key", DATASET_KEYS)
def test_self_review_is_rejected_as_a_quarantine(key: str) -> None:
    entry = _synthetic_entry(key)
    entry["reviewed_by"] = entry["asserted_by"]
    with pytest.raises(QuarantineRequired, match="assertion lifecycle validation"):
        _build(key, [entry])


@pytest.mark.parametrize("key", DATASET_KEYS)
def test_field_coverage_has_no_quarantine_findings(key: str) -> None:
    build = _build(key, [_synthetic_entry(key)])
    assert build.field_coverage.quarantined is False


@pytest.mark.parametrize("key", DATASET_KEYS)
def test_empty_intake_quarantines_via_validate_source(key: str) -> None:
    with pytest.raises(QuarantineRequired, match="no PUBLISHED assertions"):
        _build(key, [])


@pytest.mark.parametrize("key", DATASET_KEYS)
def test_regeneration_is_deterministic(key: str) -> None:
    entry = _synthetic_entry(key)
    first = _build(key, [entry])
    second = _build(key, [entry])
    assert first.projection.canonical_hash == second.projection.canonical_hash


def test_dataset_versions_do_not_collide_across_the_four_pep_datasets() -> None:
    """Guards against copy-paste error: each must have its own mapping/parser version."""
    keys = [*DATASET_KEYS, "gh_parliament"]
    adapters = {key: load_pep_adapter(key) for key in keys}
    mapping_versions = {key: adapter.mapping_version for key, adapter in adapters.items()}
    parser_versions = {key: adapter.parser.version for key, adapter in adapters.items()}
    assert len(set(mapping_versions.values())) == len(keys)
    assert len(set(parser_versions.values())) == len(keys)
