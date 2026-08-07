# Public and private data boundary

The public repository contains code, schemas, configuration contracts, and synthetic fixtures. It must never contain captured publisher output, generated NDJSON or FollowTheMoney files, release bundles, real Ghana PEP output, restricted advisories, manually received documents, OCR/transcriptions from restricted material, tenant data, credentials, or personal data in examples and logs.

Controls are layered:

- `.gitignore` blocks generated-data locations and file types;
- `.pre-commit-config.yaml` and `.githooks/pre-commit` run the tracked-file check and public-boundary tests;
- CI repeats the check independently;
- CODEOWNERS marks schemas, field controls, release logic, and Ghana dataset paths for review;
- fixtures are explicitly synthetic and tests scan for generated artifact patterns;
- runtime dependencies include no PostgreSQL driver or Elasticsearch client.

The crawler may build a restricted release only outside the repository. Its manifest propagates the restriction. The private importer must check dataset entitlement and redistribution authority. Restricted advisories remain excluded from tenant collections until the issuing authority permits redistribution.

Logs and metrics use opaque source, snapshot, release, and record identifiers. They do not contain names, identifiers, supporting passages, or source document images.

Run the public-boundary control before every review:

```sh
.venv/bin/python tools/check_public_repo.py
```

