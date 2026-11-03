"""Host allowlist against DNS rebinding (SEC-05, owner decision OF-2).

The same-origin guard compares ``Origin`` with ``Host``. After DNS rebinding a
foreign page is same-origin from the browser's point of view (attacker name in
both headers), so the request host itself must be one WAMS is meant to be
reached under:

- IP literals: IPv4 and bracketed IPv6, with or without port;
- ``localhost`` (exactly, no subdomains);
- names listed in ``WAMS_ALLOWED_HOSTS`` (comma-separated, case-insensitive,
  any port), e.g. the name of a reverse proxy.

Everything else, including a missing, empty, repeated or malformed ``Host``
header, is rejected with 400 before any other processing, for every method and
route (API, static files, report, index). The response never echoes the host.
An attacker name cannot resolve to an IP literal in the ``Host`` header: the
browser always sends the name it connected to.
"""

from __future__ import annotations

import functools
import ipaddress
import os
import re

from fastapi.responses import JSONResponse

ALLOWED_HOSTS_ENV = "WAMS_ALLOWED_HOSTS"
REJECTED_DETAIL = ("Host not allowed. Open WAMS via its IP address or localhost, "
                   "or add the name to WAMS_ALLOWED_HOSTS.")
_PORT = re.compile(r"[0-9]{1,5}")


def _host_name(host: str) -> str | None:
    """Return the lower-cased name of ``host[:port]``, or None when malformed."""
    host = host.lower()
    if host.startswith("["):
        end = host.find("]")
        if end < 0:
            return None
        name, rest = host[:end + 1], host[end + 1:]
    else:
        name, sep, port = host.partition(":")
        rest = sep + port
    if rest and (not rest.startswith(":") or not _PORT.fullmatch(rest[1:])
                 or int(rest[1:]) > 65535):
        return None
    return name or None


def _is_ip_literal(name: str) -> bool:
    if name.startswith("[") and name.endswith("]"):
        inner = name[1:-1]
        if "%" in inner:  # zone IDs never appear in a browser Host header
            return False
        try:
            ipaddress.IPv6Address(inner)
        except ValueError:
            return False
        return True
    try:
        ipaddress.IPv4Address(name)
    except ValueError:
        return False
    return True


@functools.lru_cache(maxsize=8)
def parse_allowed_hosts(raw: str | None) -> frozenset[str]:
    """Parse ``WAMS_ALLOWED_HOSTS``; a port in an entry is ignored."""
    names = set()
    for entry in (raw or "").split(","):
        name = _host_name(entry.strip())
        if name:
            names.add(name)
    return frozenset(names)


def host_allowed(host: str | None, allowed: frozenset[str]) -> bool:
    if not host:
        return False
    name = _host_name(host)
    if name is None:
        return False
    return _is_ip_literal(name) or name == "localhost" or name in allowed


class HostAllowlistGuard:
    """Pure ASGI middleware: reject requests for unknown hosts with 400."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            hosts = [v.decode("latin-1") for k, v in scope.get("headers", [])
                     if k.lower() == b"host"]
            allowed = parse_allowed_hosts(os.environ.get(ALLOWED_HOSTS_ENV))
            if len(hosts) != 1 or not host_allowed(hosts[0], allowed):
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                response = JSONResponse(status_code=400, content={"detail": REJECTED_DETAIL})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
