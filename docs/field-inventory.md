# Field inventory format

Each dataset owns a versioned `fields.yml`. Paths use `$` for the record root and `[]` for every element of a repeated source field, for example `$.INDIVIDUAL_ALIAS[].ALIAS_NAME`.

Each rule declares:

- `expected_type`: string, integer, number, boolean, null, object, or array;
- `cardinality.min` and `cardinality.max` per source record;
- `required`;
- `known_values` when the publisher field is an enumeration;
- `mapping_destination` for `mapped` fields;
- `disposition`: `mapped`, `retained`, `not_projected`, or `quarantined`;
- `reason` for every non-mapped disposition;
- `coverage.min` and `coverage.max` across records;
- `safety_critical`, which upgrades semantic drift to quarantine.

An unknown path always quarantines. An unknown enumeration always quarantines because programme, list, measure, precision, or status codes may alter legal meaning. Required-field disappearance and safety-critical type, cardinality, and coverage changes also quarantine. Non-critical changes remain findings requiring review.

Parsers must expose unexpected source fields rather than silently filtering them. The UN adapter retains unknown direct and nested XML elements in the parsed native representation so the inventory sees them. Repeated occurrences of a declared scalar become an array-shaped path and are therefore detected as structural drift.

Changing `fields.yml` is a reviewed product change. Inspect the Layer 0 artifact, establish publisher meaning, choose a disposition, update parser/mapping versions where needed, add a synthetic regression fixture, regenerate the release, and obtain compliance approval for legal semantics.

