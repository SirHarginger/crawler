"""End-to-end run-engine coverage against a loopback server, never a live publisher (matches
tests/test_cli_crawl.py's existing pattern for the lower-level `crawl-dataset` command).
"""

from __future__ import annotations

import copy
import dataclasses
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import screening_crawler.run as run_module
from datasets.un_sc_consolidated.crawler import adapter as real_un_adapter
from screening_crawler.locking import RunAlreadyInProgress, run_lock
from screening_crawler.run import CrawlRunStore, exit_code_for, run_source


class _FixtureHandler(BaseHTTPRequestHandler):
    body: bytes
    etag: str = "synthetic-etag"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.headers.get("If-None-Match") == self.etag:
            self.send_response(304)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("ETag", self.etag)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)


def _serve(body: bytes, *, etag: str = "synthetic-etag") -> ThreadingHTTPServer:
    handler = type("_BoundFixtureHandler", (_FixtureHandler,), {"body": body, "etag": etag})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    httpd._thread = thread  # type: ignore[attr-defined]
    return httpd


def _url(httpd: ThreadingHTTPServer) -> str:
    host, port = httpd.server_address[:2]
    return f"http://{host}:{port}/consolidated.xml"


@pytest.fixture
def un_source_server() -> Iterator[ThreadingHTTPServer]:
    body = Path("tests/fixtures/un_sc_consolidated/synthetic.xml").read_bytes()
    httpd = _serve(body)
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd._thread.join()  # type: ignore[attr-defined]


@pytest.fixture
def adapter_with_endpoint(un_source_server: ThreadingHTTPServer, monkeypatch: pytest.MonkeyPatch):
    adapter = copy.copy(real_un_adapter)
    adapter.metadata = dataclasses.replace(real_un_adapter.metadata, source_endpoint=_url(un_source_server))
    monkeypatch.setattr(run_module, "load_adapter", lambda key: adapter)  # noqa: ARG005
    return adapter


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_first_crawl_publishes_and_reaches_published_state(tmp_path: Path, adapter_with_endpoint) -> None:
    run_store = CrawlRunStore(tmp_path / "work" / "runs")
    run = run_source(
        "un_sc_consolidated",
        work=tmp_path / "work",
        output=tmp_path / "out",
        schemas=_repo_root() / "schemas",
        repository_root=_repo_root(),
        run_store=run_store,
        timeout=5.0,
        retries=1,
    )
    assert run.state == "PUBLISHED"
    assert run.release_id is not None
    assert exit_code_for(run) == 0
    states = [t.state for t in run.transitions]
    assert states == [
        "SCHEDULED",
        "FETCHING",
        "FETCHED",
        "PARSING",
        "NORMALIZING",
        "VALIDATING",
        "DIFFING",
        "READY",
        "PUBLISHED",
    ]
    release_dir = tmp_path / "out" / run.release_id
    assert (release_dir / "manifest.json").is_file()
    # Sidecar provenance, not part of the strict release contract (see run.py's docstring).
    sidecar = json.loads((release_dir / "crawl_run.json").read_text())
    assert sidecar["run_id"] == run.run_id
    assert sidecar["state"] == "PUBLISHED"


def test_run_is_persisted_and_inspectable_via_the_store(tmp_path: Path, adapter_with_endpoint) -> None:
    run_store = CrawlRunStore(tmp_path / "work" / "runs")
    run = run_source(
        "un_sc_consolidated",
        work=tmp_path / "work",
        output=tmp_path / "out",
        schemas=_repo_root() / "schemas",
        repository_root=_repo_root(),
        run_store=run_store,
        timeout=5.0,
        retries=1,
    )
    reloaded = CrawlRunStore(tmp_path / "work" / "runs").load("un_sc_consolidated", run.run_id)
    assert reloaded.to_dict() == run.to_dict()
    assert [r.run_id for r in run_store.list_runs("un_sc_consolidated")] == [run.run_id]
    assert run_store.last_successful("un_sc_consolidated").run_id == run.run_id


def test_second_crawl_with_matching_etag_is_unchanged(tmp_path: Path, un_source_server, monkeypatch) -> None:
    adapter = copy.copy(real_un_adapter)
    adapter.metadata = dataclasses.replace(real_un_adapter.metadata, source_endpoint=_url(un_source_server))
    monkeypatch.setattr(run_module, "load_adapter", lambda key: adapter)  # noqa: ARG005
    run_store = CrawlRunStore(tmp_path / "work" / "runs")
    kwargs = dict(
        work=tmp_path / "work",
        output=tmp_path / "out",
        schemas=_repo_root() / "schemas",
        repository_root=_repo_root(),
        run_store=run_store,
        timeout=5.0,
        retries=1,
    )
    first = run_source("un_sc_consolidated", **kwargs)
    assert first.state == "PUBLISHED"
    second = run_source("un_sc_consolidated", **kwargs)
    assert second.state == "UNCHANGED"
    assert exit_code_for(second) == 0
    # No second release was built (".pointers" is the run engine's own last-successful-release
    # pointer directory, not a release -- excluded here, not a bug).
    releases = [p for p in (tmp_path / "out").iterdir() if p.name != ".pointers"]
    assert len(releases) == 1


def test_catastrophic_shrink_is_quarantined_not_published(tmp_path: Path, monkeypatch) -> None:
    """Publish a normal first release, then simulate a second crawl whose source has almost
    nothing left in it -- must quarantine, not publish, per the doc's own "previous=700,
    new=12" example.
    """
    first_body = Path("tests/fixtures/un_sc_consolidated/synthetic.xml").read_bytes()
    httpd = _serve(first_body, etag="etag-1")
    run_store = CrawlRunStore(tmp_path / "work" / "runs")
    try:
        adapter = copy.copy(real_un_adapter)
        adapter.metadata = dataclasses.replace(
            real_un_adapter.metadata, source_endpoint=_url(httpd), anomaly_threshold=0.15
        )
        monkeypatch.setattr(run_module, "load_adapter", lambda key: adapter)  # noqa: ARG005
        first = run_source(
            "un_sc_consolidated",
            work=tmp_path / "work",
            output=tmp_path / "out",
            schemas=_repo_root() / "schemas",
            repository_root=_repo_root(),
            run_store=run_store,
            timeout=5.0,
            retries=1,
        )
        assert first.state == "PUBLISHED"
    finally:
        httpd.shutdown()
        httpd._thread.join()  # type: ignore[attr-defined]

    # Second source: an XML document with zero records under both INDIVIDUALS and ENTITIES --
    # the largest possible shrink from a real prior release (2 entities -> 0).
    empty_body = b'<?xml version="1.0" encoding="UTF-8"?><CONSOLIDATED_LIST><INDIVIDUALS/><ENTITIES/></CONSOLIDATED_LIST>'
    httpd2 = _serve(empty_body, etag="etag-2")
    try:
        adapter2 = copy.copy(real_un_adapter)
        adapter2.metadata = dataclasses.replace(
            real_un_adapter.metadata, source_endpoint=_url(httpd2), anomaly_threshold=0.15
        )
        monkeypatch.setattr(run_module, "load_adapter", lambda key: adapter2)  # noqa: ARG005
        second = run_source(
            "un_sc_consolidated",
            work=tmp_path / "work",
            output=tmp_path / "out",
            schemas=_repo_root() / "schemas",
            repository_root=_repo_root(),
            run_store=run_store,
            timeout=5.0,
            retries=1,
        )
    finally:
        httpd2.shutdown()
        httpd2._thread.join()  # type: ignore[attr-defined]

    # A drop this severe (2 records -> 0) is actually caught by two independent gates: the
    # pre-existing, cheaper source-record-volume check inside pipeline.process_snapshot (which
    # this run engine now feeds `previous_source_record_count` from the prior release's own
    # manifest) fires before this milestone's new release-level entity diff gate ever gets a
    # chance to -- both map to "did not publish", which is what actually matters operationally
    # (doc requirement: "must NOT automatically publish"), so either terminal state is a pass
    # here. test_diff.py::test_statistical_gate_rejects_catastrophic_shrink exercises the new
    # diff-level gate directly and in isolation, where it's the only thing that can fire.
    assert second.state in {"QUARANTINED", "VALIDATION_FAILED"}
    assert exit_code_for(second) in {3, 4}
    assert second.release_id is None
    # Still exactly one published release -- the failed attempt never became a second one.
    releases = [p for p in (tmp_path / "out").iterdir() if p.name != ".pointers"]
    assert len(releases) == 1


def test_duplicate_run_is_rejected_while_lock_is_held(tmp_path: Path, adapter_with_endpoint) -> None:
    lock_dir = tmp_path / "work" / "locks"
    with run_lock(lock_dir, "un_sc_consolidated"), pytest.raises(RunAlreadyInProgress):
        run_source(
            "un_sc_consolidated",
            work=tmp_path / "work",
            output=tmp_path / "out",
            schemas=_repo_root() / "schemas",
            repository_root=_repo_root(),
            run_store=CrawlRunStore(tmp_path / "work" / "runs"),
            timeout=5.0,
            retries=1,
        )


def test_fetch_failure_is_a_terminal_state_not_an_exception(tmp_path: Path, monkeypatch) -> None:
    adapter = copy.copy(real_un_adapter)
    # Nothing listens on this port -- a real connection failure, not a mocked one.
    adapter.metadata = dataclasses.replace(real_un_adapter.metadata, source_endpoint="http://127.0.0.1:1/consolidated.xml")
    monkeypatch.setattr(run_module, "load_adapter", lambda key: adapter)  # noqa: ARG005
    run = run_source(
        "un_sc_consolidated",
        work=tmp_path / "work",
        output=tmp_path / "out",
        schemas=_repo_root() / "schemas",
        repository_root=_repo_root(),
        run_store=CrawlRunStore(tmp_path / "work" / "runs"),
        timeout=1.0,
        retries=0,
    )
    assert run.state == "FETCH_FAILED"
    assert exit_code_for(run) == 6
    assert run.error_code == "FETCH_FAILED"
    assert not (tmp_path / "out").exists()


def test_error_messages_are_sanitized_of_query_string_credentials() -> None:
    from screening_crawler.run import sanitize_error

    message = "connection failed for https://example.invalid/list.xml?token=SECRET123&other=1"
    sanitized = sanitize_error(message)
    assert "SECRET123" not in sanitized
    assert "token=REDACTED" in sanitized
