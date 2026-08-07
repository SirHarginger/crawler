"""Parser contracts and source-record extraction controls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from .canonical import sha256_json
from .errors import DuplicateRecordKey
from .ids import source_record_id
from .model import Snapshot, SourceRecord, SourceRecordExtent
from .storage import BlobStore


@dataclass(frozen=True, slots=True)
class ParsedRecord:
    record_kind: str
    publisher_native_key: str
    locator: str
    native: dict[str, Any]
    artifact_ids: tuple[str, ...]
    asserted_valid_from: str | None = None
    asserted_valid_to: str | None = None
    precision: str = "unknown"
    extents: tuple[SourceRecordExtent, ...] = ()


class SourceParser(Protocol):
    version: str

    def parse(self, snapshot: Snapshot, store: BlobStore) -> list[ParsedRecord]: ...


def extract_source_records(
    snapshot: Snapshot,
    parser: SourceParser,
    store: BlobStore,
    *,
    observed_at: datetime,
) -> list[SourceRecord]:
    snapshot.require_complete()
    seen: dict[tuple[str, str], str] = {}
    result: list[SourceRecord] = []
    for parsed in parser.parse(snapshot, store):
        key = (parsed.record_kind, parsed.publisher_native_key)
        content_hash = sha256_json(parsed.native)
        previous = seen.get(key)
        if previous is not None:
            qualifier = "conflicting content" if previous != content_hash else "duplicate key"
            raise DuplicateRecordKey(
                f"{snapshot.source_key} emitted {qualifier} for {parsed.record_kind}/{parsed.publisher_native_key}"
            )
        seen[key] = content_hash
        known_artifacts = {artifact.id for artifact in snapshot.artifacts}
        if not set(parsed.artifact_ids).issubset(known_artifacts):
            raise ValueError("parser referenced an artifact outside the snapshot")
        record = SourceRecord(
            id=source_record_id(
                snapshot.source_key,
                snapshot.id,
                parsed.record_kind,
                parsed.publisher_native_key,
            ),
            source_key=snapshot.source_key,
            snapshot_id=snapshot.id,
            artifact_ids=parsed.artifact_ids,
            record_kind=parsed.record_kind,
            publisher_native_key=parsed.publisher_native_key,
            locator=parsed.locator,
            parsed=parsed.native,
            content_sha256=content_hash,
            parser_version=parser.version,
            observed_from=observed_at,
            asserted_valid_from=parsed.asserted_valid_from,
            asserted_valid_to=parsed.asserted_valid_to,
            precision=parsed.precision,
            extents=parsed.extents,
        )
        result.append(record)
    return sorted(result, key=lambda item: (item.record_kind, item.publisher_native_key, item.id))

