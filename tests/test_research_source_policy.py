"""The live source policy and the mandatory provider restriction (docs/research-egress.md,
"Source policy", "Provider restriction"). Nothing here opens a socket: the in-process web of
test_research_egress records every request, so a refusal is proven by the request that was
never made."""

from __future__ import annotations

from typing import Any

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("fastapi")
pytest.importorskip("trafilatura")

from fastapi.testclient import TestClient  # noqa: E402

from screening_crawler.research import extract as extraction  # noqa: E402
from screening_crawler.research import service  # noqa: E402
from screening_crawler.research.egress import EgressRefused, ResearchEgress  # noqa: E402
from screening_crawler.research.gateway import GatewayRejected, ResearchGateway  # noqa: E402
from screening_crawler.research.search import ExaSearchProvider  # noqa: E402
from screening_crawler.research.source_policy import (  # noqa: E402
    SourcePolicyInvalid,
    host_permitted,
    parse_domains,
    restricted_resolver,
)
from tests.test_research_egress import (  # noqa: E402
    QUERY,
    SECRET,
    TOKEN,
    Provider,
    Web,
    _page,
)

AUTH = {"Authorization": f"Bearer {TOKEN}"}
ORGANISATION = {"subject_kind": "PUBLIC_INTEREST_ORGANISATION", "name": "Bank of Fixtureland",
                "country": "gh"}
LIVE = {"RESEARCH_EGRESS_ENABLED": "true", "RESEARCH_EGRESS_TOKEN": TOKEN,
        "RESEARCH_LEAD_SECRET": SECRET.decode(), "RESEARCH_EXA_API_KEY": "placeholder",
        "RESEARCH_SOURCE_DOMAINS": "gov.gh"}


def _egress(web: Web, urls: list[str], domains: str) -> ResearchEgress:
    gateway = ResearchGateway(transport=httpx.MockTransport(web.handle),
                              resolver=restricted_resolver(parse_domains(domains), web.resolve))
    return ResearchEgress(provider=Provider(urls), gateway=gateway, lead_secret=SECRET,
                          min_seconds_between_host_requests=0.0, extractor=extraction.extract)


# --- which domains ------------------------------------------------------------------------------


@pytest.mark.parametrize(("host", "permitted"), [
    ("gov.gh", True), ("mofep.gov.gh", True), ("www.parliament.gh", True),
    ("evilgov.gh", False), ("gov.gh.evil.example", False), ("parliament.gh.example", False),
    ("example.com", False),
])
def test_a_domain_permits_itself_and_its_subdomains_only(host: str, permitted: bool) -> None:
    assert host_permitted(host, parse_domains("gov.gh, parliament.gh")) is permitted


@pytest.mark.parametrize("value", ["", " , ", "gh", "com", "*.gov.gh", "gov.gh/news",
                                   "https://gov.gh", "gov gh", "-gov.gh"])
def test_a_source_list_must_name_reviewable_domains(value: str) -> None:
    with pytest.raises(SourcePolicyInvalid):
        parse_domains(value)


@pytest.mark.parametrize("change", [{"RESEARCH_SOURCE_DOMAINS": ""}, {"RESEARCH_SOURCE_DOMAINS": "gh"}])
def test_a_live_egress_refuses_to_start_without_an_approved_source_list(
    change: dict[str, str]
) -> None:
    with pytest.raises(service.EgressNotConfigured):
        service.app_from_environment(LIVE | change)


# --- enforced before any request ----------------------------------------------------------------


def test_a_lead_outside_the_approved_domains_is_refused_before_dns_or_a_request() -> None:
    web = Web()
    web.add("https://news.example/story", _page())
    resolved: list[str] = []
    real_resolve = web.resolve

    def resolve(host: str, port: int) -> list[str]:
        resolved.append(host)
        return real_resolve(host, port)

    web.resolve = resolve  # type: ignore[method-assign]
    egress = _egress(web, ["https://news.example/story"], "gov.gh")
    with pytest.raises(EgressRefused, match="source_not_permitted"):
        egress.fetch(egress.search(QUERY)[0].lead_token)
    assert web.requests == [] and resolved == []


def test_a_redirect_to_an_unapproved_domain_is_refused_before_it_is_followed() -> None:
    web = Web()
    web.add("https://www.mofep.gov.gh/news", status=302,
            headers={"location": "https://tracker.example/landing"})
    web.add("https://tracker.example/landing", _page())
    egress = _egress(web, ["https://www.mofep.gov.gh/news"], "gov.gh")
    with pytest.raises(EgressRefused, match="source_not_permitted"):
        egress.fetch(egress.search(QUERY)[0].lead_token)
    assert web.requests and {r.headers["host"] for r in web.requests} == {"www.mofep.gov.gh"}


def test_an_approved_source_is_fetched_as_before() -> None:
    web = Web()
    web.add("https://www.mofep.gov.gh/news", _page())
    egress = _egress(web, ["https://www.mofep.gov.gh/news"], "gov.gh")
    document = egress.fetch(egress.search(QUERY)[0].lead_token)
    assert document.host == "www.mofep.gov.gh" and document.text


def test_the_resolver_refusal_is_a_gateway_rejection_with_a_stable_reason() -> None:
    resolve = restricted_resolver(parse_domains("gov.gh"), lambda host, port: ["93.184.216.34"])
    with pytest.raises(GatewayRejected) as refused:
        resolve("example.com", 443)
    assert refused.value.reason == "source_not_permitted"
    assert resolve("bog.gov.gh", 443) == ["93.184.216.34"]


# --- the provider restriction is mandatory (N1-a) -----------------------------------------------


@pytest.mark.parametrize("body", [{}, {"provider": None}], ids=["absent", "null"])
def test_a_search_that_names_no_provider_is_refused_before_the_provider_is_asked(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    calls: list[Any] = []

    def record(self: Any, query: Any, *, limit: int) -> list[Any]:
        calls.append(query)
        return []

    monkeypatch.setattr(ExaSearchProvider, "search", record)
    app, _, _ = service.app_from_environment(dict(LIVE))
    response = TestClient(app).post("/internal/research/search", headers=AUTH,
                                    json={"query": ORGANISATION, "limit": 3} | body)
    assert (response.status_code, response.json()) == (422, {"detail": "invalid_request"})
    assert calls == []
