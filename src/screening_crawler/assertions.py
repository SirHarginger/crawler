"""Append-only evidence and review contract for human-assembled PEP data."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from .errors import InvalidTransition
from .ids import stable_id
from .model import utc_iso


class AssertionState(StrEnum):
    DRAFT = "DRAFT"
    EVIDENCED = "EVIDENCED"
    REVIEWED = "REVIEWED"
    PUBLISHED = "PUBLISHED"
    DISPUTED = "DISPUTED"
    CORRECTED = "CORRECTED"
    RETIRED = "RETIRED"


_TRANSITIONS: dict[AssertionState, frozenset[AssertionState]] = {
    AssertionState.DRAFT: frozenset({AssertionState.EVIDENCED, AssertionState.RETIRED}),
    AssertionState.EVIDENCED: frozenset({AssertionState.REVIEWED, AssertionState.RETIRED}),
    AssertionState.REVIEWED: frozenset({AssertionState.PUBLISHED, AssertionState.RETIRED}),
    AssertionState.PUBLISHED: frozenset({AssertionState.DISPUTED, AssertionState.CORRECTED, AssertionState.RETIRED}),
    AssertionState.DISPUTED: frozenset({AssertionState.CORRECTED, AssertionState.PUBLISHED, AssertionState.RETIRED}),
    AssertionState.CORRECTED: frozenset({AssertionState.RETIRED}),
    AssertionState.RETIRED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class CorrectionEvent:
    corrected_at: datetime
    corrected_by: str
    reason: str
    replacement_assertion_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "corrected_at": utc_iso(self.corrected_at),
            "corrected_by": self.corrected_by,
            "reason": self.reason,
            "replacement_assertion_id": self.replacement_assertion_id,
        }


@dataclass(frozen=True, slots=True)
class PepAssertion:
    id: str
    subject_key: str
    assertion_kind: str
    source_document_id: str
    source_url_or_receipt: str
    retrieval_timestamp: datetime
    locator: str
    supporting_passage_or_region: str
    asserted_by: str
    reviewed_by: str | None
    state: AssertionState
    valid_from: str | None
    valid_to: str | None
    precision: str
    supersedes: str | None = None
    correction_history: tuple[CorrectionEvent, ...] = ()
    role_classification: str = "pep"

    @classmethod
    def draft(
        cls,
        *,
        subject_key: str,
        assertion_kind: str,
        source_document_id: str,
        source_url_or_receipt: str,
        retrieval_timestamp: datetime,
        locator: str,
        supporting_passage_or_region: str,
        asserted_by: str,
        valid_from: str | None,
        valid_to: str | None,
        precision: str,
        role_classification: str = "pep",
        supersedes: str | None = None,
    ) -> PepAssertion:
        if role_classification not in {"pep", "rca"}:
            raise ValueError("role classification must be pep or rca")
        identity = stable_id(
            "pep-assertion",
            subject_key,
            assertion_kind,
            source_document_id,
            locator,
            valid_from or "",
            valid_to or "",
            supersedes or "",
        )
        return cls(
            id=identity,
            subject_key=subject_key,
            assertion_kind=assertion_kind,
            source_document_id=source_document_id,
            source_url_or_receipt=source_url_or_receipt,
            retrieval_timestamp=retrieval_timestamp,
            locator=locator,
            supporting_passage_or_region=supporting_passage_or_region,
            asserted_by=asserted_by,
            reviewed_by=None,
            state=AssertionState.DRAFT,
            valid_from=valid_from,
            valid_to=valid_to,
            precision=precision,
            supersedes=supersedes,
            role_classification=role_classification,
        )

    def transition(self, target: AssertionState, *, actor: str | None = None) -> PepAssertion:
        if target not in _TRANSITIONS[self.state]:
            raise InvalidTransition(f"assertion cannot transition {self.state} -> {target}")
        reviewed_by = self.reviewed_by
        if target is AssertionState.REVIEWED:
            if not actor:
                raise ValueError("review requires an actor")
            if actor == self.asserted_by:
                raise ValueError("assertion and review must be performed by different people")
            reviewed_by = actor
        if target is AssertionState.PUBLISHED and not reviewed_by:
            raise InvalidTransition("an assertion must be independently reviewed before publication")
        return replace(self, state=target, reviewed_by=reviewed_by)

    def corrected_by(
        self,
        replacement: PepAssertion,
        *,
        actor: str,
        reason: str,
        corrected_at: datetime,
    ) -> PepAssertion:
        if replacement.supersedes != self.id:
            raise ValueError("replacement must explicitly supersede this assertion")
        if not reason.strip():
            raise ValueError("correction reason is required")
        event = CorrectionEvent(corrected_at, actor, reason, replacement.id)
        transitioned = self.transition(AssertionState.CORRECTED)
        return replace(transitioned, correction_history=(*self.correction_history, event))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "subject_key": self.subject_key,
            "assertion_kind": self.assertion_kind,
            "source_document_id": self.source_document_id,
            "source_url_or_receipt": self.source_url_or_receipt,
            "retrieval_timestamp": utc_iso(self.retrieval_timestamp),
            "locator": self.locator,
            "supporting_passage_or_region": self.supporting_passage_or_region,
            "asserted_by": self.asserted_by,
            "reviewed_by": self.reviewed_by,
            "state": self.state.value,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "precision": self.precision,
            "supersedes": self.supersedes,
            "correction_history": [item.to_dict() for item in self.correction_history],
            "role_classification": self.role_classification,
        }

