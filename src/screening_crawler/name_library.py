"""Versioned Ghanaian name-normalization artifacts.

The two files this module loads (``datasets/gh_name_library/*.yml``) are
placeholders: this repository has not been given a reviewed, validated
Ghanaian name corpus. Shipping fabricated frequency counts or orthographic-
equivalence claims would violate this repository's own rule against
inventing undocumented source data (see README, "No official URL or
undocumented source field is fabricated here"), so the shipped artifacts are
empty. This module's job is to make the *machinery* real and testable, so a
reviewed corpus can be dropped in later -- as data, not code -- without
further changes here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .normalization import GhanaianNameRule


@dataclass(frozen=True, slots=True)
class GivenNameFrequencyTable:
    """A versioned given-name -> frequency-rank table.

    Consumed by screening-engine's matching policy to penalise the
    name-similarity contribution of high-frequency given names (system
    documentation section 9.2). This repository only produces and versions
    the artifact; applying a matching-score penalty is a screening-engine
    responsibility and is listed as deliberately out of scope here (see
    README, "matching heuristics that have not been validated against an
    approved corpus").
    """

    version: str
    frequencies: dict[str, int]

    @classmethod
    def from_path(cls, path: Path) -> GivenNameFrequencyTable:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("frequencies"), dict):
            raise ValueError("frequency table must contain a frequencies mapping")
        frequencies = {str(key): int(value) for key, value in raw["frequencies"].items()}
        return cls(version=str(raw["version"]), frequencies=frequencies)

    def rank(self, given_name: str) -> int | None:
        return self.frequencies.get(given_name)

    @property
    def is_placeholder(self) -> bool:
        return not self.frequencies


@dataclass(frozen=True, slots=True)
class OrthographyVariantTable:
    """A versioned name -> alternate-spelling equivalence table."""

    version: str
    variants: dict[str, tuple[str, ...]]

    @classmethod
    def from_path(cls, path: Path) -> OrthographyVariantTable:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("variants"), dict):
            raise ValueError("orthography variant table must contain a variants mapping")
        variants = {
            str(key): tuple(str(item) for item in value) for key, value in raw["variants"].items()
        }
        return cls(version=str(raw["version"]), variants=variants)

    @property
    def is_placeholder(self) -> bool:
        return not self.variants

    def to_rule(self) -> GhanaianNameRule:
        return GhanaianNameRule(version=self.version, variants=self.variants)
