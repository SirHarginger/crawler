from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from datasets.un_sc_consolidated.crawler import adapter
from screening_crawler.acquisition import Acquirer, FetchResponse, SnapshotAssembler
from screening_crawler.model import CompletenessEvidence, CompletenessKind, DistributionRestriction
from screening_crawler.normalization import AdditiveNormalizer
from screening_crawler.pipeline import DatasetBuild, process_snapshot
from screening_crawler.storage import MemoryBlobStore

FIXED_TIME = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def fixture_bytes() -> bytes:
    return Path("tests/fixtures/un_sc_consolidated/synthetic.xml").read_bytes()


@pytest.fixture
def captured(fixture_bytes: bytes):
    store = MemoryBlobStore()
    artifact = Acquirer(
        store,
        crawler_version="test-v1",
        parser_version=adapter.parser.version,
        runtime_version="test-runtime",
    ).capture_response(
        source_key=adapter.metadata.key,
        response=FetchResponse(
            request_url="fixture://un/synthetic.xml",
            status=200,
            headers={"ETag": "synthetic-etag", "Content-Type": "application/xml"},
            body=fixture_bytes,
        ),
        retrieved_at=FIXED_TIME,
        restriction=DistributionRestriction.PUBLIC,
    )
    assembler = SnapshotAssembler(adapter.metadata.key, expected_parts=1)
    assembler.add(artifact)
    snapshot = assembler.finalize(
        CompletenessEvidence(CompletenessKind.SINGLE_FILE, {"fixture": True})
    )
    return store, artifact, snapshot


@pytest.fixture
def dataset_build(captured) -> DatasetBuild:
    store, _artifact, snapshot = captured
    return process_snapshot(
        adapter,
        snapshot,
        store,
        observed_at=FIXED_TIME,
        normalizer=AdditiveNormalizer("base-v1"),
    )

