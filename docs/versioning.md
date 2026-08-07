# Parser, mapping, normalization, and interpretation versioning

Versions identify behavior, not deployment dates.

- Increment the parser version when byte-to-record behavior, native shape, locators, extent logic, OCR, or archive decoding changes.
- Increment the field-inventory version whenever paths, types, enumeration sets, cardinality, coverage, dispositions, or reasons change.
- Increment the Layer 1.5 interpretation version whenever source-scoped object shape or semantic mapping changes.
- Increment the mapping version whenever entity schema, property selection, property source path, ID rule, or derivation label changes.
- Increment the normalization version whenever rule order, equivalence data, transliteration, country/date/identifier normalization, or name-frequency data changes.
- Increment the release schema version only for the bundle contract. Breaking changes require a new major version and coordinated screening-engine importer support.

Every output envelope carries the versions needed to reproduce it. A mapping or normalization change regenerates from Layer 1 and does not contact the publisher. A parser change regenerates Layer 1 from retained Layer 0 bytes. Versions must not depend on the current clock, mutable environment variables, or floating package/image tags.

Ghanaian name, Akan/Ga/Hausa orthography, Arabic-origin equivalence, transliteration, frequency, date precision, country, and identifier hooks exist, but unvalidated matching behavior is intentionally absent. Populate them only from a reviewed, versioned corpus and ship evaluation evidence with the change.

As of this writing, the orthography and frequency hooks are backed by real, loadable, versioned artifacts (`src/screening_crawler/name_library.py` loading `datasets/gh_name_library/*.yml`) rather than a hardcoded no-op, so the mechanism is exercised and tested end to end. Both shipped files are still deliberately empty (`gh-orthography-v0-placeholder`, `gh-name-frequency-v0-placeholder`) — a reviewed corpus has not been made available to this repository. Populating them is a data change to those two files plus a version bump, not a code change; `tests/test_name_library.py` includes a regression guard that fails if the shipped placeholder ever stops being empty without a corresponding review.

`mapping_version` and `normalization_version` are carried on `DatasetMetadata` per dataset, matching the "increment whenever..." guidance above -- but the *release manifest* carries only one of each, not a per-dataset map (`schemas/release-manifest.schema.json`; see `docs/release-contract.md`). `ReleaseBuilder._require_consistent_versions()` enforces this by failing loudly if a release's datasets disagree, rather than silently picking one. This has never actually fired in practice, because every CLI entry point in this repo (`build-fixture-release`, `crawl-dataset`, `build-pep-release`) builds a release from exactly one dataset today. It would need addressing — either a genuinely shared version across datasets, or splitting into per-dataset releases — before this repo could ever build a real multi-dataset release.

