"""Optional, regenerable Layer 1.5 source-scoped interpretation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .canonical import sha256_json
from .ids import stable_id
from .model import SourceRecord


@dataclass(frozen=True, slots=True)
class DomainObject:
    id: str
    source_key: str
    kind: str
    source_record_ids: tuple[str, ...]
    data: dict[str, Any]
    interpretation_version: str

    @classmethod
    def create(
        cls,
        *,
        source_key: str,
        kind: str,
        source_record_ids: tuple[str, ...],
        data: dict[str, Any],
        interpretation_version: str,
    ) -> DomainObject:
        if not source_record_ids:
            raise ValueError("domain objects must trace to Layer 1 source records")
        identity = stable_id(
            "domain-object",
            source_key,
            kind,
            *sorted(source_record_ids),
            sha256_json(data),
            interpretation_version,
        )
        return cls(identity, source_key, kind, source_record_ids, data, interpretation_version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_key": self.source_key,
            "kind": self.kind,
            "source_record_ids": list(sorted(self.source_record_ids)),
            "data": self.data,
            "interpretation_version": self.interpretation_version,
        }


class DomainInterpreter(Protocol):
    version: str
    justification: str

    def interpret(self, records: list[SourceRecord]) -> list[DomainObject]: ...

