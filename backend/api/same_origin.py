"""Central same-origin protection for state-changing requests (P1-7, L-13).

Every request whose method is not GET/HEAD/OPTIONS must not come from a
foreign web page open in the same browser. The rule mirrors the existing
router checks (``api/node_settings.py``, ``api/wallet_discovery.py``,
``api/provenance.py``, ``api/dashboard.py``):

- ``Sec-Fetch-Site: cross-site`` is rejected. ``same-origin``, ``same-site``
  and ``none`` are not rejected by this header alone (as in the router
  checks); a same-site page on a different origin still fails the Origin
  comparison below, because browsers send ``Origin`` on non-GET requests.
- A present ``Origin`` header must name the same authority (host[:port]) as
  the request's ``Host`` header. ``Origin: null`` never matches.
- Requests without ``Origin`` and without ``Sec-Fetch-Site`` (curl, scripts,
  tests) stay allowed: they cannot be triggered by a foreign page in a
  current browser.

The comparison is scheme-independent: a TLS-terminating reverse proxy may
forward plain HTTP while the browser's Origin says ``https``. A different
page on the same host[:port] with another scheme is not a realistic attacker
position. ``X-Forwarded-Host`` is not consulted, consistent with the router
checks, which compare against the ``Host``-derived request URL. Which hosts are
acceptable at all is decided earlier by ``host_allowlist.HostAllowlistGuard``
(SEC-05: DNS rebinding puts the attacker name in both Origin and Host).
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi.responses import JSONResponse

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_DEFAULT_PORTS = {"http": "80", "https": "443"}


def _split_authority(authority: str) -> tuple[str, str | None]:
    """Split ``host[:port]`` (IPv6 literals in brackets) into host and port."""
    host, sep, port = authority.rpartition(":")
    if not sep or "]" in port:  # no port, or the colon belongs to an IPv6 literal
        return authority, None
    return host, port


def origin_matches_host(origin: str, host: str | None) -> bool:
    """Compare the Origin's host[:port] with the Host header (scheme-independent).

    Browsers omit default ports in both headers. A Host header without a port
    matches an Origin on its scheme's default port (80/443): behind a
    TLS-terminating proxy the backend cannot know which default applied.
    """
    if not host or not origin or origin.strip().lower() == "null":
        return False
    try:
        parts = urlsplit(origin.strip())
        origin_port = parts.port
    except ValueError:
        return False
    if (parts.scheme not in _DEFAULT_PORTS or not parts.hostname
            or "@" in parts.netloc or parts.path or parts.query or parts.fragment):
        return False
    origin_host, _ = _split_authority(parts.netloc.lower())
    effective_port = str(origin_port) if origin_port is not None else _DEFAULT_PORTS[parts.scheme]
    host_name, host_port = _split_authority(host.strip().lower())
    if host_name != origin_host:
        return False
    if host_port is None:
        return effective_port == _DEFAULT_PORTS[parts.scheme]
    return host_port == effective_port


def is_cross_site(method: str, headers: dict[str, str]) -> bool:
    """True when the request must be rejected as cross-site."""
    if method.upper() in SAFE_METHODS:
        return False
    if headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
        return True
    origin = headers.get("origin")
    return origin is not None and not origin_matches_host(origin, headers.get("host"))


class SameOriginGuard:
    """Pure ASGI middleware: reject cross-site non-GET requests with 403."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            if is_cross_site(scope.get("method", "GET"), headers):
                response = JSONResponse(status_code=403,
                                        content={"detail": "Use same-origin requests."})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
