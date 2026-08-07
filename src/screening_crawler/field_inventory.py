"""Reviewed field disposition inventory and structural-drift detection."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from .errors import QuarantineRequired
from .model import SourceRecord


class Disposition(StrEnum):
    MAPPED = "mapped"
    RETAINED = "retained"
    NOT_PROJECTED = "not_projected"
    QUARANTINED = "quarantined"


class FindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    QUARANTINE = "quarantine"


@dataclass(frozen=True, slots=True)
class FieldRule:
    path: str
    expected_type: str
    minimum_cardinality: int
    maximum_cardinality: int | None
    required: bool
    known_values: frozenset[Any]
    mapping_destination: str | None
    disposition: Disposition
    reason: str | None
    minimum_coverage: float
    maximum_coverage: float
    safety_critical: bool


@dataclass(frozen=True, slots=True)
class FieldFinding:
    code: str
    path: str
    severity: FindingSeverity
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "path": self.path,
            "severity": self.severity.value,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class FieldCoverage:
    record_count: int
    paths: dict[str, dict[str, Any]]
    findings: tuple[FieldFinding, ...]

    @property
    def quarantined(self) -> bool:
        return any(item.severity is FindingSeverity.QUARANTINE for item in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_count": self.record_count,
            "paths": self.paths,
            "findings": [item.to_dict() for item in self.findings],
            "status": "quarantined" if self.quarantined else "valid",
        }

    def require_safe(self) -> None:
        if self.quarantined:
            messages = "; ".join(item.message for item in self.findings if item.severity is FindingSeverity.QUARANTINE)
            raise QuarantineRequired(messages)


def _value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def _flatten(value: Any, path: str = "$") -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = defaultdict(list)
    if isinstance(value, dict):
        for key in sorted(value):
            child = f"{path}.{key}"
            nested = _flatten(value[key], child)
            for nested_path, values in nested.items():
                result[nested_path].extend(values)
    elif isinstance(value, list):
        child = f"{path}[]"
        for item in value:
            nested = _flatten(item, child)
            for nested_path, values in nested.items():
                result[nested_path].extend(values)
        if not value:
            result[child] = []
    else:
        result[path].append(value)
    return dict(result)


class FieldInventory:
    def __init__(self, version: str, fields: dict[str, FieldRule]) -> None:
        self.version = version
        self.fields = fields

    @classmethod
    def from_path(cls, path: Path) -> FieldInventory:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("fields"), dict):
            raise ValueError("field inventory must contain a fields mapping")
        fields: dict[str, FieldRule] = {}
        for field_path, config in raw["fields"].items():
            if not isinstance(config, dict):
                raise ValueError(f"field rule {field_path} must be a mapping")
            disposition = Disposition(config["disposition"])
            reason = config.get("reason")
            destination = config.get("mapping_destination")
            if disposition is Disposition.MAPPED and not destination:
                raise ValueError(f"mapped field {field_path} requires mapping_destination")
            if disposition in {
                Disposition.RETAINED,
                Disposition.NOT_PROJECTED,
                Disposition.QUARANTINED,
            } and not reason:
                raise ValueError(f"{disposition.value} field {field_path} requires a reason")
            cardinality = config.get("cardinality", {})
            coverage = config.get("coverage", {})
            fields[field_path] = FieldRule(
                path=field_path,
                expected_type=str(config.get("expected_type", "string")),
                minimum_cardinality=int(cardinality.get("min", 0)),
                maximum_cardinality=(
                    int(cardinality["max"]) if cardinality.get("max") is not None else None
                ),
                required=bool(config.get("required", False)),
                known_values=frozenset(config.get("known_values", [])),
                mapping_destination=destination,
                disposition=disposition,
                reason=reason,
                minimum_coverage=float(coverage.get("min", 0.0)),
                maximum_coverage=float(coverage.get("max", 1.0)),
                safety_critical=bool(config.get("safety_critical", False)),
            )
        return cls(version=str(raw["version"]), fields=fields)

    def assess(
        self,
        records: list[SourceRecord],
        *,
        previous: FieldCoverage | None = None,
    ) -> FieldCoverage:
        per_record = [_flatten(record.parsed) for record in records]
        observed_paths = {path for paths in per_record for path in paths}
        findings: list[FieldFinding] = []
        for unknown in sorted(observed_paths - self.fields.keys()):
            findings.append(
                FieldFinding(
                    "unknown_path",
                    unknown,
                    FindingSeverity.QUARANTINE,
                    f"observed field path has no reviewed disposition: {unknown}",
                )
            )

        report: dict[str, dict[str, Any]] = {}
        total = len(records)
        for path, rule in sorted(self.fields.items()):
            present = sum(path in paths for paths in per_record)
            coverage = present / total if total else 0.0
            values = [value for paths in per_record for value in paths.get(path, [])]
            types = Counter(_value_type(value) for value in values)
            enum_values = sorted({value for value in values if isinstance(value, (str, int, bool))}, key=str)
            cardinalities = [len(paths.get(path, [])) for paths in per_record]
            report[path] = {
                "present_records": present,
                "coverage": coverage,
                "types": dict(sorted(types.items())),
                "observed_values": enum_values,
                "minimum_cardinality": min(cardinalities, default=0),
                "maximum_cardinality": max(cardinalities, default=0),
                "disposition": rule.disposition.value,
            }
            severity = FindingSeverity.QUARANTINE if rule.safety_critical else FindingSeverity.WARNING
            if rule.required and present < total:
                findings.append(FieldFinding("required_missing", path, severity, f"required field disappeared: {path}"))
            previous_path = previous.paths.get(path) if previous is not None else None
            if (
                previous_path is not None
                and previous_path.get("present_records", 0) > 0
                and present == 0
            ):
                findings.append(
                    FieldFinding(
                        "field_disappeared",
                        path,
                        FindingSeverity.QUARANTINE,
                        f"previously observed field disappeared: {path}",
                    )
                )
            if coverage < rule.minimum_coverage or coverage > rule.maximum_coverage:
                findings.append(
                    FieldFinding(
                        "coverage_shift",
                        path,
                        severity,
                        f"coverage {coverage:.4f} outside {rule.minimum_coverage:.4f}..{rule.maximum_coverage:.4f}: {path}",
                    )
                )
            unexpected_types = set(types) - {rule.expected_type}
            if unexpected_types:
                findings.append(
                    FieldFinding(
                        "type_change",
                        path,
                        severity,
                        f"unexpected types {sorted(unexpected_types)} at {path}",
                    )
                )
            if any(value < rule.minimum_cardinality for value in cardinalities):
                findings.append(FieldFinding("cardinality_low", path, severity, f"cardinality below minimum at {path}"))
            if rule.maximum_cardinality is not None and any(
                value > rule.maximum_cardinality for value in cardinalities
            ):
                findings.append(FieldFinding("cardinality_high", path, severity, f"cardinality above maximum at {path}"))
            unknown_values = set(enum_values) - rule.known_values if rule.known_values else set()
            if unknown_values:
                findings.append(
                    FieldFinding(
                        "unknown_enumeration",
                        path,
                        FindingSeverity.QUARANTINE,
                        f"unknown enumeration values at {path}: {sorted(unknown_values, key=str)}",
                    )
                )
            if rule.disposition is Disposition.QUARANTINED and present:
                findings.append(
                    FieldFinding(
                        "quarantined_field_present",
                        path,
                        FindingSeverity.QUARANTINE,
                        f"field configured to quarantine was observed: {path}",
                    )
                )
        return FieldCoverage(total, report, tuple(findings))
