"""Versioned, additive normalization extension points."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class NormalizedValue:
    original_value: str
    projected_value: str
    derivation_type: str
    rule: str
    rule_version: str
    normalization_version: str

    def __post_init__(self) -> None:
        if self.derivation_type not in {"normalized", "derived", "inferred"}:
            raise ValueError("published values do not use a normalization result")


class NormalizationRule(Protocol):
    name: str
    version: str

    def apply(self, value: str) -> tuple[str, ...]: ...


class AdditiveNormalizer:
    """Retains published input and labels every generated value explicitly."""

    def __init__(self, version: str, rules: tuple[NormalizationRule, ...] = ()) -> None:
        self.version = version
        self.rules = rules

    def expand(self, value: str) -> tuple[NormalizedValue, ...]:
        generated: list[NormalizedValue] = []
        seen = {value}
        for rule in self.rules:
            for candidate in rule.apply(value):
                if candidate in seen:
                    continue
                seen.add(candidate)
                generated.append(
                    NormalizedValue(
                        original_value=value,
                        projected_value=candidate,
                        derivation_type="derived",
                        rule=rule.name,
                        rule_version=rule.version,
                        normalization_version=self.version,
                    )
                )
        return tuple(generated)


class GhanaianNameRule:
    """Akan/Ga/Hausa orthographic-variant expansion, driven by a versioned table.

    No heuristic spelling is invented here, mirroring ``TransliterationRule``:
    the rule is a pure lookup against a table supplied by the caller.
    Constructed with no arguments it behaves exactly like the no-op it
    replaces (zero expansions for every value), which is what
    ``datasets/gh_name_library/orthography_variants.yml`` -- still an
    intentionally empty placeholder pending a reviewed corpus, see
    ``docs/versioning.md`` -- currently produces via
    ``name_library.OrthographyVariantTable.to_rule()``.
    """

    name = "ghanaian_name_extension"

    def __init__(
        self, version: str = "unconfigured-v1", variants: dict[str, tuple[str, ...]] | None = None
    ) -> None:
        self.version = version
        self.variants = variants or {}

    def apply(self, value: str) -> tuple[str, ...]:
        return self.variants.get(value, ())


class TransliterationRule:
    """Pinned equivalence map; no heuristic spelling is invented."""

    name = "pinned_transliteration_equivalence"

    def __init__(self, version: str, equivalents: dict[str, tuple[str, ...]]) -> None:
        self.version = version
        self.equivalents = equivalents

    def apply(self, value: str) -> tuple[str, ...]:
        return self.equivalents.get(value, ())

