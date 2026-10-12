"""E1: is extracted text fit to be evidence, not merely present?

Each case in `quality_cases.json` is a local fixture with the strings a reader of the page sees
(which the evidence must carry), the strings it must not carry (navigation, controls, hidden
and template text, teasers about someone else), the headline, and which extractor should have
read it. Every page goes through the real parsing child, twice.

Nothing is fetched and no page text is kept: a result holds counts, lengths, the extractor
version and the labels of the segments that were missing or wrongly present.

  python tools/research_eval/quality_eval.py --label baseline \
      --write tools/research_eval/results/e1-quality-dev-v1.baseline.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import time
from typing import Any

from screening_crawler.research import egress as egress_module
from screening_crawler.research.extract import ParserTriedNetwork, extract_isolated

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DATASET = HERE / "quality_cases.json"  # v1; `--cases` names a later set


def _document(html: str) -> tuple[Any, str]:
    """What the egress would return for this page: (extraction or None, refusal reason)."""
    try:
        extracted = extract_isolated(html)
    except ParserTriedNetwork:
        return None, "parser_network_attempt"
    if extracted is None:
        return None, egress_module.why_no_text(html)
    if not extracted.is_article:
        reason = egress_module.why_no_text(html)
        if reason != "no_article_text":
            return None, reason
        if egress_module.is_gate_page(extracted):
            return None, "not_a_content_page"
    return extracted, ""


def evaluate(case: dict[str, Any], forbidden: list[str]) -> dict[str, Any]:
    html = (ROOT / case["fixture"]).read_text(encoding="utf-8")
    started = time.monotonic()
    extracted, reason = _document(html)
    seconds = round(time.monotonic() - started, 2)
    repeat, _ = _document(html)
    row: dict[str, Any] = {"id": case["id"], "kind": case["kind"], "seconds": seconds,
                           "outcome": reason or "ok", "deterministic": extracted == repeat,
                           "expected_path": case["expected_path"]}
    expected = case["must_contain"]
    if extracted is None:
        nothing_expected = case["expected_path"] == "none"
        return row | {"path": "none", "path_ok": nothing_expected,
                      "headline_ok": nothing_expected,
                      "expected_segments": len(expected), "segments_recovered": 0,
                      "missing": expected, "forbidden_found": [], "forbidden_lead": False,
                      "text_chars": 0}
    text = extracted.text[:egress_module.MAX_TEXT_CHARS]
    whole = f"{extracted.headline}\n{text}"
    path = "article" if extracted.is_article else "structured"
    missing = [segment for segment in expected if segment not in whole]
    lead = case.get("must_not_start_with")
    return row | {
        "path": path, "extractor": extracted.extractor_version,
        "path_ok": case["expected_path"] in (path, "either"),
        "headline_ok": extracted.headline == case["headline"],
        "expected_segments": len(expected), "segments_recovered": len(expected) - len(missing),
        "missing": missing,
        "forbidden_found": [s for s in [*forbidden, *case["must_not_contain"]] if s in whole],
        "forbidden_lead": bool(lead) and text.startswith(lead),
        "text_chars": len(text), "has_published_date": extracted.published_date is not None,
        "truncated": extracted.truncated,
    }


def summarise(rows: list[dict[str, Any]], gates: dict[str, Any]) -> dict[str, Any]:
    def rate(k: int, n: int) -> dict[str, Any]:
        return {"k": k, "n": n, "rate": round(k / n, 3) if n else None}

    metrics = {
        "expected_segments_recovered": rate(sum(r["segments_recovered"] for r in rows),
                                            sum(r["expected_segments"] for r in rows)),
        "headline_correct": rate(sum(r["headline_ok"] for r in rows), len(rows)),
        "expected_path": rate(sum(r["path_ok"] for r in rows), len(rows)),
        "forbidden_segments_found": sum(len(r["forbidden_found"]) for r in rows),
        "cases_starting_with_a_forbidden_lead": sum(r["forbidden_lead"] for r in rows),
        "cases_with_differing_repeat_output": sum(not r["deterministic"] for r in rows),
        "parser_network_attempts": sum(r["outcome"] == "parser_network_attempt" for r in rows),
        "slowest_page_seconds": max(r["seconds"] for r in rows),
        "longest_text_chars": max(r["text_chars"] for r in rows),
    }
    fidelity, safety, security = gates["fidelity"], gates["content_safety"], gates["security"]
    gate = {
        "expected_segments_recovered": (metrics["expected_segments_recovered"]["rate"]
                                        >= fidelity["expected_segments_recovered_min"]),
        "headline_correct": metrics["headline_correct"]["rate"] >= fidelity["headline_correct_min"],
        "expected_path": metrics["expected_path"]["rate"] >= fidelity["expected_path_min"],
        "forbidden_segments_found": (metrics["forbidden_segments_found"]
                                     <= safety["forbidden_segments_found_max"]),
        "cases_starting_with_a_forbidden_lead": (
            metrics["cases_starting_with_a_forbidden_lead"]
            <= safety["cases_starting_with_a_forbidden_lead_max"]),
        "deterministic": (metrics["cases_with_differing_repeat_output"]
                          <= gates["determinism"]["cases_with_differing_repeat_output_max"]),
        "parser_network_attempts": (metrics["parser_network_attempts"]
                                    <= security["parser_network_attempts_max"]),
        "seconds_per_page": metrics["slowest_page_seconds"] <= security["seconds_per_page_max"],
        "text_chars": metrics["longest_text_chars"] <= security["text_chars_max"],
    }
    failing = sorted(r["id"] for r in rows if not (
        r["path_ok"] and r["headline_ok"] and not r["missing"] and not r["forbidden_found"]
        and not r["forbidden_lead"] and r["deterministic"]))
    return {"cases": len(rows), "metrics": metrics, "gate": gate, "cases_failing": failing}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True, help="which extractor state this run measures")
    parser.add_argument("--write", type=pathlib.Path)
    parser.add_argument("--cases", type=pathlib.Path, default=DATASET)
    args = parser.parse_args()
    if args.write and args.write.exists():
        print("that result exists; a result is never overwritten", file=sys.stderr)
        return 2
    raw = args.cases.read_bytes()
    dataset = json.loads(raw)
    forbidden = [s for group in dataset["forbidden_everywhere"].values() for s in group]
    rows = [evaluate(case, forbidden) for case in dataset["cases"]]
    summary = summarise(rows, dataset["gates"])
    result = {"dataset": dataset["dataset"], "version": dataset["version"],
              "dataset_sha256": hashlib.sha256(raw).hexdigest(), "label": args.label,
              "run_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "summary": summary,
              "passed": all(summary["gate"].values()), "rows": rows}
    print(json.dumps({"summary": summary, "passed": result["passed"]}, indent=2))
    if args.write:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                              encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
