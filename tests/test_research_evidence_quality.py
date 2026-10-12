"""Evidence quality (docs/research-egress.md, "Extraction"): text that a reader of the page
would not see, or that belongs to something beside the page, is not evidence; an article is
read as an article only when the page says it is one and the text accounts for the page.

Several tests first show what the bare article extractor returns for the same page. That is
the behaviour being guarded against: if it ever stops happening, the guard is still right, and
the test says which half changed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import socket
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("trafilatura")
pytest.importorskip("lxml")

from screening_crawler.research import extract as extraction  # noqa: E402
from screening_crawler.research.extract import ARTICLE_EXTRACTOR_VERSION  # noqa: E402
from screening_crawler.research.page_text import (  # noqa: E402
    TEXT_EXTRACTOR_VERSION,
    extract_article_text,
)
from screening_crawler.research.structured_text import (  # noqa: E402
    STRUCTURED_EXTRACTOR_VERSION,
    read_page,
)

ROOT = Path(__file__).resolve().parents[1]
QUALITY = ROOT / "tests/fixtures/research_quality"
HIDDEN_MARKERS = ("HIDDEN-ATTRIBUTE-TEXT", "TEMPLATE-TEXT", "NOSCRIPT-TEXT", "ARIA-HIDDEN-TEXT",
                  "DISPLAY-NONE-TEXT", "VISIBILITY-HIDDEN-TEXT", "UNTIL-FOUND-TEXT")
SENTENCE = ("Hon. Synthetic Member Alpha told the committee that the procurement review of the "
            "regional water programme would be published in full, with every contract listed. ")


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


def _quality(name: str) -> str:
    return (QUALITY / f"{name}.html").read_text(encoding="utf-8")


def _article_page(body: str, *, head: str = '<meta property="og:type" content="article">',
                  title: str = "Review to be published") -> str:
    return (f"<html><head><title>{title}</title>{head}</head><body><div class='story'>"
            f"<h1>{title}</h1>{body}</div></body></html>")


def _paragraphs(count: int, marker: str = "BODY") -> str:
    return "".join(f"<p>{marker}-{n} {SENTENCE}</p>" for n in range(count))


# --- what a browser would not render is not evidence ---------------------------------------------


def test_the_bare_article_extractor_returns_hidden_and_template_text() -> None:
    """The reason the tree is pruned first. Trafilatura strips the `<template>` tag and keeps
    its text, and has no rule for the `hidden` attribute."""
    bare = extract_article_text(_quality("article_with_hidden_text"))
    assert bare is not None
    assert {m for m in HIDDEN_MARKERS if m in bare.text} >= {
        "HIDDEN-ATTRIBUTE-TEXT", "TEMPLATE-TEXT", "UNTIL-FOUND-TEXT"}


def test_hidden_template_and_noscript_text_never_reaches_article_evidence() -> None:
    result = extraction.extract(_quality("article_with_hidden_text"))
    assert result is not None and result.is_article
    assert result.extractor_version == ARTICLE_EXTRACTOR_VERSION != TEXT_EXTRACTOR_VERSION
    assert [m for m in HIDDEN_MARKERS if m in result.text + result.headline] == []
    assert "convicted" not in result.text and "resigned in disgrace" not in result.text
    # What stood either side of the hidden blocks is all still there, in order.
    assert result.text.index("during the third session") < result.text.index("BODY-AFTER")
    assert result.text.count("BODY") == 6
    assert result.published_date == "2026-03-14"


@pytest.mark.parametrize("hidden", [
    "<span hidden>HIDDEN</span>",
    '<span style="display: none">HIDDEN</span>', '<b style="visibility:hidden">HIDDEN</b>',
    '<i style="color:red; DISPLAY : none !important">HIDDEN</i>',
    "<span hidden>HIDDEN<template>HIDDEN</template><span hidden>HIDDEN</span></span>",
])
def test_removing_an_undrawn_inline_element_leaves_the_word_around_it_whole(hidden: str) -> None:
    """`inter<span hidden>…</span>national` is one word on screen and one word here."""
    page = _article_page(f"<p>INTER{hidden}NATIONAL {SENTENCE}</p>{_paragraphs(4)}")
    for result in (extraction.extract(page), extraction.extract(page.replace("og:type", "x"))):
        assert result is not None and "HIDDEN" not in result.text
        assert "INTERNATIONAL Hon." in result.text  # in article text and in structured text


@pytest.mark.parametrize("separator", [
    '<span aria-hidden="true"> / </span>', '<span aria-hidden="true">|</span>',
    '<span class="sr-only">, </span>', '<i class="visually-hidden">and</i>',
])
def test_an_inline_element_that_is_drawn_but_dropped_still_separates_its_neighbours(
    separator: str
) -> None:
    """`aria-hidden` and the screen-reader classes mark text that is on screen or spoken. It
    is dropped, and the names either side of it must not become one word."""
    page = _article_page(f"<p>MENSAH{separator}OWUSU {SENTENCE}</p>{_paragraphs(4)}")
    for result in (extraction.extract(page), extraction.extract(page.replace("og:type", "x"))):
        assert result is not None and "MENSAHOWUSU" not in result.text
        assert "MENSAH OWUSU Hon." in result.text


@pytest.mark.parametrize("hidden", [
    "<div hidden>HIDDEN</div>", "<template>HIDDEN</template>", "<p hidden>HIDDEN</p>",
    '<section aria-hidden="true"><p>HIDDEN</p></section>',
])
def test_removing_a_hidden_block_does_not_join_what_stood_either_side(hidden: str) -> None:
    page = _article_page(f"<div>ALPHA{hidden}OMEGA {SENTENCE}</div>{_paragraphs(4)}")
    reading = read_page(page)
    assert reading is not None and "HIDDEN" not in reading.visible_html
    assert "ALPHAOMEGA" not in reading.visible_html and "ALPHA" in reading.visible_html
    structured = extraction.extract(page.replace("og:type", "x"))
    assert structured is not None and "HIDDEN" not in structured.text
    assert "ALPHAOMEGA" not in structured.text and "ALPHA" in structured.text
    article = extraction.extract(page)
    assert article is not None and "HIDDEN" not in article.text


@pytest.mark.parametrize("style", [
    "backface-visibility: hidden", "overflow: hidden", "text-overflow: clip; overflow:hidden",
    "display: block", "visibility: visible", "--note: 'display:none'",
])
def test_a_style_that_only_mentions_hiding_does_not_hide(style: str) -> None:
    """In the walk and in what is removed from the tree. Trafilatura has a looser rule of
    its own (any style containing "hidden" or "display:none"), which is its to keep."""
    page = _article_page(f'<p style="{style}">SHOWN {SENTENCE}</p>{_paragraphs(4)}')
    reading = read_page(page)
    structured = extraction.extract(page.replace("og:type", "x"))
    assert reading is not None and "SHOWN" in reading.visible_html
    assert structured is not None and "SHOWN" in structured.text


def test_a_hidden_heading_or_hidden_declaration_does_not_make_a_page_an_article() -> None:
    page = ("<html><head><title>Profile</title></head><body>"
            f"<article hidden><h1>Hidden story</h1>{_paragraphs(5, 'HIDDEN')}</article>"
            f"<div><h1>Hon. Synthetic Member Alpha</h1>{_paragraphs(3, 'PROFILE')}</div>"
            "</body></html>")
    result = extraction.extract(page)
    assert result is not None and not result.is_article
    assert result.headline == "Hon. Synthetic Member Alpha" and "HIDDEN" not in result.text


# --- a teaser beside the page is not the page ---------------------------------------------------


@pytest.mark.parametrize(("name", "headline", "expected"), [
    ("profile_with_two_paragraph_card", "Hon. Synthetic Member Alpha",
     "Member of Parliament for Fixture East"),
    ("table_page_with_two_paragraph_card", "Designated persons",
     "Synthetic Person Beta | SYN.002 | | Travel ban"),
    ("index_with_three_cards", "Members of Parliament",
     "Hon. Synthetic Member Alpha - Fixture Alpha Constituency"),
])
def test_a_news_card_on_a_page_that_is_not_an_article_does_not_replace_the_page(
    name: str, headline: str, expected: str
) -> None:
    html = _quality(name)
    bare = extract_article_text(html)  # the card, under the page's own headline
    assert bare is not None and bare.text.startswith("Court remands")
    assert bare.headline == headline and expected not in bare.text
    result = extraction.extract(html)
    assert result is not None and not result.is_article
    assert result.extractor_version == STRUCTURED_EXTRACTOR_VERSION
    assert result.headline == headline and expected in result.text.split("\n")
    assert not result.text.startswith("Court remands") and result.published_date is None


def test_a_standfirst_does_not_cost_the_article_its_body() -> None:
    result = extraction.extract(_quality("article_teaser_then_body"))
    assert result is not None and result.is_article
    for session in ("first", "second", "third", "fourth", "fifth", "sixth"):
        assert f"during the {session} session" in result.text


def test_teaser_cards_beside_a_full_article_stay_out_of_it() -> None:
    result = extraction.extract(_quality("article_with_teaser_rail"))
    assert result is not None and result.is_article
    assert "during the sixth session" in result.text
    assert "TEASER-CARD" not in result.text and "Another Person" not in result.text


def test_a_page_of_teasers_is_a_list_and_not_an_article() -> None:
    html = _quality("teaser_index")
    assert extract_article_text(html) is not None  # the bare extractor calls it an article
    result = extraction.extract(html)
    assert result is not None and not result.is_article and result.headline == "Politics"
    for name in ("Beta", "Gamma", "Delta"):
        assert f"Another Person {name}" in result.text


# --- the page has to say it is an article ---------------------------------------------------------


@pytest.mark.parametrize("head", [
    '<meta property="og:type" content="article">',
    '<meta property="og:type" content=" Article ">',
    '<meta property="article:published_time" content="2026-03-14T09:30:00+00:00">',
    '<meta name="article:section" content="Politics">',
])
def test_a_page_that_declares_an_article_in_its_metadata_is_read_as_one(head: str) -> None:
    result = extraction.extract(_article_page(_paragraphs(5), head=head))
    assert result is not None and result.is_article and "BODY-4" in result.text


@pytest.mark.parametrize("wrapper", [
    "<article><h1>Review to be published</h1>{body}</article>",
    '<div itemscope itemtype="https://schema.org/NewsArticle"><h1>Review to be published</h1>'
    "{body}</div>",
    '<div itemscope itemtype="http://schema.org/BlogPosting/"><h1>Review to be published</h1>'
    "{body}</div>",
])
def test_a_page_that_declares_an_article_in_its_body_is_read_as_one(wrapper: str) -> None:
    page = (f"<html><head><title>Review to be published</title></head><body>"
            f"{wrapper.format(body=_paragraphs(5))}</body></html>")
    result = extraction.extract(page)
    assert result is not None and result.is_article and "BODY-4" in result.text


@pytest.mark.parametrize("head", [
    "", '<meta property="og:type" content="website">', '<meta property="og:type" content="">',
    '<meta property="og:title" content="article">', '<meta name="description" content="article">',
])
def test_a_page_that_does_not_declare_an_article_is_read_as_structured_text(head: str) -> None:
    page = _article_page(_paragraphs(5), head=head)
    assert extract_article_text(page) is not None  # article-shaped all the same
    result = extraction.extract(page)
    assert result is not None and not result.is_article
    assert result.headline == "Review to be published" and result.published_date is None
    assert all(f"BODY-{n}" in result.text for n in range(5))  # nothing is lost, only the label


def test_a_schema_type_that_is_not_an_article_does_not_count() -> None:
    page = ("<html><head><title>T</title></head><body>"
            '<div itemscope itemtype="https://schema.org/Person"><h1>Hon. Member Alpha</h1>'
            f"{_paragraphs(5)}</div></body></html>")
    result = extraction.extract(page)
    assert result is not None and not result.is_article


# --- and the article has to account for the page -------------------------------------------------


def _banner_page(rows: int) -> str:
    """Shaped like a live page read on 2026-10-12: it declares an article, its content is a
    long table inside `<main>`, and the only paragraphs are a cookie banner outside it."""
    banner = "".join(
        f"<p>BANNER-{n} We use some essential cookies to make this website work, and we would "
        "like to set additional cookies to understand how you use it and improve it.</p>"
        for n in range(4))
    table = "".join(f"<tr><td>Synthetic Person {n}</td><td>SYN.{n:04d}</td><td>Asset freeze"
                    "</td></tr>" for n in range(rows))
    return ('<html><head><title>The Synthetic Sanctions List</title>'
            '<meta property="og:type" content="article"></head><body>'
            f'<div id="notice">{banner}</div><main><h1>The Synthetic Sanctions List</h1>'
            f"<table><tr><th>Name</th><th>Reference</th><th>Measure</th></tr>{table}</table>"
            "</main></body></html>")


def test_a_cookie_banner_is_not_the_article_of_a_page_whose_content_is_elsewhere() -> None:
    html = _banner_page(60)
    bare = extract_article_text(html)
    assert bare is not None and "BANNER-0" in bare.text and "SYN.0001" not in bare.text
    result = extraction.extract(html)
    assert result is not None and not result.is_article
    assert result.headline == "The Synthetic Sanctions List"
    assert "Synthetic Person 59 | SYN.0059 | Asset freeze" in result.text.split("\n")
    assert "BANNER" not in result.text  # the walk starts at <main>


def test_the_first_part_of_a_long_page_is_not_taken_for_the_whole() -> None:
    """Shaped like another live page: a long text in table cells, of which the article
    extractor returns only what happens to sit in paragraphs."""
    cells = "".join(f"<tr><td>HISTORY-{n} {SENTENCE}{SENTENCE}</td></tr>" for n in range(12))
    page = ('<html><head><title>About the Synthetic Bank</title>'
            '<meta property="og:type" content="article"></head><body><h1>About the Synthetic Bank'
            f"</h1>{_paragraphs(3, 'INTRO')}<table>{cells}</table></body></html>")
    bare = extract_article_text(page)
    assert bare is not None and "INTRO-0" in bare.text and "HISTORY-11" not in bare.text
    reading = read_page(page)
    assert reading is not None and reading.running_chars > 2 * len(bare.text)
    result = extraction.extract(page)
    assert result is not None and not result.is_article
    assert "INTRO-0" in result.text and "HISTORY-11" in result.text


def test_an_article_that_is_most_of_its_page_stays_an_article() -> None:
    # Running text beside the article, but less of it than the article itself.
    beside = "".join(f"<div class='about'><p>ABOUT-{n} {SENTENCE}</p></div>" for n in range(3))
    page = _article_page(_paragraphs(6)) .replace("</body>", f"{beside}</body>")
    reading = read_page(page)
    result = extraction.extract(page)
    assert reading is not None and result is not None and result.is_article
    assert len(result.text) < reading.running_chars <= 2 * len(result.text)


def test_a_menu_of_links_does_not_count_against_an_article() -> None:
    menu = "<ul>" + "".join(f"<li><a href='/s/{n}'>MENU-{n} {SENTENCE}</a></li>"
                            for n in range(40)) + "</ul>"
    page = _article_page(_paragraphs(5)).replace("<div class='story'>",
                                                 f"{menu}<div class='story'>")
    reading = read_page(page)
    result = extraction.extract(page)
    assert reading is not None and reading.structured is not None
    assert len(reading.structured.text) > 4 * reading.running_chars  # the menu is not running text
    assert result is not None and result.is_article and "MENU-" not in result.text


# --- unchanged properties: encoding, malformed markup, bounds, determinism ------------------------


def test_names_outside_ascii_survive_the_article_path_with_no_charset_declared() -> None:
    result = extraction.extract(_quality("non_ascii_article"))
    assert result is not None and result.is_article
    assert result.headline == "Ɔsɛe Kwadwo Méndez to chair review"
    assert "Nana Akufo‑Addo — “the review” café Ɔsɛe Kwadwo Méndez;" in result.text


def test_malformed_markup_still_yields_the_text_a_reader_would_see() -> None:
    result = extraction.extract(_quality("malformed_article"))
    assert result is not None and result.headline == "Review to be published in full"
    assert "during the sixth session" in result.text
    assert "NAVIGATION-NEWS" not in result.text and "FOOTER-COPYRIGHT" not in result.text


@pytest.mark.parametrize("name", ["article_with_hidden_text", "profile_with_two_paragraph_card",
                                  "teaser_index", "malformed_article"])
def test_the_same_page_reads_the_same_every_time_here_and_in_the_child(name: str) -> None:
    html = _quality(name)
    here = [extraction.extract(html) for _ in range(3)]
    assert here[0] is not None and here[0] == here[1] == here[2]
    assert extraction.extract_isolated(html) == extraction.extract_isolated(html) == here[0]


@pytest.mark.parametrize("hostile", [
    "<div hidden>" * 150_000 + "x",
    "<template>" * 100_000 + "x",
    "".join(f"<p hidden>{n}</p>" for n in range(120_000)),
    '<meta property="og:type" content="article">' * 100_000,
    "".join(f'<div itemscope itemtype="https://schema.org/A{n}"></div>' for n in range(60_000)),
    "<article>" * 100 + "<h1 hidden>T</h1>" * 10_000 + "<h1>Shown</h1>",
    "".join(f"<span hidden>{n}</span>tail text {n} " for n in range(60_000)),
    "<b hidden>x</b>" * 60_000 + "y" * 400_000,
    "".join(f"<h3>Heading {n}</h3>" for n in range(40_000)),
], ids=["nested-hidden", "nested-template", "many-hidden", "many-meta", "many-itemtype",
        "many-hidden-h1", "hidden-with-tails", "hidden-run-then-long-text", "many-headings"])
def test_hostile_markup_aimed_at_the_new_checks_is_read_quickly_or_not_at_all(
    hostile: str
) -> None:
    """Measured on the reading itself, which is the new code: each of these was quadratic in
    one draft or another, and lxml's own `drop_tree` still is."""
    page = (f'<html><head><title>T</title><meta property="og:type" content="article"></head>'
            f"<body>{hostile}{_paragraphs(4)}</body></html>")
    started = time.monotonic()
    read_page(page)
    assert time.monotonic() - started < 4


def test_a_page_that_grows_past_the_parser_limit_when_rewritten_is_not_cut_short() -> None:
    """Re-serialising escapes every bare `&` and quotes every attribute. A page under the
    limit can come out over it; the article extractor would then read a prefix and return an
    article that stops early, with nothing to say so. Such a page is read whole instead."""
    links = "".join(f"<link rel=alternate href=/s?a={n}&b={n}&c={n}&d={n}&e={n}>"
                    for n in range(22_000))
    body = "".join(f"<p>PARAGRAPH-{n} {SENTENCE}</p>" for n in range(800))
    page = _article_page(f"{body}<p>FINAL-PARAGRAPH He was acquitted of all charges on appeal."
                         "</p>", head=f'<meta property="og:type" content="article">{links}')
    reading = read_page(page)
    assert reading is not None and len(page) < 2_000_000 < len(reading.visible_html)
    # The limit falls in the middle of the article: the prefix holds its start and not its end.
    assert (reading.visible_html.index("PARAGRAPH-50 ") < 2_000_000
            < reading.visible_html.index("PARAGRAPH-750 "))
    prefix = extract_article_text(reading.visible_html)  # what the article path would return
    assert prefix is not None and "PARAGRAPH-0 " in prefix.text
    assert "FINAL-PARAGRAPH" not in prefix.text
    result = extraction.extract(page)
    assert result is not None and not result.is_article
    assert "PARAGRAPH-0 " in result.text and "FINAL-PARAGRAPH He was acquitted" in result.text


def test_declaring_an_article_gives_a_page_no_way_to_be_fetched_or_resolved(
    tmp_path: Path
) -> None:
    """The declaration is read from attributes and nothing in it is followed. Checked with a
    real listener and a real file, in the real child and in this process."""
    canary = tmp_path / "canary.txt"
    canary.write_text("CANARY-FILE-CONTENT")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.5)
    there = f"http://127.0.0.1:{listener.getsockname()[1]}"
    head = (f'<meta property="og:type" content="article"><meta property="og:url" content="{there}/og">'
            f'<meta property="article:author" content="{there}/author">'
            f'<link rel="canonical" href="{there}/canonical"><base href="{there}/">'
            f'<meta http-equiv="refresh" content="0;url={there}/refresh">')
    body = (f'<div itemscope itemtype="{there}/schema.org/NewsArticle" itemid="file://{canary}">'
            f'<template><img src="{there}/t.png"><iframe src="file://{canary}"></iframe></template>'
            f'<div hidden><object data="file://{canary}"></object></div>{_paragraphs(5)}</div>')
    page = _article_page(body, head=head)
    try:
        results = [extraction.extract_isolated(page), extraction.extract(page)]
        with pytest.raises(TimeoutError):
            listener.accept()
    finally:
        listener.close()
    for result in results:
        assert result is not None and result.is_article and "BODY-4" in result.text
        assert "CANARY-FILE-CONTENT" not in result.text


# --- found by independent review: a declaration permits the article path, it does not decide ---


@pytest.mark.parametrize(("name", "headline", "expected"), [
    ("declared_profile_with_card", "Hon. Synthetic Member Alpha",
     "Member of Parliament for Fixture East"),
    ("declared_table_with_card", "Designated persons",
     "Synthetic Person Beta | SYN.002 | | Travel ban"),
    ("declared_index_with_cards", "Members of Parliament",
     "Hon. Synthetic Member Alpha - Fixture Alpha Constituency"),
    ("profile_with_itemtype_card", "Hon. Synthetic Member Alpha",
     "Member of Parliament for Fixture East"),
    ("declared_linked_table_with_card", "Designated persons", "Synthetic Person 150 | SYN.0150"),
    ("declared_linked_index_with_card", "Members of Parliament", "Hon. Synthetic Member 77"),
    ("declared_definition_profile_with_card", "Justice Synthetic Gamma", "Court: Supreme Court"),
])
def test_a_news_card_does_not_replace_a_page_that_calls_itself_an_article(
    name: str, headline: str, expected: str
) -> None:
    html = _quality(name)
    bare = extract_article_text(html)  # the card, under the page's headline, the page gone
    assert bare is not None and bare.headline == headline
    assert "TEASER-CARD" in bare.text and expected.split(" | ")[0] not in bare.text
    result = extraction.extract(html)
    assert result is not None and not result.is_article
    assert result.headline == headline and expected in result.text.split("\n")
    assert not result.text.startswith(("Court remands", "TEASER-CARD"))


PROFILE = ("<div class='profile'><h1>Hon. Synthetic Member Alpha</h1><dl><dt>Constituency</dt>"
           "<dd>Fixture East</dd><dt>Party</dt><dd>Example Democratic Party</dd><dt>Profession"
           "</dt><dd>Lawyer</dd></dl></div>")
CARD_TEXT = "".join(f"<p>CARD-{n} Another Person Beta was remanded on Monday over alleged "
                    "procurement fraud at a state agency, a court official said, and the case "
                    "was adjourned to the end of the month.</p>" for n in range(3))


@pytest.mark.parametrize("card", [
    "<div class='card'><h3>Court remands Another Person Beta</h3>{text}</div>",
    "<div><h3>Court remands Another Person Beta</h3>{text}</div>",
    "<div class='news'><div class='news-item'><h2>Court remands Another Person Beta</h2>{text}"
    "</div></div>",
    "<div class='row'><div class='col-md-4'><h4>Court remands Another Person Beta</h4>{text}"
    "</div></div>",
    "<div class='widget'><h3>Latest</h3><div class='item'><h4>Court remands Another Person "
    "Beta</h4>{text}</div></div>",
    "<ul><li><h3>Court remands Another Person Beta</h3>{text}</li></ul>",
    "<section><h2><a href='/s/1'>Court remands Another Person Beta</a></h2>{text}</section>",
], ids=["class-card", "no-class", "news-item", "bootstrap-column", "widget", "list-item",
        "linked-heading"])
@pytest.mark.parametrize("card_first", [False, True], ids=["card-after", "card-before"])
def test_text_under_another_heading_is_not_the_page_whatever_the_card_is_made_of(
    card: str, card_first: bool
) -> None:
    """The article extractor keeps a card's heading for some class names and drops it for
    most. Where the text sits in the page does not depend on that."""
    block = card.format(text=CARD_TEXT)
    body = block + PROFILE if card_first else PROFILE + block
    page = ('<html><head><title>Hon. Synthetic Member Alpha | Synthetic Parliament</title>'
            f'<meta property="og:type" content="article"></head><body>{body}</body></html>')
    bare = extract_article_text(page)
    assert bare is not None and "CARD-0" in bare.text and "Fixture East" not in bare.text
    result = extraction.extract(page)
    assert result is not None and not result.is_article
    assert result.headline == "Hon. Synthetic Member Alpha"
    assert "Constituency: Fixture East" in result.text.split("\n")


@pytest.mark.parametrize(("in_profile", "card_opening"), [
    ("<dt>Last updated</dt><dd>12 March 2026</dd>", "<p class='date'>12 March 2026</p>"),
    ("<dt>Last updated</dt><dd>12 March 2026</dd>", "<p><small>12 March 2026</small></p>"),
    ("", "<p class='loc'>Fixture East</p>"),
    ("<dt>Entered by</dt><dd>By Staff Reporter</dd>", "<p>By Staff Reporter</p>"),
    ("<dt>Note</dt><dd>a</dd>", "<p>a</p>"),
], ids=["date", "date-small", "place", "byline", "one-letter"])
def test_a_short_first_line_that_also_occurs_in_the_page_does_not_vouch_for_a_card(
    in_profile: str, card_opening: str
) -> None:
    """A card whose first line is a date or a place the page also states. Finding those
    words under the page's own headline says nothing about where the card sits."""
    profile = PROFILE.replace("</dl>", f"{in_profile}</dl>")
    page = ('<html><head><title>Hon. Synthetic Member Alpha | Synthetic Parliament</title>'
            '<meta property="og:type" content="article"></head><body>'
            f"{profile}<div class='card'><h3>MP commissions plant</h3>{card_opening}{CARD_TEXT}"
            "</div></body></html>")
    result = extraction.extract(page)
    assert result is not None and not result.is_article
    assert result.headline == "Hon. Synthetic Member Alpha"
    assert "Constituency: Fixture East" in result.text.split("\n")


@pytest.mark.parametrize("block", [
    "<div id='notice'>{banner}</div>", "<div class='card'>{card}</div>",
], ids=["banner", "headless-card"])
def test_a_block_above_the_pages_headline_is_not_its_article(block: str) -> None:
    banner = "".join(f"<p>BANNER-{n} We use some essential cookies to make this website work, "
                     "and we would like to set additional cookies to understand how you use "
                     "it.</p>" for n in range(4))
    page = ('<html><head><title>Hon. Synthetic Member Alpha | Synthetic Parliament</title>'
            '<meta property="og:type" content="article"></head><body>'
            f"{block.format(banner=banner, card=CARD_TEXT)}{PROFILE}</body></html>")
    bare = extract_article_text(page)
    assert bare is not None and "Fixture East" not in bare.text
    result = extraction.extract(page)
    assert result is not None and not result.is_article
    assert "Constituency: Fixture East" in result.text.split("\n")


def test_a_long_headline_printed_at_the_top_of_the_article_text_is_not_its_first_line() -> None:
    title = "Review of the regional water programme to be published in full, says the member"
    page = (f'<html><head><title>{title}</title></head><body><article><h1>{title}</h1>'
            f"{_paragraphs(5)}</article></body></html>")
    bare = extract_article_text(page)
    assert bare is not None and bare.text.startswith(title)  # the extractor prints it first
    result = extraction.extract(page)
    assert result is not None and result.is_article and "BODY-4" in result.text
    # And a long headline does not vouch for a card below it.
    card = f"<div class='card'><h3>Court remands Another Person Beta</h3>{CARD_TEXT}</div>"
    profile = PROFILE.replace("Hon. Synthetic Member Alpha", title)
    carded = (f'<html><head><title>{title}</title><meta property="og:type" content="article">'
              f"</head><body>{profile}{card}</body></html>")
    result = extraction.extract(carded)
    assert result is not None and not result.is_article and result.headline == title


def test_a_headline_that_contains_a_cards_opening_does_not_count_as_a_place_for_it() -> None:
    lead = "Another Person Beta was remanded on Monday over alleged procurement fraud"
    card = (f"<div class='card'><h3>Court report</h3><p>{lead} at a state agency, a court "
            f"official said.</p>{CARD_TEXT}</div>")
    profile = PROFILE.replace("Hon. Synthetic Member Alpha", f"Statement: {lead}")
    page = ('<html><head><title>Statement</title><meta property="og:type" content="article">'
            f"</head><body>{profile}{card}</body></html>")
    result = extraction.extract(page)
    assert result is not None and not result.is_article
    assert "Constituency: Fixture East" in result.text.split("\n")


def test_a_card_whose_opening_also_stands_under_the_pages_headline_is_a_known_gap() -> None:
    """Recorded, not hidden. The page lists a headline as a link under its own `<h1>` and
    carries the same story as a card. Position cannot tell this from a publisher's article
    that prints its first sentence twice, which must stay an article."""
    headline = "Court remands Another Person Beta over alleged procurement fraud"
    listing = f"<ul><li><a href='/s/1'>{headline}</a></li><li><a href='/s/2'>Other</a></li></ul>"
    page = ('<html><head><title>Hon. Synthetic Member Alpha</title>'
            '<meta property="og:type" content="article"></head><body><div>'
            f"<h1>Hon. Synthetic Member Alpha</h1>{listing}</div>"
            f"<article class='card'><h3>{headline}</h3>{CARD_TEXT}</article></body></html>")
    result = extraction.extract(page)
    assert result is not None and result.is_article and "CARD-0" in result.text
    assert "Known gap, second" in (ROOT / "docs/research-egress.md").read_text(encoding="utf-8")


def test_an_article_whose_first_sentence_is_printed_twice_is_still_an_article() -> None:
    """Shaped like a live article read on 2026-10-12: the first sentence is a standfirst
    under the headline, a "listen" widget with a heading of its own follows, and the body
    then opens with the same sentence."""
    lead = f"<p>LEAD {SENTENCE}</p>"
    page = _article_page(f"<div class='excerpt'>{lead}</div><div class='player'><h3>Listen to "
                         f"this article</h3></div><div class='body'>{lead}{_paragraphs(5)}</div>"
                         "<h3>Comments</h3>")
    result = extraction.extract(page)
    assert result is not None and result.is_article and "BODY-4" in result.text


def test_a_page_with_no_heading_in_its_content_root_can_still_be_an_article() -> None:
    page = ('<html><head><title>Review to be published</title>'
            '<meta property="og:type" content="article"></head><body>'
            f"<h1>Review to be published</h1><main>{_paragraphs(5)}</main></body></html>")
    result = extraction.extract(page)
    assert result is not None and result.is_article and "BODY-4" in result.text


def test_a_page_with_no_heading_of_its_own_does_not_lend_its_title_to_a_headed_card() -> None:
    page = ('<html><head><title>Hon. Synthetic Member Alpha</title>'
            '<meta property="og:type" content="article"></head><body><div><h2>Profile</h2>'
            "<p>Member of Parliament for Fixture East since 2021.</p></div>"
            f"<div class='card'><h3>Court remands Another Person Beta</h3>{CARD_TEXT}</div>"
            "</body></html>")
    result = extraction.extract(page)
    assert result is not None and not result.is_article
    assert "Member of Parliament for Fixture East" in result.text


@pytest.mark.parametrize("opening", [
    "<p><b>ACCRA</b>, Sept. 1 — {s}</p>", "<p><a href='/x'>Hon. Alpha</a> said: {s}</p>",
    "<p><span class='dropcap'>T</span>he member said. {s}</p>", "<p>Line one<br>line two. {s}</p>",
    "<p>The inter\u00adnational re\u200bview. {s}</p>", "<p>&ldquo;Quoted&rdquo; &amp; more. {s}</p>",
    "<ul><li>First point. {s}</li></ul>", "<blockquote><p>Quoted opening. {s}</p></blockquote>",
], ids=["bold-dateline", "link", "drop-cap", "line-break", "soft-hyphen", "entities", "list",
        "blockquote"])
def test_an_article_with_an_awkward_first_paragraph_is_still_found_in_its_page(
    opening: str
) -> None:
    page = _article_page(opening.format(s=SENTENCE) + _paragraphs(4))
    result = extraction.extract(page)
    assert result is not None and result.is_article and "BODY-3" in result.text


def test_text_from_a_block_the_page_marks_as_beside_the_point_is_not_its_article() -> None:
    html = _quality("declared_short_profile_with_side_card")
    bare = extract_article_text(html)
    assert bare is not None and "TEASER-CARD" in bare.text  # the bare extractor takes it
    assert extraction.extract(html) is None  # too little of the page's own to be evidence


def test_a_card_with_no_heading_element_of_its_own_is_a_known_gap() -> None:
    """Recorded, not hidden: no h1 to h6 separates this card from the page, and it is more
    than half of the page's running text. A card titled with `<b>` or `<p class="title">`
    looks headed to a reader and is in the same position. If this ever starts passing the other way,
    the limit in the documentation should come out."""
    result = extraction.extract(_quality("declared_profile_with_headless_card"))
    assert result is not None and result.is_article and result.text.startswith("TEASER-CARD")
    assert "Known gap" in (ROOT / "docs/research-egress.md").read_text(encoding="utf-8")


def test_an_article_that_opens_with_a_second_heading_is_read_whole_as_structured_text() -> None:
    result = extraction.extract(_quality("declared_article_opening_with_subheading"))
    assert result is not None and not result.is_article  # the cost of the opening-heading rule
    assert all(f"BODY-{n}" in result.text for n in range(6)) and "STANDFIRST" in result.text


def test_a_subheading_further_down_an_article_does_not_count_against_it() -> None:
    page = _article_page(f"{_paragraphs(2)}<h2>Background</h2>{_paragraphs(3, 'LATER')}")
    result = extraction.extract(page)
    assert result is not None and result.is_article and "LATER-2" in result.text


@pytest.mark.parametrize("itemtype", [
    "https://schema.org/ParticleArticle", "https://schema.org/WarrantReport",
    "https://evil.example/schema.org/NewsArticle", "https://schema.org/Person",
    "ftp://schema.org/NewsArticle", "schema.org/NewsArticle", "NewsArticle",
])
def test_only_a_real_schema_org_article_type_around_the_heading_declares_one(
    itemtype: str
) -> None:
    def page(kind: str) -> str:
        return ("<html><head><title>Review to be published</title></head><body>"
                f'<div itemscope itemtype="{kind}"><h1>Review to be published</h1>'
                f"{_paragraphs(5)}</div></body></html>")

    loose = extraction.extract(page(itemtype))
    assert loose is not None and not loose.is_article
    for real in ("https://schema.org/NewsArticle", "http://schema.org/Article/",
                 "https://schema.org/Thing https://schema.org/BlogPosting"):
        strict = extraction.extract(page(real))
        assert strict is not None and strict.is_article


# --- provenance ------------------------------------------------------------------------------------


def test_the_two_kinds_of_evidence_carry_different_versions_and_neither_is_the_bare_one() -> None:
    assert ARTICLE_EXTRACTOR_VERSION == "trafilatura-2.0.0|precision|v1+declared-visible|v1"
    assert STRUCTURED_EXTRACTOR_VERSION == "lxml-structured-html|v2"
    assert len({ARTICLE_EXTRACTOR_VERSION, STRUCTURED_EXTRACTOR_VERSION,
                TEXT_EXTRACTOR_VERSION}) == 3
    assert len(ARTICLE_EXTRACTOR_VERSION) <= 80  # the engine's column for it
    article = extraction.extract(_quality("news_article"))
    profile = extraction.extract(_quality("profile_with_teaser_card"))
    assert article is not None and article.extractor_version == ARTICLE_EXTRACTOR_VERSION
    assert profile is not None and profile.extractor_version == STRUCTURED_EXTRACTOR_VERSION


# --- the E1 evaluation itself ----------------------------------------------------------------------

FROZEN_CASES_SHA256 = "a321b13fe9186b761f17b78cae80ed2dc0923a5ffd7023fe1f66e7ef23f7a736"
FROZEN_CASES_V2_SHA256 = "6a70efa29390fee3f41856dece2059e5b4e83a256dcea9e08fbaa5281076f243"


def _harness() -> Any:
    path = ROOT / "tools/research_eval/quality_eval.py"
    spec = importlib.util.spec_from_file_location("quality_eval", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_quality_cases_and_their_gates_are_the_frozen_ones() -> None:
    raw = (ROOT / "tools/research_eval/quality_cases.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FROZEN_CASES_SHA256, (
        "the E1 cases or gates changed; they were frozen before the extractor was changed")
    cases = json.loads(raw)
    assert len(cases["cases"]) == 20 and cases["version"] == "e1-quality-dev-v1"
    gates = cases["gates"]
    assert gates["content_safety"] == {"forbidden_segments_found_max": 0,
                                       "cases_starting_with_a_forbidden_lead_max": 0}
    assert set(gates["fidelity"].values()) == {1.0}
    assert gates["security"]["parser_network_attempts_max"] == 0
    for case in cases["cases"]:
        assert (ROOT / case["fixture"]).is_file() and case["must_contain"]
        assert "synthetic" in (ROOT / case["fixture"]).read_text(encoding="utf-8").lower()


def test_the_second_case_set_is_the_first_plus_cases_and_is_frozen_too() -> None:
    raw = (ROOT / "tools/research_eval/quality_cases_v2.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FROZEN_CASES_V2_SHA256
    v1 = json.loads((ROOT / "tools/research_eval/quality_cases.json").read_bytes())
    v2 = json.loads(raw)
    assert v2["version"] == "e1-quality-dev-v2" and len(v2["cases"]) == 33
    assert v2["cases"][:20] == v1["cases"]  # nothing in the first set was touched
    assert {k: v for k, v in v2["gates"].items() if k not in ("set_by", "definitions")} == {
        k: v for k, v in v1["gates"].items() if k not in ("set_by", "definitions")}
    assert v2["forbidden_everywhere"] == v1["forbidden_everywhere"]
    for case in v2["cases"][20:]:
        assert (ROOT / case["fixture"]).is_file()
        assert "synthetic" in (ROOT / case["fixture"]).read_text(encoding="utf-8").lower()


def test_the_harness_can_expect_a_page_to_yield_nothing() -> None:
    harness = _harness()
    cases = json.loads((ROOT / "tools/research_eval/quality_cases_v2.json").read_text("utf-8"))
    forbidden = [s for group in cases["forbidden_everywhere"].values() for s in group]
    case = next(c for c in cases["cases"] if c["id"] == "r-07")
    row = harness.evaluate(case, forbidden)
    assert (row["path"], row["path_ok"], row["headline_ok"], row["missing"]) == (
        "none", True, True, [])
    wrong = harness.evaluate(case | {"expected_path": "structured"}, forbidden)
    assert (wrong["path_ok"], wrong["headline_ok"]) == (False, False)


def test_the_quality_harness_scores_a_case_and_keeps_no_page_text() -> None:
    harness = _harness()
    cases = json.loads((ROOT / "tools/research_eval/quality_cases.json").read_text("utf-8"))
    forbidden = [s for group in cases["forbidden_everywhere"].values() for s in group]
    by_id = {case["id"]: case for case in cases["cases"]}
    good = harness.evaluate(by_id["q-05"], forbidden)
    assert (good["path"], good["path_ok"], good["headline_ok"], good["missing"],
            good["forbidden_found"], good["deterministic"]) == ("article", True, True, [], [],
                                                                True)
    assert good["extractor"] == ARTICLE_EXTRACTOR_VERSION
    # A case scored against the wrong expectations fails in every way it should.
    wrong = harness.evaluate(by_id["q-05"] | {"expected_path": "structured", "headline": "X",
                                             "must_contain": ["NOT ON THE PAGE"],
                                             "must_not_contain": ["BODY-AFTER"],
                                             "must_not_start_with": "Review to be"}, forbidden)
    assert (wrong["path_ok"], wrong["headline_ok"], wrong["missing"], wrong["forbidden_found"],
            wrong["forbidden_lead"]) == (False, False, ["NOT ON THE PAGE"], ["BODY-AFTER"], True)
    assert "procurement" not in json.dumps([good, wrong])  # labels and counts, no page text
    summary = harness.summarise([good, wrong], cases["gates"])
    assert summary["cases_failing"] == ["q-05"] and not all(summary["gate"].values())
    assert all(harness.summarise([good], cases["gates"])["gate"].values())


def test_the_quality_harness_never_overwrites_a_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    harness = _harness()
    existing = tmp_path / "result.json"
    existing.write_text("{}")
    monkeypatch.setattr("sys.argv", ["quality_eval.py", "--label", "x", "--write", str(existing)])
    assert harness.main() == 2 and existing.read_text() == "{}"
    assert "never overwritten" in capsys.readouterr().err
