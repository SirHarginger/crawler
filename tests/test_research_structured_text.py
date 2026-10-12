"""The structured-page fallback (docs/research-egress.md): pages that are not articles still
yield their visible text, the article path is unchanged, and hostile markup can neither reach
anything nor run away with time, memory or output. No network: every page is a local fixture.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("trafilatura")
pytest.importorskip("lxml")

from screening_crawler.research import extract as extraction  # noqa: E402
from screening_crawler.research import structured_text  # noqa: E402
from screening_crawler.research.extract import ARTICLE_EXTRACTOR_VERSION  # noqa: E402
from screening_crawler.research.page_text import extract_article_text  # noqa: E402
from screening_crawler.research.structured_text import (  # noqa: E402
    STRUCTURED_EXTRACTOR_VERSION,
    extract_structured_text,
)

FIXTURES = Path(__file__).parent / "fixtures" / "research_structured"
CHROME = ("NAVIGATION-", "SEARCH-", "FOOTER-", "SCRIPT-TEXT", "STYLE-TEXT")
FILLER = "<p>Synthetic Registrar Omega keeps the public register of the court.</p>" * 6


def _fixture(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Recorded, and checked at teardown: a parser that swallowed the refusal still fails."""
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


# --- what a non-article page yields -----------------------------------------------------------------


@pytest.mark.parametrize(("name", "headline", "expected"), [
    ("parliament_profile", "Hon. Synthetic Member Alpha", [
        "Member of Parliament for Fixture East", "Party: Example Democratic Party",
        "Constitutional and Legal Affairs Committee", "Partner, Fixture Chambers, 2009 to 2020."]),
    ("government_index", "Members of Parliament", [
        "Hon. Synthetic Member Alpha - Fixture Alpha Constituency",
        "Hon. Synthetic Member Delta - Fixture Delta Constituency"]),
    ("table_page", "Designated persons", [
        "Name | Reference | Listed on | Measure",
        "Synthetic Person Alpha | SYN.001 | 12 March 2019 | Asset freeze",
        "Synthetic Trading Company Limited | SYN.003 | 4 June 2021 | Asset freeze"]),
    ("definition_profile", "Justice Synthetic Gamma", [
        "Court: Supreme Court", "Appointed: 2015", "Education: University of Fixture",
        "Fixture School of Law", "Previous office: Justice of the Court of Appeal"]),
    ("landing_page", "Synthetic Ministry of Finance", [
        "2026 Budget Statement presented", "Hon. Synthetic Minister Delta, Minister",
        "Hon. Synthetic Deputy Epsilon, Deputy Minister"]),
])
def test_a_non_article_page_yields_its_visible_text_line_by_line(
    name: str, headline: str, expected: list[str]
) -> None:
    html = _fixture(name)
    assert extract_article_text(html) is None  # the article extractor has nothing for it
    result = extraction.extract(html)
    assert result is not None and not result.is_article
    assert result.extractor_version == STRUCTURED_EXTRACTOR_VERSION
    assert result.headline == headline
    lines = result.text.split("\n")
    assert [line for line in expected if line not in lines] == []
    assert not [marker for marker in CHROME if marker in result.text + result.headline]
    assert result.published_date is None  # a date on a list page is not the text's date


def test_an_empty_table_cell_keeps_its_place() -> None:
    lines = extraction.extract(_fixture("table_page")).text.split("\n")  # type: ignore[union-attr]
    assert "Synthetic Person Beta | SYN.002 | | Travel ban" in lines  # not under "Listed on"


def test_text_the_markup_hides_or_does_not_show_is_left_out() -> None:
    result = extraction.extract(_fixture("landing_page"))
    assert result is not None
    for marker in ("HIDDEN-CLASS-TEXT", "HIDDEN-ATTRIBUTE-TEXT", "HIDDEN-STYLE-TEXT",
                   "ARIA-HIDDEN-TEXT", "TEMPLATE-TEXT", "NOSCRIPT-TEXT", "ASIDE-TEXT",
                   "ROLE-NAVIGATION-LINK", "Wrongname"):
        assert marker not in result.text


def test_a_header_inside_the_content_is_kept_and_the_page_header_is_not() -> None:
    result = extraction.extract(_fixture("definition_profile"))
    assert result is not None and result.text.startswith("Justice Synthetic Gamma\n")
    assert "Synthetic Parliament" not in result.text  # the site header's own link text


def test_the_headline_falls_back_to_the_title_and_a_page_without_either_is_refused() -> None:
    titled = f"<html><head><title> Register  of Courts </title></head><body>{FILLER}</body></html>"
    result = extract_structured_text(titled)
    assert result is not None and result.headline == "Register of Courts"
    assert extract_structured_text(f"<html><body>{FILLER}</body></html>") is None


@pytest.mark.parametrize("html", [
    "", "   ", "<html></html>", "<html><head><title>T</title></head><body><p>stub</p></body></html>",
    "<html><head><title>T</title></head><body><nav>" + FILLER + "</nav></body></html>",
    "not html at all", "\x00\x01\x02",
])
def test_a_stub_or_a_page_that_is_only_chrome_yields_nothing(html: str) -> None:
    assert extraction.extract(html) is None


# --- the article path is unchanged ------------------------------------------------------------------


def test_an_article_is_still_extracted_by_the_article_extractor_alone() -> None:
    html = _fixture("article")
    article = extract_article_text(html)
    assert article is not None
    result = extraction.extract(html)
    assert result is not None and result.is_article
    # Nothing on this page is hidden, so the text is exactly what the bare extractor returns.
    assert (result.headline, result.text) == (article.headline, article.text)
    assert result.extractor_version == ARTICLE_EXTRACTOR_VERSION
    assert result.published_date == "2026-03-14"
    assert "opened a new water treatment plant" in result.text
    assert not [marker for marker in CHROME if marker in result.text]


# --- deterministic ------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["parliament_profile", "table_page", "landing_page", "article"])
def test_the_same_markup_always_gives_the_same_text(name: str) -> None:
    html = _fixture(name)
    here = [extraction.extract(html) for _ in range(3)]
    assert here[0] is not None and here[0] == here[1] == here[2]
    # And in a fresh interpreter each time: nothing depends on hash order or process state.
    assert extraction.extract_isolated(html) == extraction.extract_isolated(html) == here[0]


# --- hostile markup -----------------------------------------------------------------------------------


HEAD = "<html><head><title>T</title></head><body><h1>Hostile</h1>"
HOSTILE = {
    "deep": HEAD + "<div>" * 200_000 + FILLER,
    "deep_inline": HEAD + "<b><i>" * 100_000 + FILLER,
    "wide": HEAD + "<p>Synthetic line of text.</p>" * 60_000,
    "unclosed_table": HEAD + "<table>" + "<tr><td>cell" * 100_000,
    "nested_tables": HEAD + "<table><tr><td>" * 20_000 + FILLER,
    "huge_attribute": HEAD + "<p " + 'a="b" ' * 300_000 + ">x</p>" + FILLER,
    "one_long_line": HEAD + "<p>" + "word " * 390_000 + "</p>",
    "unterminated": HEAD + FILLER + "<p>open <b>bold <i>italic <a href='x'>link",
    "stray_closers": HEAD + "</div></p></table>" * 50_000 + FILLER,
    "comment_never_closed": HEAD + FILLER + "<!-- " + "x" * 500_000,
    "script_never_closed": HEAD + FILLER + "<script>" + "var a = 1;" * 100_000,
    "entities": HEAD + "<p>" + "&amp;&lt;&#x41;&bogus;&#99999999999;" * 40_000 + "</p>",
    "null_bytes": HEAD + FILLER.replace("court", "co\x00urt") + "\x00" * 1000,
    "mixed_scripts": HEAD + "<p>مرحبا ‮ gnirts desrever 😀 Ω</p>".encode(
        "utf-8", "ignore").decode() + FILLER,
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_hostile_markup_is_parsed_quickly_or_not_at_all(name: str) -> None:
    started = time.monotonic()
    result = extract_structured_text(HOSTILE[name])
    assert time.monotonic() - started < 10
    if result is not None:
        assert len(result.text) <= structured_text._MAX_TEXT_CHARS
        assert len(result.headline) <= 2000
        assert "\x00" not in result.text and "var a = 1" not in result.text


def test_malformed_markup_still_gives_the_text_it_contains() -> None:
    for name in ("unterminated", "stray_closers", "comment_never_closed", "script_never_closed",
                 "null_bytes"):
        result = extract_structured_text(HOSTILE[name])
        assert result is not None and "Synthetic Registrar Omega" in result.text, name


def test_the_output_is_bounded_however_large_the_page() -> None:
    result = extract_structured_text(HOSTILE["wide"])
    assert result is not None
    assert 0 < len(result.text) <= structured_text._MAX_TEXT_CHARS
    assert result.truncated  # and it says there was more
    long_line = extract_structured_text(HOSTILE["one_long_line"])
    assert long_line is not None and long_line.truncated
    assert len(long_line.text) == structured_text._MAX_TEXT_CHARS


def test_a_long_paragraph_keeps_its_end() -> None:
    result = extract_structured_text(HEAD + "<p>" + "word " * 3000 + "TAIL-MARKER</p>")
    assert result is not None and result.text.endswith("TAIL-MARKER") and not result.truncated


def test_the_element_budget_stops_the_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(structured_text, "_MAX_ELEMENTS", 50)
    result = extract_structured_text(HEAD + "<p>Synthetic line number one of many.</p>" * 500)
    assert result is not None and result.text.count("Synthetic line") < 50
    assert result.truncated
    rows = "<tr><td>Synthetic Person</td><td>SYN.001</td></tr>" * 500
    through = extraction.extract(f"{HEAD}<table>{rows}</table>")
    assert through is not None and not through.is_article and through.truncated


def test_markup_the_parser_cannot_finish_is_killed_and_yields_nothing() -> None:
    """libxml2 is quadratic in the number of different attribute names on one element. Such
    a page is slow in any parser here; what is proven is that the child is killed for it."""
    names = " ".join(f'a{n}="1"' for n in range(60_000))
    slow = HEAD + f"<p {names}>x</p>" + FILLER
    started = time.monotonic()
    assert extraction.extract_isolated(slow, timeout_seconds=3) is None
    assert 3 <= time.monotonic() - started < 9  # it ran into the limit and was not waited for


def test_hostile_pages_finish_inside_the_real_child_well_within_its_limit() -> None:
    for name in ("deep", "nested_tables", "entities", "stray_closers"):
        started = time.monotonic()
        result = extraction.extract_isolated(HOSTILE[name])
        assert time.monotonic() - started < extraction.EXTRACTION_TIMEOUT_SECONDS / 2, name
        if name in ("entities", "stray_closers"):
            assert result is not None, name  # finished with text, not killed


# --- nothing in a page is resolved, loaded or fetched ---------------------------------------------


def _external_reference_page(port: int, canary: Path) -> str:
    there = f"http://127.0.0.1:{port}"
    return (
        f'<?xml version="1.0" encoding="utf-8"?>\n<?xml-stylesheet href="{there}/s.xsl"?>\n'
        f'<!DOCTYPE html SYSTEM "{there}/page.dtd" [\n'
        f'<!ENTITY file SYSTEM "file://{canary}">\n<!ENTITY net SYSTEM "{there}/entity">\n'
        f'<!ENTITY % param SYSTEM "{there}/param.dtd"> %param;\n]>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:xi="http://www.w3.org/2001/XInclude">'
        f'<head><title>T</title><base href="{there}/"/><link rel="stylesheet" href="{there}/c.css"/>'
        f'<meta http-equiv="refresh" content="0;url={there}/refresh"/>'
        f'<link rel="preload" href="file://{canary}"/></head><body><h1>External references</h1>'
        f'<p>file: &file; net: &net;</p><xi:include href="file://{canary}" parse="text"/>'
        f'<xi:include href="{there}/include.xml"/><img src="{there}/i.png"/>'
        f'<iframe src="{there}/frame"></iframe><object data="file://{canary}"></object>'
        f'<a href="{there}/link">{there}/link</a>{FILLER}</body></html>')


def test_no_dtd_entity_xinclude_or_linked_resource_is_ever_loaded(tmp_path: Path) -> None:
    """Checked below Python: a real listener would see a connection made by libxml2 itself,
    which the interpreter-level socket guard cannot. A file the page names is never read."""
    canary = tmp_path / "canary.txt"
    canary.write_text("CANARY-FILE-CONTENT")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.5)
    try:
        html = _external_reference_page(listener.getsockname()[1], canary)
        results = [extraction.extract_isolated(html), extraction.extract(html),
                   extract_structured_text(html)]
        with pytest.raises(TimeoutError):
            listener.accept()  # nothing connected, from the child or from this process
    finally:
        listener.close()
    for result in results:
        assert result is not None and "Synthetic Registrar Omega" in result.text
        assert "CANARY-FILE-CONTENT" not in result.text + result.headline


def test_the_parser_is_an_html_parser_with_the_network_off_and_limits_on() -> None:
    from lxml import etree

    parser = structured_text._parser()
    assert isinstance(parser, etree.HTMLParser) and not isinstance(parser, etree.XMLParser)
    # The listener in the test above cannot fail on a libxml2 built without a network client,
    # which the pinned wheels are. If that ever changes, this says so.
    assert not {"http", "ftp"} & set(etree.LIBXML_FEATURES)
    source = Path(structured_text.__file__).read_text(encoding="utf-8")
    assert "no_network=True" in source and "huge_tree=False" in source
    for forbidden in ("XMLParser", "xinclude", "XSLT", "resolve_entities=True", "load_dtd",
                      "import os", "import socket", "urllib", "httpx", "requests", "open(",
                      "subprocess", "trafilatura"):
        assert forbidden not in source, forbidden


def test_the_fallback_runs_inside_the_child_without_credentials_or_sockets(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    for name in ("RESEARCH_EXA_API_KEY", "RESEARCH_EGRESS_TOKEN", "RESEARCH_LEAD_SECRET"):
        monkeypatch.setenv(name, "secret-value")
    seen: dict[str, Any] = {}
    real = extraction.read_page

    def spy(html: str) -> Any:
        seen["environment"] = [n for n in os.environ if n.startswith("RESEARCH_")]
        try:
            socket.create_connection(("192.0.2.1", 80), timeout=1)
        except OSError as error:
            seen["socket"] = str(error)
        return real(html)

    class Out:
        def send(self, value: Any) -> None:
            self.sent = value

        def close(self) -> None:
            pass

    monkeypatch.setattr(extraction, "read_page", spy)
    monkeypatch.setattr("resource.setrlimit", lambda *a: None)  # not on the test process
    out = Out()
    extraction._child(_fixture("parliament_profile"), out)  # type: ignore[arg-type]
    assert seen == {"environment": [], "socket": "no network in the parsing process"}
    result, attempts = out.sent
    assert result is not None and attempts == 1  # the attempt is counted: the page is refused


# --- found by independent review ----------------------------------------------------------------------


NON_ASCII = "<h1>Hon. Ɔsɛe Kwadwo Méndez</h1><p>Nana Akufo‑Addo — “Minister” café Accra</p>"


@pytest.mark.parametrize("head", [
    "<head><title>T</title></head>",  # no charset: the HTTP header carried it
    '<head><meta charset="utf-8"><title>T</title></head>',
    '<head><meta charset="windows-1252"><title>T</title></head>',
    '<head><meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">'
    "<title>T</title></head>",
    '<head><meta charset="utf-16"><title>T</title></head>',
])
def test_text_outside_ascii_survives_whatever_charset_the_markup_claims(head: str) -> None:
    result = extract_structured_text(f"<html>{head}<body>{NON_ASCII}{FILLER}</body></html>")
    assert result is not None and result.headline == "Hon. Ɔsɛe Kwadwo Méndez"
    assert "Nana Akufo‑Addo — “Minister” café Accra" in result.text.split("\n")


_REAL = "<h1>Hon. Member Alpha</h1><p>REAL-PROFILE of the member.</p>" + FILLER
_FAKE = "<h1>Other headline</h1><p>NOT-CONTENT Another Person Beta was arrested.</p>" + FILLER


@pytest.mark.parametrize("decoy", [
    f"<div hidden><main>{_FAKE}</main></div>",
    f"<template><main>{_FAKE}</main></template>",
    f"<aside><article>{_FAKE}</article></aside>",
    f'<div style="display:none"><div role="main">{_FAKE}</div></div>',
    f"<nav><div><article>{_FAKE}</article></div></nav>",
    f'<main class="d-none">{_FAKE}</main>',
])
@pytest.mark.parametrize("decoy_first", [True, False])
def test_a_content_root_inside_something_dropped_is_not_the_page(
    decoy: str, decoy_first: bool
) -> None:
    body = decoy + _REAL if decoy_first else _REAL + decoy
    result = extract_structured_text(
        f"<html><head><title>Profile</title></head><body>{body}</body></html>")
    assert result is not None and result.headline == "Hon. Member Alpha"
    assert "REAL-PROFILE" in result.text and "NOT-CONTENT" not in result.text


def _table(rows: str) -> list[str]:
    result = extract_structured_text(f"{HEAD}<table>{rows}</table>{FILLER}")
    assert result is not None
    return result.text.split("\n")


def test_blocks_and_line_breaks_inside_a_cell_stay_in_the_cell() -> None:
    lines = _table(
        "<tr><th><p>Name</p></th><th><p>Listed on</p></th><th><div>Measure</div></th></tr>"
        "<tr><td><p>Alpha</p></td><td><p>2019</p></td><td><p>Asset freeze</p></td></tr>"
        "<tr><td><p>Beta</p></td><td><p></p></td><td><ul><li>Travel ban</li></ul></td></tr>"
        "<tr><td>Gamma<br>Delta</td><td>2020</td><td>Chair</td></tr>")
    assert lines[1:5] == ["Name | Listed on | Measure", "Alpha | 2019 | Asset freeze",
                          "Beta | | Travel ban", "Gamma Delta | 2020 | Chair"]


def test_a_table_that_holds_a_table_is_layout_and_the_inner_table_keeps_its_rows() -> None:
    lines = _table("<tr><td>Alpha</td><td><table><tr><td>inner one</td><td>inner two</td></tr>"
                   "</table></td><td>Sanctioned</td></tr>")
    assert lines[1:4] == ["Alpha", "inner one | inner two", "Sanctioned"]


LAYOUT_PAGE = (
    "<html><head><title>Synthetic Judicial Service</title></head><body><table><tr><td>"
    "<h1>Justices of the Synthetic Court</h1><p>The court sits in Fixture City.</p>"
    "<table><tr><th>Name</th><th>Appointed</th></tr><tr><td>Justice Alpha</td><td>2015</td></tr>"
    "<tr><td>Justice Beta</td><td>2018</td></tr><tr><td>Justice Gamma</td><td></td></tr></table>"
    "<h2>Retired</h2><ul><li>Justice Delta - 2001</li><li>Justice Epsilon - 2004</li></ul>"
    f"{FILLER}</td></tr></table></body></html>")


def test_a_page_laid_out_in_a_table_keeps_its_lines_rows_and_headline() -> None:
    result = extract_structured_text(LAYOUT_PAGE)
    assert result is not None and result.headline == "Justices of the Synthetic Court"
    assert result.text.split("\n")[:9] == [
        "Justices of the Synthetic Court", "The court sits in Fixture City.",
        "Name | Appointed", "Justice Alpha | 2015", "Justice Beta | 2018", "Justice Gamma",
        "Retired", "Justice Delta - 2001", "Justice Epsilon - 2004"]  # Gamma is not "Retired"


@pytest.mark.parametrize("opening", [
    "<td>", "<th>", "<dt>", "<table><tr><td>", "<dl><dt>Term<p>", "<table><tr><th>One column",
])
def test_a_stray_or_unclosed_cell_does_not_run_the_page_into_one_line(opening: str) -> None:
    result = extract_structured_text(
        f"{HEAD}{opening}<p>First paragraph.</p><p>Second paragraph.</p>{FILLER}")
    assert result is not None
    lines = result.text.split("\n")
    assert "First paragraph." in lines and "Second paragraph." in lines
    assert max(len(line) for line in lines) < 100


def test_a_definition_term_holding_a_block_is_a_line_of_its_own() -> None:
    result = extract_structured_text(f"{HEAD}<dl><dt><p>Court</p></dt><dd>Supreme Court</dd></dl>"
                                     f"{FILLER}")
    assert result is not None and result.text.split("\n")[1:3] == ["Court", "Supreme Court"]


@pytest.mark.parametrize("name", ["parliament_profile", "government_index"])
def test_a_teaser_card_about_someone_else_does_not_become_the_page(name: str) -> None:
    """One `<article>` on a page is as often a news card as it is the content."""
    card = ("<div class='latest'><article><h3>Court remands Another Person Beta</h3>"
            "<p>Another Person Beta was remanded on Monday over alleged procurement fraud at a "
            "state agency, the third such case this year, a court official said. The court "
            "refused bail and adjourned the case to the end of the month.</p></article></div>")
    assert len(card) > 300  # long enough to have been taken as the whole page
    plain = extraction.extract(_fixture(name))
    result = extraction.extract(_fixture(name).replace("<footer>", card + "<footer>"))
    assert plain is not None and result is not None and not result.is_article
    assert result.headline == plain.headline
    assert result.text.startswith(plain.text)  # the page is all there; the card follows it
    assert not result.text.startswith("Court remands")


def test_a_page_laid_out_in_columns_without_headings_is_not_one_line() -> None:
    paragraphs = "".join(f"<p>Justice Synthetic {n}, appointed {2010 + i}.</p>"
                         for i, n in enumerate(["Alpha", "Beta", "Gamma", "Delta", "Epsilon"]))
    result = extract_structured_text(
        "<html><head><title>Synthetic Court</title></head><body><table><tr>"
        "<td><a href='/'>Home</a><br><a href='/j'>Justices</a><br><a href='/c'>Contact</a></td>"
        f"<td><font size='5'><b>Justices of the Synthetic Court</b></font>{paragraphs}"
        f"<ul><li>Retired: Justice Synthetic Zeta</li></ul>{FILLER}</td></tr></table></body></html>")
    assert result is not None
    lines = result.text.split("\n")
    assert "Justice Synthetic Gamma, appointed 2012." in lines
    assert not [line for line in lines if " | " in line] and max(map(len, lines)) < 100


def test_two_unclosed_cells_do_not_take_the_rest_of_the_page_into_a_row() -> None:
    result = extract_structured_text(f"{HEAD}<table><tr><td>C<td>D<p>After one.</p>"
                                     f"<p>After two.</p>{FILLER}")
    assert result is not None
    lines = result.text.split("\n")
    assert "After one." in lines and "After two." in lines


def test_a_column_title_set_as_a_heading_leaves_a_data_table_a_data_table() -> None:
    lines = _table("<caption><h3>Designations</h3></caption>"
                   "<tr><th><h4>Name</h4></th><th><h4>Listed on</h4></th><th>Measure</th></tr>"
                   "<tr><td>Alpha<h5 hidden>x</h5></td><td></td><td>Asset freeze</td></tr>")
    assert lines[1:4] == ["Designations", "Name | Listed on | Measure", "Alpha | | Asset freeze"]


def test_a_page_longer_than_the_parser_is_given_says_it_was_cut() -> None:
    padding = "<!--" + "x" * structured_text._MAX_HTML_CHARS + "-->"
    page = f"{HEAD}{FILLER}{padding}<p>TAIL</p></body></html>"
    direct = extract_structured_text(page)
    assert direct is not None and "TAIL" not in direct.text and direct.truncated
    isolated = extraction.extract_isolated(page)
    assert isolated is not None and isolated.truncated
    assert not extraction.extract_isolated(f"{HEAD}{FILLER}</body></html>").truncated  # type: ignore[union-attr]


def test_what_is_dropped_still_separates_what_was_around_it() -> None:
    result = extract_structured_text(
        f"{HEAD}<div>Minister: Alpha<form><input value='q'></form>Deputy: Beta<div hidden>x</div>"
        f"Gamma</div><p>Chair<span hidden>x</span>person <b>Delta</b></p>{FILLER}")
    assert result is not None
    lines = result.text.split("\n")
    # A block that was dropped kept its neighbours apart; an inline one sat inside a word.
    assert lines[1:5] == ["Minister: Alpha", "Deputy: Beta", "Gamma", "Chairperson Delta"]


def test_content_no_browser_renders_is_left_out() -> None:
    result = extract_structured_text(
        f"{HEAD}<noembed>NOEMBED-TEXT</noembed><noframes>NOFRAMES-TEXT</noframes>"
        f"<p><ruby>漢<rp>RP-TEXT</rp><rt>kan</rt></ruby></p>{FILLER}")
    assert result is not None
    for marker in ("NOEMBED-TEXT", "NOFRAMES-TEXT", "RP-TEXT"):
        assert marker not in result.text


@pytest.mark.parametrize(("h1", "headline"), [
    ("<h1>Hon. Alpha<br>MP for Fixture East</h1>", "Hon. Alpha"),
    ("<h1><div>Hon. Alpha</div></h1>", "Hon. Alpha"),
    ("<h1><span hidden>x</span> Hon. <em>Alpha</em> </h1>", "Hon. Alpha"),
    ("<h1></h1><h1>Hon. Alpha</h1>", "Hon. Alpha"),
])
def test_the_headline_is_the_first_line_of_the_first_heading_with_text(
    h1: str, headline: str
) -> None:
    result = extract_structured_text(
        f"<html><head><title>Title</title></head><body>{h1}{FILLER}</body></html>")
    assert result is not None and result.headline == headline


def test_a_date_survives_an_xml_encoding_declaration() -> None:
    page = ('<?xml version="1.0" encoding="utf-8"?><html><head><title>T</title>'
            '<meta property="article:published_time" content="2026-03-14T09:30:00+00:00">'
            "</head><body><p>x</p></body></html>")
    assert extraction.published_date(page) == "2026-03-14"
