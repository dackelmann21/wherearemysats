"""Shared HTTP helpers for the API routers (consolidation package P3-1).

Routers must not import helpers from each other; this module is the single
home of the no-store JSON reply, the bounded same-origin JSON body reader and
the request-level same-origin check. The same-origin rule is the same
scheme-independent rule as the ``SameOriginGuard`` middleware
(``backend.api.same_origin``), so a TLS-terminating reverse proxy that
forwards plain HTTP while the browser's ``Origin`` says ``https`` is accepted
at the router level as well.

``_get_trace_or_404`` and ``_ownership_custody_conflict_model`` (P3-4) are
shared by the traces router and the registry router (trace-scoped
ownership/custody conflicts); they resolve the database late-bound through
:func:`backend.api.runtime.state`.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from backend.api.runtime import state
from backend.api.same_origin import origin_matches_host
from backend.api.security_input import canonical_txid, detect_sensitive_input
from backend.models.schemas import OwnershipCustodyConflict, Trace


def _validate_txid(txid: str) -> str:
    try:
        return canonical_txid(txid)
    except ValueError:
        raise HTTPException(status_code=400, detail="The supplied transaction ID is invalid.") from None


def _check_sensitive(value: str | None, field: str = "input") -> None:
    warning = detect_sensitive_input(value)
    if warning:
        raise HTTPException(status_code=400, detail=warning)


def _ownership_custody_conflict_model(r: dict[str, Any]) -> OwnershipCustodyConflict:
    return OwnershipCustodyConflict(
        id=int(r["id"]),
        trace_id=int(r["trace_id"]),
        txid=r["txid"],
        vout=int(r["vout"]),
        address=r.get("address") or "",
        wallet_name=r.get("wallet_name") or "",
        evidence_source=r.get("evidence_source") or "",
        boundary_id=r.get("boundary_id"),
        service_name=r.get("service_name") or "",
        status=r.get("status") or "PENDING",
        detail=r.get("detail") or "",
        created_at=str(r.get("created_at") or ""),
        resolved_at=r.get("resolved_at"),
    )


async def _get_trace_or_404(trace_id: int) -> Trace:
    srv = state()
    trace = await srv.db.get_trace(trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found.")
    return trace


class ApiError(Exception):
    """Router-level request rejection carrying the HTTP status and detail.

    Independent of the discovery package's ``SessionError``: routers catch
    both where ``body()`` is used so the HTTP responses stay identical.
    """

    def __init__(self, detail: str, status: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status = status


def reply(data, status: int = 200) -> JSONResponse:
    """JSON response that is never cached (watch-only material, secrets)."""
    return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})


RETRY_AFTER_SECONDS = 2


def unavailable(detail: str, exc: BaseException) -> JSONResponse:
    """503 for a read that cannot complete right now, with its reason and a
    retry hint (FIX-OWN-1). The server logs every 503 (``UnavailableLog``)."""
    reason = str(exc) or "Execution time limit reached; retry."
    response = reply({"detail": f"{detail}: {reason}", "reason": reason,
                      "retry_after": RETRY_AFTER_SECONDS}, 503)
    response.headers["Retry-After"] = str(RETRY_AFTER_SECONDS)
    return response


def same_origin_ok(request) -> bool:
    """Scheme-independent same-origin check for one request.

    Rejects ``Sec-Fetch-Site: cross-site`` and an ``Origin`` whose authority
    does not match the ``Host`` header. No ``Origin`` and no
    ``Sec-Fetch-Site`` (curl, scripts, tests) is accepted.
    """
    headers = request.headers
    if headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
        return False
    origin = headers.get("origin")
    return origin is None or origin_matches_host(origin, headers.get("host"))


async def body(request, model, *, max_bytes: int = 16384):
    """Read a bounded same-origin JSON body and validate it against *model*.

    Raises :class:`ApiError` (status 400) with a non-echoing detail text.
    """
    if (request.headers.get("content-type", "").split(";")[0] != "application/json"
            or not same_origin_ok(request)):
        raise ApiError("Use same-origin JSON requests.", 400)
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > max_bytes:
            raise ApiError("Discovery request is too large.", 400)
    try:
        return model.model_validate(json.loads(raw))
    except Exception:
        raise ApiError("Invalid discovery request fields.", 400) from None
    finally:
        raw.clear()
