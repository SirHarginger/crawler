"""Stable identifiers derived only from stable input material."""

from __future__ import annotations

import uuid

_NAMESPACE = uuid.UUID("fd2f16c4-cb6e-5e4b-b8c9-4a574e04af7d")


def stable_id(kind: str, *parts: str) -> str:
    material = "\x1f".join((kind, *parts))
    return str(uuid.uuid5(_NAMESPACE, material))


def artifact_id(source_key: str, request_url: str, retrieved_at: str, sha256: str) -> str:
    """Identify an observation envelope while allowing byte storage to deduplicate."""

    return stable_id("artifact-observation", source_key, request_url, retrieved_at, sha256)


def snapshot_id(source_key: str, manifest_hash: str) -> str:
    return stable_id("snapshot", source_key, manifest_hash)


def source_record_id(source_key: str, snapshot: str, kind: str, native_key: str) -> str:
    return stable_id("source-record", source_key, snapshot, kind, native_key)


def entity_id(source_key: str, kind: str, native_key: str) -> str:
    return f"{source_key}-{stable_id('entity', source_key, kind, native_key)}"


def projection_id(entity: str, prop: str, value: str, record: str, path: str) -> str:
    return stable_id("projection", entity, prop, value, record, path)
