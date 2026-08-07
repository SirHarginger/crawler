from __future__ import annotations

from dataclasses import replace

from datasets.un_sc_consolidated.crawler import adapter
from screening_crawler.field_inventory import FindingSeverity


def _codes(coverage) -> set[str]:
    return {item.code for item in coverage.findings}


def test_known_fixture_has_complete_reviewed_field_accounting(dataset_build) -> None:
    coverage = dataset_build.field_coverage
    assert not coverage.quarantined
    assert coverage.record_count == 2
    assert not _codes(coverage)


def test_unknown_path_quarantines(dataset_build) -> None:
    record = dataset_build.source_records[0]
    changed = replace(record, parsed={**record.parsed, "SURPRISE_FIELD": "synthetic"})
    coverage = adapter.inventory.assess([changed, *dataset_build.source_records[1:]])
    assert coverage.quarantined
    assert "unknown_path" in _codes(coverage)


def test_unknown_enumeration_quarantines(dataset_build) -> None:
    records = list(dataset_build.source_records)
    individual = records[1]
    aliases = [dict(item) for item in individual.parsed["INDIVIDUAL_ALIAS"]]
    aliases[0]["QUALITY"] = "Unclassified Synthetic Quality"
    records[1] = replace(individual, parsed={**individual.parsed, "INDIVIDUAL_ALIAS": aliases})
    coverage = adapter.inventory.assess(records)
    assert "unknown_enumeration" in _codes(coverage)
    assert coverage.quarantined


def test_required_field_disappearance_is_safety_critical(dataset_build) -> None:
    records = list(dataset_build.source_records)
    changed_native = dict(records[0].parsed)
    changed_native.pop("FIRST_NAME")
    records[0] = replace(records[0], parsed=changed_native)
    coverage = adapter.inventory.assess(records)
    finding = next(item for item in coverage.findings if item.code == "required_missing")
    assert finding.severity is FindingSeverity.QUARANTINE


def test_type_and_cardinality_drift_are_detected(dataset_build) -> None:
    records = list(dataset_build.source_records)
    changed = replace(records[0], parsed={**records[0].parsed, "VERSIONNUM": 2})
    coverage = adapter.inventory.assess([changed, records[1]])
    assert "type_change" in _codes(coverage)


def test_excess_cardinality_is_detected(dataset_build) -> None:
    records = list(dataset_build.source_records)
    individual = records[1]
    aliases = [{"QUALITY": "Good", "ALIAS_NAME": f"Synthetic Alias {number}"} for number in range(21)]
    records[1] = replace(individual, parsed={**individual.parsed, "INDIVIDUAL_ALIAS": aliases})
    coverage = adapter.inventory.assess(records)
    assert "cardinality_high" in _codes(coverage)


def test_previously_present_optional_field_disappearance_quarantines(dataset_build) -> None:
    previous = dataset_build.field_coverage
    records = []
    for record in dataset_build.source_records:
        native = dict(record.parsed)
        native.pop("COMMENTS1", None)
        records.append(replace(record, parsed=native))
    coverage = adapter.inventory.assess(records, previous=previous)
    assert "field_disappeared" in _codes(coverage)
    assert coverage.quarantined
