"""Deterministic release-to-release entity comparison.

This is a *different* diff from `pipeline.compare_records`: that one compares Layer 1
`SourceRecord`s within a single crawl run (by publisher-native key, to feed the in-run
volume-anomaly gate). This one compares Layer 2 FollowTheMoney entities across two
already-published releases of the same dataset -- the "did the sanctions-relevant world
change" comparison the operator-facing release-comparison UI and the release-level
statistical validation gate both need. Keyed on `ProjectedEntity.id` (stable across
releases per `ids.entity_id` -- same source_key/record_kind/publisher_native_key always
hashes to the same id, per docs/architecture.md's "publisher keys define identity"), not on
name similarity, and never on row order (`entities.ndjson` rows are read in whatever order
the file iterates in; this module sorts before it ever compares or reports).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from .canonical import canonical_json

ChangeType = Literal["ADDED", "MODIFIED", "REMOVED", "UNCHANGED"]


@dataclass(frozen=True, slots=True)
class PropertyChange:
    property: str
    before: tuple[str, ...]
    after: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EntityChange:
    entity_id: str
    change_type: ChangeType
    schema: str
    caption: str
    dataset_key: str
    property_changes: tuple[PropertyChange, ...] = ()


@dataclass(frozen=True, slots=True)
class ReleaseDiff:
    """`unchanged_count` is a count, not a materialized entity list -- unchanged entities are
    (by definition) already fully described by the previous release and add nothing an
    operator needs enumerated row-by-row; keeping only their count avoids the diff itself
    growing to the size of the whole dataset on every run, matching the release format's own
    "provenance, not a second copy of the entities" principle.
    """

    added: tuple[EntityChange, ...]
    modified: tuple[EntityChange, ...]
    removed: tuple[EntityChange, ...]
    unchanged_count: int

    @property
    def added_count(self) -> int:
        return len(self.added)

    @property
    def modified_count(self) -> int:
        return len(self.modified)

    @property
    def removed_count(self) -> int:
        return len(self.removed)

    @property
    def previous_entity_count(self) -> int:
        return self.removed_count + self.modified_count + self.unchanged_count

    @property
    def new_entity_count(self) -> int:
        return self.added_count + self.modified_count + self.unchanged_count

    def as_summary_dict(self) -> dict[str, Any]:
        """The compact, UI/statistical-gate-facing summary -- previous/new counts, absolute
        and percentage delta, and the four change-type counts. Percentage delta is relative
        to the previous count; a first-ever release (previous_entity_count == 0) reports
        `null` rather than a divide-by-zero or a misleading 0%/inf%.
        """
        previous = self.previous_entity_count
        new = self.new_entity_count
        return {
            "previous_entity_count": previous,
            "new_entity_count": new,
            "absolute_delta": new - previous,
            "percentage_delta": None if previous == 0 else round((new - previous) / previous, 6),
            "added": self.added_count,
            "modified": self.modified_count,
            "removed": self.removed_count,
            "unchanged": self.unchanged_count,
        }


def _content_key(entity: Mapping[str, Any]) -> str:
    """A stable content fingerprint independent of key/value ordering, used only to decide
    "did anything actually change" -- never stored or shown. `canonical_json`'s `sort_keys`
    already normalizes dict key order; property value lists are additionally sorted here
    since `entities.ndjson` rows may list multi-value properties in publisher/processing
    order, and that order carries no meaning (docs/release-contract.md's canonical form
    sorts property values for the *published* row already, but this function must not
    silently assume every caller's input has already been through that canonicalization).
    """
    properties = entity.get("properties", {})
    normalized_properties = {name: sorted(values) for name, values in properties.items()}
    return canonical_json({"schema": entity.get("schema"), "properties": normalized_properties})


def _property_changes(before: Mapping[str, Any], after: Mapping[str, Any]) -> tuple[PropertyChange, ...]:
    before_props: dict[str, list[str]] = before.get("properties", {})
    after_props: dict[str, list[str]] = after.get("properties", {})
    changes: list[PropertyChange] = []
    for name in sorted(set(before_props) | set(after_props)):
        before_values = tuple(sorted(before_props.get(name, [])))
        after_values = tuple(sorted(after_props.get(name, [])))
        if before_values != after_values:
            changes.append(PropertyChange(property=name, before=before_values, after=after_values))
    return tuple(changes)


def diff_releases(
    previous_entities: Iterable[Mapping[str, Any]],
    new_entities: Iterable[Mapping[str, Any]],
) -> ReleaseDiff:
    """Compare two releases' `entities.ndjson` rows (already-parsed dicts, as read by
    `json.loads` per line -- this function does no file I/O of its own, callers stream from
    `LocalReleaseStore`/local release directories however is appropriate for their side).
    """
    previous_by_id = {entity["id"]: entity for entity in previous_entities}
    new_by_id = {entity["id"]: entity for entity in new_entities}
    previous_ids = set(previous_by_id)
    new_ids = set(new_by_id)

    added: list[EntityChange] = []
    for entity_id in sorted(new_ids - previous_ids):
        entity = new_by_id[entity_id]
        added.append(
            EntityChange(
                entity_id=entity_id,
                change_type="ADDED",
                schema=entity.get("schema", ""),
                caption=entity.get("caption", ""),
                dataset_key=entity.get("dataset_key", ""),
            )
        )

    removed: list[EntityChange] = []
    for entity_id in sorted(previous_ids - new_ids):
        entity = previous_by_id[entity_id]
        removed.append(
            EntityChange(
                entity_id=entity_id,
                change_type="REMOVED",
                schema=entity.get("schema", ""),
                caption=entity.get("caption", ""),
                dataset_key=entity.get("dataset_key", ""),
            )
        )

    modified: list[EntityChange] = []
    unchanged_count = 0
    for entity_id in sorted(previous_ids & new_ids):
        before = previous_by_id[entity_id]
        after = new_by_id[entity_id]
        if _content_key(before) == _content_key(after):
            unchanged_count += 1
            continue
        modified.append(
            EntityChange(
                entity_id=entity_id,
                change_type="MODIFIED",
                schema=after.get("schema", ""),
                caption=after.get("caption", ""),
                dataset_key=after.get("dataset_key", ""),
                property_changes=_property_changes(before, after),
            )
        )

    return ReleaseDiff(
        added=tuple(added), modified=tuple(modified), removed=tuple(removed), unchanged_count=unchanged_count
    )


class CatastrophicChangeDetected(Exception):
    """Raised by `evaluate_statistical_gate` when a release-to-release change exceeds the
    dataset's configured anomaly threshold. Deliberately not a `QuarantineRequired` subclass
    (crawler.errors) -- that hierarchy is for Layer 1/field-inventory findings; this is a
    release-level, post-diff gate that runs later in the pipeline and needs to be caught
    separately by the run engine to assign a distinct terminal state.
    """


def evaluate_statistical_gate(
    diff: ReleaseDiff,
    *,
    anomaly_threshold: float,
    is_first_release: bool,
) -> None:
    """The "previous=700, new=12 must not publish" gate (doc requirement, not a hypothetical):
    raises `CatastrophicChangeDetected` if the release-over-release entity count moved by more
    than `anomaly_threshold` (the same per-dataset config already used for the in-run,
    source-record-level anomaly check in `pipeline.process_snapshot` -- reused here, not
    reinvented, at the entity/release granularity that check doesn't cover). A first-ever
    release for a dataset has no previous release to compare against; there is nothing
    "catastrophic" to detect yet, so this is a deliberate, explicit bootstrap no-op rather
    than an accidental divide-by-zero pass.
    """
    if is_first_release:
        return
    previous = diff.previous_entity_count
    if previous == 0:
        # A previous release existed but had zero entities -- any non-empty new release is an
        # infinite percentage increase, not a "shrink"; nothing to gate on by this check.
        return
    change = abs(diff.new_entity_count - previous) / previous
    if change > anomaly_threshold:
        raise CatastrophicChangeDetected(
            f"entity count changed by {change:.4f} (previous={previous}, "
            f"new={diff.new_entity_count}) which exceeds anomaly_threshold={anomaly_threshold:.4f}"
        )
