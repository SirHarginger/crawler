"""Configuration-driven dataset registry and adapter contracts."""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from .domain import DomainInterpreter, DomainObject
from .field_inventory import FieldInventory
from .model import DistributionRestriction, Snapshot, SourceRecord
from .projection import ProjectionProfile
from .source import SourceParser
from .storage import BlobStore


@dataclass(frozen=True, slots=True)
class DatasetMetadata:
    key: str
    title: str
    publisher_authority: str
    legal_basis: str
    risk_semantics: str
    expected_cadence: str
    freshness_slo_hours: int
    anomaly_threshold: float
    distribution_restriction: DistributionRestriction
    redistribution_basis: str
    dataset_scope: str
    parser_version: str
    mapping_version: str
    normalization_version: str
    layer_1_5_enabled: bool
    layer_1_5_justification: str | None
    source_endpoint: str | None
    source_format: str | None

    @classmethod
    def from_path(cls, path: Path) -> DatasetMetadata:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls(
            key=str(raw["key"]),
            title=str(raw["title"]),
            publisher_authority=str(raw["publisher_authority"]),
            legal_basis=str(raw["legal_basis"]),
            risk_semantics=str(raw["risk_semantics"]),
            expected_cadence=str(raw["expected_cadence"]),
            freshness_slo_hours=int(raw["freshness_slo_hours"]),
            anomaly_threshold=float(raw["anomaly_threshold"]),
            distribution_restriction=DistributionRestriction(raw["distribution_restriction"]),
            redistribution_basis=str(raw["redistribution_basis"]),
            dataset_scope=str(raw["dataset_scope"]),
            parser_version=str(raw["parser_version"]),
            mapping_version=str(raw["mapping_version"]),
            normalization_version=str(raw["normalization_version"]),
            layer_1_5_enabled=bool(raw.get("layer_1_5_enabled", False)),
            layer_1_5_justification=raw.get("layer_1_5_justification"),
            source_endpoint=raw.get("source_endpoint"),
            source_format=raw.get("source_format"),
        )

    def validate(self) -> None:
        if self.legal_basis not in {"binding", "contractual", "informational"}:
            raise ValueError("unsupported legal basis")
        if self.risk_semantics not in {"sanction", "pep", "rca", "crime", "debarment", "advisory"}:
            raise ValueError("unsupported risk semantics")
        if not 0 <= self.anomaly_threshold <= 1:
            raise ValueError("anomaly threshold must be between zero and one")
        if self.layer_1_5_enabled and not self.layer_1_5_justification:
            raise ValueError("Layer 1.5 requires a written source-scoped justification")


class DatasetAdapter(Protocol):
    """Structural contract every dataset package's module-level ``adapter`` satisfies.

    Members are declared as read-only properties rather than plain
    attributes: adapters are only ever read through this Protocol, never
    reassigned through it, and a plain-attribute Protocol member requires
    invariant (exact-type) matches under strict mypy, which a concrete
    adapter's more specific attribute types (e.g. a dataset's own parser
    subclass) would otherwise fail.
    """

    @property
    def metadata(self) -> DatasetMetadata: ...
    @property
    def parser(self) -> SourceParser: ...
    @property
    def inventory(self) -> FieldInventory: ...
    @property
    def projection_profile(self) -> ProjectionProfile: ...
    @property
    def domain_interpreter(self) -> DomainInterpreter | None: ...

    def validate_source(self, snapshot: Snapshot, records: list[SourceRecord], store: BlobStore) -> list[str]: ...


def interpret_if_enabled(adapter: DatasetAdapter, records: list[SourceRecord]) -> list[DomainObject]:
    if not adapter.metadata.layer_1_5_enabled:
        return []
    if adapter.domain_interpreter is None:
        raise ValueError("dataset enables Layer 1.5 but provides no interpreter")
    if not adapter.domain_interpreter.justification.strip():
        raise ValueError("Layer 1.5 interpreter has no justification")
    return sorted(adapter.domain_interpreter.interpret(records), key=lambda item: item.id)


def load_adapter(key: str) -> DatasetAdapter:
    """Import ``datasets.<key>.crawler`` and return its module-level ``adapter``.

    Every onboarded dataset package exposes a module-level ``adapter``
    satisfying ``DatasetAdapter`` (see ``docs/source-onboarding.md``). Routing
    through this one lookup means adding a dataset never requires a change to
    CLI wiring.
    """
    module_name = f"datasets.{key}.crawler"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise ValueError(f"no dataset package registered for {key!r}") from exc
    try:
        adapter: DatasetAdapter = module.adapter
    except AttributeError as exc:
        raise ValueError(f"{module_name} does not expose an 'adapter'") from exc
    if adapter.metadata.key != key:
        raise ValueError(
            f"{module_name}.adapter.metadata.key is {adapter.metadata.key!r}, expected {key!r}"
        )
    return adapter

