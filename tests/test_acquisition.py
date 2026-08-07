from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from screening_crawler.acquisition import Acquirer, FetchResponse, SnapshotAssembler
from screening_crawler.errors import IncompleteSnapshot, IntegrityError, InvalidTransition
from screening_crawler.model import (
    CompletenessEvidence,
    CompletenessKind,
    DistributionRestriction,
    ManualReceipt,
    SnapshotState,
)
from screening_crawler.storage import MemoryBlobStore


def test_capture_preserves_exact_bytes_and_transport_metadata(captured, fixture_bytes: bytes) -> None:
    store, artifact, _snapshot = captured
    assert store.get(artifact.storage_key) == fixture_bytes
    assert artifact.byte_length == len(fixture_bytes)
    assert artifact.to_dict()["etag"] == "synthetic-etag"
    artifact.verify(fixture_bytes)


def test_artifact_detects_corruption(captured, fixture_bytes: bytes) -> None:
    _store, artifact, _snapshot = captured
    with pytest.raises(IntegrityError):
        artifact.verify(fixture_bytes + b"corrupt")


def test_snapshot_without_evidence_remains_incomplete(captured) -> None:
    _store, artifact, _snapshot = captured
    assembler = SnapshotAssembler(artifact.source_key)
    assembler.add(artifact)
    snapshot = assembler.finalize(None)
    assert snapshot.state is SnapshotState.INCOMPLETE
    with pytest.raises(IncompleteSnapshot):
        snapshot.require_complete()


def test_declared_part_count_must_match(captured) -> None:
    _store, artifact, _snapshot = captured
    assembler = SnapshotAssembler(artifact.source_key, expected_parts=2)
    assembler.add(artifact)
    with pytest.raises(IncompleteSnapshot):
        assembler.finalize(
            CompletenessEvidence(CompletenessKind.DECLARED_PART_COUNT, {"declared": 2})
        )


def test_multifile_snapshot_requires_and_records_all_declared_parts(captured) -> None:
    store, artifact, _snapshot = captured
    second = Acquirer(
        store,
        crawler_version="test-v1",
        parser_version="test-parser-v1",
        runtime_version="test-runtime",
    ).capture_response(
        source_key=artifact.source_key,
        response=FetchResponse(
            request_url="fixture://un/part-2.xml",
            status=200,
            headers={"content-type": "application/xml"},
            body=b"<SYNTHETIC_PART_TWO />",
        ),
        retrieved_at=datetime(2024, 3, 1, 12, 1, tzinfo=UTC),
        restriction=DistributionRestriction.PUBLIC,
    )
    assembler = SnapshotAssembler(artifact.source_key, expected_parts=2)
    assembler.add(artifact)
    assembler.add(second)
    snapshot = assembler.finalize(
        CompletenessEvidence(CompletenessKind.DECLARED_PART_COUNT, {"declared": 2})
    )
    assert snapshot.state is SnapshotState.COMPLETE
    assert len(snapshot.artifacts) == 2


def test_manual_receipt_is_restricted_by_construction() -> None:
    store = MemoryBlobStore()
    acquirer = Acquirer(
        store,
        crawler_version="test-v1",
        parser_version="manual-v1",
        runtime_version="test-runtime",
    )
    receipt = ManualReceipt(
        transmitting_authority="Synthetic Authority",
        reference_number="SYN-RECEIPT-001",
        issuing_office="Synthetic Office",
        signatory=None,
        addressee="Synthetic Recipient",
        receipt_timestamp=datetime(2024, 3, 1, tzinfo=UTC),
        internal_channel="synthetic-hand-delivery",
        received_by="synthetic-custodian",
    )
    artifact = acquirer.capture_manual_receipt(
        source_key="synthetic_restricted_advisory",
        body=b"synthetic restricted fixture",
        receipt=receipt,
        parser_version="manual-v1",
        media_type="application/pdf",
    )
    assert artifact.distribution_restriction is DistributionRestriction.RESTRICTED
    assert artifact.manual_receipt == receipt


@pytest.mark.parametrize(
    "kind",
    [
        CompletenessKind.CURSOR_EXHAUSTED,
        CompletenessKind.INDEX_LINKS_FETCHED,
        CompletenessKind.DECLARED_PAGES_FETCHED,
        CompletenessKind.MANUALLY_VERIFIED,
    ],
)
def test_supported_multipart_completeness_evidence(captured, kind: CompletenessKind) -> None:
    _store, artifact, _snapshot = captured
    assembler = SnapshotAssembler(artifact.source_key)
    assembler.add(artifact)
    snapshot = assembler.finalize(CompletenessEvidence(kind, {"synthetic": True}))
    assert snapshot.state is SnapshotState.COMPLETE


def test_snapshot_lifecycle_rejects_skipping_publication_gates(captured) -> None:
    _store, _artifact, snapshot = captured
    with pytest.raises(InvalidTransition):
        replace(snapshot, state=SnapshotState.FETCHING).transition(SnapshotState.PUBLISHED)
