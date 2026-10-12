"""Visible text from pages that are not articles: profiles, lists, tables, landing pages.

The article extractor is tuned for precision and returns nothing for a member profile, a list
of judges or a table of designations. This is the fallback for those pages. It is a fixed walk
over the parsed tree: text inside headings, paragraphs, list items, definition lists and table
cells is kept in document order; scripts, styles, templates, navigation, page headers and
footers, forms, controls and elements the markup itself marks hidden are dropped whole. No
scoring, no model, no JavaScript, nothing fetched.

It keeps what the markup shows, not what a browser would paint, and it is lower precision than
article text; it carries its own extractor version so a reader of the evidence can tell the two
apart. What it cannot do, and a reader of its output must know:

- Hidden text is recognised only from the element itself: the `hidden` attribute,
  `aria-hidden="true"`, an inline `display:none` or `visibility:hidden`, and a few framework
  class names. Text hidden by a stylesheet rule, moved off screen, made transparent or sized
  to nothing is kept. So is a closed `<details>` and struck-through `<del>` text.
- A page's own list of links is content, not chrome, unless the markup marks it as navigation.
  Related-story cards and sidebars built from plain `<div>`s are kept with the page.
- A table row is one line with ` | ` between cells and an empty cell kept in place. `rowspan`
  and `colspan` are not expanded, so a value under a spanning cell can sit one column left of
  its heading. A table that holds another table, a heading in a body cell or a cell with more
  than three blocks, or that has one cell to a row, is read as page layout: each of its cells
  is its own line, with no ` | `, and empty cells and row boundaries are not shown. A page
  laid out in columns with fewer blocks than that is read as a data table, one line to a
  row. A ` | ` or `: ` that the page itself wrote cannot be told from a separator.
- A framework class such as `hidden md:block` hides an element on small screens only; it is
  read as hidden everywhere, so that content is lost.
- Inline elements that touch in the markup touch in the text, as they do in a browser without
  styling.
- It does not judge what a page is. An error page, a sign-in page or a block page that the
  server sent as an ordinary page has headings and paragraphs too; the egress refuses the
  common ones by their wording (`egress.py`), which is a list and not a classifier.

The text is hostile external input: plain text out, never rendered, never an instruction.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice

from lxml import etree
from lxml import html as lxml_html

STRUCTURED_EXTRACTOR_VERSION = "lxml-structured-html|v2"
_MAX_HTML_CHARS = 2_000_000
_MAX_TEXT_CHARS = 200_000
_MAX_ELEMENTS = 200_000
# Half the article minimum: a real profile or list page is short, a stub is shorter still.
_MIN_TEXT_CHARS = 200
_OPENING_CHARS = 40  # a line this long is a sentence, not a date, a place or a byline
_MAX_BLOCKS_IN_A_CELL = 3  # more paragraphs or lines than this is a column of a page
_MAX_ROOTS_CONSIDERED = 200  # of each kind; a page with more has no single content root
_CONTROL = {c: None for c in (*range(9), 11, 12, *range(14, 32), 127)}

_DROPPED = frozenset({
    "script", "style", "template", "noscript", "noembed", "noframes", "head", "nav", "aside",
    "form", "button", "input", "select", "option", "optgroup", "datalist", "textarea", "label",
    "fieldset", "dialog", "menu", "iframe", "frame", "frameset", "object", "embed", "applet",
    "svg", "canvas", "audio", "video", "map", "math", "rp",
})
_CHROME = frozenset({"header", "footer"})  # dropped unless inside the page's main content
_CONTENT_ROOTS = frozenset({"main", "article"})
_DROPPED_ROLES = frozenset({"navigation", "banner", "contentinfo", "search", "complementary",
                            "dialog", "alertdialog", "menu", "menubar", "toolbar"})
# Class names that hide an element in the common frameworks. A convention, not a rendering.
_HIDDEN_CLASSES = frozenset({"hidden", "d-none", "sr-only", "visually-hidden",
                             "screen-reader-text", "invisible"})
# Whole declarations, so `backface-visibility: hidden` on a visible element is not one of them.
_HIDDEN_STYLES = frozenset({"display:none", "visibility:hidden", "content-visibility:hidden"})
# Removing one of these from the middle of a word leaves the word whole, as a browser does.
_INLINE = frozenset({"a", "abbr", "b", "bdi", "bdo", "cite", "code", "data", "del", "dfn", "em",
                     "font", "i", "ins", "kbd", "mark", "q", "s", "samp", "small", "span",
                     "strong", "sub", "sup", "time", "u", "var"})
_SCHEMA_ARTICLE_TYPES = frozenset({
    "Article", "NewsArticle", "ReportageNewsArticle", "AnalysisNewsArticle",
    "BackgroundNewsArticle", "OpinionNewsArticle", "ReviewNewsArticle", "BlogPosting",
    "TechArticle", "ScholarlyArticle", "Report"})
_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_BLOCKS = frozenset({
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "ul", "ol", "dl", "dd", "div", "section",
    "article", "main", "body", "table", "thead", "tbody", "tfoot", "tr", "caption",
    "blockquote", "pre", "address", "figure", "figcaption", "header", "footer", "details",
    "summary", "hr",
})
_CELL_END = {"td": " | ", "th": " | ", "dt": ": "}
# A definition continues its term; everything else here starts a line.
_LINE_STARTS = (_BLOCKS | {"br"}) - {"dd"}
# A dropped element of these kinds stood between two blocks of text, not inside a sentence.
_DROPPED_BLOCKS = _BLOCKS | {"form", "nav", "aside", "fieldset", "dialog", "menu", "iframe",
                             "noframes"}


@dataclass(frozen=True)
class StructuredText:
    headline: str
    text: str
    truncated: bool = False  # the walk stopped at a limit: there was more on the page
    extractor_version: str = STRUCTURED_EXTRACTOR_VERSION


def _parser() -> lxml_html.HTMLParser:
    """An HTML parser, never an XML one: it has no DTD loading, no entity declarations and no
    XInclude to resolve. `no_network` is stated rather than left to the default, and
    `huge_tree` stays off so libxml2 keeps its own depth and size limits. The encoding is
    stated because the input is always the UTF-8 this module encoded itself: left to guess,
    libxml2 reads a page that declares no charset, or a legacy one, as windows-1252."""
    return lxml_html.HTMLParser(encoding="utf-8", no_network=True, remove_comments=True,
                                remove_pis=True, collect_ids=False, huge_tree=False,
                                recover=True)


def _is_unpainted(element: etree._Element) -> bool:
    """A browser draws nothing for it: the `hidden` attribute or an inline hiding style."""
    attributes = element.attrib
    if "hidden" in attributes:
        return True
    style = "".join(attributes.get("style", "").lower().split()).replace("!important", "")
    return not _HIDDEN_STYLES.isdisjoint(style.split(";"))


def _is_hidden(element: etree._Element) -> bool:
    """Unpainted, or marked as not for the reader: `aria-hidden="true"` and the screen-reader
    class names. Those are on screen or spoken; they are dropped as decoration or duplicates."""
    attributes = element.attrib
    return (_is_unpainted(element)
            or attributes.get("aria-hidden", "").strip().lower() == "true"
            or not _HIDDEN_CLASSES.isdisjoint(attributes.get("class", "").lower().split()))


def _is_dropped(element: etree._Element, tag: str, inside_content: bool) -> bool:
    if tag in _DROPPED or (tag in _CHROME and not inside_content):
        return True
    if element.attrib.get("role", "").strip().lower() in _DROPPED_ROLES:
        return True
    return _is_hidden(element)


class _Lines:
    """Text pieces gathered into lines; a block boundary ends the line."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.size = 0
        self._pieces: list[str] = []

    def add(self, text: str | None) -> None:
        if text:
            self._pieces.append(text)

    def separate(self, separator: str) -> None:
        """After a table cell or a definition term. An empty cell keeps its separator, so a
        value never slides under the wrong column heading."""
        while self._pieces and not self._pieces[-1].strip():
            self._pieces.pop()
        if self._pieces and self._pieces[-1] not in _CELL_END.values():
            self._pieces[-1] = self._pieces[-1].rstrip()  # "Court: x", never "Court : x"
        self._pieces.append(separator)

    def end_line(self) -> None:
        while self._pieces and (self._pieces[-1] in _CELL_END.values()
                                or not self._pieces[-1].strip()):
            self._pieces.pop()  # the separator after the last cell is not page text
        line = " ".join("".join(self._pieces).translate(_CONTROL).split())
        self._pieces = []
        if line:
            self.lines.append(line)
            self.size += len(line) + 1

    def full(self) -> bool:
        return self.size >= _MAX_TEXT_CHARS


def _folded(line: str) -> str:
    """A line as it is compared: case, spacing and invisible format characters aside (a soft
    hyphen or a zero-width space is in one extractor's text and not the other's)."""
    visible = "".join(ch for ch in line if unicodedata.category(ch) != "Cf")
    return " ".join(visible.split()).casefold()


def _is_data_table(table: etree._Element) -> bool:
    """True for a table of rows and columns. A table that holds another table, a heading in
    one of its body cells or a cell with several blocks in it, or that never has two cells in
    a row, is page layout: its cells are blocks of the page, not columns, and joining them
    into rows would run unrelated text together."""
    cells_in_row: dict[etree._Element | None, int] = {}
    for node in table.iter("table", "td", "th", *_HEADINGS):
        if node is table:
            continue
        if node.tag == "table":
            return False
        if node.tag in _HEADINGS:
            # A column title set as a heading, or a hidden one, does not make a page of it.
            holder = next((a for a in node.iterancestors("td", "th", "caption", "table")), None)
            if holder is not None and holder.tag == "td" and not _is_hidden(node):
                return False
            continue
        if len(list(islice(node.iter(*_LINE_STARTS), _MAX_BLOCKS_IN_A_CELL + 1))) > (
                _MAX_BLOCKS_IN_A_CELL):
            return False
        row = node.getparent()
        cells_in_row[row] = cells_in_row.get(row, 0) + 1
    return max(cells_in_row.values(), default=0) > 1


def _is_simple_term(term: etree._Element) -> bool:
    """A `<dt>` that is a term of a definition list and holds only inline text."""
    parent = term.getparent()
    return (parent is not None and parent.tag in ("dl", "div")
            and next(term.iter(*_BLOCKS, "table", "br"), None) is None)


def _walk(root: etree._Element, *, links: bool = True
          ) -> tuple[str, str, bool, frozenset[int], int | None]:
    """(first h1, text, truncated, which lines are headings, which line is the first h1) for
    one subtree. Iterative: depth in the markup is never Python depth. With `links=False`
    the text of every link is left out: what remains is the page's running text, without its
    menus and lists of other pages."""
    lines = _Lines()
    first_h1 = ""
    first_h1_line: int | None = None
    heading_lines: set[int] = set()  # the index of every line that came from h1 to h6
    heading_from: int | None = None  # where the open heading's lines begin
    content_depth = 0  # how many <main>/<article> ancestors the walk is inside
    tables: list[bool] = []  # the open tables, innermost last: is each one a data table?
    cell: etree._Element | None = None  # the open data cell or simple term, if any
    skipped: etree._Element | None = None
    seen = 0
    truncated = False
    walker = etree.iterwalk(root, events=("start", "end"))
    for event, element in walker:
        tag = element.tag if isinstance(element.tag, str) else ""
        if event == "start":
            seen += 1
            if seen > _MAX_ELEMENTS or lines.full():
                truncated = True
                break
            if not tag or (tag == "a" and not links) or _is_dropped(
                    element, tag, content_depth > 0):
                skipped = element
                walker.skip_subtree()
                continue
            if tag in _CONTENT_ROOTS:
                content_depth += 1
            if tag == "table":
                tables.append(_is_data_table(element))
            if cell is not None:
                if tag in _LINE_STARTS:
                    lines.add(" ")  # inside a cell a block or a line break is a space
            elif tag in ("td", "th") and tables and tables[-1]:
                cell = element  # continues its row: no new line
            else:
                if tag in _LINE_STARTS or tag in _CELL_END:
                    lines.end_line()
                    if tag in _HEADINGS:
                        heading_from = len(lines.lines)
                if tag == "dt" and _is_simple_term(element):
                    cell = element
            lines.add(element.text)
            continue
        if element is skipped:
            skipped = None
            if tag in _INLINE and _is_unpainted(element):
                pass  # the text either side of it touches, as it does on screen
            elif cell is not None or tag not in _DROPPED_BLOCKS:
                lines.add(" ")  # what was dropped still separated its neighbours
            else:
                lines.end_line()
        else:
            if tag in _CONTENT_ROOTS:
                content_depth -= 1
            if tag == "table" and tables:
                tables.pop()
            if element is cell:
                cell = None
                lines.separate(_CELL_END[tag])
            elif cell is not None:
                if tag in _BLOCKS:
                    lines.add(" ")
            elif tag in _BLOCKS or tag in _CELL_END:
                lines.end_line()
                if tag in _HEADINGS and heading_from is not None:
                    if tag == "h1" and not first_h1 and len(lines.lines) > heading_from:
                        first_h1, first_h1_line = lines.lines[heading_from], heading_from
                    heading_lines.update(range(heading_from, len(lines.lines)))
                    heading_from = None
        if element is not root:
            lines.add(element.tail)
    lines.end_line()
    text = "\n".join(lines.lines)
    return (first_h1, text[:_MAX_TEXT_CHARS], truncated or len(text) > _MAX_TEXT_CHARS,
            frozenset(heading_lines), first_h1_line)


def _is_reachable(element: etree._Element) -> bool:
    """False when the element or anything around it would be dropped by the walk: a `<main>`
    inside a hidden block or a sidebar is not the page's content."""
    for node in (element, *element.iterancestors()):
        tag = node.tag if isinstance(node.tag, str) else ""
        if not tag or _is_dropped(node, tag, inside_content=False):
            return False
    return True


def _candidates(tree: etree._Element) -> list[etree._Element]:
    """Where the page says its content is, then the whole body. Only `<main>` counts: an
    `<article>` is as often a teaser card about someone else as it is the page."""
    def reachable(elements: Iterator[etree._Element]) -> list[etree._Element]:
        return [e for e in islice(elements, _MAX_ROOTS_CONSIDERED) if _is_reachable(e)]

    mains = reachable(tree.iter("main"))
    mains += reachable(e for e in tree.iter("div", "section")
                       if e.attrib.get("role", "").strip().lower() == "main")
    body = tree.find(".//body") if tree.tag != "body" else tree
    return [*mains[:1], *([body] if body is not None else [])]


@dataclass(frozen=True)
class PageReading:
    """One parse of a page, with what the article path needs to decide whether to trust itself."""

    structured: StructuredText | None
    running_chars: int  # visible text outside links, from the page's content root
    declares_article: bool  # the page's own markup says it is an article
    heading_lines: frozenset[int]  # which lines of the structured text are headings
    headline_line: int | None  # which of them is the page's own (its first h1), if any
    visible_html: str  # the page again, without the elements the markup marks as hidden

    def is_opened_by(self, text: str) -> bool:
        """Does `text` begin as the page's own content begins? Its first full line must be
        found, in what the walk read from the content root, at a place where the nearest
        heading at or above it is the page's own. Text that sits under another heading is an
        item with a title of its own, wherever the article extractor put that title; text
        above the page's headline, or not in the content root at all, is something beside
        the page.

        A full line is one of at least `_OPENING_CHARS` characters: a date, a place or a
        byline is skipped, because such a line recurs across a page and finding it under the
        page's headline says nothing. One qualifying place is enough, because a publisher
        often prints an article's first sentence twice, once as a standfirst under the
        headline and again in the body below a widget that has a heading of its own."""
        if self.structured is None:
            return False
        own = _folded(self.structured.headline)  # the article extractor may print it first
        opening = next((folded[:_OPENING_CHARS]
                        for folded in (_folded(line.lstrip("-•* ")) for line in text.split("\n"))
                        if len(folded) >= _OPENING_CHARS and folded != own), "")
        if not opening:
            return False
        above: int | None = None  # the nearest heading at or above the current line
        for n, line in enumerate(self.structured.text.split("\n")):
            if n in self.heading_lines:
                above = n
            # Both are None on a page with no h1 in its content root.
            if above == self.headline_line and n != above and opening in _folded(line):
                return True
        return False


def _drop_unrendered(tree: etree._Element) -> None:
    """Remove `<template>` and the elements the markup marks as hidden. The same rule the
    walk applies, done to the tree so that the article extractor, which keeps `<template>`
    text and does not know the `hidden` attribute, never sees it either. Not all of it is
    invisible in a browser: `aria-hidden="true"` and the screen-reader class names mark text
    that is on screen or spoken. It is dropped all the same, as Readability drops it,
    because it is so often a decorative or duplicate copy; where a page shows a date to the
    eye and repeats it for a screen reader, both are lost."""
    unrendered = []
    walker = etree.iterwalk(tree, events=("start",))
    for _, element in walker:
        if isinstance(element.tag, str) and element is not tree and (
                element.tag == "template" or _is_hidden(element)):
            unrendered.append(element)
            walker.skip_subtree()  # whatever is inside goes with it
    # The text that followed each removed element is gathered per surviving neighbour and
    # written once. Appending as it goes, as lxml's own `drop_tree` does, is quadratic in the
    # number of siblings, which hostile markup can make large.
    after_sibling: dict[etree._Element, list[str]] = {}
    at_start_of: dict[etree._Element, list[str]] = {}
    for element in unrendered:
        parent = element.getparent()
        if parent is None:
            continue
        before = element.getprevious()
        pieces = (after_sibling.setdefault(before, []) if before is not None
                  else at_start_of.setdefault(parent, []))
        # An inline element that was never drawn sat inside a word or between two; anything
        # else (a block, or an inline separator marked aria-hidden) kept its neighbours apart.
        fused = element.tag in _INLINE and _is_unpainted(element)
        pieces.append(f"{'' if fused else ' '}{element.tail or ''}")
        parent.remove(element)
    for sibling, pieces in after_sibling.items():
        sibling.tail = (sibling.tail or "") + "".join(pieces)
    for parent, pieces in at_start_of.items():
        parent.text = (parent.text or "") + "".join(pieces)


def _declares_article(tree: etree._Element) -> bool:
    """The page says, in standard markup, that it is one article: Open Graph `og:type`, an
    `article:` property, or its heading inside `<article>` or inside an element with a
    schema.org article `itemtype`. It is the page's own statement, not a finding. Many sites
    say it on every page, so it permits the article path and decides nothing by itself."""
    for meta in tree.iter("meta"):
        name = (meta.get("property") or meta.get("name") or "").strip().lower()
        if name.startswith("article:") or (
                name == "og:type" and (meta.get("content") or "").strip().lower() == "article"):
            return True
    heading = next((h for h in islice(tree.iter("h1"), _MAX_ROOTS_CONSIDERED)
                    if _is_reachable(h)), None)
    if heading is None:
        return False
    for ancestor in heading.iterancestors():
        if ancestor.tag == "article" or _is_schema_article(ancestor.get("itemtype") or ""):
            return True
    return False


def _is_schema_article(itemtype: str) -> bool:
    for kind in itemtype.split():
        scheme, _, rest = kind.partition("://")
        host, _, name = rest.partition("/")
        if scheme in ("http", "https") and host in ("schema.org", "www.schema.org") and (
                name.rstrip("/") in _SCHEMA_ARTICLE_TYPES):
            return True
    return False


def read_page(html: str) -> PageReading | None:
    """Parse the page once and read it. None when the markup cannot be parsed at all."""
    try:
        tree = lxml_html.document_fromstring(html[:_MAX_HTML_CHARS].encode("utf-8", "ignore"),
                                             parser=_parser())
        title = " ".join((tree.findtext(".//head/title") or "").translate(_CONTROL).split())
        declares_article = _declares_article(tree)
        structured, running_chars = None, 0
        heading_lines, headline_line = frozenset[int](), None
        for root in _candidates(tree):
            first_h1, text, truncated, heading_lines, headline_line = _walk(root)
            headline = (first_h1 or title)[:2000]
            if len(text) >= _MIN_TEXT_CHARS and headline:
                structured = StructuredText(
                    headline=headline, text=text,
                    truncated=truncated or len(html) > _MAX_HTML_CHARS)
                if declares_article:  # only the article path asks
                    running_chars = len(_walk(root, links=False)[1])
                break
        if structured is None or not declares_article:
            return PageReading(structured, 0, False, frozenset(), None, "")
        _drop_unrendered(tree)  # last: the walk skips these itself and marks where they stood
        return PageReading(structured, running_chars, True, heading_lines, headline_line,
                           lxml_html.tostring(tree, encoding="unicode"))
    except Exception:  # noqa: BLE001 -- hostile markup: nothing read, never a crash
        return None


def extract_structured_text(html: str) -> StructuredText | None:
    """The page's visible structured text, or None when there is too little to be a page."""
    page = read_page(html)
    return None if page is None else page.structured
