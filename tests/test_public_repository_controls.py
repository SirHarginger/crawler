from __future__ import annotations

import fnmatch
from pathlib import Path

FORBIDDEN_PATTERNS = ("*.ndjson", "*.ftm.json", "checksums.sha256")


def test_no_generated_data_is_present_in_repository_tree() -> None:
    paths = [path for path in Path(".").rglob("*") if path.is_file() and ".git" not in path.parts]
    offending = [
        path.as_posix()
        for path in paths
        if any(fnmatch.fnmatch(path.name, pattern) for pattern in FORBIDDEN_PATTERNS)
    ]
    assert offending == []


def test_fixture_is_explicitly_synthetic() -> None:
    fixture = Path("tests/fixtures/un_sc_consolidated/synthetic.xml").read_text(encoding="utf-8")
    assert "Synthetic" in fixture
    assert "SYN-" in fixture


def test_crawler_has_no_database_runtime_dependency() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    lowered = pyproject.lower()
    assert "psycopg" not in lowered
    assert "sqlalchemy" not in lowered

