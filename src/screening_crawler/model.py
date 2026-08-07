"""Immutable envelopes shared across acquisition, extraction, and release."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .canonical import sha256_bytes, sha256_json
from .errors import IncompleteSnapshot, IntegrityError, InvalidTransition
from .ids import artifact_id as make_artifact_id
from .ids import snapshot_id as make_snapshot_id


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class DistributionRestriction(StrEnum):
    PUBLIC = "public"
    RESTRICTED = "restricted"
    INTERNAL_ONLY = "internal_only"


class SnapshotState(StrEnum):
    FETCHING = "FETCHING"
    INCOMPLETE = "INCOMPLETE"
    COMPLETE = "COMPLETE"
    QUARANTINED = "QUARANTINED"
    PUBLISHED = "PUBLISHED"
    SUPERSEDED = "SUPERSEDED"


class CompletenessKind(StrEnum):
    SINGLE_FILE = "single_file_acquisition"
    DECLARED_PART_COUNT = "declared_part_count_matched"
    CURSOR_EXHAUSTED = "cursor_exhausted"
    INDEX_LINKS_FETCHED = "all_index_page_links_fetched"
    DECLARED_PAGES_FETCHED = "all_declared_pages_fetched"
    MANUALLY_VERIFIED = "manually_verified_complete"


@dataclass(frozen=True, slots=True)
class RedirectHop:
    url: str
    status: int
    location: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"url": self.url, "status": self.status, "location": self.location}


@dataclass(frozen=True, slots=True)
class ManualReceipt:
    transmitting_authority: str
    reference_number: str
    issuing_office: str
    signatory: str | None
    addressee: str
    receipt_timestamp: datetime
    internal_channel: str
    received_by: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "transmitting_authority": self.transmitting_authority,
            "reference_number": self.reference_number,
            "issuing_office": self.issuing_office,
            "signatory": self.signatory,
            "addressee": self.addressee,
            "receipt_timestamp": utc_iso(self.receipt_timestamp),
            "internal_channel": self.internal_channel,
            "received_by": self.received_by,
        }


@dataclass(frozen=True, slots=True)
class Artifact:
    id: str
    source_key: str
    request_url: str
    redirects: tuple[RedirectHop, ...]
    http_status: int
    response_headers: dict[str, str]
    sha256: str
    byte_length: int
    retrieved_at: datetime
    publisher_publication_date: str | None
    crawler_version: str
    parser_version: str
    runtime_version: str
    distribution_restriction: DistributionRestriction
    storage_key: str
    manual_receipt: ManualReceipt | None = None

    @classmethod
    def from_bytes(
        cls,
        *,
        source_key: str,
        request_url: str,
        body: bytes,
        retrieved_at: datetime,
        crawler_version: str,
        parser_version: str,
        runtime_version: str,
        distribution_restriction: DistributionRestriction,
        redirects: tuple[RedirectHop, ...] = (),
        http_status: int = 200,
        response_headers: dict[str, str] | None = None,
        publisher_publication_date: str | None = None,
        manual_receipt: ManualReceipt | None = None,
    ) -> Artifact:
        digest = sha256_bytes(body)
        return cls(
            id=make_artifact_id(source_key, request_url, utc_iso(retrieved_at), digest),
            source_key=source_key,
            request_url=request_url,
            redirects=redirects,
            http_status=http_status,
            response_headers=dict(response_headers or {}),
            sha256=digest,
            byte_length=len(body),
            retrieved_at=retrieved_at,
            publisher_publication_date=publisher_publication_date,
            crawler_version=crawler_version,
            parser_version=parser_version,
            runtime_version=runtime_version,
            distribution_restriction=distribution_restriction,
            storage_key=f"sha256/{digest[:2]}/{digest}",
            manual_receipt=manual_receipt,
        )

    def verify(self, body: bytes) -> None:
        if len(body) != self.byte_length or sha256_bytes(body) != self.sha256:
            raise IntegrityError(f"artifact {self.id} bytes do not match its envelope")

    def to_dict(self) -> dict[str, Any]:
        headers = {key.lower(): value for key, value in sorted(self.response_headers.items())}
        return {
            "id": self.id,
            "source_key": self.source_key,
            "request_url": self.request_url,
            "redirect_chain": [hop.to_dict() for hop in self.redirects],
            "http_status": self.http_status,
            "response_headers": headers,
            "etag": headers.get("etag"),
            "last_modified": headers.get("last-modified"),
            "content_type": headers.get("content-type"),
            "sha256": self.sha256,
            "byte_length": self.byte_length,
            "retrieved_at": utc_iso(self.retrieved_at),
            "publisher_publication_date": self.publisher_publication_date,
            "crawler_version": self.crawler_version,
            "parser_version": self.parser_version,
            "runtime_version": self.runtime_version,
            "distribution_restriction": self.distribution_restriction.value,
            "storage_key": self.storage_key,
            "manual_receipt": self.manual_receipt.to_dict() if self.manual_receipt else None,
        }


@dataclass(frozen=True, slots=True)
class CompletenessEvidence:
    kind: CompletenessKind
    details: dict[str, Any] = field(default_factory=dict)
    verified_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "details": self.details, "verified_by": self.verified_by}


_TRANSITIONS: dict[SnapshotState, frozenset[SnapshotState]] = {
    SnapshotState.FETCHING: frozenset({SnapshotState.INCOMPLETE, SnapshotState.COMPLETE}),
    SnapshotState.INCOMPLETE: frozenset({SnapshotState.FETCHING, SnapshotState.QUARANTINED}),
    SnapshotState.COMPLETE: frozenset({SnapshotState.QUARANTINED, SnapshotState.PUBLISHED}),
    SnapshotState.QUARANTINED: frozenset({SnapshotState.FETCHING}),
    SnapshotState.PUBLISHED: frozenset({SnapshotState.SUPERSEDED}),
    SnapshotState.SUPERSEDED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class Snapshot:
    id: str
    source_key: str
    state: SnapshotState
    artifacts: tuple[Artifact, ...]
    completeness_evidence: CompletenessEvidence | None
    expected_parts: int | None
    combined_manifest_sha256: str

    @classmethod
    def assemble(
        cls,
        *,
        source_key: str,
        artifacts: tuple[Artifact, ...],
        state: SnapshotState = SnapshotState.FETCHING,
        evidence: CompletenessEvidence | None = None,
        expected_parts: int | None = None,
    ) -> Snapshot:
        if not artifacts:
            raise ValueError("a snapshot must contain at least one artifact")
        if any(item.source_key != source_key for item in artifacts):
            raise ValueError("all artifacts must belong to the snapshot source")
        manifest_material = {
            "source_key": source_key,
            "artifacts": sorted(
                (item.request_url, item.sha256) for item in artifacts
            ),
            "expected_parts": expected_parts,
        }
        digest = sha256_json(manifest_material)
        snapshot = cls(
            id=make_snapshot_id(source_key, digest),
            source_key=source_key,
            state=SnapshotState.FETCHING,
            artifacts=tuple(sorted(artifacts, key=lambda item: item.id)),
            completeness_evidence=None,
            expected_parts=expected_parts,
            combined_manifest_sha256=digest,
        )
        if state is SnapshotState.FETCHING:
            return snapshot
        return snapshot.transition(state, evidence=evidence)

    def transition(
        self,
        target: SnapshotState,
        *,
        evidence: CompletenessEvidence | None = None,
    ) -> Snapshot:
        if target not in _TRANSITIONS[self.state]:
            raise InvalidTransition(f"snapshot cannot transition {self.state} -> {target}")
        resolved_evidence = evidence or self.completeness_evidence
        if target is SnapshotState.COMPLETE:
            if resolved_evidence is None:
                raise IncompleteSnapshot("COMPLETE requires explicit completeness evidence")
            if self.expected_parts is not None and len(self.artifacts) != self.expected_parts:
                raise IncompleteSnapshot("fetched artifact count does not match declared part count")
        if target is SnapshotState.PUBLISHED and self.state is not SnapshotState.COMPLETE:
            raise InvalidTransition("only a COMPLETE snapshot may be published")
        return Snapshot(
            id=self.id,
            source_key=self.source_key,
            state=target,
            artifacts=self.artifacts,
            completeness_evidence=resolved_evidence,
            expected_parts=self.expected_parts,
            combined_manifest_sha256=self.combined_manifest_sha256,
        )

    def require_complete(self) -> None:
        if self.state is not SnapshotState.COMPLETE or self.completeness_evidence is None:
            raise IncompleteSnapshot(f"snapshot {self.id} is not explicitly complete")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_key": self.source_key,
            "state": self.state.value,
            "artifact_ids": [artifact.id for artifact in self.artifacts],
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "expected_parts": self.expected_parts,
            "completeness_evidence": (
                self.completeness_evidence.to_dict() if self.completeness_evidence else None
            ),
            "combined_manifest_sha256": self.combined_manifest_sha256,
        }


@dataclass(frozen=True, slots=True)
class SourceRecordExtent:
    artifact_id: str
    byte_start: int
    byte_end: int
    ordinal: int
    stream: str = "raw"

    def __post_init__(self) -> None:
        if self.byte_start < 0 or self.byte_end <= self.byte_start:
            raise ValueError("extent must be a non-empty half-open byte range")
        if self.stream not in {"raw", "decompressed"}:
            raise ValueError("extent stream must be raw or decompressed")

    def to_dict(self, source_record_id: str) -> dict[str, Any]:
        return {
            "source_record_id": source_record_id,
            "artifact_id": self.artifact_id,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "ordinal": self.ordinal,
            "stream": self.stream,
        }


@dataclass(frozen=True, slots=True)
class SourceRecord:
    id: str
    source_key: str
    snapshot_id: str
    artifact_ids: tuple[str, ...]
    record_kind: str
    publisher_native_key: str
    locator: str
    parsed: dict[str, Any]
    content_sha256: str
    parser_version: str
    observed_from: datetime
    observed_to: datetime | None = None
    asserted_valid_from: str | None = None
    asserted_valid_to: str | None = None
    precision: str = "unknown"
    extents: tuple[SourceRecordExtent, ...] = ()

    def __post_init__(self) -> None:
        if self.precision not in {"exact", "month", "year", "unknown"}:
            raise ValueError("invalid precision qualifier")
        if not self.artifact_ids:
            raise ValueError("a source record must resolve to at least one artifact")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_key": self.source_key,
            "snapshot_id": self.snapshot_id,
            "artifact_ids": list(self.artifact_ids),
            "record_kind": self.record_kind,
            "publisher_native_key": self.publisher_native_key,
            "locator": self.locator,
            "parsed": self.parsed,
            "content_sha256": self.content_sha256,
            "parser_version": self.parser_version,
            "observed_from": utc_iso(self.observed_from),
            "observed_to": utc_iso(self.observed_to) if self.observed_to else None,
            "asserted_valid_from": self.asserted_valid_from,
            "asserted_valid_to": self.asserted_valid_to,
            "precision": self.precision,
        }
