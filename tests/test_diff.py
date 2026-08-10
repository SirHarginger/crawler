from __future__ import annotations

import pytest

from screening_crawler.diff import (
    CatastrophicChangeDetected,
    diff_releases,
    evaluate_statistical_gate,
)


def _entity(entity_id: str, caption: str, **properties: list[str]) -> dict:
    return {
        "id": entity_id,
        "schema": "Person",
        "caption": caption,
        "dataset_key": "un_sc_consolidated",
        "properties": {key: value for key, value in properties.items()},
    }


def test_added_entity_is_classified_correctly() -> None:
    diff = diff_releases([], [_entity("e1", "New Person", name=["New Person"])])
    assert [c.entity_id for c in diff.added] == ["e1"]
    assert diff.modified == ()
    assert diff.removed == ()
    assert diff.unchanged_count == 0


def test_removed_entity_is_classified_correctly() -> None:
    diff = diff_releases([_entity("e1", "Old Person", name=["Old Person"])], [])
    assert [c.entity_id for c in diff.removed] == ["e1"]
    assert diff.added == ()


def test_unchanged_entity_is_not_reported_individually() -> None:
    entity = _entity("e1", "Same Person", name=["Same Person"])
    diff = diff_releases([entity], [dict(entity)])
    assert diff.added == () and diff.modified == () and diff.removed == ()
    assert diff.unchanged_count == 1


def test_modified_entity_reports_field_level_before_after() -> None:
    before = _entity("e1", "John Doe", name=["John Doe"], passport=[])
    after = _entity("e1", "John Kwame Doe", name=["John Kwame Doe"], passport=["G123456"])
    diff = diff_releases([before], [after])
    assert len(diff.modified) == 1
    change = diff.modified[0]
    assert change.entity_id == "e1"
    by_property = {p.property: p for p in change.property_changes}
    assert by_property["name"].before == ("John Doe",)
    assert by_property["name"].after == ("John Kwame Doe",)
    assert by_property["passport"].before == ()
    assert by_property["passport"].after == ("G123456",)


def test_property_value_reordering_alone_is_not_a_modification() -> None:
    """Doc requirement: "Do not treat source ordering changes as entity modifications." """
    before = _entity("e1", "Person", alias=["Alpha", "Beta"])
    after = _entity("e1", "Person", alias=["Beta", "Alpha"])
    diff = diff_releases([before], [after])
    assert diff.modified == ()
    assert diff.unchanged_count == 1


def test_diff_is_deterministic_regardless_of_row_order() -> None:
    a = _entity("a", "A", name=["A"])
    b = _entity("b", "B", name=["B"])
    c = _entity("c-changed", "C", name=["C v1"])
    c2 = _entity("c-changed", "C", name=["C v2"])
    forward = diff_releases([a, c], [b, c2])
    backward = diff_releases([c, a], [c2, b])
    assert [e.entity_id for e in forward.added] == [e.entity_id for e in backward.added]
    assert [e.entity_id for e in forward.removed] == [e.entity_id for e in backward.removed]
    assert [e.entity_id for e in forward.modified] == [e.entity_id for e in backward.modified]


def test_summary_dict_computes_delta_and_percentage() -> None:
    previous = [_entity(str(i), f"P{i}", name=[f"P{i}"]) for i in range(10)]
    new = [_entity(str(i), f"P{i}", name=[f"P{i}"]) for i in range(8)]
    diff = diff_releases(previous, new)
    summary = diff.as_summary_dict()
    assert summary["previous_entity_count"] == 10
    assert summary["new_entity_count"] == 8
    assert summary["absolute_delta"] == -2
    assert summary["percentage_delta"] == pytest.approx(-0.2)
    assert summary["removed"] == 2


def test_summary_dict_reports_null_percentage_delta_on_first_release() -> None:
    diff = diff_releases([], [_entity("e1", "New", name=["New"])])
    summary = diff.as_summary_dict()
    assert summary["previous_entity_count"] == 0
    assert summary["percentage_delta"] is None


def test_statistical_gate_passes_within_threshold() -> None:
    previous = [_entity(str(i), f"P{i}", name=[f"P{i}"]) for i in range(100)]
    new = [_entity(str(i), f"P{i}", name=[f"P{i}"]) for i in range(90)]
    diff = diff_releases(previous, new)
    evaluate_statistical_gate(diff, anomaly_threshold=0.15, is_first_release=False)  # must not raise


def test_statistical_gate_rejects_catastrophic_shrink() -> None:
    """The doc's own example: previous=700, new=12 must not silently pass."""
    previous = [_entity(str(i), f"P{i}", name=[f"P{i}"]) for i in range(700)]
    new = [_entity(str(i), f"P{i}", name=[f"P{i}"]) for i in range(12)]
    diff = diff_releases(previous, new)
    with pytest.raises(CatastrophicChangeDetected):
        evaluate_statistical_gate(diff, anomaly_threshold=0.15, is_first_release=False)


def test_statistical_gate_is_a_bootstrap_no_op_on_first_release() -> None:
    diff = diff_releases([], [_entity(str(i), f"P{i}", name=[f"P{i}"]) for i in range(5)])
    evaluate_statistical_gate(diff, anomaly_threshold=0.15, is_first_release=True)  # must not raise


def test_statistical_gate_handles_previously_empty_release() -> None:
    diff = diff_releases([], [_entity("e1", "New", name=["New"])])
    # Not the "first release" bootstrap case (a previous release existed), but its entity
    # count was zero -- nothing to compute a shrink percentage against, must not divide by zero.
    evaluate_statistical_gate(diff, anomaly_threshold=0.15, is_first_release=False)
