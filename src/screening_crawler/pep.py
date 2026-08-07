"""Deterministic Person/Position/Occupancy projection for reviewed PEP assertions."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .assertions import AssertionState, PepAssertion
from .canonical import sha256_bytes
from .datasets import DatasetMetadata
from .field_inventory import FieldInventory
from .ids import entity_id, projection_id
from .model import Snapshot, SourceRecord
from .normalization import AdditiveNormalizer
from .projection import ProjectedEntity, ProjectionResult, ProjectionStatement
from .source import SourceParser
from .storage import BlobStore


@dataclass(frozen=True, slots=True)
class PepProjectionInput:
    source_record: SourceRecord
    person_native_key: str
    published_name: str
    name_source_path: str
    position_native_key: str
    published_position: str
    position_source_path: str
    valid_from: str | None
    valid_to: str | None
    date_precision: str


class PepOccupancyProjector:
    """Model time-bounded public office without a permanent PEP boolean."""

    def __init__(
        self,
        *,
        mapping_version: str,
        normalizer: AdditiveNormalizer,
    ) -> None:
        self.mapping_version = mapping_version
        self.normalizer = normalizer

    def project(self, inputs: list[PepProjectionInput]) -> ProjectionResult:
        entities: list[ProjectedEntity] = []
        statements: list[ProjectionStatement] = []
        for item in sorted(
            inputs,
            key=lambda value: (
                value.source_record.source_key,
                value.person_native_key,
                value.position_native_key,
                value.valid_from or "",
            ),
        ):
            record = item.source_record
            person_id = entity_id(record.source_key, "PEP_PERSON", item.person_native_key)
            position_id = entity_id(record.source_key, "PEP_POSITION", item.position_native_key)
            occupancy_native_key = "\x1f".join(
                (
                    item.person_native_key,
                    item.position_native_key,
                    item.valid_from or "",
                    item.valid_to or "",
                    item.date_precision,
                )
            )
            occupancy_id = entity_id(record.source_key, "PEP_OCCUPANCY", occupancy_native_key)
            person_names = [item.published_name]
            statements.append(
                self._statement(person_id, "name", item.published_name, record, item.name_source_path)
            )
            for generated in self.normalizer.expand(item.published_name):
                person_names.append(generated.projected_value)
                statements.append(
                    self._statement(
                        person_id,
                        "name",
                        generated.projected_value,
                        record,
                        item.name_source_path,
                        native=item.published_name,
                        derivation_type=generated.derivation_type,
                        rule=generated.rule,
                        version=generated.rule_version,
                    )
                )
            entities.append(
                ProjectedEntity(
                    person_id, "Person", {"name": tuple(person_names)}, (record.id,), record.source_key
                )
            )
            entities.append(
                ProjectedEntity(
                    position_id,
                    "Position",
                    {"name": (item.published_position,)},
                    (record.id,),
                    record.source_key,
                )
            )
            statements.append(
                self._statement(
                    position_id,
                    "name",
                    item.published_position,
                    record,
                    item.position_source_path,
                )
            )
            occupancy_properties: dict[str, tuple[str, ...]] = {
                "holder": (person_id,),
                "post": (position_id,),
            }
            statements.extend(
                (
                    self._statement(
                        occupancy_id,
                        "holder",
                        person_id,
                        record,
                        "$.person_native_key",
                        native=item.person_native_key,
                        derivation_type="derived",
                        rule="link_within_source_by_native_key",
                    ),
                    self._statement(
                        occupancy_id,
                        "post",
                        position_id,
                        record,
                        "$.position_native_key",
                        native=item.position_native_key,
                        derivation_type="derived",
                        rule="link_within_source_by_native_key",
                    ),
                )
            )
            if item.valid_from:
                occupancy_properties["startDate"] = (item.valid_from,)
                statements.append(
                    self._statement(
                        occupancy_id,
                        "startDate",
                        item.valid_from,
                        record,
                        "$.valid_from",
                    )
                )
            if item.valid_to:
                occupancy_properties["endDate"] = (item.valid_to,)
                statements.append(
                    self._statement(
                        occupancy_id,
                        "endDate",
                        item.valid_to,
                        record,
                        "$.valid_to",
                    )
                )
            entities.append(
                ProjectedEntity(
                    occupancy_id,
                    "Occupancy",
                    occupancy_properties,
                    (record.id,),
                    record.source_key,
                )
            )
        return ProjectionResult(
            tuple(sorted(entities, key=lambda value: value.id)),
            tuple(sorted(statements, key=lambda value: value.projection_id)),
            self.mapping_version,
            self.normalizer.version,
        )

    def _statement(
        self,
        target_id: str,
        prop: str,
        value: str,
        record: SourceRecord,
        path: str,
        *,
        native: str | None = None,
        derivation_type: str = "published",
        rule: str = "publisher_value",
        version: str = "1",
    ) -> ProjectionStatement:
        return ProjectionStatement(
            projection_id(target_id, prop, value, record.id, path),
            target_id,
            prop,
            value,
            record.id,
            path,
            sha256_bytes((native if native is not None else value).encode("utf-8")),
            derivation_type,
            rule,
            version,
            self.mapping_version,
            self.normalizer.version,
        )


class PepSourceAdapter(Protocol):
    """Structural contract for a human-assembled PEP source's module-level ``adapter``.

    Deliberately distinct from ``datasets.DatasetAdapter``: that Protocol's
    ``projection_profile`` produces exactly one entity per record, but a PEP
    occupancy assertion projects to three (Person, Position, Occupancy) via
    ``PepOccupancyProjector`` -- a genuinely different shape, not just a
    different mapping. Forcing it through ``ProjectionProfile`` would be a
    false uniformity, so PEP-shaped datasets get their own adapter contract
    and their own pipeline entry point (``pipeline.process_pep_snapshot``),
    while everything upstream of projection (capture, snapshot assembly,
    field accounting) is unchanged and shared.
    """

    @property
    def metadata(self) -> DatasetMetadata: ...
    @property
    def parser(self) -> SourceParser: ...
    @property
    def inventory(self) -> FieldInventory: ...
    @property
    def mapping_version(self) -> str: ...

    def validate_source(
        self, snapshot: Snapshot, records: list[SourceRecord], store: BlobStore
    ) -> list[str]: ...

    def projection_input_for(self, record: SourceRecord) -> PepProjectionInput: ...

    def assertion_for(self, record: SourceRecord) -> PepAssertion: ...


def load_pep_adapter(key: str) -> PepSourceAdapter:
    """Import ``datasets.<key>.crawler`` and return its module-level ``adapter``.

    Mirrors ``datasets.load_adapter`` exactly, kept separate because the
    return type is ``PepSourceAdapter``, not ``DatasetAdapter``.
    """
    module_name = f"datasets.{key}.crawler"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise ValueError(f"no dataset package registered for {key!r}") from exc
    try:
        adapter: PepSourceAdapter = module.adapter
    except AttributeError as exc:
        raise ValueError(f"{module_name} does not expose an 'adapter'") from exc
    if adapter.metadata.key != key:
        raise ValueError(
            f"{module_name}.adapter.metadata.key is {adapter.metadata.key!r}, expected {key!r}"
        )
    return adapter


def assertion_from_parsed(record: SourceRecord) -> PepAssertion:
    """Reconstruct the PUBLISHED ``PepAssertion`` a PEP-source parser embedded in a record.

    A conforming parser is expected to have already replayed and validated
    the assertion's full DRAFT -> EVIDENCED -> REVIEWED -> PUBLISHED
    lifecycle -- including the independent-reviewer invariant -- at parse
    time, only ever emitting a ``ParsedRecord`` once that succeeds (see
    ``docs/source-onboarding.md``). This function re-hydrates the same
    object from the record's stored fields for serialization; it does not
    re-derive or re-validate the lifecycle. Corrections and disputes are not
    represented here (``supersedes``/``correction_history`` are always
    empty) -- a documented simplification of a first PEP-source integration,
    not a claim that this repository supports the full correction workflow
    yet.
    """
    native = record.parsed
    return PepAssertion(
        id=record.publisher_native_key,
        subject_key=native["subject_key"],
        assertion_kind=native["assertion_kind"],
        source_document_id=native["source_document_id"],
        source_url_or_receipt=native["source_url_or_receipt"],
        retrieval_timestamp=datetime.fromisoformat(native["retrieval_timestamp"]),
        locator=native["locator"],
        supporting_passage_or_region=native["supporting_passage_or_region"],
        asserted_by=native["asserted_by"],
        reviewed_by=native["reviewed_by"],
        state=AssertionState.PUBLISHED,
        valid_from=native.get("valid_from"),
        valid_to=native.get("valid_to"),
        precision=native["precision"],
        supersedes=None,
        correction_history=(),
        role_classification=native["role_classification"],
    )

