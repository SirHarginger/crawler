from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from conftest import FIXED_TIME

from datasets.un_sc_consolidated.crawler import adapter
from screening_crawler.acquisition import Acquirer, FetchResponse, SnapshotAssembler
from screening_crawler.errors import IncompleteSnapshot, QuarantineRequired
from screening_crawler.model import (
    CompletenessEvidence,
    CompletenessKind,
    DistributionRestriction,
    SnapshotState,
)
from screening_crawler.normalization import AdditiveNormalizer
from screening_crawler.pipeline import compare_records, process_snapshot
from screening_crawler.storage import MemoryBlobStore


def test_incomplete_snapshot_never_reaches_projection(captured) -> None:
    store, _artifact, snapshot = captured
    incomplete = replace(snapshot, state=SnapshotState.INCOMPLETE, completeness_evidence=None)
    with pytest.raises(IncompleteSnapshot):
        process_snapshot(
            adapter,
            incomplete,
            store,
            observed_at=FIXED_TIME,
            normalizer=AdditiveNormalizer("base-v1"),
        )


def test_volume_anomaly_quarantines(dataset_build, captured) -> None:
    store, _artifact, snapshot = captured
    with pytest.raises(QuarantineRequired):
        process_snapshot(
            adapter,
            snapshot,
            store,
            observed_at=FIXED_TIME,
            normalizer=AdditiveNormalizer("base-v1"),
            previous_record_count=100,
        )


def test_record_delta_uses_native_identity_and_hash(dataset_build) -> None:
    previous = list(dataset_build.source_records)
    changed = replace(previous[0], content_sha256="0" * 64)
    current = [changed]
    delta = compare_records(previous, current)
    assert delta.amended == ("ENTITY:SYN-ORG-001",)
    assert delta.removed == ("INDIVIDUAL:SYN-IND-001",)


def test_parser_format_drift_reaches_field_accounting_and_quarantines(fixture_bytes: bytes) -> None:
    changed = fixture_bytes.replace(
        b"<VERSIONNUM>1</VERSIONNUM>",
        b"<VERSIONNUM>1</VERSIONNUM><NEW_PUBLISHER_FIELD>synthetic</NEW_PUBLISHER_FIELD>",
    )
    store = MemoryBlobStore()
    artifact = Acquirer(
        store,
        crawler_version="test-v1",
        parser_version=adapter.parser.version,
        runtime_version="test-runtime",
    ).capture_response(
        source_key=adapter.metadata.key,
        response=FetchResponse("fixture://drift.xml", 200, {}, changed),
        retrieved_at=datetime(2024, 3, 1, tzinfo=UTC),
        restriction=DistributionRestriction.PUBLIC,
    )
    assembler = SnapshotAssembler(adapter.metadata.key)
    assembler.add(artifact)
    snapshot = assembler.finalize(CompletenessEvidence(CompletenessKind.SINGLE_FILE))
    with pytest.raises(QuarantineRequired, match="no reviewed disposition"):
        process_snapshot(
            adapter,
            snapshot,
            store,
            observed_at=FIXED_TIME,
            normalizer=AdditiveNormalizer("base-v1"),
        )
