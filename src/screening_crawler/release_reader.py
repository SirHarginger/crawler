"""Read back an already-published release bundle -- the read-side counterpart to
`release.ReleaseBuilder` (write-only) and `validation.validate_release_bundle` (checks
structure, does not expose rows to callers). Used by the run engine to diff a new release
against the dataset's previous one, and by the `compare-releases` CLI command to diff any two
release directories a caller names directly. Deliberately narrow: this reads NDJSON rows for a
given manifest role, nothing more -- it is not a second release-parsing implementation, just a
thin iterator over files `release.py` already wrote in a known, schema-fixed shape.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any


class ReleaseReadError(Exception):
    """A release directory could not be read back (missing/unreadable manifest.json)."""


def read_manifest(release_dir: Path) -> dict[str, Any]:
    manifest_path = release_dir / "manifest.json"
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseReadError(f"cannot read manifest at {manifest_path}") from exc
    try:
        data: dict[str, Any] = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReleaseReadError(f"manifest at {manifest_path} is not valid JSON") from exc
    return data


def read_role_rows(release_dir: Path, manifest: dict[str, Any], role: str) -> Iterator[dict[str, Any]]:
    """Yield every NDJSON row across every file declared under `role` in the manifest, in file
    order (never re-sorted here -- callers needing a deterministic order, e.g. the diff
    engine, do that themselves so this stays a plain, honest passthrough of what's on disk).
    """
    for entry in manifest.get("files", []):
        if entry.get("role") != role:
            continue
        path = release_dir / entry["path"]
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield json.loads(line)


def read_entities(release_dir: Path) -> list[dict[str, Any]]:
    manifest = read_manifest(release_dir)
    return list(read_role_rows(release_dir, manifest, "entities"))


def dataset_entry(manifest: dict[str, Any], dataset_key: str) -> dict[str, Any] | None:
    for entry in manifest.get("datasets", []):
        if entry.get("key") == dataset_key:
            result: dict[str, Any] = entry
            return result
    return None
