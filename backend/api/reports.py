"""Reports, exports and health domain router (consolidation package P3-5).

Routes for the node status / health endpoints (``GET /api/health``, ``GET
/health``), the Acquisition Provenance reports (``GET
/api/reports/trace-provenance``, ``GET
/api/outpoints/{txid}/{vout}/acquisition-provenance``) and the settings
read-out (``GET /api/settings``).

The CSV exports already live in their domain routers: the acquisition /
disposal / exchange-exit CSVs in ``backend.api.acquisitions``, the inventory
CSV in ``backend.api.inventory`` and the dashboard / provenance CSVs in
``backend.api.dashboard`` / ``backend.api.provenance``.

The application's runtime objects (database, engine) remain module globals
of ``backend.api.server`` so tests can keep replacing / inspecting them
there. This module resolves them late-bound through
:func:`backend.api.runtime.state` at call time (``srv = state()`` at the top
of each endpoint) and never caches them at import time.
"""

from __future__ import annotations

from typing import Any, Optional, Literal

from fastapi import APIRouter, HTTPException, Query

from backend.api.http_common import _validate_txid, unavailable
from backend.api.runtime import state
from backend.node_settings import CHAINS
from backend.reporting.provenance import managed_provenance
from backend.reporting.read_data import ReadBudgetExceeded
from backend.version import APP_VERSION

router = APIRouter()


# ---------------------------------------------------------------------------
# Node status / health
# ---------------------------------------------------------------------------


async def checked_node_status(engine) -> dict[str, Any]:
    """``node_status()`` with the network matched against Node Settings."""
    result = await engine.node_status()
    # Match all supported networks, including testnet/signet/regtest.
    expected = CHAINS.get(engine.rpc.settings.btc_network, engine.rpc.settings.btc_network)
    result["expected_network"] = expected
    if result.get("network") is not None:
        result["network_match"] = result["network"] == expected
    return result


@router.get("/api/health")
async def health() -> dict[str, Any]:
    return await checked_node_status(state().engine)


@router.get("/health")
async def app_health() -> dict[str, Any]:
    """Application-level liveness (container health check).

    Returns 200 whenever the application process is up, independently of the
    Bitcoin Core dependency state (REQ-UMB-023: app healthy vs Bitcoin warning).
    The richer node/dependency status stays on /api/health.
    """
    srv = state()
    return {
        "status": "ok",
        "app": "wherearemysats",
        "version": APP_VERSION,
        "db_connected": srv.db.db is not None,
    }


# ---------------------------------------------------------------------------
# Acquisition Provenance reports
# ---------------------------------------------------------------------------


async def _provenance_report(**scope):
    srv = state()
    try:
        return await managed_provenance(srv.db, srv.engine, **scope)
    except ReadBudgetExceeded as exc:
        return unavailable('Acquisition provenance unavailable', exc)


@router.get('/api/reports/trace-provenance')
async def trace_provenance_endpoint(
    trace_id: Optional[int] = Query(None, ge=1),
    provenance: Optional[Literal['ANCHORED', 'PARTIAL', 'UNANCHORED', 'NEEDS_REVIEW']] = Query(None),
):
    srv = state()
    if trace_id is not None and not await srv.db.get_trace(trace_id):
        raise HTTPException(status_code=404, detail='Trace not found.')
    return await _provenance_report(trace_id=trace_id, state=provenance)


@router.get('/api/outpoints/{txid}/{vout}/acquisition-provenance')
async def outpoint_provenance_endpoint(txid: str, vout: int):
    txid = _validate_txid(txid)
    if vout < 0:
        raise HTTPException(status_code=400, detail='VOUT must be non-negative.')
    return await _provenance_report(outpoint=(txid, vout))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@router.get("/api/settings")
async def get_settings_api() -> dict[str, str]:
    srv = state()
    return await srv.db.get_all_settings()
