"""Curated official sources (docs/research-egress.md, "Curated mode"): leads from a reviewed
list of official pages instead of a search provider. No socket is opened: the in-process web
of test_research_egress records every request."""

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

from screening_crawler.research import extract as extraction  # noqa: E402
from screening_crawler.research import search as search_module  # noqa: E402
from screening_crawler.research import service  # noqa: E402
from screening_crawler.research.curated import (  # noqa: E402
    PROVIDER_NAME,
    CuratedSourceProvider,
    CuratedSources,
    CuratedSourcesInvalid,
)
from screening_crawler.research.egress import EgressRefused, ResearchEgress  # noqa: E402
from screening_crawler.research.gateway import ResearchGateway  # noqa: E402
from screening_crawler.research.query import ResearchQuery  # noqa: E402
from screening_crawler.research.source_policy import (  # noqa: E402
    parse_domains,
    restricted_resolver,
)
from tests.test_research_egress import SECRET, TOKEN, Web, _page  # noqa: E402

AUTH = {"Authorization": f"Bearer {TOKEN}"}
BANK = "Synthetic Central Bank"
PAGES = ["https://www.bog.gov.gh/about-the-bank/", "https://www.bog.gov.gh/monetary-policy/"]
DOMAINS = parse_domains("bog.gov.gh")
QUERY = {"subject_kind": "PUBLIC_INTEREST_ORGANISATION", "name": BANK, "country": "gh"}


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts: list[str] = []

    def refuse(*args: Any, **kwargs: Any) -> Any:
        attempts.append(repr(args[:2]))
        raise OSError("network is disabled in tests")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield attempts
    assert attempts == [], f"a real network call was attempted: {attempts}"


def _manifest(tmp_path: Path, subjects: list[dict[str, Any]]) -> Path:
    path = tmp_path / "curated.json"
    path.write_text(json.dumps({"subjects": subjects}))
    return path


@pytest.mark.parametrize("pages", [
    ["https://www.mofep.gov.gh/about"],          # outside the approved domains
    ["http://www.bog.gov.gh/about"],             # not https
    ["https://www.bog.gov.gh:8443/about"],       # a port
    ["https://user@www.bog.gov.gh/about"],       # credentials
    ["https://www.bog.gov.gh/search?q=x"],       # a query
    ["https://www.bog.gov.gh/a#b"],              # a fragment
    [],                                          # nothing
    [f"https://www.bog.gov.gh/{n}" for n in range(6)],  # too many
], ids=["other-domain", "http", "port", "userinfo", "query", "fragment", "empty", "too-many"])
def test_a_curated_list_with_anything_but_plain_approved_pages_is_refused(
    tmp_path: Path, pages: list[str]
) -> None:
    with pytest.raises(CuratedSourcesInvalid):
        CuratedSources.load(_manifest(tmp_path, [{"name": BANK, "pages": pages}]), DOMAINS)


def _env(path: Path) -> dict[str, str]:
    return {"RESEARCH_EGRESS_ENABLED": "true", "RESEARCH_EGRESS_TOKEN": TOKEN,
            "RESEARCH_LEAD_SECRET": SECRET.decode(), "RESEARCH_EGRESS_MODE": "curated",
            "RESEARCH_CURATED_SOURCES": str(path), "RESEARCH_SOURCE_DOMAINS": "bog.gov.gh"}


def test_curated_mode_starts_only_fully_configured_and_without_a_provider_key(
    tmp_path: Path
) -> None:
    env = _env(_manifest(tmp_path, [{"name": BANK, "pages": PAGES}]))
    service.app_from_environment(dict(env))
    for change in ({"RESEARCH_EXA_API_KEY": "placeholder"}, {"RESEARCH_SOURCE_DOMAINS": ""},
                   {"RESEARCH_SOURCE_DOMAINS": "gov.gh"}, {"RESEARCH_CURATED_SOURCES": ""},
                   {"RESEARCH_SOURCE_DOMAINS": "parliament.gh"}):  # pages outside the list
        with pytest.raises(service.EgressNotConfigured):
            service.app_from_environment(env | change)


def test_as_committed_curated_mode_answers_every_search_not_approved(tmp_path: Path) -> None:
    app, _, _ = service.app_from_environment(
        _env(_manifest(tmp_path, [{"name": BANK, "pages": PAGES}])))
    answer = TestClient(app).post("/internal/research/search", headers=AUTH,
                                  json={"query": QUERY, "limit": 4, "provider": PROVIDER_NAME})
    assert (answer.status_code, answer.json()) == (502, {"detail": "search_provider_not_approved"})


def _egress(web: Web) -> ResearchEgress:
    provider = CuratedSourceProvider(CuratedSources({" ".join(BANK.split()).casefold(): PAGES}))
    gateway = ResearchGateway(transport=httpx.MockTransport(web.handle),
                              resolver=restricted_resolver(DOMAINS, web.resolve))
    return ResearchEgress(provider=provider, gateway=gateway, lead_secret=SECRET,
                          min_seconds_between_host_requests=0.0, extractor=extraction.extract)


def test_once_approved_only_the_listed_pages_are_leads_and_are_fetched(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(search_module, "APPROVED_LIVE_PROVIDERS", frozenset({PROVIDER_NAME}))
    web = Web()
    for url in PAGES:
        web.add(url, _page())
    egress = _egress(web)
    leads = egress.search(ResearchQuery.model_validate(QUERY), provider=PROVIDER_NAME)
    assert [lead.host for lead in leads] == ["www.bog.gov.gh"] * 2
    assert all(lead.provider == PROVIDER_NAME for lead in leads)
    document = egress.fetch(leads[0].lead_token)
    assert document.final_url == PAGES[0] and document.provider == PROVIDER_NAME
    # Anyone the list does not name gets no leads, and nothing is fetched for them.
    other = ResearchQuery.model_validate(QUERY | {"name": "Synthetic Other Bank"})
    before = len(web.requests)
    assert egress.search(other, provider=PROVIDER_NAME) == []
    assert len(web.requests) == before


def test_unapproved_the_curated_provider_hands_out_no_lead(monkeypatch: pytest.MonkeyPatch) -> None:
    web = Web()
    egress = _egress(web)
    with pytest.raises(EgressRefused, match="search_provider_not_approved"):
        egress.search(ResearchQuery.model_validate(QUERY), provider=PROVIDER_NAME)
    assert web.requests == []
