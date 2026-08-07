"""End-to-end tests for the PEP-shaped pipeline, using only synthetic data.

The shipped intake/current.yml is a deliberately empty placeholder (no real
Ghana Parliament data has been provided to this repository -- see
metadata.yml). Every test here builds its own synthetic intake content
in-memory, using obviously fake names like "Synthetic Person Alpha", never
real people.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from datasets.gh_parliament.crawler import adapter
from screening_crawler.acquisition import Acquirer, FetchResponse, SnapshotAssembler
from screening_crawler.errors import QuarantineRequired
from screening_crawler.model import CompletenessEvidence, CompletenessKind, DistributionRestriction
from screening_crawler.normalization import AdditiveNormalizer
from screening_crawler.pipeline import process_pep_snapshot
from screening_crawler.storage import MemoryBlobStore

FIXED_TIME = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)

_PUBLISHED_ENTRY = {
    "subject_key": "synthetic-subject-001",
    "assertion_kind": "occupancy",
    "source_document_id": "synthetic-document-001",
    "source_url_or_receipt": "fixture://gh-parliament/synthetic",
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
    "person_native_key": "synthetic-person-001",
    "published_name": "Synthetic Person Alpha",
    "position_native_key": "synthetic-office-001",
    "published_position": "Member of Parliament for Synthetic Constituency",
}


def _intake_bytes(entries: list[dict]) -> bytes:
    payload = {"version": "gh-parliament-intake-v1", "assertions": entries}
    return yaml.safe_dump(payload).encode("utf-8")


def _snapshot_from(body: bytes):
    store = MemoryBlobStore()
    artifact = Acquirer(
        store,
        crawler_version="test-v1",
        parser_version=adapter.parser.version,
        runtime_version="test-runtime",
    ).capture_response(
        source_key=adapter.metadata.key,
        response=FetchResponse(
            request_url="fixture://gh_parliament/intake.yml",
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
    return store, snapshot


def _build(entries: list[dict]):
    store, snapshot = _snapshot_from(_intake_bytes(entries))
    return process_pep_snapshot(
        adapter,
        snapshot,
        store,
        observed_at=FIXED_TIME,
        normalizer=AdditiveNormalizer(adapter.metadata.normalization_version),
    )


def test_shipped_intake_file_is_still_an_empty_placeholder() -> None:
    raw = yaml.safe_load(Path("datasets/gh_parliament/intake/current.yml").read_text())
    assert raw["assertions"] == []


def test_published_entry_projects_to_person_position_occupancy() -> None:
    build = _build([_PUBLISHED_ENTRY])
    schemas = {entity.schema for entity in build.projection.entities}
    assert schemas == {"Person", "Position", "Occupancy"}
    person = next(e for e in build.projection.entities if e.schema == "Person")
    assert "Synthetic Person Alpha" in person.properties["name"]


def test_published_entry_produces_exactly_one_pep_assertion() -> None:
    build = _build([_PUBLISHED_ENTRY])
    assert len(build.pep_assertions) == 1
    assertion = build.pep_assertions[0]
    assert assertion.state.value == "PUBLISHED"
    assert assertion.asserted_by == "synthetic-transcriber"
    assert assertion.reviewed_by == "synthetic-reviewer"


def test_assertion_id_matches_source_record_publisher_native_key() -> None:
    build = _build([_PUBLISHED_ENTRY])
    assert build.pep_assertions[0].id == build.source_records[0].publisher_native_key


def test_non_published_entries_are_excluded_not_discarded() -> None:
    draft_entry = {**_PUBLISHED_ENTRY, "state": "DRAFT", "subject_key": "synthetic-subject-002"}
    build = _build([_PUBLISHED_ENTRY, draft_entry])
    assert len(build.source_records) == 1
    assert len(build.pep_assertions) == 1


def test_self_review_is_rejected_as_a_quarantine() -> None:
    entry = {**_PUBLISHED_ENTRY, "reviewed_by": _PUBLISHED_ENTRY["asserted_by"]}
    with pytest.raises(QuarantineRequired, match="assertion lifecycle validation"):
        _build([entry])


def test_missing_required_field_is_rejected_as_a_quarantine() -> None:
    entry = {key: value for key, value in _PUBLISHED_ENTRY.items() if key != "published_name"}
    with pytest.raises(QuarantineRequired, match="missing required fields"):
        _build([entry])


def test_empty_intake_quarantines_via_validate_source() -> None:
    with pytest.raises(QuarantineRequired, match="no PUBLISHED assertions"):
        _build([])


def test_field_coverage_has_no_quarantine_findings() -> None:
    build = _build([_PUBLISHED_ENTRY])
    assert build.field_coverage.quarantined is False


def test_regeneration_is_deterministic() -> None:
    first = _build([_PUBLISHED_ENTRY])
    second = _build([_PUBLISHED_ENTRY])
    assert first.projection.canonical_hash == second.projection.canonical_hash


def test_domain_objects_are_empty_for_pep_pipeline() -> None:
    """PEP occupancy assertions have no Layer 1.5 concept; guards against silent scope creep."""
    build = _build([_PUBLISHED_ENTRY])
    assert build.domain_objects == ()
