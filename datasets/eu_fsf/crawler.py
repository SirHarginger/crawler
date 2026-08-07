"""Versioned parser, interpretation, and projection for EU FSF-shaped XML fixtures.

The EU Financial Sanctions Files schema carries most per-record detail as XML
*attributes* on repeated elements (``nameAlias``, ``address``, ``citizenship``,
``identification``, ``birthdate``, ``regulation``) rather than as nested text
elements the way the UN and OFAC sources do. See ``metadata.yml`` for the
verification status of this structure -- it was not independently confirmed
against live content this session.
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

_ATTRIB_GROUPS = ("nameAlias", "citizenship", "address", "birthdate", "identification", "regulation")
_SUPPORTED_SUBJECT_TYPES = frozenset({"P", "E"})


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(element: ElementTree.Element, name: str) -> str | None:
    child = element.find(f"{{*}}{name}")
    if child is None or child.text is None:
        return None
    value = child.text.strip()
    return value or None


def _native_value(element: ElementTree.Element) -> Any:
    if list(element):
        grouped: dict[str, list[Any]] = {}
        for child in element:
            grouped.setdefault(_local(child.tag), []).append(_native_value(child))
        return {tag: values[0] if len(values) == 1 else values for tag, values in sorted(grouped.items())}
    if element.attrib:
        return dict(sorted(element.attrib.items()))
    return (element.text or "").strip()


def _attrib_rows(element: ElementTree.Element, tag: str) -> list[dict[str, Any]]:
    return [dict(sorted(child.attrib.items())) for child in element.findall(f"{{*}}{tag}")]


class EuFsfXmlParserV1:
    version = "eu-fsf-xml-v1"

    def parse(self, snapshot: Snapshot, store: BlobStore) -> list[ParsedRecord]:
        records: list[ParsedRecord] = []
        for artifact in snapshot.artifacts:
            body = store.get(artifact.storage_key)
            artifact.verify(body)
            for _event, element in ElementTree.iterparse(io.BytesIO(body), events=("end",)):
                tag = _local(element.tag)
                if tag != "sanctionEntity":
                    if tag == "export":
                        element.clear()
                    continue
                subject_type = element.find("{*}subjectType")
                code = subject_type.attrib.get("classificationCode") if subject_type is not None else None
                if code not in _SUPPORTED_SUBJECT_TYPES:
                    raise QuarantineRequired(
                        f"unsupported subjectType classificationCode {code!r}; only "
                        f"{sorted(_SUPPORTED_SUBJECT_TYPES)} are modelled"
                    )
                native = self._native(element, code)
                native_key = native.get("logicalId")
                if not native_key:
                    raise ValueError("sanctionEntity record lacks publisher logicalId")
                records.append(
                    ParsedRecord(
                        record_kind=code,
                        publisher_native_key=native_key,
                        locator=f"/sanctionEntity[logicalId={native_key}]",
                        native=native,
                        artifact_ids=(artifact.id,),
                    )
                )
                element.clear()
        return records

    def _native(self, element: ElementTree.Element, subject_type_code: str) -> dict[str, Any]:
        native: dict[str, Any] = dict(sorted(element.attrib.items()))
        native["subjectTypeCode"] = subject_type_code
        remark = _text(element, "remark")
        if remark is not None:
            native["remark"] = remark
        for tag in _ATTRIB_GROUPS:
            rows = _attrib_rows(element, tag)
            if rows:
                native[tag] = rows
        known = {"subjectType", "remark", *_ATTRIB_GROUPS}
        for child in element:
            tag = _local(child.tag)
            if tag in known:
                continue
            native[tag] = _native_value(child)
        return native


class EuFsfProjectionProfileV1:
    source_key = "eu_fsf"
    mapping_version = "eu-fsf-ftm-v1"

    def schema_for(self, record: SourceRecord) -> str:
        return {"P": "Person", "E": "Organization"}[record.record_kind]

    def properties_for(self, record: SourceRecord) -> list[PropertyInput]:
        native = record.parsed
        output: list[PropertyInput] = []
        names = native.get("nameAlias", [])
        for index, alias in enumerate(names):
            source_path = f"$.nameAlias[{index}]"
            whole_name = alias.get("wholeName")
            if whole_name:
                output.append(PropertyInput("alias", whole_name, f"{source_path}.wholeName"))
                if index == 0:
                    output.append(PropertyInput("name", whole_name, f"{source_path}.wholeName"))
                continue
            parts = [alias[field] for field in ("firstName", "lastName") if alias.get(field)]
            if not parts:
                continue
            joined = " ".join(parts)
            derived = PropertyInput(
                "alias",
                joined,
                f"{source_path}.firstName|{source_path}.lastName",
                "derived",
                "join_publisher_name_parts",
                "1",
                "\x1f".join(parts),
            )
            output.append(derived)
            if index == 0:
                output.append(
                    PropertyInput(
                        "name",
                        joined,
                        f"{source_path}.firstName|{source_path}.lastName",
                        "derived",
                        "join_publisher_name_parts",
                        "1",
                        "\x1f".join(parts),
                    )
                )
        for index, address in enumerate(native.get("address", [])):
            if value := address.get("city"):
                output.append(PropertyInput("address", value, f"$.address[{index}].city"))
            if value := address.get("country"):
                output.append(PropertyInput("country", value, f"$.address[{index}].country"))
        for index, item in enumerate(native.get("birthdate", [])):
            if value := item.get("birthdate"):
                output.append(PropertyInput("birthDate", value, f"$.birthdate[{index}].birthdate"))
        for index, item in enumerate(native.get("citizenship", [])):
            if value := item.get("countryIso2Code") or item.get("countryDescription"):
                output.append(PropertyInput("nationality", value, f"$.citizenship[{index}]"))
        return output


class EuFsfDomainInterpreterV1:
    version = "eu-fsf-domain-v1"
    justification = (
        "The EU FSF separates the sanctioned party from its regulation/legal-instrument "
        "membership, which FollowTheMoney flattens."
    )

    def interpret(self, records: list[SourceRecord]) -> list[DomainObject]:
        return [
            DomainObject.create(
                source_key=record.source_key,
                kind="EuRegulationMembership",
                source_record_ids=(record.id,),
                data={
                    "euReferenceNumber": record.parsed.get("euReferenceNumber"),
                    "regulations": record.parsed.get("regulation", []),
                },
                interpretation_version=self.version,
            )
            for record in records
        ]


class EuFsfDatasetAdapter:
    def __init__(self) -> None:
        self.metadata = DatasetMetadata.from_path(ROOT / "metadata.yml")
        self.metadata.validate()
        self.parser = EuFsfXmlParserV1()
        self.inventory = FieldInventory.from_path(ROOT / "fields.yml")
        self.projection_profile = EuFsfProjectionProfileV1()
        self.domain_interpreter = EuFsfDomainInterpreterV1()

    def validate_source(
        self, snapshot: Snapshot, records: list[SourceRecord], store: BlobStore
    ) -> list[str]:
        del snapshot, store
        findings: list[str] = []
        if not records:
            findings.append("EU FSF snapshot contains no designation records")
        return findings


adapter = EuFsfDatasetAdapter()
