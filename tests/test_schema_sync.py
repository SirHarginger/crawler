"""The release-manifest schema is the one interface between this repo and the
sibling `screening-engine` repo: this repo publishes a release bundle
validated against it, and screening-engine's importer independently
re-validates every bundle against the same schema before it ever touches its
reference registry. The two copies are not shared by import or submodule --
each repo carries its own file -- so nothing stops them from silently
drifting apart, which is exactly what had already happened once in this
project before the two were reconciled.

This test only runs when a `screening-engine` checkout is present as a
sibling directory (the expected local dev layout: both repos cloned side by
side). It is a local drift guard, not a CI requirement in either repo alone,
since neither repo's CI checks out the other by default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_THIS_SCHEMA = Path(__file__).resolve().parents[1] / "schemas" / "release-manifest.schema.json"
_SIBLING_SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "screening-engine"
    / "schemas"
    / "release-manifest.schema.json"
)


def test_release_manifest_schema_matches_screening_engine_sibling_repo() -> None:
    if not _SIBLING_SCHEMA.is_file():
        pytest.skip(f"no sibling screening-engine checkout at {_SIBLING_SCHEMA.parent.parent}")
    ours = _THIS_SCHEMA.read_bytes()
    theirs = _SIBLING_SCHEMA.read_bytes()
    assert ours == theirs, (
        "crawler's and screening-engine's release-manifest.schema.json have diverged -- "
        "one was edited without the other. This is the sole contract between the two "
        "repos; copy the canonical version (screening-engine's, since its importer is "
        "the one that has been exercised against real validation logic) over this one."
    )
