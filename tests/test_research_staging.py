"""The staging research source (docs/research-egress.md, "Staging"): a fixture-backed search
provider and transport behind the real egress, so an investigation can be run end to end with
no provider and no internet. What is tested here is that it is the real path, and that it
cannot be pointed at anything real.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("fastapi")
pytest.importorskip("trafilatura")

from fastapi.testclient import TestClient  # noqa: E402

from screening_crawler.research import service  # noqa: E402
from screening_crawler.research.egress import EgressRefused, ResearchEgress  # noqa: E402
from screening_crawler.research.extract import ARTICLE_EXTRACTOR_VERSION  # noqa: E402
from screening_crawler.research.gateway import GatewayRejected  # noqa: E402
from screening_crawler.research.query import ResearchQuery  # noqa: E402
from screening_crawler.research.search import ExaSearchProvider  # noqa: E402
from screening_crawler.research.staging import (  # noqa: E402
    PROVIDER_NAME,
    Fixtures,
    FixtureSearchProvider,
    FixturesInvalid,
    fixture_gateway,
)
from screening_crawler.research.structured_text import STRUCTURED_EXTRACTOR_VERSION  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "research_staging"
TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ENV = {"RESEARCH_EGRESS_ENABLED": "true", "RESEARCH_EGRESS_TOKEN": TOKEN,
       "RESEARCH_LEAD_SECRET": "s" * 40, "RESEARCH_EGRESS_MODE": "staging-fixtures",
       "RESEARCH_FIXTURE_DIR": str(FIXTURES)}
MINISTRY = {"subject_kind": "PUBLIC_INTEREST_ORGANISATION",
            "name": "Synthetic Ministry of Finance", "country": "gh"}


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The point of this mode: nothing may open a socket. Recorded and checked at teardown."""
    attempts: list[str] = []

    def refuse(*args: Any, **kwargs: Any) -> Any:
        attempts.append(repr(args[:2]))
        raise OSError("network is disabled in tests")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield attempts
    assert attempts == [], f"a real network call was attempted: {attempts}"


def _client() -> TestClient:
    app, host, _ = service.app_from_environment(dict(ENV))
    assert host == "127.0.0.1"
    return TestClient(app)


def _search(client: TestClient, query: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    response = client.post("/internal/research/search", headers=AUTH,
                           json={"query": query, "limit": limit, "provider": PROVIDER_NAME})
    assert response.status_code == 200
    return list(response.json()["leads"])


def test_a_staging_investigation_gets_leads_and_pages_through_the_real_egress() -> None:
    client = _client()
    leads = _search(client, MINISTRY)
    assert [(lead["rank"], lead["host"], lead["provider"]) for lead in leads] == [
        (n, "pages.fixture.invalid", PROVIDER_NAME) for n in (1, 2, 3, 4)]
    documents = [client.post("/internal/research/fetch", headers=AUTH,
                             json={"lead_token": lead["lead_token"]}) for lead in leads]
    assert [d.status_code for d in documents] == [200, 200, 200, 422]
    profile, article, hostile = (d.json() for d in documents[:3])
    assert documents[3].json() == {"detail": "http_error"}  # a source that is gone is a gap
    assert profile["extractor_version"] == STRUCTURED_EXTRACTOR_VERSION
    assert "Minister: Hon. Synthetic Minister Delta" in profile["text"].split("\n")
    assert article["extractor_version"] == ARTICLE_EXTRACTOR_VERSION
    assert article["published_date"] == "2026-09-14"
    assert article["final_url"] == "https://pages.fixture.invalid/news/quarterly-report"
    for document in (profile, article, hostile):
        assert len(document["content_sha256"]) == 64 and document["provider"] == PROVIDER_NAME
        assert "NAVIGATION-" not in document["text"] and "FOOTER-" not in document["text"]
    # Hostile text is text: returned as data, and it caused nothing.
    assert "ignore all previous instructions" in hostile["text"]


def test_a_subject_the_fixtures_do_not_name_has_no_leads() -> None:
    assert _search(_client(), MINISTRY | {"name": "Somebody Else Entirely"}) == []


def test_only_a_token_the_egress_issued_is_fetched_in_staging_too() -> None:
    client = _client()
    lead = _search(client, MINISTRY)[0]["lead_token"]
    payload, _, signature = lead.partition(".")
    for forged in (f"{payload}.{'0' * len(signature)}", payload, "https://evil.example/x",
                   f"{payload[:-4]}AAAA.{signature}"):
        response = client.post("/internal/research/fetch", headers=AUTH,
                               json={"lead_token": forged})
        assert response.status_code in (403, 422), forged[:20]
        assert response.json()["detail"] in ("invalid_lead", "invalid_request")
    assert client.post("/internal/research/fetch", json={"lead_token": lead}).status_code == 401


def test_the_staging_gateway_can_reach_the_fixture_host_and_nothing_else() -> None:
    fixtures = Fixtures.load(FIXTURES)
    gateway = fixture_gateway(fixtures)
    assert gateway.fetch("https://pages.fixture.invalid/ministry/about",
                         max_bytes=1_000_000).status == 200
    for url in ("https://example.com/", "https://www.parliament.gh/members",
                "https://pages.fixture.invalid.evil.example/x", "http://127.0.0.1/x",
                "http://169.254.169.254/latest/meta-data/"):
        with pytest.raises(GatewayRejected):
            gateway.fetch(url, max_bytes=1_000_000)


def test_a_fixture_page_that_is_not_html_or_is_missing_is_refused_like_any_other() -> None:
    fixtures = Fixtures.load(FIXTURES)
    egress = ResearchEgress(provider=FixtureSearchProvider(fixtures),
                            gateway=fixture_gateway(fixtures), lead_secret=b"s" * 40,
                            min_seconds_between_host_requests=0.0)
    query = ResearchQuery(subject_kind="PUBLIC_INTEREST_ORGANISATION",
                          name="Synthetic Unreachable Body")
    reasons = []
    for lead in egress.search(query):
        with pytest.raises(EgressRefused) as refused:
            egress.fetch(lead.lead_token)
        reasons.append(refused.value.reason)
    assert reasons == ["http_error", "unsupported_content_type"]


@pytest.mark.parametrize(("change", "message"), [
    ({"RESEARCH_EGRESS_ENABLED": "false"}, "disabled"),
    ({"RESEARCH_EGRESS_MODE": "anything"}, "RESEARCH_EGRESS_MODE"),
    ({"RESEARCH_FIXTURE_DIR": ""}, "RESEARCH_FIXTURE_DIR"),
    ({"RESEARCH_EXA_API_KEY": "placeholder"}, "takes no provider key"),
    ({"RESEARCH_EGRESS_TOKEN": ""}, "RESEARCH_EGRESS_TOKEN"),
    ({"RESEARCH_EGRESS_HOST": "0.0.0.0"}, "loopback or private"),  # noqa: S104
])
def test_staging_mode_is_off_unless_fully_and_exclusively_configured(
    change: dict[str, str], message: str
) -> None:
    with pytest.raises(service.EgressNotConfigured, match=message):
        service.app_from_environment(ENV | change)


def test_live_mode_still_needs_a_provider_key_and_ignores_the_fixture_directory() -> None:
    live = {k: v for k, v in ENV.items() if k != "RESEARCH_EGRESS_MODE"}
    with pytest.raises(service.EgressNotConfigured, match="RESEARCH_EXA_API_KEY"):
        service.app_from_environment(live)


@pytest.mark.parametrize("manifest", [
    {"host": "www.parliament.gh", "subjects": [], "pages": {}},
    {"host": "pages.fixture.invalid:8080", "subjects": [], "pages": {}},
    {"host": "pages.fixture.invalid", "subjects": [],
     "pages": {"/a": {"file": "../../../etc/hostname"}}},
    {"host": "pages.fixture.invalid", "subjects": [], "pages": {"a": {"file": None}}},
    {"host": "pages.fixture.invalid", "subjects": [{"name": "X Y", "pages": ["/missing"]}],
     "pages": {}},
    {"subjects": [], "pages": {}},
], ids=["real-host", "host-with-port", "file-outside", "relative-path", "undefined-page",
        "no-host"])
def test_a_manifest_that_names_a_real_host_or_a_file_outside_is_refused(
    manifest: dict[str, Any], tmp_path: Path
) -> None:
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(FixturesInvalid):
        Fixtures.load(tmp_path)
    with pytest.raises(service.EgressNotConfigured):
        service.app_from_environment(ENV | {"RESEARCH_FIXTURE_DIR": str(tmp_path)})


def test_the_fixture_pages_are_synthetic() -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["host"].endswith(".invalid")
    for page in sorted(FIXTURES.glob("*.html")):
        assert "Synthetic" in page.read_text(encoding="utf-8"), page.name


# --- provider restriction (docs/research-egress.md, "Provider restriction") ----------------------


def _live_client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, list[Any]]:
    """The egress as configured for live use, with the real provider class instrumented: any
    call to its search is recorded and answered with nothing, never sent anywhere."""
    calls: list[Any] = []

    def record(self: Any, query: ResearchQuery, *, limit: int) -> list[Any]:
        calls.append(query)
        return []

    monkeypatch.setattr(ExaSearchProvider, "search", record)
    live = {k: v for k, v in ENV.items() if k not in ("RESEARCH_EGRESS_MODE",
                                                      "RESEARCH_FIXTURE_DIR")}
    app, _, _ = service.app_from_environment(live | {"RESEARCH_EXA_API_KEY": "placeholder",
                                                     "RESEARCH_SOURCE_DOMAINS": "gov.gh"})
    return TestClient(app), calls


def test_a_staging_restricted_search_is_answered_by_the_fixtures_and_says_so() -> None:
    response = _client().post("/internal/research/search", headers=AUTH, json={
        "query": MINISTRY, "limit": 4, "provider": PROVIDER_NAME})
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == PROVIDER_NAME and body["leads"]
    assert {lead["provider"] for lead in body["leads"]} == {PROVIDER_NAME}


def test_a_live_egress_refuses_a_staging_restricted_search_before_asking_its_provider(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    client, calls = _live_client(monkeypatch)
    response = client.post("/internal/research/search", headers=AUTH, json={
        "query": MINISTRY, "limit": 4, "provider": PROVIDER_NAME})
    assert (response.status_code, response.json()) == (403, {"detail": "provider_not_permitted"})
    assert calls == []
    # The instrument works: a search restricted to the live provider does reach its method.
    assert client.post("/internal/research/search", headers=AUTH,
                       json={"query": MINISTRY, "limit": 4, "provider": "exa"}).status_code == 200
    assert len(calls) == 1


@pytest.mark.parametrize("provider", ["exa", "staging-fixtures-2", "other"])
def test_any_provider_but_the_configured_one_is_refused_and_costs_no_search(
    provider: str
) -> None:
    asked: list[Any] = []
    fixtures = Fixtures.load(FIXTURES)

    class Recording(FixtureSearchProvider):
        def search(self, query: ResearchQuery, *, limit: int) -> list[Any]:
            asked.append(query)
            return super().search(query, limit=limit)

    egress = ResearchEgress(provider=Recording(fixtures), gateway=fixture_gateway(fixtures),
                            lead_secret=b"s" * 40, max_searches_per_day=1)
    with pytest.raises(EgressRefused, match="provider_not_permitted"):
        egress.search(ResearchQuery.model_validate(MINISTRY), limit=4, provider=provider)
    assert asked == []
    # Refused before the daily budget: the one permitted search is still available.
    assert egress.search(ResearchQuery.model_validate(MINISTRY), limit=4, provider=PROVIDER_NAME)
    assert len(asked) == 1


@pytest.mark.parametrize("body", [
    {"provider": ""}, {"provider": "Staging Fixtures"}, {"provider": 1},
    {"provider": "x" * 41}, {"providers": ["staging-fixtures"]},
], ids=["empty", "not-a-code", "not-a-string", "too-long", "unknown-field"])
def test_a_malformed_restriction_or_an_unknown_field_is_refused_before_any_search(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    # An unknown field is how an egress older than the restriction answers it: the request
    # model forbids extra fields, so it refuses rather than searching unrestricted.
    client, calls = _live_client(monkeypatch)
    response = client.post("/internal/research/search", headers=AUTH,
                           json={"query": MINISTRY, "limit": 4} | body)
    assert (response.status_code, response.json()) == (422, {"detail": "invalid_request"})
    assert calls == []
