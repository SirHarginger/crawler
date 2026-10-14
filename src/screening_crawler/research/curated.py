"""Owner-curated official sources as research leads (docs/research-egress.md, "Curated mode").

`RESEARCH_EGRESS_MODE=curated` answers a search from a reviewed list of official pages per
subject instead of asking a search provider: no third party ever sees the subject's name. The
list (`RESEARCH_CURATED_SOURCES`, a JSON file) names each subject and the exact pages that may
be fetched for it. Everything after that is the live path unchanged: signed leads, the real
gateway behind the approved source domains (source_policy.py), the parsing child and the
extractors.

The provider is network-capable (its leads are real pages), so it runs only once it is named
in `search.APPROVED_LIVE_PROVIDERS`, which is empty: as committed this mode answers every search
with `provider_not_approved`. Each listed page must be an https URL on an approved source
domain, without credentials, port, query or fragment, checked when the egress starts.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass
from urllib.parse import urlsplit

from .query import ResearchQuery
from .search import SearchLead, require_approval
from .source_policy import host_permitted

PROVIDER_NAME = "curated-official"
MAX_PAGES_PER_SUBJECT = 5


class CuratedSourcesInvalid(ValueError):
    pass


def _fold(name: str) -> str:
    return " ".join(name.split()).casefold()


def _page(url: str, domains: frozenset[str]) -> str:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise CuratedSourcesInvalid("a curated page is not a valid URL") from None
    host = (parts.hostname or "").lower()
    if (parts.scheme != "https" or parts.username is not None or parts.password is not None
            or port is not None or parts.query or parts.fragment or not host):
        raise CuratedSourcesInvalid("a curated page is a plain https URL")
    if not host_permitted(host, domains):
        raise CuratedSourcesInvalid("a curated page is outside the approved source domains")
    return url


@dataclass(frozen=True)
class CuratedSources:
    subjects: dict[str, list[str]]  # folded subject name -> page URLs, in rank order

    @classmethod
    def load(cls, path: pathlib.Path, domains: frozenset[str]) -> CuratedSources:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            subjects = {_fold(str(s["name"])): [str(u) for u in s["pages"]]
                        for s in manifest["subjects"]}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise CuratedSourcesInvalid(f"unreadable curated sources: {type(exc).__name__}"
                                        ) from None
        if not subjects:
            raise CuratedSourcesInvalid("the curated sources name no subject")
        for pages in subjects.values():
            if not 1 <= len(pages) <= MAX_PAGES_PER_SUBJECT:
                raise CuratedSourcesInvalid(
                    f"a subject has 1 to {MAX_PAGES_PER_SUBJECT} curated pages")
            for url in pages:
                _page(url, domains)
        return cls(subjects)


class CuratedSourceProvider:
    """Leads for the subjects the reviewed list names; none for anyone else."""

    name = PROVIDER_NAME

    def __init__(self, sources: CuratedSources) -> None:
        self._sources = sources

    def search(self, query: ResearchQuery, *, limit: int) -> list[SearchLead]:
        require_approval(self.name, None)  # its leads are real pages
        pages = self._sources.subjects.get(_fold(query.name), [])
        return [SearchLead(url, rank, self.name)
                for rank, url in enumerate(pages[:limit], start=1)]
