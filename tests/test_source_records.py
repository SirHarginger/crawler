from __future__ import annotations

import pytest
from conftest import FIXED_TIME

from screening_crawler.errors import DuplicateRecordKey
from screening_crawler.source import ParsedRecord, extract_source_records


class DuplicateParser:
    version = "synthetic-duplicate-v1"

    def __init__(self, conflict: bool) -> None:
        self.conflict = conflict

    def parse(self, snapshot, store):
        artifact = snapshot.artifacts[0]
        first = ParsedRecord("ENTITY", "duplicate", "/one", {"name": "Synthetic One"}, (artifact.id,))
        second_native = {"name": "Synthetic Two"} if self.conflict else first.native
        return [first, ParsedRecord("ENTITY", "duplicate", "/two", second_native, (artifact.id,))]


@pytest.mark.parametrize("conflict", [False, True])
def test_duplicate_publisher_keys_quarantine(captured, conflict: bool) -> None:
    store, _artifact, snapshot = captured
    with pytest.raises(DuplicateRecordKey):
        extract_source_records(snapshot, DuplicateParser(conflict), store, observed_at=FIXED_TIME)


def test_source_records_are_deterministic_and_bitemporal(dataset_build) -> None:
    records = dataset_build.source_records
    assert [item.publisher_native_key for item in records] == ["SYN-ORG-001", "SYN-IND-001"]
    assert all(item.observed_from == FIXED_TIME for item in records)
    assert all(item.precision == "unknown" for item in records)
    assert all(item.artifact_ids for item in records)

