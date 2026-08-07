"""One-directional orchestration from complete snapshot to release-ready data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .assertions import PepAssertion
from .datasets import DatasetAdapter, interpret_if_enabled
from .domain import DomainObject
from .errors import QuarantineRequired
from .field_inventory import FieldCoverage
from .model import Snapshot, SourceRecord
from .normalization import AdditiveNormalizer
from .pep import PepOccupancyProjector, PepSourceAdapter
from .projection import ProjectionResult, Projector
from .source import extract_source_records
from .storage import BlobStore


@dataclass(frozen=True, slots=True)
class DatasetBuild:
    snapshot: Snapshot
    source_records: tuple[SourceRecord, ...]
    field_coverage: FieldCoverage
    domain_objects: tuple[DomainObject, ...]
    projection: ProjectionResult
    pep_assertions: tuple[PepAssertion, ...] = ()


def process_snapshot(
    adapter: DatasetAdapter,
    snapshot: Snapshot,
    store: BlobStore,
    *,
    observed_at: datetime,
    normalizer: AdditiveNormalizer,
    previous_record_count: int | None = None,
    previous_field_coverage: FieldCoverage | None = None,
) -> DatasetBuild:
    """Run extraction and safety gates without network or clock access."""

    snapshot.require_complete()
    if snapshot.source_key != adapter.metadata.key:
        raise ValueError("snapshot does not belong to adapter dataset")
    records = extract_source_records(snapshot, adapter.parser, store, observed_at=observed_at)
    source_findings = adapter.validate_source(snapshot, records, store)
    if source_findings:
        raise QuarantineRequired("; ".join(source_findings))
    if previous_record_count is not None and previous_record_count > 0:
        change = abs(len(records) - previous_record_count) / previous_record_count
        if change > adapter.metadata.anomaly_threshold:
            raise QuarantineRequired(
                f"record volume change {change:.4f} exceeds dataset threshold "
                f"{adapter.metadata.anomaly_threshold:.4f}"
            )
    coverage = adapter.inventory.assess(records, previous=previous_field_coverage)
    coverage.require_safe()
    domains = interpret_if_enabled(adapter, records)
    projection = Projector(adapter.projection_profile, normalizer).project(records)
    return DatasetBuild(snapshot, tuple(records), coverage, tuple(domains), projection)


def process_pep_snapshot(
    adapter: PepSourceAdapter,
    snapshot: Snapshot,
    store: BlobStore,
    *,
    observed_at: datetime,
    normalizer: AdditiveNormalizer,
    previous_record_count: int | None = None,
    previous_field_coverage: FieldCoverage | None = None,
) -> DatasetBuild:
    """The PEP-shaped counterpart to ``process_snapshot``.

    Capture, snapshot assembly, extraction, and field accounting are
    identical to ``process_snapshot`` -- only projection differs, because a
    PEP occupancy assertion produces three linked entities (Person,
    Position, Occupancy) via ``PepOccupancyProjector``, not one entity via
    the generic ``Projector``/``ProjectionProfile`` pair. A conforming
    parser only ever emits a ``ParsedRecord`` for an assertion it has
    already validated as independently-reviewed and ``PUBLISHED`` (see
    ``pep.assertion_from_parsed``); this function does not re-check that,
    only the parser can, since it alone sees the pre-publication lifecycle.
    """

    snapshot.require_complete()
    if snapshot.source_key != adapter.metadata.key:
        raise ValueError("snapshot does not belong to adapter dataset")
    records = extract_source_records(snapshot, adapter.parser, store, observed_at=observed_at)
    source_findings = adapter.validate_source(snapshot, records, store)
    if source_findings:
        raise QuarantineRequired("; ".join(source_findings))
    if previous_record_count is not None and previous_record_count > 0:
        change = abs(len(records) - previous_record_count) / previous_record_count
        if change > adapter.metadata.anomaly_threshold:
            raise QuarantineRequired(
                f"record volume change {change:.4f} exceeds dataset threshold "
                f"{adapter.metadata.anomaly_threshold:.4f}"
            )
    coverage = adapter.inventory.assess(records, previous=previous_field_coverage)
    coverage.require_safe()
    projection = PepOccupancyProjector(
        mapping_version=adapter.mapping_version, normalizer=normalizer
    ).project([adapter.projection_input_for(record) for record in records])
    assertions = tuple(adapter.assertion_for(record) for record in records)
    return DatasetBuild(snapshot, tuple(records), coverage, (), projection, assertions)


@dataclass(frozen=True, slots=True)
class RecordDelta:
    added: tuple[str, ...]
    amended: tuple[str, ...]
    removed: tuple[str, ...]
    unchanged: tuple[str, ...]


def compare_records(previous: list[SourceRecord], current: list[SourceRecord]) -> RecordDelta:
    """Compare within one source by publisher-native identity, never by name similarity."""

    old = {(item.record_kind, item.publisher_native_key): item for item in previous}
    new = {(item.record_kind, item.publisher_native_key): item for item in current}
    old_keys = set(old)
    new_keys = set(new)
    added_keys = new_keys - old_keys
    removed_keys = old_keys - new_keys
    common = old_keys & new_keys
    amended_keys = {key for key in common if old[key].content_sha256 != new[key].content_sha256}
    unchanged_keys = common - amended_keys

    def encode(keys: set[tuple[str, str]]) -> tuple[str, ...]:
        return tuple(sorted(f"{kind}:{native}" for kind, native in keys))

    return RecordDelta(
        added=encode(added_keys),
        amended=encode(amended_keys),
        removed=encode(removed_keys),
        unchanged=encode(unchanged_keys),
    )
