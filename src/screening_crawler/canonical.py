"""Canonical serialization and hashing shared by every pipeline layer."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any


def canonical_json(value: Any) -> str:
    """Return deterministic UTF-8-compatible JSON without insignificant whitespace."""

    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_json_bytes(value: Any, *, newline: bool = False) -> bytes:
    suffix = "\n" if newline else ""
    return (canonical_json(value) + suffix).encode("utf-8")


def canonical_ndjson(rows: Iterable[Mapping[str, Any]]) -> bytes:
    """Serialize rows in caller-defined deterministic order with one final LF."""

    materialized = [canonical_json(row) for row in rows]
    if not materialized:
        return b""
    return ("\n".join(materialized) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))

