"""CLI coverage for the new operator entrypoints (list-sources, crawl, list-runs,
inspect-run, list-releases, compare-releases). Never touches the network -- `crawl` here
always points at a loopback server, same pattern as tests/test_cli_crawl.py and
tests/test_run_engine.py.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import screening_crawler.cli as cli_module
import screening_crawler.run as run_module
from datasets.un_sc_consolidated.crawler import adapter as real_un_adapter


class _FixtureHandler(BaseHTTPRequestHandler):
    body: bytes

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)


@pytest.fixture
def un_source_server() -> Iterator[str]:
    body = Path("tests/fixtures/un_sc_consolidated/synthetic.xml").read_bytes()
    handler = type("_BoundFixtureHandler", (_FixtureHandler,), {"body": body})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address[:2]
        yield f"http://{host}:{port}/consolidated.xml"
    finally:
        httpd.shutdown()
        thread.join()


@pytest.fixture
def adapter_with_endpoint(un_source_server: str, monkeypatch: pytest.MonkeyPatch):
    adapter = copy.copy(real_un_adapter)
    adapter.metadata = dataclasses.replace(real_un_adapter.metadata, source_endpoint=un_source_server)
    monkeypatch.setattr(run_module, "load_adapter", lambda key: adapter)  # noqa: ARG005
    return adapter


def test_list_sources_reports_every_machine_dataset(capsys: pytest.CaptureFixture[str]) -> None:
    status = cli_module._list_sources(argparse.Namespace())
    assert status == 0
    rows = json.loads(capsys.readouterr().out)
    keys = {row["source_key"] for row in rows}
    assert "un_sc_consolidated" in keys
    # PEP-shaped/human-intake datasets (no source_endpoint) must not be reported as live-crawlable.
    un_row = next(row for row in rows if row["source_key"] == "un_sc_consolidated")
    assert un_row["live_crawl_enabled"] is True


def test_crawl_then_list_runs_then_inspect_run_round_trip(
    tmp_path: Path, adapter_with_endpoint, capsys: pytest.CaptureFixture[str]
) -> None:
    crawl_args = argparse.Namespace(
        dataset="un_sc_consolidated",
        all=False,
        work=tmp_path / "work",
        output=tmp_path / "out",
        schemas=cli_module._repository_root() / "schemas",
        timeout=5.0,
        retries=1,
    )
    status = cli_module._crawl(crawl_args)
    assert status == 0
    crawl_result = json.loads(capsys.readouterr().out)
    assert crawl_result["state"] == "PUBLISHED"
    run_id = crawl_result["run_id"]

    list_args = argparse.Namespace(work=tmp_path / "work", source="un_sc_consolidated")
    assert cli_module._list_runs(list_args) == 0
    runs = json.loads(capsys.readouterr().out)
    assert [r["run_id"] for r in runs] == [run_id]

    inspect_args = argparse.Namespace(work=tmp_path / "work", source="un_sc_consolidated", run_id=run_id)
    assert cli_module._inspect_run(inspect_args) == 0
    full_run = json.loads(capsys.readouterr().out)
    assert full_run["run_id"] == run_id
    assert full_run["state"] == "PUBLISHED"

    # Also resolvable without knowing which source it belongs to.
    inspect_no_source = argparse.Namespace(work=tmp_path / "work", source=None, run_id=run_id)
    assert cli_module._inspect_run(inspect_no_source) == 0


def test_list_releases_reports_published_releases(
    tmp_path: Path, adapter_with_endpoint, capsys: pytest.CaptureFixture[str]
) -> None:
    crawl_args = argparse.Namespace(
        dataset="un_sc_consolidated",
        all=False,
        work=tmp_path / "work",
        output=tmp_path / "out",
        schemas=cli_module._repository_root() / "schemas",
        timeout=5.0,
        retries=1,
    )
    cli_module._crawl(crawl_args)
    capsys.readouterr()

    list_args = argparse.Namespace(output=tmp_path / "out", dataset=None)
    assert cli_module._list_releases(list_args) == 0
    releases = json.loads(capsys.readouterr().out)
    assert len(releases) == 1
    assert releases[0]["datasets"] == ["un_sc_consolidated"]
    assert releases[0]["entity_count"] == 2

    filtered = argparse.Namespace(output=tmp_path / "out", dataset="nonexistent_dataset")
    assert cli_module._list_releases(filtered) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_compare_releases_reports_added_modified_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Two distinct fixture bodies, published to two distinct output roots so they can be
    # compared directly by path without needing the pointer/diff machinery in between.
    original = Path("tests/fixtures/un_sc_consolidated/synthetic.xml").read_text()
    changed = original.replace("Ama", "Ama-Changed").replace("SYN-ORG-001", "SYN-ORG-002")

    def _publish(body: str, out_dir: Path) -> None:
        handler = type(
            "_H",
            (_FixtureHandler,),
            {"body": body.encode()},
        )
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = httpd.server_address[:2]
            adapter = copy.copy(real_un_adapter)
            adapter.metadata = dataclasses.replace(
                real_un_adapter.metadata, source_endpoint=f"http://{host}:{port}/x.xml"
            )
            monkeypatch.setattr(run_module, "load_adapter", lambda key: adapter)  # noqa: ARG005
            args = argparse.Namespace(
                dataset="un_sc_consolidated",
                all=False,
                work=out_dir / "work",
                output=out_dir / "out",
                schemas=cli_module._repository_root() / "schemas",
                timeout=5.0,
                retries=1,
            )
            cli_module._crawl(args)
        finally:
            httpd.shutdown()
            thread.join()

    _publish(original, tmp_path / "old")
    _publish(changed, tmp_path / "new")
    capsys.readouterr()

    old_release = next((tmp_path / "old" / "out").glob("release-*"))
    new_release = next((tmp_path / "new" / "out").glob("release-*"))

    args = argparse.Namespace(old_release=old_release, new_release=new_release, detail=True)
    assert cli_module._compare_releases(args) == 0
    result = json.loads(capsys.readouterr().out)
    # SYN-ORG-001 removed, SYN-ORG-002 added, the individual record modified (name changed).
    assert result["summary"]["removed"] == 1
    assert result["summary"]["added"] == 1
    assert result["summary"]["modified"] == 1
    assert {row["entity_id"] for row in result["removed"]} != {row["entity_id"] for row in result["added"]}
