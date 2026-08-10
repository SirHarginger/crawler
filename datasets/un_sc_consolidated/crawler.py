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
    # v2 (2026-08-06): INDIVIDUAL_DOCUMENT and INDIVIDUAL_PLACE_OF_BIRTH are now collected
    # explicitly as lists (docs/versioning.md: "byte-to-record behavior, native shape... "
    # requires a bump) -- v1 silently kept only the last of either when a record had more
    # than one, confirmed against the real live source (one individual had 14 documents).
    version = "un-xml-v2"

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
            "GENDER",
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
        known_direct = set(scalar_fields) | {f"{prefix}_ALIAS", f"{prefix}_ADDRESS"}
        if tag == "INDIVIDUAL":
            # These four are genuinely repeatable in the live source (a real individual was
            # observed 2026-08-06 with 14 INDIVIDUAL_DOCUMENT children) -- the generic
            # single-value fallback below would silently keep only the *last* one and drop
            # the rest, which is exactly the "invented missing values by omission" this
            # parser must not do. Collected explicitly, the same way ALIAS/ADDRESS already
            # are, so nothing is lost regardless of how many a given record has.
            dates = _children(element, "INDIVIDUAL_DATE_OF_BIRTH")
            nationalities = _children(element, "NATIONALITY")
            documents = _children(element, "INDIVIDUAL_DOCUMENT")
            places_of_birth = _children(element, "INDIVIDUAL_PLACE_OF_BIRTH")
            if dates:
                native["INDIVIDUAL_DATE_OF_BIRTH"] = dates
            if nationalities:
                native["NATIONALITY"] = nationalities
            if documents:
                native["INDIVIDUAL_DOCUMENT"] = documents
            if places_of_birth:
                native["INDIVIDUAL_PLACE_OF_BIRTH"] = places_of_birth
            known_direct |= {
                "INDIVIDUAL_DATE_OF_BIRTH",
                "NATIONALITY",
                "INDIVIDUAL_DOCUMENT",
                "INDIVIDUAL_PLACE_OF_BIRTH",
            }
        # Everything else observed in the live source (TITLE, DESIGNATION, LIST_TYPE,
        # LAST_DAY_UPDATED, LAST_REVIEWED_ON, NAME_ORIGINAL_SCRIPT, HAS_INTERPOL_LINK,
        # INTERPOL_LINK, SORT_KEY, SORT_KEY_LAST_MOD, ...) is confirmed (live data, 2026-08-06,
        # 736 individuals / 275 entities) to appear at most once per record, so the generic
        # single-value fallback below does not lose anything for them -- verified by direct
        # inspection, not assumed. `properties_for` reads several of these straight out of the
        # generic capture rather than needing a second explicit list here.
        for child in element:
            child_tag = child.tag.rsplit("}", 1)[-1]
            if child_tag not in known_direct:
                native[child_tag] = _native_value(child)
        return native


def _as_strings(value: Any) -> list[str]:
    """Some `<TAG><VALUE>...</VALUE></TAG>`-wrapped leaves genuinely repeat VALUE within a
    single wrapper in the live source (observed: a dual-nationality individual's single
    <NATIONALITY> element carrying two <VALUE> children, not two <NATIONALITY> elements) --
    `_native_value`'s grouping collapses that to a plain string when there's exactly one, or
    a list when there's more than one. Every wrapped-VALUE read in `properties_for` goes
    through this so a genuinely-multi-valued field is never truncated to its first value or
    passed as a raw list into a code path (`ids.stable_id`) that requires strings.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if item]
    return [value] if value else []


class UnProjectionProfileV1:
    source_key = "un_sc_consolidated"
    # v2 (2026-08-06): property selection extended -- gender, title, position, documents
    # (passportNumber/idNumber), place of birth, program, notes, sourceUrl, modifiedAt,
    # original-script alias, and a `topics: sanction` marker were added (docs/versioning.md:
    # "property selection... changes" requires a bump).
    mapping_version = "un-ftm-v2"

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
            # FollowTheMoney's `address` property is a single free-text string, not a
            # structured object -- compose the parts the publisher actually supplied rather
            # than reporting only CITY (the fuller STREET/STATE_PROVINCE/ZIP_CODE detail
            # still survives independently in Layer 1's `parsed`, so nothing here is lost,
            # only what's *projected* is now more complete than before).
            parts = [address.get(part) for part in ("STREET", "CITY", "STATE_PROVINCE", "ZIP_CODE")]
            composed = ", ".join(part for part in parts if part)
            if composed:
                output.append(
                    PropertyInput(
                        "address", composed, f"$.{prefix}_ADDRESS[{index}]", "derived", "join_address_parts"
                    )
                )
            if value := address.get("COUNTRY"):
                output.append(PropertyInput("country", value, f"$.{prefix}_ADDRESS[{index}].COUNTRY"))
        # Fields common to both INDIVIDUAL and ENTITY records: sanctions/list information,
        # source links, and amendment/update dates (doc requirement: "amendment/update date").
        if value := native.get("UN_LIST_TYPE"):
            output.append(PropertyInput("program", value, "$.UN_LIST_TYPE"))
        if value := native.get("COMMENTS1"):
            output.append(PropertyInput("notes", value, "$.COMMENTS1"))
        if value := native.get("INTERPOL_LINK"):
            output.append(PropertyInput("sourceUrl", value, "$.INTERPOL_LINK"))
        original_script = native.get("NAME_ORIGINAL_SCRIPT")
        original_script_value = original_script.get("VALUE") if isinstance(original_script, dict) else original_script
        if original_script_value:
            # A name in its original (often non-Latin) script is still a name form for this
            # entity, not a separate fact about it -- FollowTheMoney has no dedicated
            # "original script" property, and `alias` is the closest honest fit.
            output.append(PropertyInput("alias", original_script_value, "$.NAME_ORIGINAL_SCRIPT"))
        modified_source = native.get("LAST_REVIEWED_ON") or native.get("LAST_DAY_UPDATED")
        if isinstance(modified_source, dict):
            path = "LAST_REVIEWED_ON" if native.get("LAST_REVIEWED_ON") else "LAST_DAY_UPDATED"
            for value in _as_strings(modified_source.get("VALUE")):
                output.append(PropertyInput("modifiedAt", value, f"$.{path}.VALUE"))
        # This dataset's own risk_semantics is "sanction" (metadata.yml) -- every entity it
        # produces is, by construction, a sanctions-list entry. Without this, nothing in the
        # projected entity itself says so: `topics` (not a filename or a database column) is
        # what screening-engine's Yente-compatibility layer actually reads to flag an entity
        # as a match target (`registry.topic.RISKS` intersection, indexer/yente54.py).
        output.append(PropertyInput("topics", "sanction", "$.UN_LIST_TYPE", "inferred", "dataset_risk_semantics"))
        if record.record_kind == "INDIVIDUAL":
            if value := native.get("GENDER"):
                output.append(PropertyInput("gender", value, "$.GENDER"))
            title = native.get("TITLE")
            if isinstance(title, dict):
                for value in _as_strings(title.get("VALUE")):
                    output.append(PropertyInput("title", value, "$.TITLE.VALUE"))
            designation = native.get("DESIGNATION")
            if isinstance(designation, dict):
                for value in _as_strings(designation.get("VALUE")):
                    output.append(PropertyInput("position", value, "$.DESIGNATION.VALUE"))
            for index, item in enumerate(native.get("INDIVIDUAL_DATE_OF_BIRTH", [])):
                for field in ("DATE", "YEAR"):
                    if value := item.get(field):
                        output.append(PropertyInput("birthDate", value, f"$.INDIVIDUAL_DATE_OF_BIRTH[{index}].{field}"))
            for index, item in enumerate(native.get("NATIONALITY", [])):
                for value in _as_strings(item.get("VALUE")):
                    output.append(PropertyInput("nationality", value, f"$.NATIONALITY[{index}].VALUE"))
            for index, item in enumerate(native.get("INDIVIDUAL_PLACE_OF_BIRTH", [])):
                parts = [item.get(part) for part in ("CITY", "STATE_PROVINCE", "COUNTRY")]
                composed = ", ".join(part for part in parts if part)
                if composed:
                    output.append(
                        PropertyInput(
                            "birthPlace", composed, f"$.INDIVIDUAL_PLACE_OF_BIRTH[{index}]", "derived", "join_place_parts"
                        )
                    )
            for index, document in enumerate(native.get("INDIVIDUAL_DOCUMENT", [])):
                number = document.get("NUMBER")
                if not number:
                    continue
                doc_type = (document.get("TYPE_OF_DOCUMENT") or "").lower()
                # FollowTheMoney has no generic "identity document number" property; route by
                # the publisher's own stated document type rather than inventing a distinction
                # it didn't make. The full document (type, issuing country/city, dates, notes)
                # is retained regardless in Layer 1 (`record.parsed`), so this routing decision
                # never loses the underlying evidence, only chooses which projected property a
                # human/matcher sees it under.
                prop = "passportNumber" if "passport" in doc_type else "idNumber"
                output.append(PropertyInput(prop, number, f"$.INDIVIDUAL_DOCUMENT[{index}].NUMBER"))
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
