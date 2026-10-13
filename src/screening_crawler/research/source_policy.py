"""Which hosts a live research fetch may reach (docs/research-egress.md, "Source policy").

A live egress is given the approved source domains, `RESEARCH_SOURCE_DOMAINS`, and refuses to
start without them. The list is enforced in the gateway's resolver: the gateway asks it for
every hop, the first request and each redirect alike, before any DNS lookup and before any
connection. A host outside the list is refused as `source_not_permitted`, so a lead the search
provider returned, or a redirect a page sent, never reaches an unapproved site.

A domain permits itself and its subdomains ("gov.gh" permits "mofep.gov.gh"). Which domains
are approved is an owner decision; nothing here chooses them.
"""

from __future__ import annotations

import re

from .gateway import GatewayRejected, Resolver, _system_resolver

# Two or more lowercase labels: a single label ("gh", "com") would approve a whole top-level
# domain, which is not a decision anyone can review.
_DOMAIN = re.compile(r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}")


class SourcePolicyInvalid(ValueError):
    pass


def parse_domains(value: str) -> frozenset[str]:
    domains = frozenset(part.strip().lower().rstrip(".") for part in value.split(",")
                        if part.strip())
    bad = sorted(d for d in domains if not _DOMAIN.fullmatch(d))
    if bad or not domains:
        raise SourcePolicyInvalid("RESEARCH_SOURCE_DOMAINS must list domains such as gov.gh")
    return domains


def host_permitted(host: str, domains: frozenset[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith(f".{d}") for d in domains)


def restricted_resolver(domains: frozenset[str],
                        resolver: Resolver = _system_resolver) -> Resolver:
    def resolve(host: str, port: int) -> list[str]:
        if not host_permitted(host, domains):
            raise GatewayRejected("source_not_permitted", "not an approved source domain")
        return resolver(host, port)

    return resolve
