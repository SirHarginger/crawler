"""Versioned parser, interpretation, and projection for OFAC Consolidated-shaped XML fixtures.

The Consolidated (non-SDN) list is published through the same pipeline and
XML schema as us_ofac_sdn (see that dataset's crawler.py for the identical
structure), but is kept as its own self-contained dataset package rather
than importing shared code: it is a legally distinct list with its own
CODEOWNERS review and its own risk of independent drift, and every other
dataset in this repository follows the same one-package-per-dataset
convention.
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

_SCALAR_TEXT_FIELDS = ("uid", "firstName", "lastName", "sdnType", "remarks")
_LIST_WRAPPERS = (
    "programList",
    "akaList",
    "addressList",
    "idList",
    "dateOfBirthList",
    "nationalityList",
)
_SUPPORTED_SDN_TYPES = frozenset({"Individual", "Entity"})


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(element: ElementTree.Element, name: str) -> str | None:
    # `{*}name` matches `name` under any (or no) namespace -- the real
    # publisher XML is namespaced, fixtures may or may not be.
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
    return {
        tag: values[0] if len(values) == 1 else values
        for tag, values in sorted(grouped.items())
    }


def _texts(element: ElementTree.Element, wrapper: str, item: str) -> list[str]:
    container = element.find(f"{{*}}{wrapper}")
    if container is None:
        return []
    return [
        value
        for child in container.findall(f"{{*}}{item}")
        if (value := (child.text or "").strip())
    ]


def _rows(element: ElementTree.Element, wrapper: str, item: str) -> list[dict[str, Any]]:
    container = element.find(f"{{*}}{wrapper}")
    if container is None:
        return []
    return [_native_value(child) for child in container.findall(f"{{*}}{item}")]


class OfacConsolidatedXmlParserV1:
    version = "ofac-consolidated-xml-v1"

    def parse(self, snapshot: Snapshot, store: BlobStore) -> list[ParsedRecord]:
        records: list[ParsedRecord] = []
        for artifact in snapshot.artifacts:
            body = store.get(artifact.storage_key)
            artifact.verify(body)
            for _event, element in ElementTree.iterparse(io.BytesIO(body), events=("end",)):
                tag = _local(element.tag)
                if tag != "sdnEntry":
                    if tag in {"sdnList", "publshInformation"}:
                        element.clear()
                    continue
                sdn_type = _text(element, "sdnType")
                if sdn_type not in _SUPPORTED_SDN_TYPES:
                    raise QuarantineRequired(
                        f"unsupported sdnType {sdn_type!r}; only "
                        f"{sorted(_SUPPORTED_SDN_TYPES)} are modelled"
                    )
                native = self._native(element)
                native_key = native.get("uid")
                if not native_key:
                    raise ValueError("sdnEntry record lacks publisher uid")
                records.append(
                    ParsedRecord(
                        record_kind=sdn_type,
                        publisher_native_key=native_key,
                        locator=f"/sdnEntry[uid={native_key}]",
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
        programs = _texts(element, "programList", "program")
        if programs:
            native["programList"] = programs
        for wrapper, item in (
            ("akaList", "aka"),
            ("addressList", "address"),
            ("idList", "id"),
            ("dateOfBirthList", "dateOfBirthItem"),
            ("nationalityList", "nationality"),
        ):
            rows = _rows(element, wrapper, item)
            if rows:
                native[wrapper] = rows
        # Nothing observed is ever silently discarded: any structure this
        # dataset does not yet model is still captured so field accounting
        # can flag it as an unknown path rather than lose it.
        known = set(_SCALAR_TEXT_FIELDS) | set(_LIST_WRAPPERS)
        for child in element:
            tag = _local(child.tag)
            if tag in known:
                continue
            native[tag] = _native_value(child)
        return native


class OfacConsolidatedProjectionProfileV1:
    source_key = "us_ofac_consolidated"
    mapping_version = "ofac-consolidated-ftm-v1"

    def schema_for(self, record: SourceRecord) -> str:
        return {"Individual": "Person", "Entity": "Organization"}[record.record_kind]

    def properties_for(self, record: SourceRecord) -> list[PropertyInput]:
        native = record.parsed
        output: list[PropertyInput] = []
        name_parts = [native[field] for field in ("firstName", "lastName") if native.get(field)]
        if name_parts:
            output.append(
                PropertyInput(
                    "name",
                    " ".join(name_parts),
                    "$.firstName|$.lastName",
                    "derived",
                    "join_publisher_name_parts",
                    "1",
                    "\x1f".join(name_parts),
                )
            )
        for index, aka in enumerate(native.get("akaList", [])):
            parts = [aka[field] for field in ("firstName", "lastName") if aka.get(field)]
            if parts:
                output.append(
                    PropertyInput(
                        "alias",
                        " ".join(parts),
                        f"$.akaList[{index}].firstName|$.akaList[{index}].lastName",
                        "derived",
                        "join_publisher_name_parts",
                        "1",
                        "\x1f".join(parts),
                    )
                )
        for index, address in enumerate(native.get("addressList", [])):
            if value := address.get("city"):
                output.append(PropertyInput("address", value, f"$.addressList[{index}].city"))
            if value := address.get("country"):
                output.append(PropertyInput("country", value, f"$.addressList[{index}].country"))
        for index, item in enumerate(native.get("dateOfBirthList", [])):
            if value := item.get("dateOfBirth"):
                output.append(
                    PropertyInput("birthDate", value, f"$.dateOfBirthList[{index}].dateOfBirth")
                )
        for index, item in enumerate(native.get("nationalityList", [])):
            if value := item.get("country"):
                output.append(
                    PropertyInput("nationality", value, f"$.nationalityList[{index}].country")
                )
        return output


class OfacConsolidatedDomainInterpreterV1:
    version = "ofac-consolidated-domain-v1"
    justification = (
        "OFAC separates the sanctioned party from its program/list membership and "
        "identifier records, which FollowTheMoney flattens."
    )

    def interpret(self, records: list[SourceRecord]) -> list[DomainObject]:
        return [
            DomainObject.create(
                source_key=record.source_key,
                kind="OfacSanctionsEntry",
                source_record_ids=(record.id,),
                data={
                    "programs": record.parsed.get("programList", []),
                    "identifiers": [
                        {
                            "idType": item.get("idType"),
                            "idNumber": item.get("idNumber"),
                            "idCountry": item.get("idCountry"),
                        }
                        for item in record.parsed.get("idList", [])
                    ],
                },
                interpretation_version=self.version,
            )
            for record in records
        ]


class OfacConsolidatedDatasetAdapter:
    def __init__(self) -> None:
        self.metadata = DatasetMetadata.from_path(ROOT / "metadata.yml")
        self.metadata.validate()
        self.parser = OfacConsolidatedXmlParserV1()
        self.inventory = FieldInventory.from_path(ROOT / "fields.yml")
        self.projection_profile = OfacConsolidatedProjectionProfileV1()
        self.domain_interpreter = OfacConsolidatedDomainInterpreterV1()

    def validate_source(
        self, snapshot: Snapshot, records: list[SourceRecord], store: BlobStore
    ) -> list[str]:
        del snapshot, store
        findings: list[str] = []
        if not records:
            findings.append("OFAC Consolidated snapshot contains no designation records")
        return findings


adapter = OfacConsolidatedDatasetAdapter()
