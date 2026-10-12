"""Text and publication date from hostile HTML, in a process that can be killed.

Parsing arbitrary markup has no reliable time or memory bound, so it does not run in the
service process. One short-lived child parses one page under a time limit and an address-space
limit; if it overruns, it is killed and the page yields nothing. The child starts with the
`RESEARCH_*` variables removed from `os.environ` and with Python's socket calls replaced by
ones that refuse and count. (The service also deletes its secrets from its own environment
once it has read them, so a child started afterwards never had them: removing a variable
inside the child does not erase it from the block the process was started with.) On sockets: a parser has no reason to open one, and if it ever tries, the page
is refused. That is a guard inside the interpreter, not an operating-system sandbox; a network
policy on this process is still a deployment control.

Nothing here opens a socket. The date finder is given a parsed tree, never a string: handed a
string that looks like a URL, `htmldate` would download it.

The page is parsed once and what a browser would not render (`<template>`, hidden elements)
is removed before anything reads it. Then two extractors, in a fixed order:

- Article text, only when all of these hold: the page's own markup declares an article; the
  structured walk found the page readable at all; the article text is at least half of the
  page's running text; and it begins with text from the page's content root whose nearest
  heading there is the page's own. A news card on a profile page, a cookie banner or the first paragraph of a long
  page is text the article extractor will happily return; it is not the page.
- Structured text for every other page. It is marked with its own version and carries no
  publication date, because a date found on a list or a landing page says nothing reliable
  about the text taken from it.
"""

from __future__ import annotations

import multiprocessing
import os
from dataclasses import dataclass, replace
from multiprocessing.connection import Connection

from .page_text import TEXT_EXTRACTOR_VERSION, extract_article_text
from .structured_text import STRUCTURED_EXTRACTOR_VERSION, read_page

# The shared article extractor, given only the rendered part of a page that declares an
# article. The text can differ from what the bare extractor returns, so it has its own name.
ARTICLE_EXTRACTOR_VERSION = f"{TEXT_EXTRACTOR_VERSION}+declared-visible|v1"
# Trafilatura prefers another extraction of the same page when it is more than twice as long
# as its own (`compare_extraction`). The same factor here: an "article" that is less than half
# of the page's running text is a fragment of the page, or something beside it.
_MAX_RUNNING_TEXT_TO_ARTICLE = 2

EXTRACTION_TIMEOUT_SECONDS = 15.0
_MEMORY_LIMIT_BYTES = 1024 * 1024 * 1024
_MAX_HTML_CHARS = 2_000_000


@dataclass(frozen=True)
class Extracted:
    headline: str
    text: str
    published_date: str | None  # as stated by the page (YYYY-MM-DD), or None
    extractor_version: str = ARTICLE_EXTRACTOR_VERSION
    truncated: bool = False  # extraction stopped at a limit: there was more on the page

    @property
    def is_article(self) -> bool:
        return self.extractor_version == ARTICLE_EXTRACTOR_VERSION


class ParserTriedNetwork(Exception):
    """Parsing a page tried to open a socket. The page yields nothing."""


def _refuse_sockets() -> list[str]:
    """Replace the socket entry points in this process. Returns the list attempts land in."""
    import socket

    attempts: list[str] = []

    def refuse(*args: object, **kwargs: object) -> object:
        attempts.append("socket")
        raise OSError("no network in the parsing process")

    socket.getaddrinfo = refuse  # type: ignore[assignment]
    socket.create_connection = refuse  # type: ignore[assignment]
    socket.socket.connect = refuse  # type: ignore[method-assign,assignment]
    socket.socket.connect_ex = refuse  # type: ignore[method-assign,assignment]
    return attempts


def published_date(html: str) -> str | None:
    """The page's own publication date, read from its markup. A heuristic; never a fetch."""
    try:
        from htmldate import find_date
        from lxml import html as lxml_html

        # Bytes and a stated encoding: lxml refuses a string that carries an XML encoding
        # declaration, and a page that begins with one would lose its date.
        tree = lxml_html.fromstring(
            html[:_MAX_HTML_CHARS].encode("utf-8", "ignore"),
            parser=lxml_html.HTMLParser(encoding="utf-8", no_network=True, huge_tree=False))
        found = find_date(tree, original_date=True, extensive_search=False)
    except Exception:  # noqa: BLE001 -- hostile markup: no date, never a crash
        return None
    return found if isinstance(found, str) and len(found) == 10 else None


def extract(html: str) -> Extracted | None:
    """In this process. For tests and for callers that already isolate the work."""
    page = read_page(html)
    if page is None:
        return None
    # Re-serialising can lengthen a page past what the article extractor reads; it would then
    # see a prefix and return an article that stops early. Such a page is read whole instead.
    if page.declares_article and len(page.visible_html) <= _MAX_HTML_CHARS:
        article = extract_article_text(page.visible_html)
        if article is not None and (
                page.running_chars <= _MAX_RUNNING_TEXT_TO_ARTICLE * len(article.text)
                and page.is_opened_by(article.text)):
            return Extracted(article.headline, article.text,
                             published_date(page.visible_html))
    structured = page.structured
    if structured is None:
        return None
    return Extracted(structured.headline, structured.text, None, STRUCTURED_EXTRACTOR_VERSION,
                     structured.truncated)


def _child(html: str, out: Connection) -> None:
    for name in [n for n in os.environ if n.startswith("RESEARCH_")]:
        del os.environ[name]  # the page parser has no use for the provider key or any secret
    attempts = _refuse_sockets()
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (_MEMORY_LIMIT_BYTES, _MEMORY_LIMIT_BYTES))
    except (ImportError, ValueError, OSError):  # no such limit on this platform
        pass
    try:
        result = extract(html)
        out.send((result, len(attempts)))
    except BaseException:  # noqa: BLE001 -- out of memory included: the page yields nothing
        out.send((None, len(attempts)))
    finally:
        out.close()


def extract_isolated(html: str, *, timeout_seconds: float = EXTRACTION_TIMEOUT_SECONDS
                     ) -> Extracted | None:
    """`extract`, in a child process that is killed if it runs past the time limit. Raises
    `ParserTriedNetwork` if parsing attempted a network call."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(target=_child, args=(html[:_MAX_HTML_CHARS], sender), daemon=True)
    child.start()
    sender.close()
    try:
        if not receiver.poll(timeout_seconds):
            return None
        result, attempts = receiver.recv()
    except (EOFError, OSError, TypeError, ValueError):
        return None
    finally:
        if child.is_alive():
            child.kill()
        child.join(5)
        receiver.close()
    if attempts:
        raise ParserTriedNetwork
    if not isinstance(result, Extracted):
        return None
    # The child was given a prefix of an over-long page: what it returned is not all of it.
    return replace(result, truncated=True) if len(html) > _MAX_HTML_CHARS else result
