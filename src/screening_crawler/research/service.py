"""The internal HTTP interface to the research egress.

Two POST routes for the screening engine, on a loopback or private address, behind a bearer
token that is checked before a request body is read. There is no unauthenticated route, no
route that takes a URL, and no interactive documentation. A rejected request is answered with
a fixed code: what was sent is never echoed back. The service refuses to start unless it is explicitly enabled and fully
configured: off is the default.

  RESEARCH_EGRESS_ENABLED=true          required, exactly "true"
  RESEARCH_EGRESS_TOKEN=...             shared with the engine; at least 32 characters
  RESEARCH_LEAD_SECRET=...              signs lead tokens; at least 32 characters
  RESEARCH_EXA_API_KEY=...              the search provider credential
  RESEARCH_EXA_NAMED_PERSON_TERMS_APPROVED=true
                                        only when terms that permit named-person queries
                                        (a DPA and zero data retention) are in place.
                                        Without it, a query about a person is refused.
  RESEARCH_EGRESS_HOST=127.0.0.1        loopback or private only
  RESEARCH_EGRESS_PORT=8090
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import os
import pathlib
from dataclasses import asdict
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from .egress import EgressRefused, ResearchEgress
from .gateway import ResearchGateway
from .query import ResearchQuery
from .search import ExaSearchProvider, SearchProvider
from .staging import Fixtures, FixtureSearchProvider, FixturesInvalid, fixture_gateway

_MIN_SECRET_LENGTH = 32
STAGING_MODE = "staging-fixtures"
_MAX_BODY_BYTES = 16 * 1024  # a query or one lead token; nothing larger is legitimate


class EgressNotConfigured(Exception):
    pass


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: ResearchQuery
    limit: int = Field(default=5, ge=1, le=10)
    # The only provider the caller permits (docs/research-egress.md, "Provider restriction").
    # An egress older than this field refuses the whole request as invalid, before searching.
    provider: str | None = Field(default=None, pattern=r"^[a-z0-9-]{1,40}$")


class FetchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    lead_token: str = Field(min_length=20, max_length=8000)


# Loopback and the private ranges only. `ipaddress.is_private` is wider than this (it includes
# documentation and link-local ranges), so the ranges are named.
_INTERNAL_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"))


def _is_internal_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address.version == network.version and address in network
               for network in _INTERNAL_NETWORKS)


def create_app(egress: ResearchEgress, token: str) -> FastAPI:
    if len(token) < _MIN_SECRET_LENGTH:
        raise EgressNotConfigured("the egress token must be at least 32 characters")
    app = FastAPI(title="research egress", docs_url=None, redoc_url=None, openapi_url=None)
    expected = token.encode()

    class _Gate(BaseHTTPMiddleware):
        """Runs before routing and before any body is parsed: no token, no processing."""

        async def dispatch(self, request: Request, call_next: RequestResponseEndpoint
                           ) -> Response:
            header = request.headers.get("authorization", "")
            supplied = header.removeprefix("Bearer ").encode("utf-8", "surrogateescape")
            if not header.startswith("Bearer ") or not hmac.compare_digest(supplied, expected):
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
            length = request.headers.get("content-length", "")
            if request.method == "POST" and (not (length.isascii() and length.isdigit())
                                             or int(length) > _MAX_BODY_BYTES):
                return JSONResponse({"detail": "invalid_request"}, status_code=413)
            return await call_next(request)

    app.add_middleware(_Gate)

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Never the default body: that would repeat the rejected input, which is exactly the
        # data this contract exists to keep from travelling.
        return JSONResponse({"detail": "invalid_request"}, status_code=422)

    @app.post("/internal/research/search")
    def search(body: SearchRequest) -> dict[str, Any]:
        try:
            leads = egress.search(body.query, limit=body.limit, provider=body.provider)
        except EgressRefused as exc:
            status = 403 if exc.reason == "provider_not_permitted" else 502
            raise HTTPException(status_code=status, detail=exc.reason) from None
        return {"query_sha256": body.query.sha256(), "provider": egress.provider.name,
                "leads": [asdict(lead) for lead in leads]}

    @app.post("/internal/research/fetch")
    def fetch(body: FetchRequest) -> dict[str, Any]:
        try:
            document = egress.fetch(body.lead_token)
        except EgressRefused as exc:
            status = (403 if exc.reason in ("invalid_lead", "expired_lead")
                      else 503 if exc.reason in ("host_busy", "egress_busy") else 422)
            raise HTTPException(status_code=status, detail=exc.reason) from None
        payload = asdict(document)
        payload["retrieved_at"] = document.retrieved_at.isoformat()
        return payload

    return app


def app_from_environment(environ: dict[str, str] | None = None) -> tuple[FastAPI, str, int]:
    env = os.environ if environ is None else environ
    if env.get("RESEARCH_EGRESS_ENABLED") != "true":
        raise EgressNotConfigured("research egress is disabled (RESEARCH_EGRESS_ENABLED != true)")
    mode = env.get("RESEARCH_EGRESS_MODE", "live")
    if mode not in ("live", STAGING_MODE):
        raise EgressNotConfigured("RESEARCH_EGRESS_MODE must be live or staging-fixtures")
    source = "RESEARCH_FIXTURE_DIR" if mode == STAGING_MODE else "RESEARCH_EXA_API_KEY"
    missing = [name for name in ("RESEARCH_EGRESS_TOKEN", "RESEARCH_LEAD_SECRET", source)
               if not env.get(name)]
    if missing:
        raise EgressNotConfigured(f"missing configuration: {', '.join(missing)}")
    if mode == STAGING_MODE and env.get("RESEARCH_EXA_API_KEY"):
        # One or the other: a process that serves fixtures holds no provider credential.
        raise EgressNotConfigured("staging-fixtures mode takes no provider key")
    if len(env["RESEARCH_LEAD_SECRET"]) < _MIN_SECRET_LENGTH:
        raise EgressNotConfigured("the lead secret must be at least 32 characters")
    host = env.get("RESEARCH_EGRESS_HOST", "127.0.0.1")
    if not _is_internal_host(host):
        raise EgressNotConfigured("the egress binds to a loopback or private address only")
    provider: SearchProvider
    if mode == STAGING_MODE:
        try:
            fixtures = Fixtures.load(pathlib.Path(env["RESEARCH_FIXTURE_DIR"]))
        except FixturesInvalid as exc:
            raise EgressNotConfigured(str(exc)) from None
        # No provider is contacted and the gateway can resolve the fixture host only.
        provider, gateway = FixtureSearchProvider(fixtures), fixture_gateway(fixtures)
        pace = 0.0  # pacing protects a publisher; there is none here
    else:
        provider = ExaSearchProvider(
            api_key=env["RESEARCH_EXA_API_KEY"],
            named_person_terms_approved=(
                env.get("RESEARCH_EXA_NAMED_PERSON_TERMS_APPROVED") == "true"))
        gateway, pace = ResearchGateway(), 2.0
    egress = ResearchEgress(provider=provider, gateway=gateway,
                            lead_secret=env["RESEARCH_LEAD_SECRET"].encode(),
                            min_seconds_between_host_requests=pace)
    port = env.get("RESEARCH_EGRESS_PORT", "8090")
    if not (port.isascii() and port.isdigit()) or not 1024 <= int(port) <= 65535:
        raise EgressNotConfigured("RESEARCH_EGRESS_PORT must be a port number from 1024 to 65535")
    app = create_app(egress, env["RESEARCH_EGRESS_TOKEN"])
    # Read once, then gone from this process's environment: every page is parsed in a child
    # process, and a child inherits whatever is still here when it starts.
    if environ is None:
        for name in [n for n in os.environ if n.startswith("RESEARCH_")]:
            del os.environ[name]
    return app, host, int(port)


def _hide_start_up_environment() -> bool:
    """Deleting a variable does not erase it from the block this process was started with,
    and a parsing child runs as the same user: it could read that block from /proc. Marking
    the process non-dumpable makes its /proc entries unreadable to it. Linux only, best
    effort; where it fails the secrets should come from somewhere other than the environment."""
    try:
        import ctypes

        pr_set_dumpable = 4
        return bool(ctypes.CDLL(None, use_errno=True).prctl(pr_set_dumpable, 0, 0, 0, 0) == 0)
    except (OSError, AttributeError):
        return False


def main() -> None:
    import uvicorn

    # Structured lines with hashes, hosts and outcome codes; never a query or page text.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    app, host, port = app_from_environment()
    if not _hide_start_up_environment():
        logging.getLogger(__name__).warning("research_egress_start_up_environment_readable")
    uvicorn.run(app, host=host, port=port, access_log=False)


if __name__ == "__main__":
    main()
