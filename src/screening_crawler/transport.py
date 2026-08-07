"""Concrete network transport for live acquisition.

``acquisition.FetchTransport`` is deliberately a ``Protocol`` — the docs call
it "a network boundary implemented by deployments, never by projection code."
``storage.BlobStore`` follows the same pattern but still ships one safe first
implementation (``LocalBlobStore``) so the repository is not abstract end to
end. ``HttpFetchTransport`` is that same "one safe first deployment" for
fetching: a plain standard-library HTTP client with conditional-GET support,
captured redirect chains, bounded reads, and retry with backoff. Deployments
remain free to substitute their own transport; nothing in this repository
should need to before it can run against a real publisher.
"""

from __future__ import annotations

import email.message
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from http.client import HTTPMessage, HTTPResponse
from typing import Any

from . import __version__
from .acquisition import FetchRequest, FetchResponse
from .errors import FetchError
from .model import RedirectHop

_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})


class _TransientFetchFailure(Exception):
    """Internal retry signal. Never escapes ``HttpFetchTransport.fetch``."""


class _RecordingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Records every hop instead of only exposing the final destination."""

    def __init__(self) -> None:
        super().__init__()
        self.hops: list[RedirectHop] = []

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        self.hops.append(
            RedirectHop(url=req.full_url, status=code, location=headers.get("Location"))
        )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _PassThroughNotModifiedHandler(urllib.request.BaseHandler):
    """Lets HTTP 304 through as a normal response instead of raising."""

    def http_error_304(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: email.message.Message,
    ) -> Any:
        fp.status = code
        return fp


@dataclass(frozen=True, slots=True)
class HttpFetchTransport:
    """Fetch over HTTP(S) using only the standard library.

    Every dataset onboarded so far is a plain GET against a publisher
    endpoint, so no third-party HTTP dependency is introduced. Conditional
    requests are the caller's responsibility: pass ``If-None-Match`` and/or
    ``If-Modified-Since`` in ``FetchRequest.headers`` and a publisher that
    supports them will answer with a 304 that this transport returns as a
    normal, non-error ``FetchResponse`` rather than raising.
    """

    timeout: float = 30.0
    max_retries: int = 3
    backoff_seconds: float = 1.0
    max_response_bytes: int = 200 * 1024 * 1024
    user_agent: str = f"screening-crawler/{__version__}"
    sleep: Callable[[float], None] = time.sleep

    def fetch(self, request: FetchRequest) -> FetchResponse:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return self._attempt(request)
            except _TransientFetchFailure as exc:
                last_error = exc
                if attempt == self.max_retries:
                    break
                self.sleep(self.backoff_seconds * (2 ** (attempt - 1)))
        raise FetchError(
            f"failed to fetch {request.url} after {self.max_retries} attempt(s): {last_error}"
        ) from last_error

    def _attempt(self, request: FetchRequest) -> FetchResponse:
        redirect_handler = _RecordingRedirectHandler()
        opener = urllib.request.build_opener(redirect_handler, _PassThroughNotModifiedHandler())
        headers = {"User-Agent": self.user_agent, **request.headers}
        req = urllib.request.Request(request.url, headers=headers, method="GET")
        try:
            response = opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in _TRANSIENT_STATUSES:
                raise _TransientFetchFailure(f"HTTP {exc.code}") from exc
            raise FetchError(
                f"HTTP {exc.code} fetching {request.url}: {exc.reason}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise _TransientFetchFailure(str(exc)) from exc

        with response:
            status = response.status
            body = self._read_bounded(response, request.url)
            response_headers = dict(response.headers.items())

        return FetchResponse(
            request_url=request.url,
            status=status,
            headers=response_headers,
            body=body,
            redirects=tuple(redirect_handler.hops),
            publisher_publication_date=response_headers.get("Last-Modified"),
        )

    def _read_bounded(self, response: HTTPResponse, url: str) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > self.max_response_bytes:
                raise FetchError(
                    f"response from {url} exceeded {self.max_response_bytes} byte cap; "
                    "refusing to buffer further"
                )
            chunks.append(chunk)
        return b"".join(chunks)
