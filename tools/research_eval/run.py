"""DEV evaluation of the research egress on public figures and organisations (experiments A and
B in the screening engine's docs/research/governed-live-evidence-and-disambiguation.md).

A: does the search provider return leads on official or credible hosts?
B: do those pages fetch and yield text that mentions the subject?

It runs here, on the egress side, because this is the only place live search and fetches are
allowed. It keeps hosts, ranks, outcome codes and counts. It keeps no title, snippet, page text
or provider response, and prints no query.

NOT APPROVED TO RUN against Exa's standard terms: the subject list names people, and Exa's
privacy policy says query data must not contain personal information. The script refuses
unless the deployment declares that terms covering named-person queries are in place
(RESEARCH_EXA_NAMED_PERSON_TERMS_APPROVED=true), as well as --live and a key.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
from typing import Any

from screening_crawler.research.egress import EgressRefused, ResearchEgress
from screening_crawler.research.query import ResearchQuery
from screening_crawler.research.search import ExaSearchProvider
from screening_crawler.research.source_policy import (
    SourcePolicyInvalid,
    parse_domains,
    policy_gateway,
)

HERE = pathlib.Path(__file__).resolve().parent
QUERY_FIELDS = ("subject_kind", "name", "role", "organisation", "country", "birth_year")


def on_host(host: str, hosts: list[str]) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def evaluate_subject(egress: ResearchEgress, subject: dict[str, Any],
                     credible: list[str]) -> dict[str, Any]:
    query = ResearchQuery(**{k: subject[k] for k in QUERY_FIELDS if k in subject})
    row: dict[str, Any] = {"id": subject["id"], "query_sha256": query.sha256(),
                           "search": "ok", "leads": []}
    try:
        leads = egress.search(query, provider=egress.provider.name, limit=5)
    except EgressRefused as exc:
        row["search"] = exc.reason
        return row
    name = " ".join(query.name.casefold().split())
    for lead in leads:
        official = on_host(lead.host, subject.get("official_hosts", []))
        entry: dict[str, Any] = {"rank": lead.rank, "host": lead.host, "official": official,
                                 "relevant": official or on_host(lead.host, credible)}
        try:
            document = egress.fetch(lead.lead_token)
            entry["fetch"] = "ok"
            entry["mentions_subject"] = name in " ".join(document.text.casefold().split())
            entry["has_published_date"] = document.published_date is not None
        except EgressRefused as exc:
            entry["fetch"] = exc.reason
        row["leads"].append(entry)
    return row


def summarise(rows: list[dict[str, Any]], subjects: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {s["id"]: s for s in subjects}
    searched = [r for r in rows if r["search"] == "ok"]
    leads = [lead for r in searched for lead in r["leads"]]
    relevant = [lead for lead in leads if lead["relevant"]]
    with_official = [r for r in searched if by_id[r["id"]].get("official_hosts")]

    def rate(k: int, n: int) -> dict[str, Any]:
        return {"k": k, "n": n, "rate": round(k / n, 3) if n else None}

    outcomes: dict[str, int] = {}
    for lead in leads:
        outcomes[lead["fetch"]] = outcomes.get(lead["fetch"], 0) + 1
    return {
        "subjects": len(rows),
        "search_failure_rate": rate(len(rows) - len(searched), len(rows)),
        "relevant_lead_rate": rate(sum(any(x["relevant"] for x in r["leads"])
                                       for r in searched), len(searched)),
        "official_lead_rate": rate(sum(any(x["official"] for x in r["leads"])
                                       for r in with_official), len(with_official)),
        "relevant_lead_fetch_and_mention_rate": rate(
            sum(x["fetch"] == "ok" and x.get("mentions_subject", False) for x in relevant),
            len(relevant)),
        "fetch_outcomes": dict(sorted(outcomes.items())),
        "leads_per_search": round(len(leads) / len(searched), 2) if searched else None,
    }


def gate(summary: dict[str, Any], requirements: dict[str, Any]) -> dict[str, bool]:
    """The owner's two pilot gates. Official-source Hit@5 and the fetch-and-mention rate are
    reported, on their own eligible subsets, and are not gates."""
    failure = summary["search_failure_rate"]["rate"]
    relevant = summary["relevant_lead_rate"]["rate"]
    return {
        "api_technical_success": failure is not None
        and 1 - failure >= requirements["api_technical_success_min"],
        "relevant_lead_hit_at_5": relevant is not None
        and relevant >= requirements["relevant_lead_hit_at_5_min"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true",
                        help="really call the search provider and fetch pages")
    parser.add_argument("--write", type=pathlib.Path)
    parser.add_argument("--source-domains", default="",
                        help="the approved source domains, comma-separated (source_policy.py)")
    args = parser.parse_args()
    key = os.environ.get("RESEARCH_EXA_API_KEY", "")
    approved = os.environ.get("RESEARCH_EXA_NAMED_PERSON_TERMS_APPROVED") == "true"
    if not args.live or not key or not approved:
        print("not run: needs --live, RESEARCH_EXA_API_KEY and "
              "RESEARCH_EXA_NAMED_PERSON_TERMS_APPROVED=true (named-person search is not "
              "approved under Exa's standard terms); nothing was sent", file=sys.stderr)
        return 2
    try:
        domains = parse_domains(args.source_domains)
    except SourcePolicyInvalid as exc:
        print(f"refused: {exc}; nothing was sent", file=sys.stderr)
        return 2
    dataset = json.loads((HERE / "subjects.json").read_text())
    egress = ResearchEgress(
        provider=ExaSearchProvider(api_key=key, named_person_terms_approved=True),
        gateway=policy_gateway(domains), lead_secret=os.urandom(32))
    rows = [evaluate_subject(egress, s, dataset["credible_hosts"]) for s in dataset["subjects"]]
    summary = summarise(rows, dataset["subjects"])
    result = {"dataset": dataset["dataset"], "version": dataset["version"],
              "provider": "exa", "run_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "requirements_status": dataset["requirements_status"],
              "summary": summary, "gate": gate(summary, dataset["requirements"]), "rows": rows}
    print(json.dumps({k: result[k] for k in ("summary", "gate")}, indent=2))
    if args.write:
        args.write.write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
