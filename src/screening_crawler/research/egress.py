"""The egress itself: search for leads, fetch only what a lead was issued for.

Two operations and nothing else. `search` asks the provider and hands back LEAD TOKENS, not
URLs. `fetch` accepts a lead token and nothing else, so no caller -- the engine, a model, or
text on a web page -- can make this process request a URL it did not itself obtain from the
provider. A token is the URL and an expiry with an HMAC over them: unforgeable and stateless,
but NOT opaque -- whoever holds a token can read the URL in it. What a caller cannot do is
choose one.

A fetched page is returned as bounded plain text with provenance. Raw bytes never leave this
process and are not stored here. Links, scripts, forms and instructions in a page are text at
most; nothing in a page causes another request.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from .extract import Extracted, ParserTriedNetwork, extract_isolated
from .gateway import GatewayRejected, ResearchGateway
from .query import ResearchQuery
from .search import SearchFailure, SearchProvider

logger = logging.getLogger(__name__)

EGRESS_VERSION = "research_egress_v1"
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 20_000
LEAD_TTL_SECONDS = 900
MAX_HOST_WAIT_SECONDS = 10.0
MAX_CONCURRENT_FETCHES = 4
_HTML_TYPES = ("text/html", "application/xhtml+xml")
_CHARSETS = frozenset({"utf-8", "utf8", "iso-8859-1", "latin-1", "us-ascii", "ascii",
                       "windows-1252", "cp1252"})


class EgressRefused(Exception):
    """`reason` is a stable machine code; nothing about the page or the query is in it."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class IssuedLead:
    lead_token: str
    rank: int
    host: str
    provider: str
    published_hint: str | None


@dataclass(frozen=True)
class FetchedDocument:
    requested_url: str
    final_url: str
    host: str
    http_status: int
    declared_content_type: str | None
    retrieved_at: datetime
    content_sha256: str  # of the page bytes as received
    byte_size: int
    redirect_hosts: tuple[str, ...]
    headline: str
    text: str  # extracted plain text, bounded; untrusted data
    text_sha256: str
    text_truncated: bool
    extractor_version: str
    published_date: str | None  # as found in the page (YYYY-MM-DD), never the provider's hint
    provider: str
    rank: int
    query_sha256: str
    egress_version: str = EGRESS_VERSION


# Plain substrings only. These are matched in the service process against hostile markup, so
# nothing here may backtrack: no regular expression is used in this classifier.
_NEEDS_JS = ("enable javascript", "javascript is required", "javascript is disabled",
             "requires javascript", "need to enable javascript")
_APP_ROOTS = tuple(f"id={quote}{name}{quote}" for name in ("root", "app", "__next", "__nuxt")
                   for quote in ('"', "'"))
_CONSENT = ("cookie consent", "cookie-consent", "cookieconsent", "consent banner",
            "consent-banner", "consent manager", "consent wall", "accept all cookies",
            "accept cookies", "we value your privacy", "onetrust", "cookiebot", "didomi",
            "quantcast choice")


def _visible_chars(markup: str) -> int:
    """Roughly how much text a reader would see: characters outside tags and outside script,
    style and noscript blocks. One forward pass with plain string searches -- this runs in the
    service process on hostile input, so it must not be able to backtrack."""
    lowered = markup.lower()
    count = position = 0
    in_space = True
    length = len(markup)
    while position < length:
        opening = lowered.find("<", position)
        chunk_end = length if opening < 0 else opening
        for ch in markup[position:chunk_end]:
            if ch.isspace():
                in_space = True
            else:
                count += 1 + (1 if in_space and count else 0)
                in_space = False
        if opening < 0:
            break
        closing = lowered.find(">", opening + 1)
        if closing < 0:
            break
        position = closing + 1
        for block in ("script", "style", "noscript"):
            if lowered.startswith(block, opening + 1):
                end = lowered.find("</" + block, position)
                if end < 0:
                    return count  # an unclosed block hides the rest
                after = lowered.find(">", end)
                position = length if after < 0 else after + 1
                break
    return count


# What a server says when it sends an error, a block or a sign-in page as an ordinary one.
# A list of wordings, matched in short structured pages only; it will miss others.
_GATE_PAGE_CHARS = 1500
_GATE_PHRASES = (
    "page not found", "page cannot be found", "page could not be found", "error 404",
    "404 error", "404 not found", "no longer available", "access denied", "access forbidden",
    "403 forbidden", "permission denied", "not authorized", "not authorised",
    "forgot your password", "forgot password", "subscribe to read", "subscribe to continue",
    "subscribers only", "already a subscriber", "just a moment", "checking your browser",
    "verify you are human", "are you a robot", "captcha", "unusual traffic",
    "request blocked", "has been blocked", "too many requests", "service unavailable",
    "temporarily unavailable", "bad gateway", "internal server error", "under maintenance",
    "site maintenance", "session has expired",
)
# Ordinary pages carry a "log in" link; only a page whose own heading says it is one counts.
_GATE_HEADLINES = ("sign in", "log in", "login", "404", "not found", "forbidden",
                   "unauthorized", "unauthorised", "error")


def _words(text: str) -> str:
    """Lower-case words with single spaces around each, so a phrase matches whole words only
    ("sign in" is not inside "design in"). One pass; no pattern to backtrack."""
    return f" {' '.join(''.join(ch if ch.isalnum() else ' ' for ch in text.lower()).split())} "


def is_gate_page(page: Extracted) -> bool:
    """A short structured page worded like an error, block, sign-in or paywall page."""
    if len(page.text) >= _GATE_PAGE_CHARS:
        return False
    headline, body = _words(page.headline[:500]), _words(page.text)
    return (any(f" {phrase} " in headline for phrase in _GATE_HEADLINES)
            or any(f" {phrase} " in headline + body for phrase in _GATE_PHRASES))


def why_no_text(html: str) -> str:
    """Why a page gave no article text, as far as its markup shows. A coarse reading for the
    record of gaps, not a decision: `js_required`, `consent_wall` or `no_article_text`."""
    sample = html[:500_000]
    lowered = sample.lower()
    visible = _visible_chars(sample)
    if any(phrase in lowered for phrase in _NEEDS_JS) or (
            visible < 600 and (lowered.count("<script") >= 3
                               or any(root in lowered for root in _APP_ROOTS))):
        return "js_required"
    if visible < 2000 and any(phrase in lowered for phrase in _CONSENT):
        return "consent_wall"
    return "no_article_text"


@dataclass
class ResearchEgress:
    provider: SearchProvider
    gateway: ResearchGateway
    lead_secret: bytes = field(repr=False)
    min_seconds_between_host_requests: float = 2.0
    # Searches cost money and each one discloses a query: a hard ceiling per UTC day.
    max_searches_per_day: int = 200
    # Parses hostile markup; by default in a child process that is killed on overrun.
    extractor: Callable[[str], Extracted | None] = extract_isolated
    clock: Callable[[], float] = time.time  # wall clock: lead expiry and the daily budget
    monotonic: Callable[[], float] = time.monotonic  # pacing
    sleep: Callable[[float], None] = time.sleep
    _last_request: dict[str, float] = field(default_factory=dict, repr=False)
    _searches: tuple[int, int] = field(default=(0, 0), repr=False)  # (UTC day, count)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _fetching: threading.BoundedSemaphore = field(
        default_factory=lambda: threading.BoundedSemaphore(MAX_CONCURRENT_FETCHES), repr=False)

    def __post_init__(self) -> None:
        if len(self.lead_secret) < 32:
            raise ValueError("the lead secret must be at least 32 bytes")

    # -- lead tokens -----------------------------------------------------------------------------

    def _sign(self, payload: bytes) -> str:
        return hmac.new(self.lead_secret, payload, hashlib.sha256).hexdigest()

    def _issue(self, url: str, rank: int, provider: str, query_sha256: str) -> str:
        payload = json.dumps({"u": url, "r": rank, "p": provider, "q": query_sha256,
                              "e": int(self.clock()) + LEAD_TTL_SECONDS},
                             separators=(",", ":"), sort_keys=True).encode()
        return f"{base64.urlsafe_b64encode(payload).decode()}.{self._sign(payload)}"

    def _redeem(self, token: str) -> dict[str, Any]:
        encoded, _, signature = token.partition(".")
        try:
            payload = base64.b64decode(encoded.encode("ascii"), altchars=b"-_", validate=True)
            supplied = signature.encode("ascii")
        except (binascii.Error, ValueError, UnicodeError):
            raise EgressRefused("invalid_lead") from None
        if not hmac.compare_digest(self._sign(payload).encode("ascii"), supplied):
            raise EgressRefused("invalid_lead")
        claims = json.loads(payload)  # our own bytes: the signature was checked first
        if int(claims["e"]) < int(self.clock()):
            raise EgressRefused("expired_lead")
        return dict(claims)

    # -- operations ------------------------------------------------------------------------------

    def search(self, query: ResearchQuery, *, limit: int = 5,
               provider: str | None = None) -> list[IssuedLead]:
        """`provider`, when given, is the only provider this caller may use. Checked before
        the budget and before the provider is asked, so a refused search sends nothing."""
        digest = query.sha256()
        if provider is not None and provider != self.provider.name:
            logger.warning("research_search", extra={
                "query_sha256": digest, "outcome": "provider_not_permitted",
                "provider": self.provider.name})
            raise EgressRefused("provider_not_permitted")
        with self._lock:
            day = int(self.clock() // 86400)
            used = self._searches[1] if self._searches[0] == day else 0
            if used >= self.max_searches_per_day:
                logger.warning("research_search", extra={
                    "query_sha256": digest, "outcome": "budget_exhausted",
                    "provider": self.provider.name})
                raise EgressRefused("search_budget_exhausted")
            self._searches = (day, used + 1)
        try:
            leads = self.provider.search(query, limit=limit)
        except SearchFailure as exc:
            logger.warning("research_search", extra={"query_sha256": digest, "outcome": exc.reason,
                                                     "provider": self.provider.name})
            raise EgressRefused(f"search_{exc.reason}") from None
        logger.info("research_search", extra={"query_sha256": digest, "outcome": "ok",
                                              "provider": self.provider.name, "leads": len(leads)})
        return [IssuedLead(self._issue(lead.url, lead.rank, lead.provider, digest), lead.rank,
                           (urlsplit(lead.url).hostname or "").lower(), lead.provider,
                           lead.published_hint) for lead in leads]

    def fetch(self, lead_token: str) -> FetchedDocument:
        claims = self._redeem(lead_token)
        url = str(claims["u"])
        host = (urlsplit(url).hostname or "").lower()
        if not self._fetching.acquire(blocking=False):
            raise EgressRefused("egress_busy")
        try:
            return self._fetch(url, host, claims)
        finally:
            self._fetching.release()

    def _fetch(self, url: str, host: str, claims: dict[str, Any]) -> FetchedDocument:
        self._wait_for(host)
        try:
            fetched = self.gateway.fetch(url, max_bytes=MAX_PAGE_BYTES)
        except GatewayRejected as exc:
            logger.info("research_fetch", extra={"host": host, "outcome": exc.reason,
                                                 "query_sha256": claims["q"]})
            raise EgressRefused(exc.reason) from None
        declared = (fetched.declared_content_type or "").split(";", 1)[0].strip().lower()
        if declared not in _HTML_TYPES:
            logger.info("research_fetch", extra={"host": host, "outcome": "unsupported_content_type",
                                                 "query_sha256": claims["q"]})
            raise EgressRefused("unsupported_content_type")
        html = fetched.body.decode(self._charset(fetched.declared_content_type), errors="replace")
        try:
            article = self.extractor(html)
            reason = "no_article_text" if article is None else ""
        except ParserTriedNetwork:
            article, reason = None, "parser_network_attempt"
        if article is not None and not article.is_article:
            # The fallback reads any page with words on it. A script shell, a consent wall,
            # an error page or a sign-in page has words too, and they are not the page.
            reason = why_no_text(html)
            if reason == "no_article_text":
                reason = "not_a_content_page" if is_gate_page(article) else ""
            article = None if reason else article
        elif article is None and reason == "no_article_text":
            reason = why_no_text(html)
        if article is None:
            logger.info("research_fetch", extra={"host": host, "outcome": reason,
                                                 "query_sha256": claims["q"]})
            raise EgressRefused(reason)
        text = article.text[:MAX_TEXT_CHARS]
        final_host = (urlsplit(fetched.final_url).hostname or "").lower()
        logger.info("research_fetch", extra={
            "host": final_host, "outcome": "ok", "query_sha256": claims["q"],
            "content_sha256": fetched.content_sha256, "bytes": len(fetched.body)})
        return FetchedDocument(
            requested_url=fetched.requested_url, final_url=fetched.final_url, host=final_host,
            http_status=fetched.status, declared_content_type=declared,
            retrieved_at=fetched.retrieved_at, content_sha256=fetched.content_sha256,
            byte_size=len(fetched.body),
            redirect_hosts=tuple((urlsplit(h.url).hostname or "").lower()
                                 for h in fetched.redirects),
            headline=article.headline[:500], text=text,
            text_sha256=hashlib.sha256(text.encode()).hexdigest(),
            text_truncated=article.truncated or len(article.text) > MAX_TEXT_CHARS,
            extractor_version=article.extractor_version, published_date=article.published_date,
            provider=str(claims["p"]), rank=int(claims["r"]), query_sha256=str(claims["q"]))

    # -- helpers ---------------------------------------------------------------------------------

    @staticmethod
    def _charset(content_type: str | None) -> str:
        for part in (content_type or "").split(";")[1:]:
            name, _, value = part.strip().partition("=")
            if name.lower() == "charset" and value.strip('"\' ').lower() in _CHARSETS:
                return value.strip('"\' ').lower()
        return "utf-8"

    def _wait_for(self, host: str) -> None:
        """One request per host every few seconds, however many leads point at it. A caller
        is never parked for long: past a short wait the fetch is refused instead."""
        with self._lock:
            now = self.monotonic()
            wait = (self._last_request.get(host, float("-inf"))
                    + self.min_seconds_between_host_requests - now)
            if wait > MAX_HOST_WAIT_SECONDS:
                raise EgressRefused("host_busy")
            self._last_request[host] = now + max(wait, 0.0)
            if len(self._last_request) > 4096:
                self._last_request.pop(next(iter(self._last_request)))
        if wait > 0:
            self.sleep(wait)
