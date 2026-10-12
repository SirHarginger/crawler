"""Experiment B: fetch and extraction reliability on a frozen list of public URLs.

No search provider is involved. Each URL in `fetch_urls.json` goes through the same governed
path as any research fetch: a lead token, the `ResearchGateway`, the HTML-only gate and the
isolated parser. The run also counts every connection this process makes and checks each one
against what the gateway validated.

It keeps URLs (public, from the frozen list), outcome codes, sizes and counts. It keeps no page
text.

  python tools/research_eval/fetch_eval.py --live --write tools/research_eval/results/fetch-dev-v1.json

The list is frozen and a result is never overwritten. A later run of the same list goes to a
new file and states why with `--rerun-reason`.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import pathlib
import socket
import sys
import time
from typing import Any

from screening_crawler.research import gateway as gateway_module
from screening_crawler.research.egress import EgressRefused, ResearchEgress
from screening_crawler.research.gateway import ResearchGateway
from screening_crawler.research.query import ResearchQuery
from screening_crawler.research.search import SearchLead

HERE = pathlib.Path(__file__).resolve().parent
DATASET = HERE / "fetch_urls.json"
UNSUPPORTED_REASONS = ("js_required", "consent_wall")
# A fixed, non-personal query object: the egress needs one to issue leads; nothing is searched.
_PLACEHOLDER = ResearchQuery(subject_kind="PUBLIC_INTEREST_ORGANISATION",
                             name="Frozen fetch list")


class FrozenList:
    """A `SearchProvider` that returns the frozen URLs. It contacts nothing."""

    name = "frozen-list"

    def __init__(self, urls: list[str]) -> None:
        self.urls = urls

    def search(self, query: ResearchQuery, *, limit: int) -> list[SearchLead]:
        return [SearchLead(url, rank, self.name) for rank, url in enumerate(self.urls, start=1)]


class NetworkLedger:
    """Every address the gateway validated, and every connection this process attempted."""

    def __init__(self) -> None:
        self.validated: set[str] = set()
        self.connections: list[str] = []
        self._connect = socket.socket.connect

    def resolver(self, host: str, port: int) -> list[str]:
        addresses = gateway_module._system_resolver(host, port)
        self.validated.update(addresses)
        return addresses

    def install(self) -> None:
        ledger = self

        def connect(sock: socket.socket, address: Any) -> Any:
            if isinstance(address, tuple) and address and isinstance(address[0], str):
                ledger.connections.append(address[0])
            return ledger._connect(sock, address)

        socket.socket.connect = connect  # type: ignore[method-assign,assignment]

    def uninstall(self) -> None:
        socket.socket.connect = self._connect  # type: ignore[method-assign]

    def summary(self) -> dict[str, Any]:
        def public(address: str) -> bool:
            try:
                return gateway_module._is_public_address(address)
            except ValueError:
                return False

        return {
            "connections": len(self.connections),
            "distinct_addresses": len(set(self.connections)),
            "network_policy_violations": sum(not public(a) for a in self.connections),
            "ungoverned_outbound_requests": sum(a not in self.validated
                                                for a in self.connections),
        }


def classify(entry: dict[str, Any], outcome: str) -> str:
    if entry["format"] != "html":
        return "unsupported_format"
    if outcome == "ok":
        return "extracted"
    if outcome in UNSUPPORTED_REASONS:
        return outcome
    if outcome in ("no_article_text", "not_a_content_page"):
        return "extraction_failed"
    if outcome == "parser_network_attempt":
        return "parser_network_attempt"
    return "fetch_failed"


def evaluate(egress: ResearchEgress, urls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    egress.provider = FrozenList([u["url"] for u in urls])
    leads = egress.search(_PLACEHOLDER, limit=len(urls))
    rows = []
    for entry, lead in zip(urls, leads, strict=True):
        started = time.monotonic()
        row: dict[str, Any] = {"id": entry["id"], "category": entry["category"],
                               "format": entry["format"], "host": lead.host}
        try:
            document = egress.fetch(lead.lead_token)
            row["outcome"] = "ok"
            row["bytes"] = document.byte_size
            row["text_chars"] = len(document.text)
            row["extractor"] = document.extractor_version
            row["has_published_date"] = document.published_date is not None
            row["redirected_to_another_host"] = document.host != lead.host
            mention = entry.get("expected_mention")
            if mention:
                row["mention_recovered"] = (
                    " ".join(mention.casefold().split())
                    in " ".join((document.headline + " " + document.text).casefold().split()))
        except EgressRefused as exc:
            row["outcome"] = exc.reason
        row["class"] = classify(entry, row["outcome"])
        row["seconds"] = round(time.monotonic() - started, 1)
        rows.append(row)
    return rows


def summarise(rows: list[dict[str, Any]], network: dict[str, Any],
              gates: dict[str, Any]) -> dict[str, Any]:
    def rate(k: int, n: int) -> dict[str, Any]:
        return {"k": k, "n": n, "rate": round(k / n, 3) if n else None}

    eligible = [r for r in rows if r["format"] == "html"]
    fetched = [r for r in eligible if r["class"] not in ("fetch_failed",)]
    supported = [r for r in fetched if r["class"] not in UNSUPPORTED_REASONS]
    extracted = [r for r in supported if r["class"] == "extracted"]
    labelled = [r for r in extracted if "mention_recovered" in r]
    parser_attempts = sum(r["class"] == "parser_network_attempt" for r in rows)
    outcomes: dict[str, int] = {}
    for r in rows:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
    metrics = {
        "eligible_fetch_success": rate(len(fetched), len(eligible)),
        "usable_extraction": rate(len(extracted), len(supported)),
        "subject_mention_recovered": rate(sum(r["mention_recovered"] for r in labelled),
                                          len(labelled)),
        "network_policy_violations": network["network_policy_violations"],
        "ungoverned_outbound_requests": network["ungoverned_outbound_requests"]
        + parser_attempts,
    }

    def at_least(name: str) -> bool:
        value = metrics[name]["rate"]
        return value is not None and value >= gates[f"{name}_min"]

    return {
        "urls": len(rows), "eligible": len(eligible),
        "metrics": metrics,
        "reported_separately": {
            "unsupported_format_by_label": [
                {"id": r["id"], "format": r["format"], "outcome": r["outcome"]}
                for r in rows if r["format"] != "html"],
            "js_required": [r["id"] for r in rows if r["class"] == "js_required"],
            "consent_wall": [r["id"] for r in rows if r["class"] == "consent_wall"],
            "robots_disallowed": [r["id"] for r in rows if r["outcome"] == "robots_disallowed"],
            # Structured text is lower precision than article text; never read the two as one.
            "extracted_by": {name: sorted(r["id"] for r in extracted if r.get("extractor") == name)
                             for name in sorted({r.get("extractor", "") for r in extracted})}},
        "outcomes": dict(sorted(outcomes.items())),
        "pages_with_a_published_date": rate(sum(r.get("has_published_date", False)
                                                for r in extracted), len(extracted)),
        "median_seconds_per_page": sorted(r["seconds"] for r in rows)[len(rows) // 2],
        "network": network,
        "gate": {
            "eligible_fetch_success": at_least("eligible_fetch_success"),
            "usable_extraction": at_least("usable_extraction"),
            "subject_mention_recovered": at_least("subject_mention_recovered"),
            "network_policy_violations": metrics["network_policy_violations"]
            <= gates["network_policy_violations_max"],
            "ungoverned_outbound_requests": metrics["ungoverned_outbound_requests"]
            <= gates["ungoverned_outbound_requests_max"]},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="really fetch the frozen URLs")
    parser.add_argument("--write", type=pathlib.Path)
    parser.add_argument("--rerun-reason", default="",
                        help="why the frozen list is being run again; kept in the result")
    args = parser.parse_args()
    if not args.live:
        print("needs --live; nothing was fetched", file=sys.stderr)
        return 2
    if any(name.startswith("RESEARCH_EXA") for name in os.environ):
        print("a provider credential is set; this experiment must run without one",
              file=sys.stderr)
        return 2
    if args.write and args.write.exists():
        print("a result already exists for this frozen list; a rerun needs a recorded reason "
              "and a new file", file=sys.stderr)
        return 2
    raw = DATASET.read_bytes()
    dataset = json.loads(raw)
    ledger = NetworkLedger()
    egress = ResearchEgress(provider=FrozenList([]), lead_secret=os.urandom(32),
                            gateway=ResearchGateway(resolver=ledger.resolver))
    for address in ipaddress.ip_network("127.0.0.0/30"):  # never legitimately contacted
        assert str(address) not in ledger.validated
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    ledger.install()
    try:
        rows = evaluate(egress, dataset["urls"])
    finally:
        ledger.uninstall()
    summary = summarise(rows, ledger.summary(), dataset["gates"])
    result = {"dataset": dataset["dataset"], "version": dataset["version"],
              "dataset_sha256": hashlib.sha256(raw).hexdigest(), "started": started,
              "finished": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "search_provider_contacted": False, "rerun_reason": args.rerun_reason,
              "summary": summary,
              "passed": all(summary["gate"].values()), "rows": rows}
    print(json.dumps({"summary": summary, "passed": result["passed"]}, indent=2))
    if args.write:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
