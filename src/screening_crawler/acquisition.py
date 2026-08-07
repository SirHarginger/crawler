"""Exact-byte acquisition envelopes and complete snapshot assembly."""

from __future__ import annotations

import platform
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .model import (
    Artifact,
    CompletenessEvidence,
    DistributionRestriction,
    ManualReceipt,
    RedirectHop,
    Snapshot,
    SnapshotState,
)
from .storage import BlobStore


@dataclass(frozen=True, slots=True)
class FetchRequest:
    url: str
    headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class FetchResponse:
    request_url: str
    status: int
    headers: dict[str, str]
    body: bytes
    redirects: tuple[RedirectHop, ...] = ()
    publisher_publication_date: str | None = None


class FetchTransport(Protocol):
    """Network boundary implemented by deployments, never by projection code."""

    def fetch(self, request: FetchRequest) -> FetchResponse: ...


class Acquirer:
    def __init__(
        self,
        store: BlobStore,
        *,
        crawler_version: str,
        parser_version: str,
        runtime_version: str | None = None,
    ) -> None:
        self.store = store
        self.crawler_version = crawler_version
        self.parser_version = parser_version
        self.runtime_version = runtime_version or platform.python_version()

    def capture_response(
        self,
        *,
        source_key: str,
        response: FetchResponse,
        retrieved_at: datetime,
        restriction: DistributionRestriction,
    ) -> Artifact:
        artifact = Artifact.from_bytes(
            source_key=source_key,
            request_url=response.request_url,
            body=response.body,
            retrieved_at=retrieved_at,
            crawler_version=self.crawler_version,
            parser_version=self.parser_version,
            runtime_version=self.runtime_version,
            distribution_restriction=restriction,
            redirects=response.redirects,
            http_status=response.status,
            response_headers=response.headers,
            publisher_publication_date=response.publisher_publication_date,
        )
        self.store.put_verified(artifact.storage_key, response.body, artifact.sha256)
        return artifact

    def capture_manual_receipt(
        self,
        *,
        source_key: str,
        body: bytes,
        receipt: ManualReceipt,
        parser_version: str,
        media_type: str,
    ) -> Artifact:
        artifact = Artifact.from_bytes(
            source_key=source_key,
            request_url=f"manual-receipt:{receipt.reference_number}",
            body=body,
            retrieved_at=receipt.receipt_timestamp,
            crawler_version=self.crawler_version,
            parser_version=parser_version,
            runtime_version=self.runtime_version,
            distribution_restriction=DistributionRestriction.RESTRICTED,
            response_headers={"content-type": media_type},
            manual_receipt=receipt,
        )
        self.store.put_verified(artifact.storage_key, body, artifact.sha256)
        return artifact


class SnapshotAssembler:
    """Tracks multipart completeness without inferring success from parseability."""

    def __init__(self, source_key: str, *, expected_parts: int | None = None) -> None:
        if expected_parts is not None and expected_parts < 1:
            raise ValueError("expected_parts must be positive")
        self.source_key = source_key
        self.expected_parts = expected_parts
        self._artifacts: dict[str, Artifact] = {}

    def add(self, artifact: Artifact) -> None:
        if artifact.source_key != self.source_key:
            raise ValueError("artifact source does not match assembler")
        self._artifacts[artifact.id] = artifact

    def snapshot(self) -> Snapshot:
        return Snapshot.assemble(
            source_key=self.source_key,
            artifacts=tuple(self._artifacts.values()),
            expected_parts=self.expected_parts,
        )

    def finalize(self, evidence: CompletenessEvidence | None) -> Snapshot:
        snapshot = self.snapshot()
        if evidence is None:
            return snapshot.transition(SnapshotState.INCOMPLETE)
        return snapshot.transition(SnapshotState.COMPLETE, evidence=evidence)

