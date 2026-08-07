from __future__ import annotations

from datasets.un_sc_consolidated.crawler import adapter
from screening_crawler.normalization import AdditiveNormalizer, TransliterationRule
from screening_crawler.projection import Projector


def test_projection_is_deterministic_across_input_order(dataset_build) -> None:
    projector = Projector(adapter.projection_profile, AdditiveNormalizer("base-v1"))
    forward = projector.project(list(dataset_build.source_records))
    reverse = projector.project(list(reversed(dataset_build.source_records)))
    assert forward.canonical_entities() == reverse.canonical_entities()
    assert forward.canonical_hash == reverse.canonical_hash


def test_every_alias_is_retained_independently(dataset_build) -> None:
    person = next(item for item in dataset_build.projection.entities if item.schema == "Person")
    assert set(person.properties["alias"]) == {"A. Example", "Example Ama"}


def test_every_property_has_provenance(dataset_build) -> None:
    statements = {
        (item.ftm_entity_id, item.ftm_property, item.projected_value)
        for item in dataset_build.projection.statements
    }
    for entity in dataset_build.projection.entities:
        assert entity.source_record_ids
        for prop, values in entity.properties.items():
            assert all((entity.id, prop, value) in statements for value in values)


def test_additive_normalization_keeps_published_and_labels_derived(dataset_build) -> None:
    normalizer = AdditiveNormalizer(
        "equivalence-v1",
        (TransliterationRule("map-v1", {"A. Example": ("Alpha Example",)}),),
    )
    projection = Projector(adapter.projection_profile, normalizer).project(list(dataset_build.source_records))
    person = next(item for item in projection.entities if item.schema == "Person")
    assert {"A. Example", "Alpha Example"}.issubset(person.properties["alias"])
    derived = next(item for item in projection.statements if item.projected_value == "Alpha Example")
    assert derived.derivation_type == "derived"
    assert derived.derivation_rule == "pinned_transliteration_equivalence"
    published = next(item for item in projection.statements if item.projected_value == "A. Example")
    assert published.derivation_type == "published"


def test_regeneration_from_layer_one_has_identical_hash(dataset_build) -> None:
    regenerated = Projector(
        adapter.projection_profile,
        AdditiveNormalizer(dataset_build.projection.normalization_version),
    ).project(list(dataset_build.source_records))
    assert regenerated.canonical_hash == dataset_build.projection.canonical_hash

