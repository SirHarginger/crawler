"""Deterministic FollowTheMoney projection with property-level lineage."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .canonical import canonical_json_bytes, sha256_bytes
from .ids import entity_id, projection_id
from .model import SourceRecord
from .normalization import AdditiveNormalizer


@dataclass(frozen=True, slots=True)
class PropertyInput:
    property_name: str
    value: str
    source_path: str
    derivation_type: str = "published"
    derivation_rule: str = "publisher_value"
    derivation_version: str = "1"
    native_value: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectionStatement:
    projection_id: str
    ftm_entity_id: str
    ftm_property: str
    projected_value: str
    source_record_id: str
    source_path: str
    native_value_hash: str
    derivation_type: str
    derivation_rule: str
    derivation_version: str
    mapping_version: str
    normalization_version: str

    def to_dict(self) -> dict[str, str]:
        return {
            "projection_id": self.projection_id,
            "ftm_entity_id": self.ftm_entity_id,
            "ftm_property": self.ftm_property,
            "projected_value": self.projected_value,
            "source_record_id": self.source_record_id,
            "source_path": self.source_path,
            "native_value_hash": self.native_value_hash,
            "derivation_type": self.derivation_type,
            "derivation_rule": self.derivation_rule,
            "derivation_version": self.derivation_version,
            "mapping_version": self.mapping_version,
            "normalization_version": self.normalization_version,
        }


@dataclass(frozen=True, slots=True)
class ProjectedEntity:
    id: str
    schema: str
    properties: dict[str, tuple[str, ...]]
    source_record_ids: tuple[str, ...]
    dataset_key: str

    @property
    def caption(self) -> str:
        """A human-readable label. screening-engine's importer requires every
        entity to carry one (schemas/release-manifest.schema.json's consumer,
        importer/verifier.py's required_entity_fields); derived from the first
        published name-shaped property rather than passed in separately, so
        callers can't drift it out of sync with the properties that produced it."""
        for key in ("name", "alias", "weakAlias"):
            values = self.properties.get(key)
            if values:
                return values[0]
        return self.id

    def to_dict(self) -> dict[str, Any]:
        ordered_properties = {
            key: list(sorted(set(values))) for key, values in sorted(self.properties.items())
        }
        return {
            "id": self.id,
            "schema": self.schema,
            "caption": self.caption,
            "dataset_key": self.dataset_key,
            "datasets": [self.dataset_key],
            "properties": ordered_properties,
            "referents": list(sorted(set(self.source_record_ids))),
        }


class ProjectionProfile(Protocol):
    source_key: str
    mapping_version: str

    def schema_for(self, record: SourceRecord) -> str: ...

    def properties_for(self, record: SourceRecord) -> list[PropertyInput]: ...


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    entities: tuple[ProjectedEntity, ...]
    statements: tuple[ProjectionStatement, ...]
    mapping_version: str
    normalization_version: str

    def canonical_entities(self) -> bytes:
        rows = [entity.to_dict() for entity in sorted(self.entities, key=lambda item: item.id)]
        return canonical_json_bytes(rows, newline=True)

    @property
    def canonical_hash(self) -> str:
        return sha256_bytes(self.canonical_entities())


class Projector:
    def __init__(self, profile: ProjectionProfile, normalizer: AdditiveNormalizer) -> None:
        self.profile = profile
        self.normalizer = normalizer

    def project(self, records: list[SourceRecord]) -> ProjectionResult:
        entities: list[ProjectedEntity] = []
        statements: list[ProjectionStatement] = []
        for record in sorted(records, key=lambda item: (item.record_kind, item.publisher_native_key)):
            stable_entity_id = entity_id(
                record.source_key,
                record.record_kind,
                record.publisher_native_key,
            )
            properties: dict[str, list[str]] = {}
            for item in self.profile.properties_for(record):
                if item.derivation_type not in {"published", "normalized", "derived", "inferred"}:
                    raise ValueError("unsupported derivation type")
                properties.setdefault(item.property_name, []).append(item.value)
                statements.append(self._statement(stable_entity_id, record, item))
                if item.property_name in {"name", "alias", "weakAlias"} and item.derivation_type == "published":
                    for generated in self.normalizer.expand(item.value):
                        derived = PropertyInput(
                            property_name=item.property_name,
                            value=generated.projected_value,
                            source_path=item.source_path,
                            derivation_type=generated.derivation_type,
                            derivation_rule=generated.rule,
                            derivation_version=generated.rule_version,
                            native_value=item.value,
                        )
                        properties[item.property_name].append(derived.value)
                        statements.append(self._statement(stable_entity_id, record, derived))
            if not properties:
                raise ValueError(f"projection profile emitted no properties for {record.id}")
            entities.append(
                ProjectedEntity(
                    id=stable_entity_id,
                    schema=self.profile.schema_for(record),
                    properties={key: tuple(values) for key, values in properties.items()},
                    source_record_ids=(record.id,),
                    dataset_key=record.source_key,
                )
            )
        statement_keys = {
            (
                item.ftm_entity_id,
                item.ftm_property,
                item.projected_value,
                item.source_record_id,
                item.source_path,
                item.derivation_type,
            ): item
            for item in statements
        }
        return ProjectionResult(
            entities=tuple(sorted(entities, key=lambda item: item.id)),
            statements=tuple(sorted(statement_keys.values(), key=lambda item: item.projection_id)),
            mapping_version=self.profile.mapping_version,
            normalization_version=self.normalizer.version,
        )

    def _statement(
        self,
        stable_entity_id: str,
        record: SourceRecord,
        item: PropertyInput,
    ) -> ProjectionStatement:
        native = item.native_value if item.native_value is not None else item.value
        return ProjectionStatement(
            projection_id=projection_id(
                stable_entity_id,
                item.property_name,
                item.value,
                record.id,
                item.source_path,
            ),
            ftm_entity_id=stable_entity_id,
            ftm_property=item.property_name,
            projected_value=item.value,
            source_record_id=record.id,
            source_path=item.source_path,
            native_value_hash=sha256_bytes(native.encode("utf-8")),
            derivation_type=item.derivation_type,
            derivation_rule=item.derivation_rule,
            derivation_version=item.derivation_version,
            mapping_version=self.profile.mapping_version,
            normalization_version=self.normalizer.version,
        )

