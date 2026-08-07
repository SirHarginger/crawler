"""Versioned parser, interpretation, and projection for UN-shaped XML fixtures."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from screening_crawler.datasets import DatasetMetadata
from screening_crawler.domain import DomainObject
from screening_crawler.errors import QuarantineRequired
from screening_crawler.field_inventory import FieldInventory
from screening_crawler.model import Snapshot, SourceRecord
from screening_crawler.projection import PropertyInput
from screening_crawler.source import ParsedRecord
from screening_crawler.storage import BlobStore

ROOT = Path(__file__).parent


def _text(element: ElementTree.Element, name: str) -> str | None:
    child = element.find(name)
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _native_value(element: ElementTree.Element) -> Any:
    children = list(element)
    if not children:
        return (element.text or "").strip()
    grouped: dict[str, list[Any]] = {}
    for child in children:
        tag = child.tag.rsplit("}", 1)[-1]
        grouped.setdefault(tag, []).append(_native_value(child))
    return {
        tag: values[0] if len(values) == 1 else values
        for tag, values in sorted(grouped.items())
    }


def _children(element: ElementTree.Element, name: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for child in element.findall(name):
        row = _native_value(child)
        if row:
            rows.append(row)
    return rows


class UnXmlParserV1:
    version = "un-xml-v1"

    def parse(self, snapshot: Snapshot, store: BlobStore) -> list[ParsedRecord]:
        records: list[ParsedRecord] = []
        for artifact in snapshot.artifacts:
            body = store.get(artifact.storage_key)
            artifact.verify(body)
            for _event, element in ElementTree.iterparse(io.BytesIO(body), events=("end",)):
                tag = element.tag.rsplit("}", 1)[-1]
                if tag in {"INDIVIDUALS", "ENTITIES"}:
                    expected = "INDIVIDUAL" if tag == "INDIVIDUALS" else "ENTITY"
                    unexpected = sorted(
                        {
                            child.tag.rsplit("}", 1)[-1]
                            for child in element
                            if child.tag.rsplit("}", 1)[-1] != expected
                        }
                    )
                    if unexpected:
                        raise QuarantineRequired(
                            f"unknown record structures under {tag}: {unexpected}"
                        )
                    element.clear()
                    continue
                if tag == "CONSOLIDATED_LIST":
                    unexpected = sorted(
                        {
                            child.tag.rsplit("}", 1)[-1]
                            for child in element
                            if child.tag.rsplit("}", 1)[-1] not in {"INDIVIDUALS", "ENTITIES"}
                        }
                    )
                    if unexpected:
                        raise QuarantineRequired(
                            f"unknown top-level UN structures: {unexpected}"
                        )
                    element.clear()
                    continue
                if tag not in {"INDIVIDUAL", "ENTITY"}:
                    continue
                native = self._native(element, tag)
                native_key = native.get("DATAID")
                if not native_key:
                    raise ValueError(f"{tag} record lacks publisher DATAID")
                records.append(
                    ParsedRecord(
                        record_kind=tag,
                        publisher_native_key=native_key,
                        locator=f"/{tag}[DATAID={native_key}]",
                        native=native,
                        artifact_ids=(artifact.id,),
                    )
                )
                element.clear()
        return records

    def _native(self, element: ElementTree.Element, tag: str) -> dict[str, Any]:
        scalar_fields = (
            "DATAID",
            "VERSIONNUM",
            "FIRST_NAME",
            "SECOND_NAME",
            "THIRD_NAME",
            "FOURTH_NAME",
            "UN_LIST_TYPE",
            "REFERENCE_NUMBER",
            "LISTED_ON",
            "COMMENTS1",
        )
        native: dict[str, Any] = {}
        for field in scalar_fields:
            values = [value for child in element.findall(field) if (value := (child.text or "").strip())]
            if values:
                native[field] = values[0] if len(values) == 1 else values
        prefix = "INDIVIDUAL" if tag == "INDIVIDUAL" else "ENTITY"
        aliases = _children(element, f"{prefix}_ALIAS")
        addresses = _children(element, f"{prefix}_ADDRESS")
        if aliases:
            native[f"{prefix}_ALIAS"] = aliases
        if addresses:
            native[f"{prefix}_ADDRESS"] = addresses
        if tag == "INDIVIDUAL":
            dates = _children(element, "INDIVIDUAL_DATE_OF_BIRTH")
            nationalities = _children(element, "NATIONALITY")
            if dates:
                native["INDIVIDUAL_DATE_OF_BIRTH"] = dates
            if nationalities:
                native["NATIONALITY"] = nationalities
        known_direct = set(scalar_fields) | {f"{prefix}_ALIAS", f"{prefix}_ADDRESS"}
        if tag == "INDIVIDUAL":
            known_direct |= {"INDIVIDUAL_DATE_OF_BIRTH", "NATIONALITY"}
        for child in element:
            child_tag = child.tag.rsplit("}", 1)[-1]
            if child_tag not in known_direct:
                native[child_tag] = _native_value(child)
        return native


class UnProjectionProfileV1:
    source_key = "un_sc_consolidated"
    mapping_version = "un-ftm-v1"

    def schema_for(self, record: SourceRecord) -> str:
        return {"INDIVIDUAL": "Person", "ENTITY": "Organization"}[record.record_kind]

    def properties_for(self, record: SourceRecord) -> list[PropertyInput]:
        native = record.parsed
        output: list[PropertyInput] = []
        name_parts = [
            native[field] for field in ("FIRST_NAME", "SECOND_NAME", "THIRD_NAME", "FOURTH_NAME") if native.get(field)
        ]
        if name_parts:
            output.append(
                PropertyInput(
                    "name",
                    " ".join(name_parts),
                    "$.FIRST_NAME|$.SECOND_NAME|$.THIRD_NAME|$.FOURTH_NAME",
                    "derived",
                    "join_publisher_name_parts",
                    "1",
                    "\x1f".join(name_parts),
                )
            )
        prefix = "INDIVIDUAL" if record.record_kind == "INDIVIDUAL" else "ENTITY"
        for index, alias in enumerate(native.get(f"{prefix}_ALIAS", [])):
            if value := alias.get("ALIAS_NAME"):
                output.append(PropertyInput("alias", value, f"$.{prefix}_ALIAS[{index}].ALIAS_NAME"))
        for index, address in enumerate(native.get(f"{prefix}_ADDRESS", [])):
            if value := address.get("CITY"):
                output.append(PropertyInput("address", value, f"$.{prefix}_ADDRESS[{index}].CITY"))
            if value := address.get("COUNTRY"):
                output.append(PropertyInput("country", value, f"$.{prefix}_ADDRESS[{index}].COUNTRY"))
        if record.record_kind == "INDIVIDUAL":
            for index, item in enumerate(native.get("INDIVIDUAL_DATE_OF_BIRTH", [])):
                for field in ("DATE", "YEAR"):
                    if value := item.get(field):
                        output.append(PropertyInput("birthDate", value, f"$.INDIVIDUAL_DATE_OF_BIRTH[{index}].{field}"))
            for index, item in enumerate(native.get("NATIONALITY", [])):
                if value := item.get("VALUE"):
                    output.append(PropertyInput("nationality", value, f"$.NATIONALITY[{index}].VALUE"))
        if value := native.get("LISTED_ON"):
            output.append(PropertyInput("createdAt", value, "$.LISTED_ON"))
        return output


class UnDomainInterpreterV1:
    version = "un-domain-v1"
    justification = "UN list type and reference structure carry source-specific legal meaning."

    def interpret(self, records: list[SourceRecord]) -> list[DomainObject]:
        return [
            DomainObject.create(
                source_key=record.source_key,
                kind="UnDesignationContext",
                source_record_ids=(record.id,),
                data={
                    "list_type": record.parsed["UN_LIST_TYPE"],
                    "reference_number": record.parsed["REFERENCE_NUMBER"],
                    "listed_on": record.parsed["LISTED_ON"],
                },
                interpretation_version=self.version,
            )
            for record in records
        ]


class UnDatasetAdapter:
    def __init__(self) -> None:
        self.metadata = DatasetMetadata.from_path(ROOT / "metadata.yml")
        self.metadata.validate()
        self.parser = UnXmlParserV1()
        self.inventory = FieldInventory.from_path(ROOT / "fields.yml")
        self.projection_profile = UnProjectionProfileV1()
        self.domain_interpreter = UnDomainInterpreterV1()

    def validate_source(self, snapshot: Snapshot, records: list[SourceRecord], store: BlobStore) -> list[str]:
        del snapshot, store
        findings: list[str] = []
        if not records:
            findings.append("UN snapshot contains no designation records")
        return findings


adapter = UnDatasetAdapter()
