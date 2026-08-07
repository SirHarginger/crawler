"""HttpFetchTransport exercised against a loopback test server, never a live publisher."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from screening_crawler.acquisition import FetchRequest
from screening_crawler.errors import FetchError
from screening_crawler.transport import HttpFetchTransport


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return  # keep test output quiet

    def do_GET(self) -> None:  # noqa: N802
        state = self.server.state
        state["requests"] += 1

        if self.path == "/ok":
            if self.headers.get("If-None-Match") == "synthetic-etag":
                self.send_response(304)
                self.end_headers()
                return
            body = b"<SYNTHETIC_LIST/>"
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.send_header("ETag", "synthetic-etag")
            self.send_header("Last-Modified", "Fri, 01 Mar 2024 12:00:00 GMT")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
            return

        if self.path == "/flaky":
            if state["requests"] <= 2:
                self.send_response(503)
                self.end_headers()
                return
            body = b"<RECOVERED/>"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/broken":
            self.send_response(500)
            self.end_headers()
            return

        if self.path == "/missing":
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(404)
        self.end_headers()


class _Server(ThreadingHTTPServer):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.state = {"requests": 0}


@pytest.fixture
def server() -> Iterator[_Server]:
    httpd = _Server(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        thread.join()


def _url(server: _Server, path: str) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}{path}"


def test_fetch_returns_body_and_headers(server: _Server) -> None:
    transport = HttpFetchTransport(max_retries=1)
    response = transport.fetch(FetchRequest(url=_url(server, "/ok"), headers={}))
    assert response.status == 200
    assert response.body == b"<SYNTHETIC_LIST/>"
    assert response.headers["ETag"] == "synthetic-etag"
    assert response.publisher_publication_date == "Fri, 01 Mar 2024 12:00:00 GMT"


def test_conditional_request_returns_not_modified_without_raising(server: _Server) -> None:
    transport = HttpFetchTransport(max_retries=1)
    response = transport.fetch(
        FetchRequest(url=_url(server, "/ok"), headers={"If-None-Match": "synthetic-etag"})
    )
    assert response.status == 304
    assert response.body == b""


def test_redirect_chain_is_captured(server: _Server) -> None:
    transport = HttpFetchTransport(max_retries=1)
    response = transport.fetch(FetchRequest(url=_url(server, "/redirect"), headers={}))
    assert response.status == 200
    assert response.body == b"<SYNTHETIC_LIST/>"
    assert len(response.redirects) == 1
    assert response.redirects[0].status == 302
    assert response.redirects[0].location == "/ok"


def test_transient_failure_retries_then_succeeds(server: _Server) -> None:
    transport = HttpFetchTransport(max_retries=5, backoff_seconds=0, sleep=lambda _seconds: None)
    response = transport.fetch(FetchRequest(url=_url(server, "/flaky"), headers={}))
    assert response.status == 200
    assert response.body == b"<RECOVERED/>"
    assert server.state["requests"] == 3


def test_exhausted_retries_raise_fetch_error(server: _Server) -> None:
    transport = HttpFetchTransport(max_retries=2, backoff_seconds=0, sleep=lambda _seconds: None)
    with pytest.raises(FetchError):
        transport.fetch(FetchRequest(url=_url(server, "/broken"), headers={}))
    assert server.state["requests"] == 2


def test_client_error_fails_fast_without_retrying(server: _Server) -> None:
    transport = HttpFetchTransport(max_retries=5, backoff_seconds=0, sleep=lambda _seconds: None)
    with pytest.raises(FetchError):
        transport.fetch(FetchRequest(url=_url(server, "/missing"), headers={}))
    assert server.state["requests"] == 1


def test_response_over_byte_cap_is_rejected(server: _Server) -> None:
    transport = HttpFetchTransport(max_retries=1, max_response_bytes=4)
    with pytest.raises(FetchError):
        transport.fetch(FetchRequest(url=_url(server, "/ok"), headers={}))
