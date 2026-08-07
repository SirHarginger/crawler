from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from datasets.eu_fsf.crawler import adapter
from screening_crawler.acquisition import Acquirer, FetchResponse, SnapshotAssembler
from screening_crawler.errors import QuarantineRequired
from screening_crawler.model import CompletenessEvidence, CompletenessKind, DistributionRestriction
from screening_crawler.normalization import AdditiveNormalizer
from screening_crawler.pipeline import process_snapshot
from screening_crawler.storage import MemoryBlobStore

FIXED_TIME = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
FIXTURE_PATH = Path("tests/fixtures/eu_fsf/synthetic.xml")


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
            request_url="fixture://eu_fsf/synthetic.xml",
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
def eu_build():
    store, snapshot = _snapshot_from(FIXTURE_PATH.read_bytes())
    return process_snapshot(
        adapter,
        snapshot,
        store,
        observed_at=FIXED_TIME,
        normalizer=AdditiveNormalizer(adapter.metadata.normalization_version),
    )


def test_person_and_organization_records_are_projected(eu_build) -> None:
    schemas = {entity.schema for entity in eu_build.projection.entities}
    assert schemas == {"Person", "Organization"}
    assert len(eu_build.projection.entities) == 2


def test_primary_name_comes_from_first_namealias_wholename(eu_build) -> None:
    person = next(e for e in eu_build.projection.entities if e.schema == "Person")
    assert "Synthetic Testperson" in person.properties["name"]


def test_every_namealias_is_also_emitted_as_an_alias(eu_build) -> None:
    person = next(e for e in eu_build.projection.entities if e.schema == "Person")
    assert "Synthetic Testperson" in person.properties["alias"]
    assert "Synth Alias" in person.properties["alias"]


def test_address_birthdate_and_nationality_are_mapped(eu_build) -> None:
    person = next(e for e in eu_build.projection.entities if e.schema == "Person")
    assert "Synthetic City" in person.properties["address"]
    assert "Synthetica" in person.properties["country"]
    assert "1975-04-12" in person.properties["birthDate"]
    assert "ZZ" in person.properties["nationality"]


def test_organization_name_comes_from_wholename_without_first_last(eu_build) -> None:
    entity = next(e for e in eu_build.projection.entities if e.schema == "Organization")
    assert "Synthetic Holdings Ltd" in entity.properties["name"]


def test_domain_interpretation_carries_regulation_membership(eu_build) -> None:
    assert len(eu_build.domain_objects) == 2
    context = next(obj for obj in eu_build.domain_objects if obj.data["euReferenceNumber"] == "EU.SYN.9001")
    assert context.data["regulations"][0]["programme"] == "SYNTHETIC-EU"


def test_field_coverage_has_no_quarantine_findings(eu_build) -> None:
    assert eu_build.field_coverage.quarantined is False


def test_unsupported_subject_type_quarantines() -> None:
    body = FIXTURE_PATH.read_bytes().replace(
        b'<subjectType classificationCode="P"/>', b'<subjectType classificationCode="V"/>'
    )
    store, snapshot = _snapshot_from(body)
    with pytest.raises(QuarantineRequired, match="unsupported subjectType"):
        process_snapshot(
            adapter,
            snapshot,
            store,
            observed_at=FIXED_TIME,
            normalizer=AdditiveNormalizer(adapter.metadata.normalization_version),
        )


def test_regeneration_is_deterministic(eu_build) -> None:
    store, snapshot = _snapshot_from(FIXTURE_PATH.read_bytes())
    rebuilt = process_snapshot(
        adapter,
        snapshot,
        store,
        observed_at=FIXED_TIME,
        normalizer=AdditiveNormalizer(adapter.metadata.normalization_version),
    )
    assert rebuilt.projection.canonical_hash == eu_build.projection.canonical_hash
