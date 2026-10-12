"""Article text extraction for live-captured pages (docs/visual-investigation.md).

A live page can only enter the article pipeline as what the crawler would have delivered: a
headline and plain body text. Trafilatura does the extraction; this module bounds its input
and output and never lets it fetch anything.

The text is hostile external input. It is returned as plain text, never rendered or executed,
and whatever it says is content to be analysed -- never an instruction.
"""

from __future__ import annotations

from dataclasses import dataclass

import trafilatura

TEXT_EXTRACTOR_VERSION = "trafilatura-2.0.0|precision|v1"
_MAX_HTML_CHARS = 2_000_000
_MAX_TEXT_CHARS = 200_000
_MIN_TEXT_CHARS = 400  # shorter than this is a stub, a teaser or a consent wall, not an article
_CONTROL = {c: None for c in (*range(9), 11, 12, *range(14, 32), 127)}


@dataclass(frozen=True)
class ArticleText:
    headline: str
    text: str
    extractor_version: str = TEXT_EXTRACTOR_VERSION


def _plain(value: object, limit: int) -> str:
    text = value if isinstance(value, str) else ""
    return text.translate(_CONTROL).encode("utf-8", "ignore").decode("utf-8").strip()[:limit]


def extract_article_text(html: str, *, fallback_headline: str = "") -> ArticleText | None:
    """The page's headline and body, or None when it does not read as an article."""
    try:
        extracted = trafilatura.bare_extraction(
            html[:_MAX_HTML_CHARS], include_comments=False, include_tables=False,
            include_images=False, include_links=False, favor_precision=True,
            with_metadata=True,
        )
    except Exception:  # noqa: BLE001 -- hostile markup: no article, never a crash
        return None
    if extracted is None:
        return None
    data = extracted if isinstance(extracted, dict) else extracted.as_dict()
    text = _plain(data.get("text"), _MAX_TEXT_CHARS)
    headline = _plain(data.get("title"), 2000) or _plain(fallback_headline, 2000)
    if len(text) < _MIN_TEXT_CHARS or not headline:
        return None
    return ArticleText(headline=headline, text=text)
