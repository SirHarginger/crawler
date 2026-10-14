"""Which hosts a live research fetch may reach (docs/research-egress.md, "Source policy").

A live egress is given the approved source domains, `RESEARCH_SOURCE_DOMAINS`, and refuses to
start without them. The list is enforced in the gateway's resolver: the gateway asks it for
every hop, the first request and each redirect alike, before any DNS lookup and before any
connection. A host outside the list is refused as `source_not_permitted`, so a lead the search
provider returned, or a redirect a page sent, never reaches an unapproved site.

An entry approves one organisation's site: a registrable domain under the Public Suffix List
("bog.gov.gh", "parliament.gh") or a host beneath one ("www.bog.gov.gh"). It approves itself
and its subdomains, never a sibling. A public suffix ("gov.gh", "com.gh", "co.uk",
"github.io") would approve every site registered under it and is refused, as is a domain under
a top-level domain the list does not know, an IP address, and anything that is not a plain
host name. The list ships with the pinned `publicsuffixlist` package, so no network is used to
read it; updating it is a dependency change. Entries are ASCII: an internationalised name is
given in its punycode (`xn--`) form, the form the gateway resolves, so what the owner approves
is exactly what is fetched (two IDNA standards map some Unicode names differently). Which
domains are approved is an owner decision; nothing here chooses them.

`policy_gateway` is the only place a gateway that can reach a network is built (a test keeps
it so), and the egress refuses one built any other way (egress.py).
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any

from publicsuffixlist import PublicSuffixList

from .gateway import GatewayRejected, ResearchGateway, Resolver, _system_resolver

# Unknown top-level domains are not registrable: an entry must sit under a known suffix.
_PSL = PublicSuffixList(accept_unknown=False)
_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


class SourcePolicyInvalid(ValueError):
    pass


def _approved_domain(entry: str) -> str:
    """The entry in IDNA form, or SourcePolicyInvalid."""
    text = entry.strip().lower().rstrip(".")
    try:
        ipaddress.ip_address(text.strip("[]"))
    except ValueError:
        pass
    else:
        raise SourcePolicyInvalid("an IP address is not a source domain")
    if not text.isascii():
        raise SourcePolicyInvalid("give an internationalised name in its xn-- form")
    domain = text
    labels = domain.split(".")
    if len(domain) > 253 or len(labels) < 2 or not all(_LABEL.fullmatch(x) for x in labels):
        raise SourcePolicyInvalid("not a plain domain name")
    if _PSL.privatesuffix(domain) is None:
        raise SourcePolicyInvalid("a public suffix or unknown top-level domain is not approvable")
    return domain


def parse_domains(value: str) -> frozenset[str]:
    entries = [part for part in value.split(",") if part.strip()]
    if not entries:
        raise SourcePolicyInvalid("RESEARCH_SOURCE_DOMAINS must list domains such as bog.gov.gh")
    try:
        return frozenset(_approved_domain(entry) for entry in entries)
    except SourcePolicyInvalid as exc:
        raise SourcePolicyInvalid(f"RESEARCH_SOURCE_DOMAINS: {exc}") from None


def host_permitted(host: str, domains: frozenset[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith(f".{d}") for d in domains)


@dataclass(frozen=True)
class RestrictedResolver:
    """The gateway's resolver, refusing every host outside the approved domains."""

    domains: frozenset[str]
    resolver: Resolver = _system_resolver

    def __post_init__(self) -> None:
        # However it was built: every entry is an approvable domain, and there is one.
        if not self.domains or any(_approved_domain(d) != d for d in self.domains):
            raise SourcePolicyInvalid("approved source domains are parsed by parse_domains")

    def __call__(self, host: str, port: int) -> list[str]:
        if not host_permitted(host, self.domains):
            raise GatewayRejected("source_not_permitted", "not an approved source domain")
        return self.resolver(host, port)


def restricted_resolver(domains: frozenset[str],
                        resolver: Resolver = _system_resolver) -> RestrictedResolver:
    return RestrictedResolver(domains, resolver)


def policy_gateway(domains: frozenset[str], resolver: Resolver = _system_resolver,
                   **kwargs: Any) -> ResearchGateway:
    """A gateway that can reach the approved source domains and nothing else."""
    if not domains:
        raise SourcePolicyInvalid("a live gateway needs approved source domains")
    return ResearchGateway(resolver=restricted_resolver(domains, resolver), **kwargs)
