"""Fail when generated or sensitive data patterns enter the tracked file set."""

from __future__ import annotations

import fnmatch
import subprocess
import sys

FORBIDDEN_NAMES = (
    "*.ndjson",
    "*.ftm.json",
    "checksums.sha256",
    "field_coverage.json",
    "validation_report.json",
    "*.env",
)
FORBIDDEN_PREFIXES = ("data/", "capture_store/", "generated/", "release/", "releases/")
RESTRICTED_PREFIXES = ("datasets/gh_nsc_advisory/generated/", "datasets/gh_pep/generated/")


def tracked_paths() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        check=True,
        capture_output=True,
    )
    return [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def main() -> int:
    violations: list[str] = []
    for path in tracked_paths():
        name = path.rsplit("/", 1)[-1]
        if path.startswith(FORBIDDEN_PREFIXES + RESTRICTED_PREFIXES) or any(fnmatch.fnmatch(name, pattern) for pattern in FORBIDDEN_NAMES):
            violations.append(path)
    if violations:
        print("Generated, secret, or restricted data must not be tracked:", file=sys.stderr)
        for path in violations:
            print(f"- {path}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

