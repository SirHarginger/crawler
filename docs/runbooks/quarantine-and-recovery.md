# Quarantine and recovery runbook

## Trigger conditions

Quarantine on unknown paths or enumeration values, conflicting native keys, safety-critical type/cardinality/coverage drift, source validation failure, volume anomalies, corrupt content-addressed bytes, unsafe archives, failed release checksums, missing provenance, or schema/count mismatch. Incomplete snapshots stop before quarantine and remain unavailable for projection.

## Response

1. Stop promotion. Keep the last validated published release active in screening-engine.
2. Preserve the exact Layer 0 artifacts, response envelope, snapshot manifest, parser version, and validation findings. Do not edit captured bytes.
3. Classify the failure as acquisition truncation, publisher format change, semantic change, parser defect, mapping defect, or storage integrity incident.
4. Compare artifact and source-record hashes with the previous successful observation. Do not compare or merge by names.
5. For format or semantic change, confirm publisher meaning, update the appropriate versioned parser/inventory/interpreter/mapping, and add a synthetic regression fixture.
6. For incomplete acquisition, retry idempotently into a new assembly and record the correct completeness evidence. Never force the old snapshot to complete.
7. For integrity or checksum failure, isolate the bundle and rebuild from verified retained inputs into a new release path.
8. Run the complete test, lint, type, public-boundary, fixture build, and release-validation commands from the README.
9. Obtain compliance review for new legal semantics, enumeration values, distributions, or Ghana assertions.
10. Publish a new immutable release. Never modify the quarantined or previously published bundle.

## Recovery acceptance

Recovery is complete only when acquisition completeness is evidenced, field coverage has no quarantine finding, source validation passes, regeneration produces the same canonical hash, every property has provenance, checksum inventory is exact, release schema and counts validate, and atomic publication succeeds. Record the incident cause and version changes in the operational incident system outside this public repository.

