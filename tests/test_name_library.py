"""The shipped Ghana name-library artifacts are placeholders; the machinery
around them is real. Tests here prove the machinery works using obviously
synthetic example data -- never real Ghanaian names, which this repository
has no reviewed corpus to validate against.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from screening_crawler.name_library import GivenNameFrequencyTable, OrthographyVariantTable
from screening_crawler.normalization import AdditiveNormalizer

FREQUENCY_PATH = Path("datasets/gh_name_library/given_name_frequency.yml")
VARIANTS_PATH = Path("datasets/gh_name_library/orthography_variants.yml")


def test_shipped_frequency_table_is_still_an_unpopulated_placeholder() -> None:
    table = GivenNameFrequencyTable.from_path(FREQUENCY_PATH)
    assert table.version == "gh-name-frequency-v0-placeholder"
    assert table.is_placeholder is True
    assert table.rank("anything") is None


def test_shipped_orthography_table_is_still_an_unpopulated_placeholder() -> None:
    table = OrthographyVariantTable.from_path(VARIANTS_PATH)
    assert table.version == "gh-orthography-v0-placeholder"
    assert table.is_placeholder is True
    assert table.to_rule().apply("anything") == ()


def test_rule_from_shipped_placeholder_expands_nothing_via_normalizer() -> None:
    """Regression guard: today's behaviour must stay a true no-op end to end."""
    table = OrthographyVariantTable.from_path(VARIANTS_PATH)
    normalizer = AdditiveNormalizer("test-v1", (table.to_rule(),))
    assert normalizer.expand("Any Published Name") == ()


def test_frequency_table_rank_lookup_with_synthetic_data() -> None:
    table = GivenNameFrequencyTable(
        version="synthetic-test-v1",
        frequencies={"Testname-Common": 1, "Testname-Rare": 9000},
    )
    assert table.is_placeholder is False
    assert table.rank("Testname-Common") == 1
    assert table.rank("Testname-Nonexistent") is None


def test_orthography_variant_expansion_with_synthetic_data() -> None:
    table = OrthographyVariantTable(
        version="synthetic-test-v1",
        variants={"Testword-Canonical": ("Testword-VariantA", "Testword-VariantB")},
    )
    rule = table.to_rule()
    assert rule.version == "synthetic-test-v1"
    assert rule.apply("Testword-Canonical") == ("Testword-VariantA", "Testword-VariantB")
    assert rule.apply("Testword-Unrelated") == ()


def test_orthography_rule_wires_into_additive_normalizer() -> None:
    table = OrthographyVariantTable(
        version="synthetic-test-v1",
        variants={"Testword-Canonical": ("Testword-VariantA",)},
    )
    normalizer = AdditiveNormalizer("test-v1", (table.to_rule(),))
    generated = normalizer.expand("Testword-Canonical")
    assert len(generated) == 1
    assert generated[0].projected_value == "Testword-VariantA"
    assert generated[0].derivation_type == "derived"
    assert generated[0].rule == "ghanaian_name_extension"
    assert generated[0].rule_version == "synthetic-test-v1"


@pytest.mark.parametrize("payload", ["version: v1\n", "version: v1\nfrequencies: not-a-mapping\n"])
def test_malformed_frequency_table_raises(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "bad.yml"
    path.write_text(payload)
    with pytest.raises(ValueError, match="frequencies mapping"):
        GivenNameFrequencyTable.from_path(path)


@pytest.mark.parametrize("payload", ["version: v1\n", "version: v1\nvariants: not-a-mapping\n"])
def test_malformed_variant_table_raises(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "bad.yml"
    path.write_text(payload)
    with pytest.raises(ValueError, match="variants mapping"):
        OrthographyVariantTable.from_path(path)
