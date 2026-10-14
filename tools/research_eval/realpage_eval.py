"""E1.1: how good is extracted evidence on real pages that a person has labelled?

Four steps, each its own command, in this order:

  select    choose the pages by the frozen rules in `realpage_protocol.json` (seeded; fetches
            the registered listings and the carried-over pages' links through the gateway)
  snapshot  fetch every selected page once through the gateway and keep the bytes OUTSIDE the
            repository; the manifest keeps URL, time, status and hashes
  dump      write a plain rendering of each snapshot for the annotator, made without the
            extractor under test, also outside the repository
  score     replay the snapshots through the real egress and score them against the labels

Third-party page bodies are never written into the repository. The manifest and the labels hold
URLs, hashes and short decisive segments; a result holds counts and segment indexes.

  python tools/research_eval/realpage_eval.py select --live
  python tools/research_eval/realpage_eval.py snapshot --live --snapshots ~/trustora-e11-snapshots
  python tools/research_eval/realpage_eval.py dump --snapshots ~/trustora-e11-snapshots
  python tools/research_eval/realpage_eval.py score --snapshots ~/trustora-e11-snapshots \\
      --write tools/research_eval/results/e11-realpage-dev-v1.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import random
import re
import sys
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

from screening_crawler.research import extract as extract_module
from screening_crawler.research.egress import EgressRefused, ResearchEgress
from screening_crawler.research.gateway import FetchResult, GatewayRejected, ResearchGateway
from screening_crawler.research.query import ResearchQuery
from screening_crawler.research.search import SearchFailure, SearchLead, require_approval
from screening_crawler.research.source_policy import (
    SourcePolicyInvalid,
    parse_domains,
    policy_gateway,
)
from screening_crawler.research.structured_text import STRUCTURED_EXTRACTOR_VERSION

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PROTOCOL = HERE / "realpage_protocol.json"
MANIFEST = HERE / "realpage_cases.json"
LABELS = HERE / "realpage_labels.json"
JUDGEMENTS = HERE / "realpage_judgements.json"
FINGERPRINTED = ("src/screening_crawler/research/extract.py",
                 "src/screening_crawler/research/structured_text.py",
                 "src/screening_crawler/research/page_text.py",
                 "src/screening_crawler/research/egress.py",
                 "tools/research_eval/realpage_eval.py")
MAX_BYTES = 2 * 1024 * 1024
UNSUPPORTED_OUTCOMES = ("js_required", "consent_wall", "unsupported_content_type", "too_large",
                        "http_error", "robots_disallowed", "timeout", "empty_body")
PAGE_TYPES = ("article", "profile", "list_index", "table", "landing", "unsupported_other")
ROUTES = ("article", "structured", "refusal")
FORBIDDEN_KINDS = ("navigation", "cookie_banner", "footer_legal", "related_item", "hidden",
                   "control", "other")
SEVERITIES = ("NONE", "LOW", "MATERIAL")
CATASTROPHIC = ("wrong_item", "hidden_as_evidence", "empty_despite_content")
_NOT_A_PAGE = re.compile(r"\.(pdf|jpe?g|png|gif|svg|webp|zip|docx?|xlsx?|pptx?|mp[34]|xml|json|"
                         r"css|js|ico)$", re.IGNORECASE)
_NOT_AN_ARTICLE = ("/category/", "/tag/", "/author/", "/feed", "/wp-json", "/wp-content",
                   "/page/", "/search")
_PLACEHOLDER = ResearchQuery(subject_kind="PUBLIC_INTEREST_ORGANISATION", name="Frozen page list")


def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint() -> dict[str, str]:
    """What exactly is being measured: the extractor's files, its versions, this script."""
    return {name: sha256_file(ROOT / name) for name in FINGERPRINTED} | {
        "article_extractor_version": extract_module.ARTICLE_EXTRACTOR_VERSION,
        "structured_extractor_version": STRUCTURED_EXTRACTOR_VERSION}


def fold(text: str) -> str:
    """A segment and a text as they are compared: case and spacing aside."""
    return " ".join(text.split()).casefold()


# --- select -----------------------------------------------------------------------------------


def links_in(body: bytes, base_url: str) -> list[str]:
    """Every absolute http(s) URL a listing or page names, in href attributes, JSON or XML."""
    text = body.decode("utf-8", "replace").replace("\\/", "/")
    found = set(re.findall(r"https?://[^\s\"'<>\\)]+", text))
    for href in re.findall(r"href=[\"']([^\"'#]+)", text):
        try:
            found.add(urljoin(base_url, href))
        except ValueError:  # a listing is hostile input: a malformed link is not a candidate
            continue
    usable = set()
    for url in found:
        try:
            urlsplit(url)
        except ValueError:
            continue
        usable.add(urldefrag(url.rstrip(".,"))[0])
    return sorted(usable)


def same_site(url: str, host: str) -> bool:
    name = (urlsplit(url).hostname or "").lower()
    return name.removeprefix("www.") == host.lower().removeprefix("www.")


def is_article_link(url: str) -> bool:
    """Slug-shaped: a last path segment of at least four hyphenated words, not a listing."""
    path = urlsplit(url).path
    last = path.rstrip("/").rsplit("/", 1)[-1]
    return (last.count("-") >= 3 and not _NOT_A_PAGE.search(path)
            and not any(part in path for part in _NOT_AN_ARTICLE) and not urlsplit(url).query)


def is_page_link(url: str) -> bool:
    parts = urlsplit(url)
    return (parts.scheme in ("http", "https") and not _NOT_A_PAGE.search(parts.path)
            and parts.path not in ("", "/") and not any(p in parts.path for p in _NOT_AN_ARTICLE))


def pick(candidates: list[str], count: int, seed: str, taken: set[str]) -> list[str]:
    pool = sorted(set(candidates) - taken)
    return random.Random(seed).sample(pool, min(count, len(pool)))  # noqa: S311 -- reproducible


def select(protocol: dict[str, Any], gateway: ResearchGateway, pace: float = 2.5
           ) -> list[dict[str, Any]]:
    seed = protocol["sampling"]["seed"]
    carried = json.loads((HERE / "fetch_urls.json").read_text(encoding="utf-8"))["urls"]
    cases = [{"id": f"f-{entry['id']}", "stratum": "carried_over", "url": entry["url"],
              "category": entry["category"], "selected_by": "fetch-dev-v1, frozen 2026-10-11",
              "read_during_e1": entry["id"] in protocol["sampling"]["read_during_e1"]}
             for entry in carried]
    taken = {case["url"] for case in cases}

    def listing(url: str) -> bytes | None:
        time.sleep(pace)
        try:
            return bytes(gateway.fetch(url, max_bytes=MAX_BYTES).body)
        except GatewayRejected:
            return None

    for rule in protocol["sampling"]["fresh"]:
        body = listing(rule["listing_url"])
        host = rule["host"]
        keep = is_article_link if rule["kind"] == "news" else is_page_link
        candidates = [] if body is None else [
            url for url in links_in(body, rule["listing_url"])
            if same_site(url, host) and keep(url)
            and all(word in urlsplit(url).path.lower() for word in rule.get("path_contains", []))]
        chosen = pick(candidates, rule["count"], f"{seed}|{rule['id']}", taken)
        taken.update(chosen)
        for n, url in enumerate(chosen, start=1):
            cases.append({"id": f"{rule['id']}-{n}", "stratum": rule["stratum"], "url": url,
                          "category": rule["category"], "read_during_e1": False,
                          "selected_by": f"seeded sample of {len(candidates)} candidate links "
                                         f"from {rule['listing_url']}"})
        if not chosen:
            cases.append({"id": f"{rule['id']}-0", "stratum": rule["stratum"], "url": None,
                          "category": rule["category"], "read_during_e1": False,
                          "selected_by": "no candidate: the listing could not be fetched or "
                                         "named no page that fits the rule"})
    return cases


# --- snapshot ---------------------------------------------------------------------------------


def snapshot(cases: list[dict[str, Any]], gateway: ResearchGateway, directory: pathlib.Path,
             pace: float = 2.5) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    last: dict[str, float] = {}
    for case in cases:
        if not case["url"] or "fetch" in case:
            continue
        host = (urlsplit(case["url"]).hostname or "").lower()
        wait = pace - (time.monotonic() - last.get(host, float("-inf")))
        if wait > 0:
            time.sleep(wait)
        try:
            fetched = gateway.fetch(case["url"], max_bytes=MAX_BYTES)
        except GatewayRejected as exc:
            case["fetch"] = {"outcome": exc.reason,
                             "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds")}
        else:
            (directory / f"{case['id']}.bin").write_bytes(fetched.body)
            case["fetch"] = {
                "outcome": "ok", "final_url": fetched.final_url, "status": fetched.status,
                "declared_content_type": fetched.declared_content_type,
                "retrieved_at": fetched.retrieved_at.isoformat(timespec="seconds"),
                "content_sha256": hashlib.sha256(fetched.body).hexdigest(),
                "bytes": len(fetched.body)}
        last[host] = time.monotonic()


# --- dump: the annotator's view, made without the extractor under test -------------------------


def plain_rendering(body: bytes) -> str:
    """Every text node of the page in document order, scripts and styles aside, one block a
    line, with the elements the markup marks hidden shown as such. Deliberately naive: it is
    what the annotator reads to find segments, and must not share the extractor's judgement."""
    from lxml import html as lxml_html

    tree = lxml_html.document_fromstring(
        body, parser=lxml_html.HTMLParser(encoding="utf-8", no_network=True, huge_tree=False))
    for element in list(tree.iter("script", "style")):
        element.drop_tree()
    lines = [f"TITLE: {' '.join((tree.findtext('.//title') or '').split())}"]
    for element in tree.iter():
        if not isinstance(element.tag, str):
            continue
        marked = ("hidden" in element.attrib or element.tag == "template"
                  or element.get("aria-hidden") == "true")
        for text in (element.text, element.tail):
            words = " ".join((text or "").split())
            if words:
                at_tail = text is element.tail
                tag = element.getparent().tag if at_tail and element.getparent() is not None \
                    else element.tag
                lines.append(f"[{'HIDDEN ' if marked and not at_tail else ''}{tag}] {words}")
    return "\n".join(lines)


# --- score ------------------------------------------------------------------------------------


class Replay:
    """A gateway that returns the stored snapshot for a URL, or the stored refusal. No network."""

    def __init__(self, cases: list[dict[str, Any]], directory: pathlib.Path) -> None:
        self.cases = {case["url"]: case for case in cases if case["url"]}
        self.directory = directory

    def fetch(self, url: str, *, max_bytes: int) -> FetchResult:
        case = self.cases[url]
        fetch = case["fetch"]
        if fetch["outcome"] != "ok":
            raise GatewayRejected(fetch["outcome"])
        body = (self.directory / f"{case['id']}.bin").read_bytes()
        if hashlib.sha256(body).hexdigest() != fetch["content_sha256"]:
            raise RuntimeError(f"snapshot for {case['id']} does not match the manifest hash")
        return FetchResult(
            requested_url=url, final_url=fetch["final_url"], status=fetch["status"],
            declared_content_type=fetch["declared_content_type"], body=body,
            content_sha256=fetch["content_sha256"], redirects=(), resolved_address="0.0.0.0",  # noqa: S104
            retrieved_at=datetime.fromisoformat(fetch["retrieved_at"]), elapsed_seconds=0.0)


class _Leads:
    name = "frozen-page-list"

    def __init__(self, urls: list[str]) -> None:
        self.urls = urls

    def search(self, query: ResearchQuery, *, limit: int) -> list[SearchLead]:
        return [SearchLead(url, rank, self.name) for rank, url in enumerate(self.urls, start=1)]


def extract_all(cases: list[dict[str, Any]], directory: pathlib.Path) -> dict[str, Any]:
    """Each page through the real egress: lead token, content-type gate, parsing child, refusals."""
    fetched = [case for case in cases if case["url"] and "fetch" in case]
    egress = ResearchEgress(provider=_Leads([case["url"] for case in fetched]),
                            gateway=Replay(fetched, directory),  # type: ignore[arg-type,unused-ignore]
                            lead_secret=os.urandom(32), min_seconds_between_host_requests=0.0,
                            max_searches_per_day=10)
    documents: dict[str, Any] = {}
    leads = egress.search(_PLACEHOLDER, provider=egress.provider.name,
                           limit=len(fetched)) if fetched else []
    for case, lead in zip(fetched, leads, strict=True):
        try:
            documents[case["id"]] = egress.fetch(lead.lead_token)
        except EgressRefused as exc:
            documents[case["id"]] = exc.reason
    return documents


def route_of(document: Any) -> str:
    if isinstance(document, str):
        return "refusal"
    return ("article" if document.extractor_version == extract_module.ARTICLE_EXTRACTOR_VERSION
            else "structured")


def score_case(case: dict[str, Any], label: dict[str, Any], document: Any) -> dict[str, Any]:
    route = route_of(document)
    row: dict[str, Any] = {
        "id": case["id"], "stratum": case["stratum"], "category": case["category"],
        "host": (urlsplit(case["url"]).hostname or "").lower(),
        "read_during_e1": case["read_during_e1"], "page_type": label["page_type"],
        "supported": label["supported"], "route": route,
        "outcome": document if route == "refusal" else "ok",
        "route_acceptable": route in label["acceptable_routes"],
        "annotation_status": label["annotation_status"]}
    expected, forbidden = label["expected"], label["forbidden"]
    critical = [n for n, segment in enumerate(expected) if segment["critical"]]
    if route == "refusal":
        return row | {"extractor": None, "text_chars": 0, "truncated": False,
                      "expected": len(expected), "expected_missing": list(range(len(expected))),
                      "critical": len(critical), "critical_missing": critical,
                      "forbidden": len(forbidden), "forbidden_found": [],
                      "hidden_found": 0, "hidden_labelled": 0, "has_published_date": False}
    whole = fold(f"{document.headline}\n{document.text}")
    missing = [n for n, segment in enumerate(expected) if fold(segment["text"]) not in whole]
    found = [n for n, segment in enumerate(forbidden) if fold(segment["text"]) in whole]
    return row | {
        "extractor": document.extractor_version, "text_chars": len(document.text),
        "truncated": document.text_truncated, "expected": len(expected),
        "expected_missing": missing, "critical": len(critical),
        "critical_missing": [n for n in missing if n in critical],
        "forbidden": len(forbidden), "forbidden_found": found,
        "hidden_found": sum(forbidden[n]["kind"] == "hidden" for n in found),
        "hidden_labelled": sum(segment["kind"] == "hidden" for segment in forbidden),
        "has_published_date": document.published_date is not None}


def rate(k: int, n: int) -> dict[str, Any]:
    return {"k": k, "n": n, "rate": round(k / n, 3) if n else None}


def aggregate(rows: list[dict[str, Any]], judgements: dict[str, Any]) -> dict[str, Any]:
    """Automated counts and, kept apart from them, what was judged from the output.

    A catastrophic failure is judged for every supported page, refused ones included: the
    protocol counts an empty result despite content as one. Contamination is judged only where
    text came back. An unsupported page that returned text all the same is listed by itself:
    it is in no rate, and must not disappear."""
    supported = [r for r in rows if r["supported"]]
    extracted = [r for r in supported if r["route"] != "refusal"]
    articles = [r for r in supported if r["page_type"] == "article"]
    judged = [judgements[r["id"]] for r in supported if r["id"] in judgements]
    read = [judgements[r["id"]] for r in extracted
            if judgements.get(r["id"], {}).get("contamination") is not None]
    structured = [r for r in extracted if r["route"] == "structured"
                  and judgements.get(r["id"], {}).get("contamination") is not None]
    stray = [r for r in rows if not r["supported"] and r["route"] != "refusal"]
    return {
        "automated": {
            "pages": len(rows), "supported_pages": len(supported),
            "unsupported_pages": rate(len(rows) - len(supported), len(rows)),
            "supported_pages_refused": rate(len(supported) - len(extracted), len(supported)),
            "expected_segments_recovered": rate(
                sum(r["expected"] - len(r["expected_missing"]) for r in supported),
                sum(r["expected"] for r in supported)),
            "critical_segments_recovered": rate(
                sum(r["critical"] - len(r["critical_missing"]) for r in supported),
                sum(r["critical"] for r in supported)),
            "page_success": rate(sum(not r["critical_missing"] and r["route"] != "refusal"
                                     for r in supported), len(supported)),
            "forbidden_segments_absent": rate(
                sum(r["forbidden"] - len(r["forbidden_found"]) for r in extracted),
                sum(r["forbidden"] for r in extracted)),
            "hidden_segments_found": sum(r["hidden_found"] for r in extracted),
            "hidden_segments_checked": sum(r["hidden_labelled"] for r in extracted),
            "route_acceptable": rate(sum(r["route_acceptable"] for r in rows), len(rows)),
            "routes": {route: sum(r["route"] == route for r in rows) for route in ROUTES},
            "articles_demoted": rate(sum(r["route"] == "structured" for r in articles),
                                     len(articles)),
            "articles_refused": rate(sum(r["route"] == "refusal" for r in articles),
                                     len(articles)),
            "truncated": sum(r["truncated"] for r in extracted),
            "unsupported_pages_that_returned_text": sorted(r["id"] for r in stray)},
        "judged_from_output": {
            "pages_judged": rate(len(judged), len(supported)),
            "texts_judged_for_contamination": rate(len(read), len(extracted)),
            "catastrophic": rate(sum(bool(j["catastrophic"]) for j in judged), len(judged)),
            "catastrophic_kinds": sorted(j["catastrophic"] for j in judged if j["catastrophic"]),
            "contamination": {s: sum(j["contamination"] == s for j in read)
                              for s in SEVERITIES},
            "material_contamination": rate(sum(j["contamination"] == "MATERIAL" for j in read),
                                           len(read)),
            "structured_material_contamination": rate(
                sum(judgements[r["id"]]["contamination"] == "MATERIAL" for r in structured),
                len(structured)),
            "unsupported_pages_that_returned_text": {
                r["id"]: judgements.get(r["id"], {}).get("contamination") for r in stray},
            "known_gap_1_pages": sorted(i for i, j in judgements.items() if j["known_gap"] == 1),
            "known_gap_2_pages": sorted(i for i, j in judgements.items() if j["known_gap"] == 2)},
    }


def gate(summary: dict[str, Any], gates: dict[str, Any]) -> dict[str, bool | None]:
    """True, False, or None where the gate needs a judgement that has not been made."""
    automated, judged = summary["automated"], summary["judged_from_output"]
    all_judged = judged["pages_judged"]["rate"] == 1.0

    def at_least(value: dict[str, Any], name: str) -> bool | None:
        return None if value["rate"] is None else value["rate"] >= gates[name]

    def at_most(value: dict[str, Any], name: str) -> bool | None:
        return None if value["rate"] is None else value["rate"] <= gates[name]

    return {
        "catastrophic_rate_max": at_most(judged["catastrophic"], "catastrophic_rate_max")
        if all_judged else None,
        "hidden_segments_found_max": automated["hidden_segments_found"]
        <= gates["hidden_segments_found_max"],
        "critical_segments_recovered_min": at_least(automated["critical_segments_recovered"],
                                                    "critical_segments_recovered_min"),
        "page_success_min": at_least(automated["page_success"], "page_success_min"),
        "forbidden_segments_absent_min": at_least(automated["forbidden_segments_absent"],
                                                  "forbidden_segments_absent_min"),
        "material_contamination_rate_max": at_most(judged["material_contamination"],
                                                   "material_contamination_rate_max")
        if judged["texts_judged_for_contamination"]["rate"] == 1.0 else None,
        "articles_demoted_rate_max": at_most(automated["articles_demoted"],
                                             "articles_demoted_rate_max"),
    }


def validate(protocol: dict[str, Any], cases: list[dict[str, Any]], labels: dict[str, Any],
             judgements: dict[str, Any]) -> list[str]:
    """Problems with the manifest, labels or judgements. Empty when they can be scored."""
    problems = []
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        problems.append("duplicate case ids")
    for case in cases:
        if case["url"] and "fetch" not in case:
            problems.append(f"{case['id']}: not snapshotted")
        if case["url"] and case.get("fetch", {}).get("outcome") == "ok" and (
                len(case["fetch"].get("content_sha256", "")) != 64):
            problems.append(f"{case['id']}: no content hash")
    limit = protocol["annotation"]["segment_chars_max"]
    for case_id in ids:
        label = labels.get(case_id)
        if label is None:
            problems.append(f"{case_id}: no label")
            continue
        if label["page_type"] not in PAGE_TYPES:
            problems.append(f"{case_id}: page type {label['page_type']!r}")
        if not set(label["acceptable_routes"]) <= set(ROUTES) or not label["acceptable_routes"]:
            problems.append(f"{case_id}: acceptable routes")
        if label["supported"] and not any(s["critical"] for s in label["expected"]):
            problems.append(f"{case_id}: a supported page needs a critical expected segment")
        for segment in [*label["expected"], *label["forbidden"]]:
            if not 0 < len(segment["text"]) <= limit:
                problems.append(f"{case_id}: a segment is empty or over {limit} characters")
        for segment in label["forbidden"]:
            if segment["kind"] not in FORBIDDEN_KINDS:
                problems.append(f"{case_id}: forbidden kind {segment['kind']!r}")
    for case_id, judgement in judgements.items():
        if case_id not in ids:
            problems.append(f"judgement for unknown case {case_id}")
        if judgement.get("contamination") not in (*SEVERITIES, None):
            problems.append(f"{case_id}: contamination {judgement.get('contamination')!r}")
        if judgement.get("catastrophic", "") not in (*CATASTROPHIC, None):
            problems.append(f"{case_id}: catastrophic {judgement.get('catastrophic', '')!r}")
        if judgement.get("known_gap", "") not in (1, 2, None):
            problems.append(f"{case_id}: known gap {judgement.get('known_gap', '')!r}")
    return problems


def score(protocol: dict[str, Any], cases: list[dict[str, Any]], labels: dict[str, Any],
          judgements: dict[str, Any], directory: pathlib.Path) -> dict[str, Any]:
    documents = extract_all(cases, directory)
    rows = []
    for case in cases:
        if not case["url"]:
            continue
        document = documents.get(case["id"], "not_fetched")
        rows.append(score_case(case, labels[case["id"]], document))
    summary = aggregate(rows, judgements)

    def by(key: str) -> dict[str, Any]:
        return {str(value): aggregate([r for r in rows if r[key] == value],
                                      judgements)["automated"]
                for value in sorted({r[key] for r in rows}, key=str)}

    # Pages nobody had read while the extractor was being changed, less any that turned out
    # to hold the same content as one that had been.
    same_as = {case_id: j["duplicate_of"] for case_id, j in judgements.items()
               if j.get("duplicate_of")}
    seen = {r["id"] for r in rows if r["read_during_e1"]}
    unseen = [r for r in rows if r["id"] not in seen and same_as.get(r["id"]) not in seen]
    unseen_summary = aggregate(unseen, judgements)
    statuses = sorted({r["annotation_status"] for r in rows})
    return {"summary": summary, "gate": gate(summary, protocol["gates"]),
            "unseen_pages": {"summary": unseen_summary,
                             "gate": gate(unseen_summary, protocol["gates"]),
                             "excluded_as_duplicates": sorted(
                                 i for i, d in same_as.items() if d in seen)},
            "labels_are": statuses,
            "human_annotation_complete": statuses == ["human_verified"],
            "by_category": by("category"), "by_page_type": by("page_type"),
            "by_host": by("host"), "by_read_during_e1": by("read_during_e1"), "rows": rows}


# --- command line -----------------------------------------------------------------------------


def _load(path: pathlib.Path, default: Any) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _write(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> int:  # noqa: PLR0911 -- one return per refusal, each with its reason
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("select", "snapshot", "dump", "score", "fingerprint"))
    parser.add_argument("--live", action="store_true", help="really fetch through the gateway")
    parser.add_argument("--source-domains", default="",
                        help="the approved source domains, comma-separated (source_policy.py)")
    parser.add_argument("--snapshots", type=pathlib.Path)
    parser.add_argument("--write", type=pathlib.Path)
    args = parser.parse_args()
    if args.command == "fingerprint":
        print(json.dumps(fingerprint(), indent=2))
        return 0
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    if args.command in ("select", "snapshot"):
        if not args.live:
            print("needs --live; nothing was fetched", file=sys.stderr)
            return 2
        if any(name.startswith("RESEARCH_") for name in os.environ):
            print("a service credential is set; this runs without one", file=sys.stderr)
            return 2
        try:  # live fetching needs an approved source of pages and approved domains
            require_approval(_Leads.name, None)
            gateway = policy_gateway(parse_domains(args.source_domains))
        except (SearchFailure, SourcePolicyInvalid) as exc:
            print(f"refused: {exc}; nothing was fetched", file=sys.stderr)
            return 2
    if args.command == "select":
        if MANIFEST.exists():
            print("the page list exists; it is selected once", file=sys.stderr)
            return 2
        cases = select(protocol, gateway)
        _write(MANIFEST, {"dataset": protocol["dataset"], "version": protocol["version"],
                          "protocol_sha256": sha256_file(PROTOCOL), "cases": cases})
        print(f"{len(cases)} cases selected")
        return 0
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest["protocol_sha256"] != sha256_file(PROTOCOL):
        print("the protocol changed after the pages were selected", file=sys.stderr)
        return 2
    if args.snapshots is None or ROOT in (args.snapshots.resolve(),
                                          *args.snapshots.resolve().parents):
        print("--snapshots must name a directory outside the repository", file=sys.stderr)
        return 2
    if args.command == "snapshot":
        snapshot(manifest["cases"], gateway, args.snapshots)
        _write(MANIFEST, manifest)
        return 0
    if args.command == "dump":
        for case in manifest["cases"]:
            source = args.snapshots / f"{case['id']}.bin"
            if source.exists() and source.stat().st_size:
                (args.snapshots / f"{case['id']}.plain.txt").write_text(
                    f"URL: {case['url']}\n{plain_rendering(source.read_bytes())}\n",
                    encoding="utf-8")
        return 0
    if args.write and args.write.exists():
        print("that result exists; a result is never overwritten", file=sys.stderr)
        return 2
    labels = _load(LABELS, {"labels": {}})["labels"]
    judgements = _load(JUDGEMENTS, {"judgements": {}})["judgements"]
    problems = validate(protocol, manifest["cases"], labels, judgements)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 2
    current = fingerprint()
    result = {"dataset": protocol["dataset"], "version": protocol["version"],
              "protocol_sha256": sha256_file(PROTOCOL), "manifest_sha256": sha256_file(MANIFEST),
              "labels_sha256": sha256_file(LABELS),
              "judgements_sha256": sha256_file(JUDGEMENTS) if JUDGEMENTS.exists() else None,
              "fingerprint": current,
              "extractor_matches_protocol": all(
                  current[name] == value
                  for name, value in protocol["fingerprint"].items() if name != FINGERPRINTED[-1]),
              } | score(protocol, manifest["cases"], labels, judgements, args.snapshots)
    print(json.dumps({k: result[k] for k in ("summary", "gate", "labels_are",
                                              "human_annotation_complete",
                                              "extractor_matches_protocol")}, indent=2))
    if args.write:
        _write(args.write, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
