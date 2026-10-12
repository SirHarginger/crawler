"""Research Gateway: the single egress point for live-investigation web acquisition
(docs/visual-investigation.md).

Every live page or image fetch goes through `ResearchGateway.fetch`. Nothing else in the
investigation path opens a socket, and no model or agent tool is ever handed a network client.

Controls, all enforced per hop (a redirect is a new request, validated from scratch):
  * http/https only, default ports only, no credentials in the URL
  * the host is resolved once, EVERY resolved address must be globally routable, and the
    connection is pinned to the validated address (no second lookup -> no DNS rebinding)
  * bounded redirects, with loop detection
  * no https -> http downgrade on a redirect
  * one connection per request, never reused: TLS verifies the real hostname on every hop
  * connect/read timeouts plus one wall-clock deadline covering every hop, the robots fetches
    and slow-dripped headers
  * a hard cap on response bytes; compressed transfer encodings are refused outright, so there
    is nothing to inflate
  * robots.txt, for the gateway's own user-agent token

This module imports nothing from the engine: it holds no database handle, no settings and no
credentials, so it can move into its own container unchanged.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

# httpx logs every request line at INFO, URL included. A research URL can carry a person's
# name in its path, so request lines stay out of the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

ROBOTS_USER_AGENT_TOKEN = "screening-research"  # noqa: S105 -- a robots token, not a secret
_DEFAULT_PORTS = {"http": 80, "https": 443}
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ROBOTS_MAX_BYTES = 512 * 1024
_ROBOTS_MAX_HOSTS = 256
_ROBOTS_TTL_SECONDS = 3600.0
_ROBOTS_FAILURE_TTL_SECONDS = 300.0
_MAX_URL_LENGTH = 4096
_HOST_PATTERN = re.compile(r"[a-z0-9.\-]{1,253}|[0-9a-f:.]{2,45}")
# Ranges `ipaddress.is_global` still calls global but which are not public unicast: NAT64 and
# IPv4-compatible / SIIT prefixes (they embed an arbitrary IPv4 address, private ones
# included), deprecated site-local, and two IPv4 protocol-assignment blocks.
_DENIED_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in ("64:ff9b::/96", "::/96", "::ffff:0:0:0/96", "fec0::/10", "192.0.0.0/24",
              "192.88.99.0/24")
)

Resolver = Callable[[str, int], list[str]]


class GatewayRejected(Exception):
    """The gateway refused or could not complete a fetch. `reason` is a stable machine code
    (metrics label, rejection summary); `detail` is for an operator and never contains bytes."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class GatewayPolicy:
    max_redirects: int = 5
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 10.0
    total_timeout_seconds: float = 25.0
    respect_robots: bool = True
    user_agent: str = f"{ROBOTS_USER_AGENT_TOKEN}/0.1 (+compliance due-diligence research)"


@dataclass(frozen=True)
class RedirectHop:
    url: str
    status: int
    location: str


@dataclass(frozen=True)
class FetchResult:
    requested_url: str
    final_url: str
    status: int
    declared_content_type: str | None
    body: bytes
    content_sha256: str
    redirects: tuple[RedirectHop, ...]
    resolved_address: str
    retrieved_at: datetime
    elapsed_seconds: float


def _system_resolver(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise GatewayRejected("dns_failure", type(exc).__name__) from exc
    return [str(info[4][0]) for info in infos]


def _is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_multicast or ip.is_reserved or any(ip in network for network in _DENIED_NETWORKS):
        return False
    # `is_global` is False for private, loopback, link-local (incl. the 169.254.169.254 cloud
    # metadata address), CGNAT, documentation and unspecified ranges.
    return ip.is_global


@dataclass(frozen=True)
class _Target:
    url: str
    scheme: str
    host: str
    address: str


@dataclass
class ResearchGateway:
    """`transport` and `resolver` are injection points for tests; every control above runs in
    this class regardless of which transport carries the bytes."""

    policy: GatewayPolicy = field(default_factory=GatewayPolicy)
    transport: httpx.BaseTransport | None = None
    resolver: Resolver = _system_resolver
    _robots: dict[str, tuple[float, RobotFileParser | None]] = field(
        default_factory=dict, repr=False
    )
    # One gateway may serve several threads; the robots cache is the only shared state.
    _robots_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def fetch(self, url: str, *, max_bytes: int) -> FetchResult:
        """GET `url`, following validated redirects. Raises `GatewayRejected`; a non-2xx final
        status is a rejection too (`http_error`), so a caller never parses an error page."""
        deadline = time.monotonic() + self.policy.total_timeout_seconds
        return self._follow(url, max_bytes, deadline, check_robots=self.policy.respect_robots)

    def _follow(
        self, url: str, max_bytes: int, deadline: float, *, check_robots: bool
    ) -> FetchResult:
        started = time.monotonic()
        hops: list[RedirectHop] = []
        seen: set[str] = set()
        current = url
        while True:
            target = self._validate(current)
            if target.url in seen:
                raise GatewayRejected("redirect_loop", target.host)
            seen.add(target.url)
            if hops and target.scheme == "http" and hops[-1].url.startswith("https:"):
                raise GatewayRejected("scheme_downgrade", target.host)
            if check_robots and not self._robots_allow(target, deadline):
                raise GatewayRejected("robots_disallowed", target.host)
            status, headers, body = self._get(target, max_bytes, deadline)
            if status in _REDIRECT_STATUSES:
                location = headers.get("location", "")
                if not location:
                    raise GatewayRejected("http_error", str(status))
                if len(hops) >= self.policy.max_redirects:
                    raise GatewayRejected("redirect_limit", str(self.policy.max_redirects))
                hops.append(RedirectHop(url=target.url, status=status, location=location))
                try:
                    current = urljoin(target.url, location)
                except ValueError as exc:
                    raise GatewayRejected("malformed_url", "redirect location") from exc
                continue
            if not 200 <= status < 300:
                raise GatewayRejected("http_error", str(status))
            return FetchResult(
                requested_url=url,
                final_url=target.url,
                status=status,
                declared_content_type=headers.get("content-type"),
                body=body,
                content_sha256=hashlib.sha256(body).hexdigest(),
                redirects=tuple(hops),
                resolved_address=target.address,
                retrieved_at=datetime.now(UTC),
                elapsed_seconds=time.monotonic() - started,
            )

    def _validate(self, url: str) -> _Target:
        if len(url) > _MAX_URL_LENGTH:
            raise GatewayRejected("malformed_url", "too long")
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise GatewayRejected("malformed_url") from exc
        scheme = parts.scheme.lower()
        if scheme not in _DEFAULT_PORTS:
            raise GatewayRejected("unsupported_scheme", scheme or "(none)")
        if parts.username is not None or parts.password is not None:
            raise GatewayRejected("credentials_in_url")
        raw_host = (parts.hostname or "").lower().rstrip(".")
        if not raw_host:
            raise GatewayRejected("no_host")
        try:  # an internationalised name is fetched under its ASCII (punycode) form
            host = raw_host if ":" in raw_host else raw_host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise GatewayRejected("malformed_url", "host") from exc
        if not _HOST_PATTERN.fullmatch(host):  # no spaces, control bytes or header injection
            raise GatewayRejected("malformed_url", "host")
        if port is not None and port != _DEFAULT_PORTS[scheme]:
            raise GatewayRejected("port_not_allowed", str(port))
        addresses = self.resolver(host, _DEFAULT_PORTS[scheme])
        if not addresses:
            raise GatewayRejected("dns_failure", "no addresses")
        if any(not _is_public_address(a) for a in addresses):
            raise GatewayRejected("private_address", host)
        netloc = f"[{host}]" if ":" in host else host
        clean = urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
        return _Target(clean, scheme, host, addresses[0])

    def _get(
        self, target: _Target, max_bytes: int, deadline: float
    ) -> tuple[int, httpx.Headers, bytes]:
        """One request on its own connection, under a hard wall-clock bound. The read runs in
        a worker thread so that a server dripping headers or body one byte at a time -- which
        never trips a per-read timeout -- is still cut off: on expiry the client is closed
        under the worker, which ends its read."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GatewayRejected("timeout", "overall deadline")
        timeout = httpx.Timeout(
            min(self.policy.read_timeout_seconds, remaining),
            connect=min(self.policy.connect_timeout_seconds, remaining),
        )
        # trust_env=False: an HTTP(S)_PROXY variable must never reroute research traffic.
        # A fresh client per request: a pooled connection is keyed by the pinned address, so
        # reusing one for another hostname on the same address would skip that hostname's
        # certificate check.
        client = httpx.Client(
            transport=self.transport, timeout=timeout, follow_redirects=False, trust_env=False
        )
        outcome: list[tuple[int, httpx.Headers, bytes] | BaseException] = []

        def work() -> None:
            try:
                outcome.append(self._read(client, target, max_bytes, deadline))
            except BaseException as exc:  # noqa: BLE001 -- re-raised on the calling thread
                outcome.append(exc)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        worker.join(remaining)
        if worker.is_alive():
            client.close()
            raise GatewayRejected("timeout", "overall deadline")
        client.close()
        result = outcome[0]
        if isinstance(result, GatewayRejected):
            raise result
        if isinstance(result, httpx.TimeoutException):
            raise GatewayRejected("timeout", type(result).__name__) from result
        if isinstance(result, httpx.HTTPError):
            raise GatewayRejected("transport_error", type(result).__name__) from result
        if isinstance(result, httpx.InvalidURL | UnicodeError | ValueError):
            raise GatewayRejected("malformed_url", type(result).__name__) from result
        if isinstance(result, BaseException):
            raise result
        return result

    def _read(
        self, client: httpx.Client, target: _Target, max_bytes: int, deadline: float
    ) -> tuple[int, httpx.Headers, bytes]:
        parts = urlsplit(target.url)
        literal = f"[{target.address}]" if ":" in target.address else target.address
        pinned = urlunsplit((target.scheme, literal, parts.path, parts.query, ""))
        request = client.build_request(
            "GET",
            pinned,
            headers={
                "Host": target.host,
                "User-Agent": self.policy.user_agent,
                "Accept-Encoding": "identity",
                "Connection": "close",
            },
            extensions={"sni_hostname": target.host},
        )
        response = client.send(request, stream=True)
        try:
            if response.status_code in _REDIRECT_STATUSES:
                return response.status_code, response.headers, b""
            # Identity was requested. A compressed answer is refused rather than inflated: the
            # byte cap below must be a cap on what this process allocates.
            encoding = response.headers.get("content-encoding", "").strip().lower()
            if encoding not in ("", "identity"):
                raise GatewayRejected("content_encoding_not_allowed", encoding[:32])
            declared = response.headers.get("content-length", "")
            if declared.isascii() and declared.isdigit() and int(declared[:18]) > max_bytes:
                raise GatewayRejected("too_large", "declared length")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_raw():
                total += len(chunk)
                if total > max_bytes:
                    raise GatewayRejected("too_large", f"exceeded {max_bytes} bytes")
                if time.monotonic() > deadline:
                    raise GatewayRejected("timeout", "overall deadline")
                chunks.append(chunk)
            return response.status_code, response.headers, b"".join(chunks)
        finally:
            response.close()

    def _robots_allow(self, target: _Target, deadline: float) -> bool:
        origin = f"{target.scheme}://{target.host}"
        with self._robots_lock:
            cached = self._robots.get(origin)
        if cached is None or cached[0] < time.monotonic():
            parser = self._load_robots(origin, deadline)  # a fetch: never under the lock
            ttl = _ROBOTS_TTL_SECONDS if parser is not None else _ROBOTS_FAILURE_TTL_SECONDS
            cached = (time.monotonic() + ttl, parser)
            with self._robots_lock:
                if len(self._robots) >= _ROBOTS_MAX_HOSTS and origin not in self._robots:
                    self._robots.pop(next(iter(self._robots)))
                self._robots[origin] = cached
        parser = cached[1]
        if parser is None:  # robots.txt unreachable or a server error: treat as "do not fetch"
            return False
        return parser.can_fetch(ROBOTS_USER_AGENT_TOKEN, target.url)

    def _load_robots(self, origin: str, deadline: float) -> RobotFileParser | None:
        """robots.txt is fetched under the same controls as anything else, redirects included
        (`site/robots.txt` -> `www.site/robots.txt` is common and its rules still apply)."""
        parser = RobotFileParser()
        try:
            fetched = self._follow(
                f"{origin}/robots.txt", _ROBOTS_MAX_BYTES, deadline, check_robots=False
            )
        except GatewayRejected as exc:
            if exc.reason == "timeout":
                raise  # out of time is the caller's answer, not a cached robots verdict
            if exc.reason == "http_error" and exc.detail[:1] == "4":
                parser.parse([])  # no robots.txt: nothing is disallowed
                return parser
            return None
        parser.parse(fetched.body.decode("utf-8", errors="replace").splitlines())
        return parser
