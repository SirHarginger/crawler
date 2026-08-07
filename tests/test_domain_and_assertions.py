from __future__ import annotations

from datetime import UTC, datetime

import pytest

from screening_crawler.assertions import AssertionState, PepAssertion
from screening_crawler.normalization import AdditiveNormalizer
from screening_crawler.pep import PepOccupancyProjector, PepProjectionInput

NOW = datetime(2024, 1, 1, tzinfo=UTC)


def _draft(*, supersedes: str | None = None, role: str = "pep") -> PepAssertion:
    return PepAssertion.draft(
        subject_key="synthetic-subject-001",
        assertion_kind="occupancy",
        source_document_id="synthetic-document-001",
        source_url_or_receipt="fixture://ghana-register/synthetic",
        retrieval_timestamp=NOW,
        locator="page 1, synthetic table, row 1",
        supporting_passage_or_region="region:synthetic-001",
        asserted_by="synthetic-reviewer-a",
        valid_from="2024",
        valid_to=None,
        precision="year",
        role_classification=role,
        supersedes=supersedes,
    )


def test_layer_1_5_objects_are_traceable_and_regenerable(dataset_build) -> None:
    assert len(dataset_build.domain_objects) == len(dataset_build.source_records)
    assert all(item.source_record_ids for item in dataset_build.domain_objects)
    assert all(item.interpretation_version == "un-domain-v1" for item in dataset_build.domain_objects)


def test_assertion_requires_independent_review_before_publication() -> None:
    evidenced = _draft().transition(AssertionState.EVIDENCED)
    with pytest.raises(ValueError):
        evidenced.transition(AssertionState.REVIEWED, actor="synthetic-reviewer-a")
    reviewed = evidenced.transition(AssertionState.REVIEWED, actor="synthetic-reviewer-b")
    published = reviewed.transition(AssertionState.PUBLISHED)
    assert published.reviewed_by == "synthetic-reviewer-b"


def test_correction_appends_history_and_never_overwrites() -> None:
    original = (
        _draft()
        .transition(AssertionState.EVIDENCED)
        .transition(AssertionState.REVIEWED, actor="synthetic-reviewer-b")
        .transition(AssertionState.PUBLISHED)
    )
    replacement = _draft(supersedes=original.id)
    corrected = original.corrected_by(
        replacement,
        actor="synthetic-reviewer-c",
        reason="Synthetic fixture correction",
        corrected_at=NOW,
    )
    assert corrected.state is AssertionState.CORRECTED
    assert corrected.correction_history[0].replacement_assertion_id == replacement.id
    assert original.state is AssertionState.PUBLISHED


def test_relatives_and_close_associates_use_separate_role_classification() -> None:
    rca = _draft(role="rca")
    assert rca.role_classification == "rca"
    with pytest.raises(ValueError):
        _draft(role="sanction")


def test_pep_projection_uses_person_position_and_occupancy(dataset_build) -> None:
    record = dataset_build.source_records[1]
    projection = PepOccupancyProjector(
        mapping_version="pep-map-v1",
        normalizer=AdditiveNormalizer("base-v1"),
    ).project(
        [
            PepProjectionInput(
                source_record=record,
                person_native_key="synthetic-person-001",
                published_name="Synthetic Person Alpha",
                name_source_path="$.published_name",
                position_native_key="synthetic-office-001",
                published_position="Synthetic Public Office",
                position_source_path="$.published_position",
                valid_from="2024",
                valid_to=None,
                date_precision="year",
            )
        ]
    )
    assert {item.schema for item in projection.entities} == {"Person", "Position", "Occupancy"}
    assert all(item.source_record_ids == (record.id,) for item in projection.entities)
