from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from datasets.us_ofac_sdn.crawler import adapter
from screening_crawler.acquisition import Acquirer, FetchResponse, SnapshotAssembler
from screening_crawler.errors import QuarantineRequired
from screening_crawler.model import CompletenessEvidence, CompletenessKind, DistributionRestriction
from screening_crawler.normalization import AdditiveNormalizer
from screening_crawler.pipeline import process_snapshot
from screening_crawler.storage import MemoryBlobStore

FIXED_TIME = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
FIXTURE_PATH = Path("tests/fixtures/us_ofac_sdn/synthetic.xml")


def _snapshot_from(body: bytes):
    store = MemoryBlobStore()
    artifact = Acquirer(
        store,
        crawler_version="test-v1",
        parser_version=adapter.parser.version,
        runtime_version="test-runtime",
    ).capture_response(
        source_key=adapter.metadata.key,
        response=FetchResponse(
            request_url="fixture://us_ofac_sdn/synthetic.xml",
            status=200,
            headers={"content-type": "application/xml"},
            body=body,
        ),
        retrieved_at=FIXED_TIME,
        restriction=DistributionRestriction.PUBLIC,
    )
    assembler = SnapshotAssembler(adapter.metadata.key, expected_parts=1)
    assembler.add(artifact)
    snapshot = assembler.finalize(CompletenessEvidence(CompletenessKind.SINGLE_FILE, {"fixture": True}))
    return store, snapshot


@pytest.fixture
def ofac_build():
    store, snapshot = _snapshot_from(FIXTURE_PATH.read_bytes())
    return process_snapshot(
        adapter,
        snapshot,
        store,
        observed_at=FIXED_TIME,
        normalizer=AdditiveNormalizer(adapter.metadata.normalization_version),
    )


def test_individual_and_entity_records_are_projected(ofac_build) -> None:
    schemas = {entity.schema for entity in ofac_build.projection.entities}
    assert schemas == {"Person", "Organization"}
    assert len(ofac_build.projection.entities) == 2


def test_individual_name_is_joined_from_first_and_last(ofac_build) -> None:
    person = next(e for e in ofac_build.projection.entities if e.schema == "Person")
    assert "Synthetic Testperson" in person.properties["name"]


def test_entity_name_comes_from_lastname_only(ofac_build) -> None:
    entity = next(e for e in ofac_build.projection.entities if e.schema == "Organization")
    assert "Synthetic Holdings Ltd" in entity.properties["name"]


def test_alias_address_birthdate_and_nationality_are_mapped(ofac_build) -> None:
    person = next(e for e in ofac_build.projection.entities if e.schema == "Person")
    assert "Synth Alias" in person.properties["alias"]
    assert "Synthetic City" in person.properties["address"]
    assert "Synthetica" in person.properties["country"]
    assert "1975-04-12" in person.properties["birthDate"]
    assert "Synthetica" in person.properties["nationality"]


def test_domain_interpretation_carries_programs_and_identifiers(ofac_build) -> None:
    assert len(ofac_build.domain_objects) == 2
    person_context = next(
        obj for obj in ofac_build.domain_objects if obj.data["identifiers"]
    )
    assert "SYNTHETIC-SDGT" in person_context.data["programs"]
    assert person_context.data["identifiers"][0]["idNumber"] == "SYN0000001"


def test_field_coverage_has_no_quarantine_findings(ofac_build) -> None:
    assert ofac_build.field_coverage.quarantined is False


def test_unsupported_sdn_type_quarantines() -> None:
    body = FIXTURE_PATH.read_bytes().replace(b"<sdnType>Individual</sdnType>", b"<sdnType>Vessel</sdnType>")
    store, snapshot = _snapshot_from(body)
    with pytest.raises(QuarantineRequired, match="unsupported sdnType"):
        process_snapshot(
            adapter,
            snapshot,
            store,
            observed_at=FIXED_TIME,
            normalizer=AdditiveNormalizer(adapter.metadata.normalization_version),
        )


def test_regeneration_is_deterministic(ofac_build) -> None:
    store, snapshot = _snapshot_from(FIXTURE_PATH.read_bytes())
    rebuilt = process_snapshot(
        adapter,
        snapshot,
        store,
        observed_at=FIXED_TIME,
        normalizer=AdditiveNormalizer(adapter.metadata.normalization_version),
    )
    assert rebuilt.projection.canonical_hash == ofac_build.projection.canonical_hash
