"""Inventory / Documentation Coverage domain router (consolidation package
P3-4).

Routes for the Documentation Coverage report (``GET
/api/reports/documentation-coverage``) and the Wallet Inventory (``GET
/api/inventory``, served by the same endpoint function), the inventory
refresh status / start and the inventory CSV export.

``documentation_coverage_endpoint`` is also passed as a callable into the
provenance router by ``backend.api.server`` (the composition root may import
from routers; routers never import from each other).

The application's runtime objects (database, engine, settings, the
``coverage_requests`` single-flight registry and the ``inventory_manager()``
accessor) remain module globals of ``backend.api.server`` so tests can keep
replacing / inspecting them there. This module resolves them late-bound
through :func:`backend.api.runtime.state` at call time (``srv = state()`` at
the top of each endpoint) and never caches them at import time.
"""

from __future__ import annotations

import logging
from typing import Optional, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from backend.api.runtime import state
from backend.reporting.coverage import DocumentationCoverage, documentation_coverage
from backend.reporting.read_data import ReadBudgetExceeded, revision
from backend.reporting.requests import CoverageDisconnected
from backend.reporting.no_acquisition import InventoryReport
from backend.rpc.bitcoin import RpcError

logger = logging.getLogger("lineage.api")

router = APIRouter()


@router.get("/api/reports/documentation-coverage", response_model=DocumentationCoverage)
@router.get("/api/inventory", response_model=InventoryReport)
async def documentation_coverage_endpoint(request: Request, response: Response,
    provenance: Optional[Literal['ANCHORED', 'PARTIAL', 'UNANCHORED', 'NEEDS_REVIEW']] = Query(None),
    wallet: Optional[str] = Query(None),
) -> DocumentationCoverage:
    srv = state()
    response.headers["Cache-Control"] = "no-store"
    try:
        key = (id(srv.db), id(srv.engine), await revision(srv.db))
        report = await srv.coverage_requests.run(key,
            lambda: documentation_coverage(srv.db, srv.engine, settings=srv.settings), request, scope=key[:2])
        if request.url.path == '/api/inventory':
            from backend.reporting.no_acquisition import positions
            if await revision(srv.db) != key[2]:
                raise ReadBudgetExceeded('Evidence changed during inventory lineage generation; retry.')
            enriched = await positions(srv.db, srv.engine, report, limit=report.utxo_count)
            result = report.model_dump()
            result['utxos'] = [u for u in enriched['positions']
                if (provenance is None or u['acquisition_provenance'] == provenance)
                and (wallet is None or any(m['wallet_id'] == wallet for m in u['wallet_matches']))]
            result['coverage_totals'] = enriched['coverage_totals']
            result['classification_version'] = enriched['classification_version']
            return InventoryReport(**result)
        # Filter only the detail rows, keeping the global population/summary.
        # Never mutate the shared single-flight result for another caller.
        if provenance is not None or wallet is not None:
            return report.model_copy(update={'utxos': [u for u in report.utxos
                if (provenance is None or u.acquisition_provenance == provenance)
                and (wallet is None or any(m['wallet_id'] == wallet for m in u.wallet_matches))]})
        return report
    except CoverageDisconnected:
        return Response(status_code=499)
    except ReadBudgetExceeded as exc:
        logger.warning("documentation coverage aborted: %s", exc)
        raise HTTPException(status_code=503, detail=
            "Documentation Coverage unavailable: execution limit reached or evidence changed. Please retry.") from exc
    except RpcError as exc:
        raise HTTPException(
            status_code=503,
            detail="Documentation Coverage unavailable: current holdings could not be verified with Bitcoin Core.",
        ) from exc


@router.get('/api/inventory/refresh')
async def inventory_refresh_status():
    srv = state()
    return JSONResponse(srv.inventory_manager().status(), headers={"Cache-Control": "no-store"})


@router.post('/api/inventory/refresh', status_code=202)
async def inventory_refresh_start(request: Request):
    srv = state()
    from backend.api.http_common import ApiError, body, reply
    from backend.discovery.wallet_scan import SessionError
    from pydantic import BaseModel, ConfigDict
    class RefreshRequest(BaseModel):
        model_config = ConfigDict(extra='forbid')
    try:
        await body(request, RefreshRequest)  # raises ApiError (400)
        return reply(srv.inventory_manager().start(), 202)  # raises SessionError (409)
    except (ApiError, SessionError) as exc:
        return reply({"detail": str(exc)}, exc.status)


@router.get('/api/inventory/export.csv')
async def inventory_export(request: Request,
    provenance: Optional[Literal['ANCHORED', 'PARTIAL', 'UNANCHORED', 'NEEDS_REVIEW']] = Query(None),
    wallet: Optional[str] = Query(None),
    coverage_status: Optional[Literal['COVERED', 'MIXED', 'UNCOVERED', 'UNRESOLVED']] = None):
    srv = state()
    from backend.reporting.csv_exports import inventory_csv
    before = await revision(srv.db)
    report = await documentation_coverage_endpoint(request, Response(), provenance, wallet)
    if isinstance(report, Response):
        return report
    if coverage_status is not None:
        from backend.reporting.no_acquisition import positions
        try:
            if await revision(srv.db) != before:
                raise ReadBudgetExceeded('Evidence changed; retry.')
            summary = await positions(srv.db, srv.engine, report, limit=report.utxo_count)
        except (ReadBudgetExceeded, TimeoutError):
            raise HTTPException(503, 'Inventory lineage unavailable; retry.')
        selected = {u['outpoint'] for u in summary['positions'] if u['coverage_status'] == coverage_status}
        report = report.model_copy(update={'utxos': [u for u in report.utxos if u.outpoint in selected]})
    return Response(inventory_csv(report), media_type='text/csv; charset=utf-8', headers={
        'Content-Disposition': 'attachment; filename="wallet-inventory.csv"', 'Cache-Control': 'no-store'})
