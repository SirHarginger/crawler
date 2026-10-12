"""Research egress: the one place live-investigation search and page fetches leave the platform
(docs/research-egress.md).

The screening engine never fetches the public internet (docs/decisions/crawler-engine-boundary.md).
It calls this package's internal service with a structured, minimised query; the service asks a
search provider for leads, fetches only pages it issued a lead for, and returns bounded plain
text with provenance. Web content is untrusted data here and everywhere downstream.

Needs the optional `research` dependencies. Disabled unless explicitly enabled.
"""
