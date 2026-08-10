"""Static enforcement of this repo's side of the crawler/screening-engine boundary (see
docs/architecture.md's "Responsibility boundary" and the "System invariants" section): the
crawler must never gain a database or Elasticsearch client, and must never contain code that
could mutate production screening state. AST-based (parses real imports), not a raw string
grep over file contents, so it can't be defeated by comments/docstrings and doesn't false-
positive on unrelated text containing the same substrings.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Modules that would give the crawler a live database or search-index client -- the
# architectural line this repo must never cross (doc: "the crawler MUST NOT have production
# DB credentials / write to screening PostgreSQL / write to Elasticsearch / control aliases").
FORBIDDEN_MODULES = {
    "psycopg", "psycopg2", "elasticsearch", "elastic_transport", "sqlalchemy", "asyncpg",
}


def _python_files() -> list[Path]:
    files: list[Path] = []
    for base in (REPO_ROOT / "src", REPO_ROOT / "datasets"):
        if base.is_dir():
            files.extend(base.rglob("*.py"))
    return files


def _imported_top_level_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])
    return modules


def test_no_source_file_imports_a_database_or_search_client() -> None:
    offenders = {}
    for path in _python_files():
        hit = _imported_top_level_modules(path) & FORBIDDEN_MODULES
        if hit:
            offenders[str(path.relative_to(REPO_ROOT))] = hit
    assert not offenders, (
        f"Files importing a forbidden DB/search client (crawler must hold no such "
        f"credentials): {offenders}"
    )


def test_pyproject_declares_no_database_or_search_dependency() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    declared = set(data["project"].get("dependencies", []))
    declared |= set(data["project"].get("optional-dependencies", {}).get("dev", []))
    lowered = {dep.lower() for dep in declared}
    for forbidden in FORBIDDEN_MODULES:
        assert not any(forbidden in dep for dep in lowered), (
            f"pyproject.toml declares a dependency touching '{forbidden}' -- "
            "the crawler must never gain a database/search-index dependency"
        )


def test_no_source_file_references_production_alias_names() -> None:
    """A narrower, deliberately-scoped literal check (not a broad grep): the crawler source
    tree must never even mention the specific alias names screening-engine's promotion
    mechanism owns -- there is no legitimate reason for this repo to know them by name.
    """
    forbidden_literals = ("screening-current", "screening-current-entities")
    offenders = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        if any(literal in text for literal in forbidden_literals):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, f"Files referencing production alias names: {offenders}"
