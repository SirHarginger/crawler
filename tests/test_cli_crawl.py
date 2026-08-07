"""End-to-end crawl-dataset run against a loopback server, never a live publisher.

``metadata.yml`` deliberately keeps ``source_endpoint: null`` until a real
endpoint is confirmed (see docs/source-onboarding.md open questions), so this
test points a copy of the real adapter at a local test server instead of
mutating the tracked dataset config.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import screening_crawler.cli as cli_module
from datasets.un_sc_consolidated.crawler import adapter as real_un_adapter


class _FixtureHandler(BaseHTTPRequestHandler):
    body: bytes

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.headers.get("If-None-Match") == "synthetic-etag":
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("ETag", "synthetic-etag")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)


@pytest.fixture
def un_source_server() -> Iterator[str]:
    fixture_bytes = Path("tests/fixtures/un_sc_consolidated/synthetic.xml").read_bytes()
    handler = type("_BoundFixtureHandler", (_FixtureHandler,), {"body": fixture_bytes})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address[:2]
        yield f"http://{host}:{port}/consolidated.xml"
    finally:
        httpd.shutdown()
        thread.join()


def test_crawl_dataset_publishes_release_from_live_source(
    tmp_path: Path, un_source_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter_with_endpoint = copy.copy(real_un_adapter)
    adapter_with_endpoint.metadata = dataclasses.replace(
        real_un_adapter.metadata, source_endpoint=un_source_server
    )
    monkeypatch.setattr(
        cli_module, "load_adapter", lambda key: adapter_with_endpoint  # noqa: ARG005
    )

    args = argparse.Namespace(
        dataset="un_sc_consolidated",
        work=tmp_path / "work",
        output=tmp_path / "out",
        schemas=cli_module._repository_root() / "schemas",
        timeout=5.0,
        retries=2,
        if_none_match=None,
        if_modified_since=None,
    )
    status = cli_module._crawl_dataset(args)
    assert status == 0
    releases = list((tmp_path / "out").iterdir())
    assert len(releases) == 1
    assert (releases[0] / "manifest.json").exists()


def test_crawl_dataset_not_modified_publishes_nothing(
    tmp_path: Path, un_source_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter_with_endpoint = copy.copy(real_un_adapter)
    adapter_with_endpoint.metadata = dataclasses.replace(
        real_un_adapter.metadata, source_endpoint=un_source_server
    )
    monkeypatch.setattr(
        cli_module, "load_adapter", lambda key: adapter_with_endpoint  # noqa: ARG005
    )

    args = argparse.Namespace(
        dataset="un_sc_consolidated",
        work=tmp_path / "work",
        output=tmp_path / "out",
        schemas=cli_module._repository_root() / "schemas",
        timeout=5.0,
        retries=2,
        if_none_match="synthetic-etag",
        if_modified_since=None,
    )
    status = cli_module._crawl_dataset(args)
    assert status == 0
    assert not (tmp_path / "out").exists()
