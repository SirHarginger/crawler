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

Two extractors run, in a fixed order. The article extractor goes first and its result is used
unchanged. Only when it finds no article does the structured-page extractor read the same
markup; what it returns is marked with its own version and carries no publication date, because
a date found on a list or a landing page says nothing reliable about the text taken from it.
"""

from __future__ import annotations

import multiprocessing
import os
from dataclasses import dataclass, replace
from multiprocessing.connection import Connection

from .page_text import TEXT_EXTRACTOR_VERSION, extract_article_text
from .structured_text import STRUCTURED_EXTRACTOR_VERSION, extract_structured_text

EXTRACTION_TIMEOUT_SECONDS = 15.0
_MEMORY_LIMIT_BYTES = 1024 * 1024 * 1024
_MAX_HTML_CHARS = 2_000_000


@dataclass(frozen=True)
class Extracted:
    headline: str
    text: str
    published_date: str | None  # as stated by the page (YYYY-MM-DD), or None
    extractor_version: str = TEXT_EXTRACTOR_VERSION
    truncated: bool = False  # extraction stopped at a limit: there was more on the page

    @property
    def is_article(self) -> bool:
        return self.extractor_version == TEXT_EXTRACTOR_VERSION


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
    article = extract_article_text(html)
    if article is not None:
        return Extracted(article.headline, article.text, published_date(html))
    page = extract_structured_text(html)
    if page is None:
        return None
    return Extracted(page.headline, page.text, None, STRUCTURED_EXTRACTOR_VERSION,
                     page.truncated)


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
