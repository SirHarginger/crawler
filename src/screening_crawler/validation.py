"""Release contract validation independent of the screening engine.

Mirrors the checks screening-engine's own importer/verifier.py performs
(schema validation, per-file checksum re-verification, a checksum-ledger
cross-check, per-dataset count verification), plus this repo's own deeper
cross-checks (entity/statement provenance, projected-property coverage) that
importer/verifier.py doesn't attempt because it treats parsed content as
convenience, not authority. Catching a contract violation here means it
never reaches a hand-off to screening-engine at all.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

from .errors import ReleaseValidationError

REQUIRED_ROLES = frozenset(
    {
        "checksums",
        "artifacts",
        "source_records",
        "source_record_extents",
        "projection_statements",
        "entities",
        "field_coverage",
        "validation_report",
    }
)


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    code: str
    message: str
    path: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"code": self.code, "message": self.message, "path": self.path}


def _load_ndjson(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            if not line.endswith("\n"):
                raise ReleaseValidationError(
                    f"{path.name}:{number} lacks the canonical LF terminator"
                )
            rows.append(json.loads(line))
    return rows


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _files_by_role(manifest: dict[str, Any], role: str) -> list[dict[str, Any]]:
    return [item for item in manifest["files"] if item["role"] == role]


def _rows_for_role(bundle: Path, manifest: dict[str, Any], role: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in _files_by_role(manifest, role):
        rows.extend(_load_ndjson(bundle / item["path"]))
    return rows


def validate_release_bundle(bundle: Path, schema_root: Path) -> list[ValidationFinding]:
    findings: list[ValidationFinding] = []
    manifest_path = bundle / "manifest.json"
    if not manifest_path.is_file():
        raise ReleaseValidationError("release bundle is incomplete: manifest.json is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    schema = json.loads((schema_root / "release-manifest.schema.json").read_text(encoding="utf-8"))
    try:
        jsonschema.Draft202012Validator(
            schema, format_checker=jsonschema.FormatChecker()
        ).validate(manifest)
    except jsonschema.ValidationError as exc:
        raise ReleaseValidationError(f"manifest schema validation failed: {exc.message}") from exc

    roles_present = {item["role"] for item in manifest["files"]}
    missing_roles = REQUIRED_ROLES - roles_present
    if missing_roles:
        raise ReleaseValidationError(f"release bundle is missing required roles: {sorted(missing_roles)}")

    for item in manifest["files"]:
        path = bundle / item["path"]
        if not path.is_file():
            raise ReleaseValidationError(f"declared file is missing from bundle: {item['path']}")
        if path.stat().st_size != item["size_bytes"]:
            raise ReleaseValidationError(f"size mismatch: {item['path']}")
        if _sha256_path(path) != item["sha256"]:
            raise ReleaseValidationError(f"checksum mismatch: {item['path']}")

    checksums_entries = _files_by_role(manifest, "checksums")
    if len(checksums_entries) != 1:
        raise ReleaseValidationError("exactly one checksums-role file is required")
    declared: dict[str, str] = {}
    for line in (bundle / checksums_entries[0]["path"]).read_text(encoding="ascii").splitlines():
        digest, separator, relative = line.partition("  ")
        if not separator or len(digest) != 64:
            raise ReleaseValidationError("invalid checksums ledger line")
        declared[relative] = digest
    expected = {item["path"]: item["sha256"] for item in manifest["files"] if item["role"] != "checksums"}
    if declared != expected:
        raise ReleaseValidationError("checksum ledger does not cover exactly the declared files")

    records = _rows_for_role(bundle, manifest, "source_records")
    statements = _rows_for_role(bundle, manifest, "projection_statements")
    entities = _rows_for_role(bundle, manifest, "entities")

    source_record_schema = json.loads(
        (schema_root / "source-record.schema.json").read_text(encoding="utf-8")
    )
    statement_schema = json.loads(
        (schema_root / "projection-statement.schema.json").read_text(encoding="utf-8")
    )
    for row in records:
        try:
            jsonschema.Draft202012Validator(
                source_record_schema, format_checker=jsonschema.FormatChecker()
            ).validate(row)
        except jsonschema.ValidationError as exc:
            raise ReleaseValidationError(
                f"source record schema validation failed: {exc.message}"
            ) from exc
    for row in statements:
        try:
            jsonschema.Draft202012Validator(
                statement_schema, format_checker=jsonschema.FormatChecker()
            ).validate(row)
        except jsonschema.ValidationError as exc:
            raise ReleaseValidationError(
                f"projection statement schema validation failed: {exc.message}"
            ) from exc

    record_ids = {row["id"] for row in records}
    entity_ids = {row["id"] for row in entities}
    if len(record_ids) != len(records):
        raise ReleaseValidationError("source record IDs are not unique")
    if len(entity_ids) != len(entities):
        raise ReleaseValidationError("entity IDs are not unique")
    for entity in entities:
        if not entity.get("referents") or not set(entity["referents"]).issubset(record_ids):
            raise ReleaseValidationError(f"entity does not resolve to source records: {entity.get('id')}")
    for statement in statements:
        if statement["source_record_id"] not in record_ids:
            raise ReleaseValidationError("projection statement references an unknown source record")
        if statement["ftm_entity_id"] not in entity_ids:
            raise ReleaseValidationError("projection statement references an unknown entity")
    projected = {
        (item["ftm_entity_id"], item["ftm_property"], item["projected_value"]) for item in statements
    }
    for entity in entities:
        for prop, values in entity["properties"].items():
            for value in values:
                if (entity["id"], prop, value) not in projected:
                    raise ReleaseValidationError(
                        f"projected property has no provenance: {entity['id']}/{prop}/{value}"
                    )

    observed_source_records: dict[str, int] = {}
    for row in records:
        observed_source_records[row["dataset_key"]] = observed_source_records.get(row["dataset_key"], 0) + 1
    observed_entities: dict[str, int] = {}
    for row in entities:
        observed_entities[row["dataset_key"]] = observed_entities.get(row["dataset_key"], 0) + 1
    observed_statements: dict[str, int] = {}
    for row in statements:
        observed_statements[row["dataset_key"]] = observed_statements.get(row["dataset_key"], 0) + 1
    for declared_item in manifest["datasets"]:
        key = declared_item["key"]
        if declared_item["source_record_count"] != observed_source_records.get(key, 0):
            raise ReleaseValidationError(f"manifest source-record count mismatch for {key}")
        if declared_item["entity_count"] != observed_entities.get(key, 0):
            raise ReleaseValidationError(f"manifest entity count mismatch for {key}")
        if declared_item["projection_statement_count"] != observed_statements.get(key, 0):
            raise ReleaseValidationError(f"manifest projection-statement count mismatch for {key}")

    coverage_rows = _rows_for_role(bundle, manifest, "field_coverage")
    if any(row.get("disposition") == "quarantined" for row in coverage_rows):
        raise ReleaseValidationError("field coverage contains a quarantined field")

    validation_rows = _rows_for_role(bundle, manifest, "validation_report")
    if any(row.get("valid") is not True for row in validation_rows):
        raise ReleaseValidationError("validation report contains a non-valid dataset")

    assertions = _rows_for_role(bundle, manifest, "pep_assertions")
    if assertions:
        assertion_schema = json.loads(
            (schema_root / "pep-assertion.schema.json").read_text(encoding="utf-8")
        )
        publisher_native_keys = {row["source_record_key"] for row in records}
        for row in assertions:
            try:
                jsonschema.Draft202012Validator(
                    assertion_schema, format_checker=jsonschema.FormatChecker()
                ).validate({key: value for key, value in row.items() if key != "dataset_key"})
            except jsonschema.ValidationError as exc:
                raise ReleaseValidationError(
                    f"pep assertion schema validation failed: {exc.message}"
                ) from exc
            if row["state"] != "PUBLISHED":
                raise ReleaseValidationError(
                    f"pep assertion {row['id']} is not PUBLISHED; only independently-reviewed "
                    "assertions may be released"
                )
            if row["reviewed_by"] == row["asserted_by"] or not row["reviewed_by"]:
                raise ReleaseValidationError(f"pep assertion {row['id']} lacks an independent reviewer")
            if row["id"] not in publisher_native_keys:
                raise ReleaseValidationError(
                    f"pep assertion {row['id']} does not resolve to a Layer 1 source record"
                )

    return findings
