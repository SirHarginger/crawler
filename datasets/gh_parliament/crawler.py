"""Intake parser and PEP-shaped adapter for the Parliament of Ghana register.

Unlike every other dataset in this repository, there is no publisher bytes
to parse: the "source" is a reviewed intake file (``intake/current.yml``)
maintained by an external dual-review process. This parser's job is to
replay and validate each entry's DRAFT -> EVIDENCED -> REVIEWED -> PUBLISHED
lifecycle (see ``screening_crawler.assertions``) and reject -- via
``QuarantineRequired``, the same safety gate every other dataset uses for
malformed input -- anything that doesn't hold up, including a reviewer who
is the same person as the asserter. Only entries already marked
``state: PUBLISHED`` in the intake file are considered at all; everything
else is left for a future crawl once it clears review.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from screening_crawler.assertions import AssertionState, PepAssertion
from screening_crawler.datasets import DatasetMetadata
from screening_crawler.errors import InvalidTransition, QuarantineRequired
from screening_crawler.field_inventory import FieldInventory
from screening_crawler.model import Snapshot, SourceRecord, utc_iso
from screening_crawler.pep import PepProjectionInput, assertion_from_parsed
from screening_crawler.source import ParsedRecord
from screening_crawler.storage import BlobStore

ROOT = Path(__file__).parent

_REQUIRED_ENTRY_FIELDS = (
    "subject_key",
    "assertion_kind",
    "source_document_id",
    "source_url_or_receipt",
    "retrieval_timestamp",
    "locator",
    "supporting_passage_or_region",
    "asserted_by",
    "reviewed_by",
    "state",
    "valid_from",
    "valid_to",
    "precision",
    "role_classification",
    "person_native_key",
    "published_name",
    "position_native_key",
    "published_position",
)


class GhParliamentIntakeParserV1:
    version = "gh-parliament-intake-v1"

    def parse(self, snapshot: Snapshot, store: BlobStore) -> list[ParsedRecord]:
        records: list[ParsedRecord] = []
        for artifact in snapshot.artifacts:
            body = store.get(artifact.storage_key)
            artifact.verify(body)
            raw = yaml.safe_load(body.decode("utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("assertions"), list):
                raise QuarantineRequired("intake file must contain an 'assertions' list")
            for index, entry in enumerate(raw["assertions"]):
                record = self._record_for(entry, index, artifact.id)
                if record is not None:
                    records.append(record)
        return records

    def _record_for(self, entry: Any, index: int, artifact_id: str) -> ParsedRecord | None:
        if not isinstance(entry, dict):
            raise QuarantineRequired(f"intake entry {index} is not a mapping")
        missing = [field for field in _REQUIRED_ENTRY_FIELDS if field not in entry]
        if missing:
            raise QuarantineRequired(f"intake entry {index} missing required fields: {missing}")
        if entry["state"] != "PUBLISHED":
            # Not yet ready for release. It stays in the intake file and
            # will be picked up once a future review marks it PUBLISHED --
            # not discarded, just not part of this release.
            return None

        try:
            retrieval_timestamp = datetime.fromisoformat(entry["retrieval_timestamp"])
        except (TypeError, ValueError) as exc:
            raise QuarantineRequired(
                f"intake entry {index} has an invalid retrieval_timestamp"
            ) from exc

        try:
            assertion = PepAssertion.draft(
                subject_key=entry["subject_key"],
                assertion_kind=entry["assertion_kind"],
                source_document_id=entry["source_document_id"],
                source_url_or_receipt=entry["source_url_or_receipt"],
                retrieval_timestamp=retrieval_timestamp,
                locator=entry["locator"],
                supporting_passage_or_region=entry["supporting_passage_or_region"],
                asserted_by=entry["asserted_by"],
                valid_from=entry["valid_from"],
                valid_to=entry["valid_to"],
                precision=entry["precision"],
                role_classification=entry["role_classification"],
            )
            assertion = assertion.transition(AssertionState.EVIDENCED)
            assertion = assertion.transition(AssertionState.REVIEWED, actor=entry["reviewed_by"])
            assertion = assertion.transition(AssertionState.PUBLISHED)
        except (ValueError, InvalidTransition) as exc:
            raise QuarantineRequired(
                f"intake entry {index} failed assertion lifecycle validation: {exc}"
            ) from exc

        native: dict[str, Any] = {
            "subject_key": assertion.subject_key,
            "assertion_kind": assertion.assertion_kind,
            "source_document_id": assertion.source_document_id,
            "source_url_or_receipt": assertion.source_url_or_receipt,
            "retrieval_timestamp": utc_iso(assertion.retrieval_timestamp),
            "locator": assertion.locator,
            "supporting_passage_or_region": assertion.supporting_passage_or_region,
            "asserted_by": assertion.asserted_by,
            "reviewed_by": assertion.reviewed_by,
            "valid_from": assertion.valid_from,
            "valid_to": assertion.valid_to,
            "precision": assertion.precision,
            "role_classification": assertion.role_classification,
            "person_native_key": entry["person_native_key"],
            "published_name": entry["published_name"],
            "position_native_key": entry["position_native_key"],
            "published_position": entry["published_position"],
        }
        return ParsedRecord(
            record_kind="PepOccupancyAssertion",
            publisher_native_key=assertion.id,
            locator=assertion.locator,
            native=native,
            artifact_ids=(artifact_id,),
            asserted_valid_from=assertion.valid_from,
            asserted_valid_to=assertion.valid_to,
            precision=assertion.precision,
        )


class GhParliamentPepAdapter:
    def __init__(self) -> None:
        self.metadata = DatasetMetadata.from_path(ROOT / "metadata.yml")
        self.metadata.validate()
        self.parser = GhParliamentIntakeParserV1()
        self.inventory = FieldInventory.from_path(ROOT / "fields.yml")
        self.mapping_version = self.metadata.mapping_version

    def validate_source(
        self, snapshot: Snapshot, records: list[SourceRecord], store: BlobStore
    ) -> list[str]:
        del snapshot, store
        findings: list[str] = []
        if not records:
            findings.append(
                "gh_parliament intake produced no PUBLISHED assertions "
                "(intake/current.yml is still a placeholder, or nothing has cleared review yet)"
            )
        return findings

    def projection_input_for(self, record: SourceRecord) -> PepProjectionInput:
        native = record.parsed
        return PepProjectionInput(
            source_record=record,
            person_native_key=native["person_native_key"],
            published_name=native["published_name"],
            name_source_path="$.published_name",
            position_native_key=native["position_native_key"],
            published_position=native["published_position"],
            position_source_path="$.published_position",
            valid_from=native.get("valid_from"),
            valid_to=native.get("valid_to"),
            date_precision=native["precision"],
        )

    def assertion_for(self, record: SourceRecord) -> PepAssertion:
        return assertion_from_parsed(record)


adapter = GhParliamentPepAdapter()
