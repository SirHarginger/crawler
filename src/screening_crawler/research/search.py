"""Search providers, behind one small interface.

A provider turns a `ResearchQuery` into ranked LEADS: URLs worth fetching. A lead's title,
snippet, author and provider id are untrusted text the platform has no use for and, under the
provider's terms, no right to keep, so the adapter does not return them. Nothing a provider says
is evidence; evidence is what the gateway fetches from the publisher itself.

The raw provider response lives only inside `search()` and is never logged or stored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol
from urllib.parse import urlsplit

import httpx

from .query import ResearchQuery

MAX_LEADS = 10
_MAX_URL_LENGTH = 2000
_DATE_HINT = re.compile(r"\d{4}-\d{2}-\d{2}")


class SearchFailure(Exception):
    """`reason` is a stable machine code. The message never carries the query or a credential."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SearchLead:
    url: str
    rank: int
    provider: str
    # The provider's ESTIMATE of when the page was published. A hint for ordering only; the
    # page's own date is established from the page.
    published_hint: str | None = None


class SearchProvider(Protocol):
    @property
    def name(self) -> str: ...

    def search(self, query: ResearchQuery, *, limit: int) -> list[SearchLead]: ...


def _lead_url(value: Any) -> str | None:
    if not isinstance(value, str) or not 0 < len(value) <= _MAX_URL_LENGTH:
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None:
        return None
    return value


# The network-capable search providers that may be asked anything at all. EMPTY: no live
# provider is approved (engine docs/decisions/research-access.md). Adding a name is an owner
# decision recorded there; nothing else, no setting and no caller, can approve one.
APPROVED_LIVE_PROVIDERS: frozenset[str] = frozenset()


def require_approval(name: str, transport: httpx.BaseTransport | None) -> None:
    """Called before a provider sends anything. A provider that can reach a network must be
    approved. An in-memory `httpx.MockTransport` cannot reach one, which is a property of the
    type, not of the caller: tests and offline replays may use it unapproved."""
    if isinstance(transport, httpx.MockTransport):
        return
    if name not in APPROVED_LIVE_PROVIDERS:
        raise SearchFailure("provider_not_approved")


@dataclass
class ExaSearchProvider:
    """Exa's `POST /search` (https://exa.ai/docs/reference/search). Results only: no page
    content is requested from Exa.

    NOT APPROVED for named-person queries under Exa's standard terms (owner decision,
    2026-10-11): its privacy policy says query data must not contain personal information. A
    query about a person is refused here, before anything is sent, unless the deployment
    states that terms covering that use (a DPA and zero data retention) are in place."""

    api_key: str
    named_person_terms_approved: bool = False
    timeout_seconds: float = 20.0
    transport: httpx.BaseTransport | None = None
    # Fixed for the class: approval is by name, so no instance may take another's name or
    # send to another endpoint.
    name: ClassVar[str] = "exa"
    endpoint: ClassVar[str] = "https://api.exa.ai/search"

    def __repr__(self) -> str:  # the key never reaches a log line or a traceback
        return "ExaSearchProvider(api_key=<redacted>)"

    def search(self, query: ResearchQuery, *, limit: int) -> list[SearchLead]:
        require_approval(self.name, self.transport)
        if query.subject_kind == "PUBLIC_FIGURE" and not self.named_person_terms_approved:
            raise SearchFailure("provider_terms_not_approved")
        wanted = max(1, min(limit, MAX_LEADS))
        payload: dict[str, Any] = {"query": query.text(), "numResults": wanted,
                                   "type": "auto", "moderation": True}
        if query.country:
            payload["userLocation"] = query.country.upper()
        try:
            # trust_env=False: no proxy variable reroutes a query. No redirects: the configured
            # host is the only host contacted.
            with httpx.Client(transport=self.transport, timeout=self.timeout_seconds,
                              trust_env=False, follow_redirects=False) as client:
                response = client.post(self.endpoint, json=payload,
                                       headers={"x-api-key": self.api_key})
        except httpx.TimeoutException:
            raise SearchFailure("timeout") from None
        except httpx.HTTPError:
            raise SearchFailure("unavailable") from None
        if response.status_code == 429:
            raise SearchFailure("rate_limited")
        if response.status_code in (401, 403):
            raise SearchFailure("unauthorized")
        if response.status_code != 200:
            raise SearchFailure("provider_error")
        try:
            body = response.json()
        except (ValueError, RecursionError):
            raise SearchFailure("malformed_response") from None
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            raise SearchFailure("malformed_response")
        leads: list[SearchLead] = []
        seen: set[str] = set()
        for item in results:
            url = _lead_url(item.get("url")) if isinstance(item, dict) else None
            if url is None or url in seen:
                continue
            seen.add(url)
            hint = item.get("publishedDate")
            leads.append(SearchLead(
                url=url, rank=len(leads) + 1, provider=self.name,
                published_hint=hint[:10] if isinstance(hint, str)
                and _DATE_HINT.match(hint) else None))
            if len(leads) == wanted:
                break
        return leads
