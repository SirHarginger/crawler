"""The live source policy and the mandatory provider restriction (docs/research-egress.md,
"Source policy", "Provider restriction"). Nothing here opens a socket: the in-process web of
test_research_egress records every request, so a refusal is proven by the request that was
never made."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("fastapi")
pytest.importorskip("trafilatura")

from fastapi.testclient import TestClient  # noqa: E402
from httpx import BaseTransport, Request, Response  # noqa: E402

from screening_crawler.research import extract as extraction  # noqa: E402
from screening_crawler.research import search as search_module  # noqa: E402
from screening_crawler.research import service  # noqa: E402
from screening_crawler.research.egress import EgressRefused, ResearchEgress  # noqa: E402
from screening_crawler.research.gateway import GatewayRejected, ResearchGateway  # noqa: E402
from screening_crawler.research.query import ResearchQuery  # noqa: E402
from screening_crawler.research.search import (  # noqa: E402
    ExaSearchProvider,
    SearchFailure,
    SearchLead,
)
from screening_crawler.research.source_policy import (  # noqa: E402
    SourcePolicyInvalid,
    host_permitted,
    parse_domains,
    policy_gateway,
    restricted_resolver,
)
from tests.test_research_egress import (  # noqa: E402
    QUERY,
    SECRET,
    TOKEN,
    Provider,
    Web,
    _harness,
    _page,
)

AUTH = {"Authorization": f"Bearer {TOKEN}"}
ORGANISATION = {"subject_kind": "PUBLIC_INTEREST_ORGANISATION", "name": "Bank of Fixtureland",
                "country": "gh"}
LIVE = {"RESEARCH_EGRESS_ENABLED": "true", "RESEARCH_EGRESS_TOKEN": TOKEN,
        "RESEARCH_LEAD_SECRET": SECRET.decode(), "RESEARCH_EXA_API_KEY": "placeholder",
        "RESEARCH_SOURCE_DOMAINS": "bog.gov.gh"}


def _egress(web: Web, urls: list[str], domains: str) -> ResearchEgress:
    gateway = ResearchGateway(transport=httpx.MockTransport(web.handle),
                              resolver=restricted_resolver(parse_domains(domains), web.resolve))
    return ResearchEgress(provider=Provider(urls), gateway=gateway, lead_secret=SECRET,
                          min_seconds_between_host_requests=0.0, extractor=extraction.extract)


# --- which domains (R3) ---------------------------------------------------------------------------


@pytest.mark.parametrize(("host", "permitted"), [
    ("bog.gov.gh", True), ("www.bog.gov.gh", True), ("parliament.gh", True),
    ("www.parliament.gh", True),
    # a sibling under the same public suffix is a different organisation
    ("mofep.gov.gh", False), ("gov.gh", False),
    # lookalikes and suffix confusion
    ("evilbog.gov.gh", False), ("bog.gov.gh.evil.example", False),
    ("bog-gov.gh", False), ("parliament.gh.example", False), ("xparliament.gh", False),
])
def test_an_entry_permits_itself_and_its_subdomains_never_a_sibling(host: str,
                                                                    permitted: bool) -> None:
    assert host_permitted(host, parse_domains("bog.gov.gh, parliament.gh")) is permitted


def test_an_exact_host_entry_does_not_widen_to_its_parent() -> None:
    domains = parse_domains("www.bog.gov.gh")
    assert host_permitted("www.bog.gov.gh", domains)
    assert not host_permitted("bog.gov.gh", domains)
    assert not host_permitted("mail.bog.gov.gh", domains)


@pytest.mark.parametrize("value", [
    # public suffixes: each would approve every site registered under it
    "gov.gh", "com.gh", "gh", "co.uk", "uk", "com", "github.io",
    # unknown top-level domains, reserved names
    "example.invalid", "bog.gov.zz", "localhost",
    # not plain host names
    "", " , ", "*.bog.gov.gh", "bog.gov.gh/news", "https://bog.gov.gh", "user@bog.gov.gh",
    "bog.gov.gh:443", "bog gov.gh", "-bog.gov.gh", "bog..gov.gh",
    # IP literals
    "93.184.216.34", "127.0.0.1", "::1", "[2001:db8::1]",
], ids=lambda v: v or "empty")
def test_a_broad_or_malformed_entry_is_refused(value: str) -> None:
    with pytest.raises(SourcePolicyInvalid):
        parse_domains(value)


def test_one_bad_entry_refuses_the_whole_list() -> None:
    with pytest.raises(SourcePolicyInvalid):
        parse_domains("bog.gov.gh, com.gh")


@pytest.mark.parametrize("value", ["bücher.de", "faß.de", "bog.gov.gh\u3002"])
def test_an_internationalised_entry_must_be_given_in_its_xn_form(value: str) -> None:
    # Two IDNA standards map some Unicode names to different hosts ("faß.de"): the owner
    # approves the exact ASCII host the gateway resolves, or nothing.
    with pytest.raises(SourcePolicyInvalid):
        parse_domains(value)


def test_an_xn_entry_is_held_as_given_ignoring_case_and_a_trailing_dot() -> None:
    domains = parse_domains("XN--BCHER-KVA.DE.")
    assert domains == frozenset({"xn--bcher-kva.de"})
    assert host_permitted("www.xn--bcher-kva.de", domains)


@pytest.mark.parametrize("change", [{"RESEARCH_SOURCE_DOMAINS": ""},
                                    {"RESEARCH_SOURCE_DOMAINS": "gov.gh"},
                                    {"RESEARCH_SOURCE_DOMAINS": "com.gh"}])
def test_a_live_egress_refuses_to_start_without_a_specific_source_list(
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
    egress = _egress(web, ["https://news.example/story"], "bog.gov.gh")
    with pytest.raises(EgressRefused, match="source_not_permitted"):
        egress.fetch(egress.search(QUERY, provider="fake")[0].lead_token)
    assert web.requests == [] and resolved == []


def test_a_redirect_to_an_unapproved_domain_is_refused_before_it_is_followed() -> None:
    web = Web()
    web.add("https://www.bog.gov.gh/news", status=302,
            headers={"location": "https://www.mofep.gov.gh/landing"})  # a sibling: not approved
    web.add("https://www.mofep.gov.gh/landing", _page())
    egress = _egress(web, ["https://www.bog.gov.gh/news"], "bog.gov.gh")
    with pytest.raises(EgressRefused, match="source_not_permitted"):
        egress.fetch(egress.search(QUERY, provider="fake")[0].lead_token)
    assert web.requests and {r.headers["host"] for r in web.requests} == {"www.bog.gov.gh"}


def test_an_approved_source_is_fetched_as_before() -> None:
    web = Web()
    web.add("https://www.bog.gov.gh/news", _page())
    egress = _egress(web, ["https://www.bog.gov.gh/news"], "bog.gov.gh")
    document = egress.fetch(egress.search(QUERY, provider="fake")[0].lead_token)
    assert document.host == "www.bog.gov.gh" and document.text


def test_the_resolver_refusal_is_a_gateway_rejection_with_a_stable_reason() -> None:
    resolve = restricted_resolver(parse_domains("bog.gov.gh"),
                                  lambda host, port: ["93.184.216.34"])
    with pytest.raises(GatewayRejected) as refused:
        resolve("example.com", 443)
    assert refused.value.reason == "source_not_permitted"
    assert resolve("www.bog.gov.gh", 443) == ["93.184.216.34"]


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


# --- no network-capable provider runs unapproved (R2) --------------------------------------------


class RecordingTransport(BaseTransport):
    """Stands where a real network transport would (it is not a MockTransport, so it counts
    as one) and records every request instead of sending it."""

    def __init__(self) -> None:
        self.requests: list[Request] = []

    def handle_request(self, request: Request) -> Response:
        self.requests.append(request)
        return Response(200, json={"results": []})


def test_as_committed_no_live_provider_is_approved() -> None:
    assert frozenset() == search_module.APPROVED_LIVE_PROVIDERS


def test_an_unapproved_provider_sends_nothing_whoever_calls_it() -> None:
    transport = RecordingTransport()
    exa = ExaSearchProvider(api_key="k" * 32, transport=transport)
    organisation = ResearchQuery.model_validate(ORGANISATION)
    # Directly, as a script would.
    with pytest.raises(SearchFailure, match="provider_not_approved"):
        exa.search(organisation, limit=3)
    # Through the egress, in process, naming the provider correctly.
    egress = ResearchEgress(provider=exa, gateway=policy_gateway(parse_domains("bog.gov.gh")),
                            lead_secret=SECRET)
    with pytest.raises(EgressRefused, match="search_provider_not_approved"):
        egress.search(organisation, provider="exa", limit=3)
    # Through the evaluation harness (tools/research_eval/run.py).
    row = _harness().evaluate_subject(egress, {"id": "s1", **ORGANISATION}, [])
    assert row["search"] == "search_provider_not_approved" and row["leads"] == []
    assert transport.requests == []


def test_once_approved_the_provider_is_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = RecordingTransport()
    monkeypatch.setattr(search_module, "APPROVED_LIVE_PROVIDERS", frozenset({"exa"}))
    ExaSearchProvider(api_key="k" * 32, transport=transport).search(
        ResearchQuery.model_validate(ORGANISATION), limit=3)
    assert len(transport.requests) == 1  # the instrument records a real attempt


@pytest.mark.parametrize("provider", ["", "exa", "staging-fixtures"])
def test_in_process_a_missing_or_other_provider_is_refused_before_the_provider(
    provider: str
) -> None:
    web = Web()
    recorded = Provider([])
    egress = ResearchEgress(provider=recorded, gateway=ResearchGateway(
        transport=httpx.MockTransport(web.handle), resolver=web.resolve), lead_secret=SECRET)
    with pytest.raises(EgressRefused, match="provider_not_permitted"):
        egress.search(QUERY, provider=provider)
    assert recorded.calls == 0


def test_in_process_the_provider_cannot_be_left_out() -> None:
    egress = ResearchEgress(provider=Provider([]), lead_secret=SECRET,
                            gateway=ResearchGateway(transport=httpx.MockTransport(Web().handle)))
    with pytest.raises(TypeError):
        egress.search(QUERY)  # type: ignore[call-arg]


# --- the boundary is the egress and the one gateway constructor (R2/R3 review) -------------------


class Unannounced:
    """A provider that performs no approval check of its own: the egress must refuse it."""

    name = "frozen-list"

    def __init__(self) -> None:
        self.calls = 0

    def search(self, query: ResearchQuery, *, limit: int) -> list[SearchLead]:
        self.calls += 1
        return [SearchLead("https://www.bog.gov.gh/about", 1, self.name)]


def test_the_egress_refuses_any_unapproved_provider_on_a_network_gateway() -> None:
    provider = Unannounced()
    egress = ResearchEgress(provider=provider, lead_secret=SECRET,
                            gateway=policy_gateway(parse_domains("bog.gov.gh")))
    with pytest.raises(EgressRefused, match="search_provider_not_approved"):
        egress.search(QUERY, provider="frozen-list")
    assert provider.calls == 0


def test_an_egress_on_a_network_gateway_without_approved_domains_cannot_be_built() -> None:
    for gateway in (ResearchGateway(), ResearchGateway(resolver=lambda host, port: [])):
        with pytest.raises(ValueError, match="approved source domains"):
            ResearchEgress(provider=Provider([]), gateway=gateway, lead_secret=SECRET)
    with pytest.raises(SourcePolicyInvalid):
        policy_gateway(frozenset())


def test_only_the_policy_constructor_builds_a_gateway_that_can_reach_a_network() -> None:
    """Governance: outside tests, `ResearchGateway(` is written in two places only -- the
    policy constructor and the fixture gateway, whose transport is in memory."""
    import re

    root = Path(__file__).resolve().parents[1]
    allowed = {"src/screening_crawler/research/source_policy.py",
               "src/screening_crawler/research/staging.py"}
    found = {path.relative_to(root).as_posix()
             for folder in ("src", "tools") for path in (root / folder).rglob("*.py")
             if re.search(r"\bResearchGateway\(", path.read_text(encoding="utf-8"))}
    assert found == allowed


@pytest.mark.parametrize("tool", ["fetch_eval", "realpage_eval", "run"])
def test_an_evaluation_tool_refuses_to_fetch_without_approved_domains_or_approval(
    tool: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _harness(tool)
    argv = {"fetch_eval": ["fetch_eval", "--live"],
            "realpage_eval": ["realpage_eval", "select", "--live"],
            "run": ["run", "--live"]}[tool]
    monkeypatch.setattr("sys.argv", argv)
    if tool == "run":  # it also needs its key and terms switch before reaching the domains
        monkeypatch.setenv("RESEARCH_EXA_API_KEY", "k" * 32)
        monkeypatch.setenv("RESEARCH_EXA_NAMED_PERSON_TERMS_APPROVED", "true")
    for name in [n for n in __import__("os").environ if n.startswith("RESEARCH_")
                 and tool != "run"]:
        monkeypatch.delenv(name)
    assert module.main() == 2
    assert "nothing was" in capsys.readouterr().err


def test_a_resolver_cannot_be_built_around_the_domain_parser() -> None:
    for domains in (frozenset({"com"}), frozenset({"gov.gh"}), frozenset({"BOG.GOV.GH"}),
                    frozenset()):
        with pytest.raises(SourcePolicyInvalid):
            restricted_resolver(domains)


def test_a_fetch_rechecks_the_gateway_and_the_provider_before_any_request(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    web = Web()
    web.add("https://www.bog.gov.gh/news", _page())
    approved = frozenset({"fake"})
    monkeypatch.setattr(search_module, "APPROVED_LIVE_PROVIDERS", approved)
    egress = ResearchEgress(provider=Provider(["https://www.bog.gov.gh/news"]),
                            lead_secret=SECRET, gateway=policy_gateway(
                                parse_domains("bog.gov.gh"), resolver=web.resolve,
                                transport=RecordingTransport()))
    token = egress.search(QUERY, provider="fake")[0].lead_token
    # The approval is withdrawn after the lead was issued: the lead is not fetched.
    monkeypatch.setattr(search_module, "APPROVED_LIVE_PROVIDERS", frozenset())
    with pytest.raises(EgressRefused, match="search_provider_not_approved"):
        egress.fetch(token)
    # The gateway's resolver is swapped after the egress was built: refused as well.
    monkeypatch.setattr(search_module, "APPROVED_LIVE_PROVIDERS", approved)
    egress.gateway.resolver = web.resolve
    with pytest.raises(EgressRefused, match="source_policy_missing"):
        egress.fetch(token)
    assert web.requests == [] and egress.gateway.transport.requests == []  # type: ignore[union-attr]
