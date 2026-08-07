"""Versioned parser, interpretation, and projection for UK Sanctions List-shaped XML fixtures.

See ``metadata.yml`` for verification status -- this is the least-verified
dataset onboarded so far. The publisher itself changed during this session's
research (OFSI Consolidated List -> FCDO UK Sanctions List), and the
positional ``Name1``..``Name6`` fields modelled here come from the
historical OFSI schema's public documentation, not a live capture of the
current file.
"""

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

_NAME_FIELDS = ("Name1", "Name2", "Name3", "Name4", "Name5", "Name6")
_SCALAR_TEXT_FIELDS = (
    "UniqueID",
    *_NAME_FIELDS,
    "Gender",
    "DateOfBirth",
    "TownOfBirth",
    "CountryOfBirth",
    "Position",
    "RegimeName",
    "ListingDate",
    "GroupTypeDescription",
)
_LIST_WRAPPERS = ("Alias", "Address", "Nationality")
_SUPPORTED_GROUP_TYPES = frozenset({"Individual", "Entity"})


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(element: ElementTree.Element, name: str) -> str | None:
    child = element.find(f"{{*}}{name}")
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
        tag = _local(child.tag)
        grouped.setdefault(tag, []).append(_native_value(child))
    return {tag: values[0] if len(values) == 1 else values for tag, values in sorted(grouped.items())}


def _rows(element: ElementTree.Element, tag: str) -> list[Any]:
    return [_native_value(child) for child in element.findall(f"{{*}}{tag}")]


def _join_name(row: dict[str, Any]) -> str:
    return " ".join(row[field] for field in _NAME_FIELDS if row.get(field))


class GbOfsiXmlParserV1:
    version = "gb-ofsi-xml-v1"

    def parse(self, snapshot: Snapshot, store: BlobStore) -> list[ParsedRecord]:
        records: list[ParsedRecord] = []
        for artifact in snapshot.artifacts:
            body = store.get(artifact.storage_key)
            artifact.verify(body)
            for _event, element in ElementTree.iterparse(io.BytesIO(body), events=("end",)):
                tag = _local(element.tag)
                if tag != "FinancialSanctionsTarget":
                    if tag in {"ConsolidatedList", "FinancialSanctionsTargets"}:
                        element.clear()
                    continue
                group_type = _text(element, "GroupTypeDescription")
                if group_type not in _SUPPORTED_GROUP_TYPES:
                    raise QuarantineRequired(
                        f"unsupported GroupTypeDescription {group_type!r}; only "
                        f"{sorted(_SUPPORTED_GROUP_TYPES)} are modelled"
                    )
                native = self._native(element)
                native_key = native.get("UniqueID")
                if not native_key:
                    raise ValueError("FinancialSanctionsTarget record lacks publisher UniqueID")
                records.append(
                    ParsedRecord(
                        record_kind=group_type,
                        publisher_native_key=native_key,
                        locator=f"/FinancialSanctionsTarget[UniqueID={native_key}]",
                        native=native,
                        artifact_ids=(artifact.id,),
                    )
                )
                element.clear()
        return records

    def _native(self, element: ElementTree.Element) -> dict[str, Any]:
        native: dict[str, Any] = {}
        for field in _SCALAR_TEXT_FIELDS:
            value = _text(element, field)
            if value is not None:
                native[field] = value
        for wrapper in _LIST_WRAPPERS:
            rows = _rows(element, wrapper)
            if rows:
                native[wrapper] = rows
        known = set(_SCALAR_TEXT_FIELDS) | set(_LIST_WRAPPERS)
        for child in element:
            tag = _local(child.tag)
            if tag in known:
                continue
            native[tag] = _native_value(child)
        return native


class GbOfsiProjectionProfileV1:
    source_key = "gb_ofsi"
    mapping_version = "gb-ofsi-ftm-v1"

    def schema_for(self, record: SourceRecord) -> str:
        return {"Individual": "Person", "Entity": "Organization"}[record.record_kind]

    def properties_for(self, record: SourceRecord) -> list[PropertyInput]:
        native = record.parsed
        output: list[PropertyInput] = []
        name_parts = [native[field] for field in _NAME_FIELDS if native.get(field)]
        if name_parts:
            output.append(
                PropertyInput(
                    "name",
                    " ".join(name_parts),
                    "|".join(f"$.{field}" for field in _NAME_FIELDS if native.get(field)),
                    "derived",
                    "join_publisher_name_parts",
                    "1",
                    "\x1f".join(name_parts),
                )
            )
        for index, alias in enumerate(native.get("Alias", [])):
            joined = _join_name(alias)
            if joined:
                output.append(
                    PropertyInput(
                        "alias",
                        joined,
                        f"$.Alias[{index}]",
                        "derived",
                        "join_publisher_name_parts",
                        "1",
                        joined,
                    )
                )
        for index, address in enumerate(native.get("Address", [])):
            if value := address.get("AddressLine1"):
                output.append(PropertyInput("address", value, f"$.Address[{index}].AddressLine1"))
            if value := address.get("Country"):
                output.append(PropertyInput("country", value, f"$.Address[{index}].Country"))
        if value := native.get("DateOfBirth"):
            output.append(PropertyInput("birthDate", value, "$.DateOfBirth"))
        for index, value in enumerate(native.get("Nationality", [])):
            if isinstance(value, str) and value:
                output.append(PropertyInput("nationality", value, f"$.Nationality[{index}]"))
        return output


class GbOfsiDomainInterpreterV1:
    version = "gb-ofsi-domain-v1"
    justification = (
        "The UK Sanctions List separates the sanctioned target from its regime/listing "
        "membership, which FollowTheMoney flattens."
    )

    def interpret(self, records: list[SourceRecord]) -> list[DomainObject]:
        return [
            DomainObject.create(
                source_key=record.source_key,
                kind="UkSanctionsListing",
                source_record_ids=(record.id,),
                data={
                    "regimeName": record.parsed.get("RegimeName"),
                    "listingDate": record.parsed.get("ListingDate"),
                },
                interpretation_version=self.version,
            )
            for record in records
        ]


class GbOfsiDatasetAdapter:
    def __init__(self) -> None:
        self.metadata = DatasetMetadata.from_path(ROOT / "metadata.yml")
        self.metadata.validate()
        self.parser = GbOfsiXmlParserV1()
        self.inventory = FieldInventory.from_path(ROOT / "fields.yml")
        self.projection_profile = GbOfsiProjectionProfileV1()
        self.domain_interpreter = GbOfsiDomainInterpreterV1()

    def validate_source(
        self, snapshot: Snapshot, records: list[SourceRecord], store: BlobStore
    ) -> list[str]:
        del snapshot, store
        findings: list[str] = []
        if not records:
            findings.append("UK Sanctions List snapshot contains no designation records")
        return findings


adapter = GbOfsiDatasetAdapter()
