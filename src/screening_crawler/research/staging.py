"""A fixture-backed research source for staging: the whole egress, with no internet.

Staging needs to prove that an investigation can ask for research, receive a lead, redeem it,
and store what came back, before any search provider's terms allow a real query. This module
supplies the two things that would otherwise touch the outside world and nothing else:

- a `SearchProvider` that answers from a manifest of local pages, and
- a `ResearchGateway` whose transport serves those pages in-process and whose resolver knows
  one host only.

Everything between them is the real code: lead tokens are signed and redeemed, the gateway
validates the URL, the scheme, the port, robots and the size, the page is parsed in the
killable child, and the extractors and refusals are the production ones.

The fixture host must end in `.invalid`, a top-level domain reserved so that it can never
exist (RFC 2606). The resolver refuses every other name, so an egress in this mode cannot
reach a real site even if a fixture page links to one. It proves the integration, not live
retrieval.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from typing import Any

import httpx

from .gateway import GatewayRejected, ResearchGateway
from .query import ResearchQuery
from .search import SearchLead

PROVIDER_NAME = "staging-fixtures"
_MAX_FIXTURE_BYTES = 2 * 1024 * 1024
# Never dialled: the transport below answers in-process. The gateway insists on a public
# address before it will make a request, so the one fixture host is given this one.
_STAND_IN_ADDRESS = "93.184.216.34"


class FixturesInvalid(ValueError):
    pass


def _fold(name: str) -> str:
    return " ".join(name.split()).casefold()


@dataclass(frozen=True)
class Fixtures:
    host: str
    pages: dict[str, dict[str, Any]]  # path -> {"file" | None, "status", "content_type"}
    subjects: dict[str, list[str]]  # folded subject name -> paths, in rank order
    directory: pathlib.Path

    @classmethod
    def load(cls, directory: pathlib.Path) -> Fixtures:
        try:
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
            host = str(manifest["host"]).lower()
            pages = {str(path): dict(page) for path, page in manifest["pages"].items()}
            subjects = {_fold(str(s["name"])): [str(p) for p in s["pages"]]
                        for s in manifest["subjects"]}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise FixturesInvalid(f"unreadable fixture manifest: {type(exc).__name__}") from None
        if not host.endswith(".invalid") or "/" in host or ":" in host:
            raise FixturesInvalid("the fixture host must be a name under .invalid")
        root = directory.resolve()
        for path, page in pages.items():
            if not path.startswith("/"):
                raise FixturesInvalid("a fixture path starts with /")
            name = page.get("file")
            if name is not None and (root / str(name)).resolve().parent != root:
                raise FixturesInvalid("a fixture file lives in the fixture directory itself")
        for paths in subjects.values():
            if any(path not in pages for path in paths):
                raise FixturesInvalid("a subject names a page the manifest does not define")
        return cls(host, pages, subjects, root)


class FixtureSearchProvider:
    """Leads for the subjects the manifest names; none for anyone else. Contacts nothing."""

    name = PROVIDER_NAME

    def __init__(self, fixtures: Fixtures) -> None:
        self._fixtures = fixtures

    def search(self, query: ResearchQuery, *, limit: int) -> list[SearchLead]:
        paths = self._fixtures.subjects.get(_fold(query.name), [])
        return [SearchLead(f"https://{self._fixtures.host}{path}", rank, self.name)
                for rank, path in enumerate(paths[:limit], start=1)]


def fixture_gateway(fixtures: Fixtures) -> ResearchGateway:
    """The real gateway, with a transport that serves the fixture pages and a resolver that
    knows only the fixture host."""

    def resolve(host: str, port: int) -> list[str]:
        if host.lower() != fixtures.host:
            raise GatewayRejected("dns_failure", "not the fixture host")
        return [_STAND_IN_ADDRESS]

    def serve(request: httpx.Request) -> httpx.Response:
        page = fixtures.pages.get(request.url.raw_path.decode("ascii", "replace"))
        if request.headers.get("host", "").lower() != fixtures.host or page is None:
            return httpx.Response(404, stream=httpx.ByteStream(b""))
        body = b""
        if page.get("file") is not None:
            body = (fixtures.directory / str(page["file"])).read_bytes()[:_MAX_FIXTURE_BYTES]
        return httpx.Response(
            int(page.get("status", 200)),
            headers={"content-type": str(page.get("content_type", "text/html; charset=utf-8"))},
            stream=httpx.ByteStream(body))

    return ResearchGateway(transport=httpx.MockTransport(serve), resolver=resolve)
