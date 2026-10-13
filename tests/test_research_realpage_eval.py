"""The E1.1 real-page evaluation tool (docs/research-egress.md, "DEV evaluation"): pages are
chosen by rule, replayed from snapshots with no network, and scored against labels in a way
that keeps counts apart from judgement. The tool is tested on synthetic pages; the frozen
protocol, manifest and labels are checked for consistency with one another.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import socket
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("trafilatura")
pytest.importorskip("httpx")

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools/research_eval"
QUALITY = ROOT / "tests/fixtures/research_quality"
PROTOCOL_SHA256 = "2ee1003c9132547b40295773b771860875f3745bd068a04cc572e3967b0db749"
MANIFEST_SHA256 = "c7690712950211c06d6c533790a214ff544922eb106824b533932b339be6dfd9"
# The labels as frozen before the extractor was run, and as amended afterwards in one metadata
# field only (`annotated_from`); a test below checks that nothing else differs.
LABELS_FROZEN_SHA256 = "ab968bb69d1380fb6dc663c744603748e790d1d71fd003d0e3746ee91bbb8b2a"
LABELS_SHA256 = "36395fd20591d11b719e730ecf8fc9bb358a42f8acd9f9b00663fe3cd5a84ccf"


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts: list[str] = []

    def refuse(*args: Any, **kwargs: Any) -> Any:
        attempts.append(repr(args[:2]))
        raise OSError("network is disabled in tests")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    yield attempts
    assert attempts == [], f"a real network call was attempted: {attempts}"


@pytest.fixture(scope="module")
def tool() -> Any:
    spec = importlib.util.spec_from_file_location("realpage_eval", TOOLS / "realpage_eval.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _case(directory: Path, case_id: str, fixture: str | None, *, outcome: str = "ok",
          content_type: str = "text/html; charset=utf-8", category: str = "c") -> dict[str, Any]:
    case: dict[str, Any] = {"id": case_id, "stratum": "s", "category": category,
                            "url": f"https://site.example/{case_id}", "read_during_e1": False}
    if outcome != "ok":
        return case | {"fetch": {"outcome": outcome, "retrieved_at": "2026-10-13T10:00:00+00:00"}}
    body = (QUALITY / fixture).read_bytes() if fixture else b""
    (directory / f"{case_id}.bin").write_bytes(body)
    return case | {"fetch": {
        "outcome": "ok", "final_url": case["url"], "status": 200,
        "declared_content_type": content_type, "retrieved_at": "2026-10-13T10:00:00+00:00",
        "content_sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}}


def _label(page_type: str, routes: list[str], expected: list[tuple[str, bool]],
           forbidden: list[tuple[str, str]], *, supported: bool = True) -> dict[str, Any]:
    return {"page_type": page_type, "supported": supported, "acceptable_routes": routes,
            "expected": [{"text": t, "critical": c} for t, c in expected],
            "forbidden": [{"text": t, "kind": k} for t, k in forbidden],
            "annotation_status": "ai_preannotation"}


@pytest.fixture
def world(tmp_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any], Path]:
    cases = [
        _case(tmp_path, "art", "news_article.html"),
        _case(tmp_path, "hid", "article_with_hidden_text.html"),
        _case(tmp_path, "prof", "profile_with_two_paragraph_card.html", category="official"),
        _case(tmp_path, "gap", "declared_profile_with_headless_card.html", category="official"),
        _case(tmp_path, "pdf", None, outcome="too_large"),
        _case(tmp_path, "empty", None),
    ]
    labels = {
        "art": _label("article", ["article"], [("Review to be published in full", True),
                                               ("during the sixth session", True),
                                               ("NOT ON THE PAGE", False)],
                      [("NAVIGATION-NEWS", "navigation"), ("FOOTER-COPYRIGHT", "footer_legal")]),
        "hid": _label("article", ["article"], [("BODY-AFTER", True)],
                      [("HIDDEN-ATTRIBUTE-TEXT", "hidden"), ("TEMPLATE-TEXT", "hidden")]),
        "prof": _label("profile", ["structured"],
                       [("Member of Parliament for Fixture East", True)],
                       [("NAVIGATION-NEWS", "navigation"), ("TEASER-CARD", "related_item")]),
        "gap": _label("profile", ["structured"], [("Court: Supreme Court", True)],
                      [("NAVIGATION-HOME", "navigation")]),
        "pdf": _label("unsupported_other", ["refusal"], [], [], supported=False),
        "empty": _label("unsupported_other", ["refusal"], [], [], supported=False),
    }
    return cases, labels, tmp_path


def test_pages_are_scored_through_the_real_egress_with_counts_and_no_page_text(
    tool: Any, world: tuple[list[dict[str, Any]], dict[str, Any], Path]
) -> None:
    cases, labels, directory = world
    protocol = json.loads((TOOLS / "realpage_protocol.json").read_text(encoding="utf-8"))
    result = tool.score(protocol, cases, labels, {}, directory)
    rows = {row["id"]: row for row in result["rows"]}
    assert (rows["art"]["route"], rows["art"]["expected_missing"], rows["art"]["critical_missing"],
            rows["art"]["forbidden_found"]) == ("article", [2], [], [])
    assert rows["hid"]["hidden_found"] == 0 and rows["hid"]["route"] == "article"
    # The card is on the page and the walk keeps it: the label says it should not be there.
    assert (rows["prof"]["route"], rows["prof"]["route_acceptable"],
            rows["prof"]["forbidden_found"]) == ("structured", True, [1])
    # The recorded known gap: the card is returned as the article and the profile is gone.
    assert (rows["gap"]["route"], rows["gap"]["route_acceptable"],
            rows["gap"]["critical_missing"]) == ("article", False, [0])
    assert (rows["pdf"]["route"], rows["pdf"]["outcome"]) == ("refusal", "too_large")
    assert rows["empty"]["route"] == "refusal"
    automated = result["summary"]["automated"]
    assert automated["unsupported_pages"] == {"k": 2, "n": 6, "rate": 0.333}
    assert automated["expected_segments_recovered"] == {"k": 4, "n": 6, "rate": 0.667}
    assert automated["critical_segments_recovered"] == {"k": 4, "n": 5, "rate": 0.8}
    assert automated["page_success"] == {"k": 3, "n": 4, "rate": 0.75}
    assert automated["forbidden_segments_absent"] == {"k": 6, "n": 7, "rate": 0.857}
    assert automated["articles_demoted"] == {"k": 0, "n": 2, "rate": 0.0}
    assert automated["routes"] == {"article": 3, "structured": 1, "refusal": 2}
    assert result["by_category"]["official"]["page_success"]["k"] == 1
    assert result["labels_are"] == ["ai_preannotation"]
    assert result["human_annotation_complete"] is False
    assert "procurement" not in json.dumps(result)  # indexes and counts, never the page's words


def test_gates_that_need_a_judgement_stay_undecided_until_every_page_is_judged(
    tool: Any, world: tuple[list[dict[str, Any]], dict[str, Any], Path]
) -> None:
    cases, labels, directory = world
    protocol = json.loads((TOOLS / "realpage_protocol.json").read_text(encoding="utf-8"))
    undecided = tool.score(protocol, cases, labels, {}, directory)["gate"]
    assert undecided["catastrophic_rate_max"] is None
    assert undecided["material_contamination_rate_max"] is None
    assert undecided["hidden_segments_found_max"] is True
    assert undecided["critical_segments_recovered_min"] is False  # 0.8 < 0.95
    judgements = {
        "art": {"contamination": "NONE", "catastrophic": None, "known_gap": None},
        "hid": {"contamination": "NONE", "catastrophic": None, "known_gap": None},
        "prof": {"contamination": "MATERIAL", "catastrophic": None, "known_gap": None},
        "gap": {"contamination": "MATERIAL", "catastrophic": "wrong_item", "known_gap": 1},
    }
    result = tool.score(protocol, cases, labels, judgements, directory)
    judged = result["summary"]["judged_from_output"]
    assert judged["pages_judged"]["rate"] == 1.0
    assert judged["catastrophic"] == {"k": 1, "n": 4, "rate": 0.25}
    assert judged["catastrophic_kinds"] == ["wrong_item"]
    assert judged["contamination"] == {"NONE": 2, "LOW": 0, "MATERIAL": 2}
    assert judged["structured_material_contamination"] == {"k": 1, "n": 1, "rate": 1.0}
    assert judged["known_gap_1_pages"] == ["gap"] and judged["known_gap_2_pages"] == []
    assert result["gate"]["catastrophic_rate_max"] is False
    assert result["gate"]["material_contamination_rate_max"] is False


def test_a_refused_page_with_content_is_catastrophic_and_a_stray_text_is_not_lost(
    tool: Any, tmp_path: Path
) -> None:
    """Two things a rate over "pages that returned text" would hide: a supported page that
    was refused although it has content, and an unsupported page that returned text anyway."""
    shell = (b'<html><head><title>T</title></head><body><div id="root"></div>'
             b'<script src="a.js"></script><script src="b.js"></script><script>x()</script>'
             b"</body></html>")
    refused = _case(tmp_path, "refused", "news_article.html")
    (tmp_path / "refused.bin").write_bytes(shell)
    refused["fetch"] |= {"content_sha256": hashlib.sha256(shell).hexdigest(), "bytes": len(shell)}
    cases = [refused, _case(tmp_path, "stray", "profile_with_two_paragraph_card.html"),
             _case(tmp_path, "seen", "news_article.html") | {"read_during_e1": True},
             _case(tmp_path, "twin", "news_article.html")]
    article = _label("article", ["article"], [("during the sixth session", True)], [])
    labels = {"refused": _label("landing", ["structured"], [("Some content", True)], []),
              "stray": _label("unsupported_other", ["refusal"], [], [], supported=False),
              "seen": article, "twin": article}
    clean = {"contamination": "NONE", "catastrophic": None, "known_gap": None}
    judgements = {
        "refused": {"contamination": None, "catastrophic": "empty_despite_content",
                    "known_gap": None},
        "stray": {"contamination": "MATERIAL", "catastrophic": None, "known_gap": None},
        "seen": clean, "twin": clean | {"duplicate_of": "seen"}}
    protocol = json.loads((TOOLS / "realpage_protocol.json").read_text(encoding="utf-8"))
    assert tool.validate(protocol, cases, labels, judgements) == []
    result = tool.score(protocol, cases, labels, judgements, tmp_path)
    judged = result["summary"]["judged_from_output"]
    assert judged["pages_judged"] == {"k": 3, "n": 3, "rate": 1.0}
    assert judged["catastrophic"] == {"k": 1, "n": 3, "rate": 0.333}
    assert judged["catastrophic_kinds"] == ["empty_despite_content"]
    assert judged["contamination"] == {"NONE": 2, "LOW": 0, "MATERIAL": 0}  # of text returned
    assert judged["unsupported_pages_that_returned_text"] == {"stray": "MATERIAL"}
    assert result["summary"]["automated"]["unsupported_pages_that_returned_text"] == ["stray"]
    assert result["gate"]["catastrophic_rate_max"] is False
    # A returned text with no contamination judgement leaves that gate undecided too.
    unread = judgements | {"seen": clean | {"contamination": None}}
    assert tool.score(protocol, cases, labels, unread, tmp_path)["gate"][
        "material_contamination_rate_max"] is None
    # Without the judgement for the refused page the gate is undecided, never quietly met.
    partial = {k: v for k, v in judgements.items() if k != "refused"}
    assert tool.score(protocol, cases, labels, partial, tmp_path)["gate"][
        "catastrophic_rate_max"] is None
    # Pages not read before, less the one that duplicates a page that was.
    unseen = result["unseen_pages"]
    assert unseen["excluded_as_duplicates"] == ["twin"]
    assert unseen["summary"]["automated"]["pages"] == 2  # refused and stray
    assert unseen["summary"]["automated"]["articles_demoted"]["n"] == 0


def test_scoring_is_the_same_every_time(
    tool: Any, world: tuple[list[dict[str, Any]], dict[str, Any], Path]
) -> None:
    cases, labels, directory = world
    protocol = json.loads((TOOLS / "realpage_protocol.json").read_text(encoding="utf-8"))
    first = tool.score(protocol, cases, labels, {}, directory)
    assert tool.score(protocol, cases, labels, {}, directory) == first


def test_a_snapshot_that_no_longer_matches_its_recorded_hash_is_refused(
    tool: Any, world: tuple[list[dict[str, Any]], dict[str, Any], Path]
) -> None:
    cases, labels, directory = world
    (directory / "art.bin").write_bytes(b"<html>tampered</html>")
    protocol = json.loads((TOOLS / "realpage_protocol.json").read_text(encoding="utf-8"))
    with pytest.raises(RuntimeError, match="does not match the manifest hash"):
        tool.score(protocol, cases, labels, {}, directory)


def test_labels_and_judgements_are_validated_before_anything_is_scored(
    tool: Any, world: tuple[list[dict[str, Any]], dict[str, Any], Path]
) -> None:
    cases, labels, _ = world
    protocol = json.loads((TOOLS / "realpage_protocol.json").read_text(encoding="utf-8"))
    assert tool.validate(protocol, cases, labels, {}) == []
    broken = json.loads(json.dumps(labels))
    del broken["art"]
    broken["hid"]["page_type"] = "blog"
    broken["prof"]["expected"][0]["critical"] = False
    broken["gap"]["forbidden"][0] = {"text": "x" * 161, "kind": "advert"}
    problems = tool.validate(protocol, [*cases, cases[0]], broken,
                             {"ghost": {"contamination": "SOME"}})
    text = "\n".join(problems)
    assert "ghost: catastrophic ''" in text and "ghost: known gap ''" in text
    for expected in ("duplicate case ids", "art: no label", "hid: page type 'blog'",
                     "prof: a supported page needs a critical expected segment",
                     "gap: a segment is empty or over 160 characters",
                     "gap: forbidden kind 'advert'", "judgement for unknown case ghost",
                     "ghost: contamination 'SOME'"):
        assert expected in text


def test_candidate_links_come_from_any_listing_format_and_survive_hostile_ones(tool: Any) -> None:
    listing = (b'<a href="/news/a-b-c-d-story">x</a><a href="http://[bad">y</a>'
               b'{"link":"https:\\/\\/site.example\\/2026\\/10\\/one-two-three-four\\/"}'
               b"<link>https://site.example/category/x-y-z-w</link>"
               b"<guid>https://other.example/a-b-c-d</guid>"
               b'<a href="https://www.site.example/file-a-b-c.pdf">pdf</a>')
    links = tool.links_in(listing, "https://site.example/")
    assert "https://site.example/news/a-b-c-d-story" in links
    assert "https://site.example/2026/10/one-two-three-four/" in links
    news = [u for u in links if tool.same_site(u, "site.example") and tool.is_article_link(u)]
    assert news == ["https://site.example/2026/10/one-two-three-four/",
                    "https://site.example/news/a-b-c-d-story"]
    assert tool.same_site("https://www.site.example/x", "site.example")
    assert not tool.is_page_link("https://site.example/") and tool.is_page_link(
        "https://site.example/members?mp=1")


def test_the_draw_is_seeded_and_never_repeats_a_page(tool: Any) -> None:
    pool = [f"https://site.example/p{n}" for n in range(30)]
    first = tool.pick(pool, 3, "seed|rule", set())
    assert first == tool.pick(list(reversed(pool)), 3, "seed|rule", set())
    assert first != tool.pick(pool, 3, "other|rule", set())
    assert not set(tool.pick(pool, 3, "seed|rule", set(first))) & set(first)
    assert tool.pick(pool[:2], 5, "seed|rule", set()) == sorted(pool[:2]) or len(
        tool.pick(pool[:2], 5, "seed|rule", set())) == 2


def test_the_annotators_rendering_does_not_use_the_extractor_and_marks_hidden_text(
    tool: Any
) -> None:
    rendering = tool.plain_rendering((QUALITY / "article_with_hidden_text.html").read_bytes())
    assert "[HIDDEN p] UNTIL-FOUND-TEXT" in rendering and "NAVIGATION-NEWS" in rendering
    assert "var tracking" not in rendering and rendering.startswith("TITLE: Review")
    source = (TOOLS / "realpage_eval.py").read_text(encoding="utf-8")
    body = source[source.index("def plain_rendering"):source.index("# --- score")]
    for name in ("extract_module", "extract_isolated", "read_page", "structured_text",
                 "trafilatura", "egress"):
        assert name not in body, name


def test_fetching_commands_refuse_without_the_live_flag_or_inside_the_repository(
    tool: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for argv, message in (
        (["select"], "needs --live"), (["snapshot"], "needs --live"),
        (["score", "--snapshots", str(ROOT / "tools")], "outside the repository"),
        (["score", "--snapshots", str(ROOT)], "outside the repository"),
        (["dump"], "outside the repository"),
    ):
        monkeypatch.setattr("sys.argv", ["realpage_eval.py", *argv])
        assert tool.main() == 2
        assert message in capsys.readouterr().err
    monkeypatch.setenv("RESEARCH_EXA_API_KEY", "x")
    monkeypatch.setattr("sys.argv", ["realpage_eval.py", "select", "--live"])
    assert tool.main() == 2 and "credential" in capsys.readouterr().err


# --- the frozen E1.1 files ----------------------------------------------------------------------


def test_the_protocol_and_the_page_list_are_the_frozen_ones(tool: Any) -> None:
    assert tool.sha256_file(TOOLS / "realpage_protocol.json") == PROTOCOL_SHA256
    assert tool.sha256_file(TOOLS / "realpage_cases.json") == MANIFEST_SHA256
    protocol = json.loads((TOOLS / "realpage_protocol.json").read_text(encoding="utf-8"))
    manifest = json.loads((TOOLS / "realpage_cases.json").read_text(encoding="utf-8"))
    assert manifest["protocol_sha256"] == PROTOCOL_SHA256
    gates = protocol["gates"]
    assert (gates["catastrophic_rate_max"], gates["hidden_segments_found_max"]) == (0.0, 0)
    assert gates["critical_segments_recovered_min"] == 0.95 and gates["page_success_min"] == 0.9
    cases = manifest["cases"]
    assert 30 <= len(cases) <= 50 and len({c["id"] for c in cases}) == len(cases)
    assert sum(c["stratum"] == "carried_over" for c in cases) == 32
    for case in cases:
        assert case["url"].startswith("https://") and case["selected_by"]
        fetch = case["fetch"]
        assert fetch["retrieved_at"].startswith("2026-10-13")
        if fetch["outcome"] == "ok":
            assert len(fetch["content_sha256"]) == 64
    # No page body is in the repository: no snapshot file, and no long text in any E1.1 file.
    assert not list(TOOLS.rglob("*.bin")) and not list(TOOLS.rglob("*.plain.txt"))
    for name in ("realpage_cases.json", "realpage_labels.json", "realpage_judgements.json",
                 *(f"results/{p.name}" for p in (TOOLS / "results").glob("e11-*.json"))):
        longest = max(_strings(json.loads((TOOLS / name).read_text(encoding="utf-8"))), key=len)
        assert len(longest) <= 600, (name, longest[:60])


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    children = value.values() if isinstance(value, dict) else value if isinstance(
        value, list) else []
    return [text for child in children for text in _strings(child)] or [""]


def test_the_labels_are_the_frozen_ones_but_for_one_field_of_metadata(tool: Any) -> None:
    assert tool.sha256_file(TOOLS / "realpage_labels.json") == LABELS_SHA256
    document = json.loads((TOOLS / "realpage_labels.json").read_text(encoding="utf-8"))
    assert LABELS_FROZEN_SHA256 in document["amended"]
    # Put the one field back as it was and drop the amendment note: the frozen file again.
    frozen = "plain rendering of the snapshot; not rendered in a browser; not from extractor output"
    for label in document["labels"].values():
        label["annotated_from"] = frozen
    del document["amended"]
    restored = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode()
    assert hashlib.sha256(restored).hexdigest() == LABELS_FROZEN_SHA256
    # And every stored result was scored from one of those two files.
    for result in sorted((TOOLS / "results").glob("e11-realpage-dev-v1.*.json")):
        stored = json.loads(result.read_text(encoding="utf-8"))
        assert stored["labels_sha256"] in (LABELS_FROZEN_SHA256, LABELS_SHA256), result.name
        assert stored["protocol_sha256"] == PROTOCOL_SHA256


def test_every_page_is_labelled_and_the_labels_can_be_scored(tool: Any) -> None:
    protocol = json.loads((TOOLS / "realpage_protocol.json").read_text(encoding="utf-8"))
    manifest = json.loads((TOOLS / "realpage_cases.json").read_text(encoding="utf-8"))
    labels = json.loads((TOOLS / "realpage_labels.json").read_text(encoding="utf-8"))["labels"]
    judgements = json.loads(
        (TOOLS / "realpage_judgements.json").read_text(encoding="utf-8"))["judgements"]
    assert tool.validate(protocol, manifest["cases"], labels, judgements) == []
    assert set(labels) == {case["id"] for case in manifest["cases"]}
    for label in labels.values():
        assert label["annotation_status"] in ("ai_preannotation", "human_verified")
        assert label["annotated_from"]
        for segment in [*label["expected"], *label["forbidden"]]:
            assert "@" not in segment["text"]  # no e-mail address is used as a segment
