"""FastAPI application: REST API + static frontend hosting."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.api.http_common import _check_sensitive
from backend.config import Settings, get_settings
from backend.api.node_settings import create_node_settings_router
from backend.api.host_allowlist import HostAllowlistGuard
from backend.api.same_origin import SameOriginGuard
# Traces / Analyze domain router (P3-2). ``_build_full_trace`` is re-exported
# for the tests that import it from here.
from backend.api.traces import (  # noqa: F401 (re-export)
    _build_full_trace,
    router as traces_router,
)
# Acquisitions / Disposals / Exchange Exits domain router (P3-3).
from backend.api.acquisitions import router as acquisitions_router
# Inventory / Documentation Coverage domain router (P3-4).
# ``documentation_coverage_endpoint`` is also handed to the provenance router.
from backend.api.inventory import documentation_coverage_endpoint, router as inventory_router
# Registry / Ownership domain router (P3-4).
from backend.api.registry import router as registry_router
# Reports / exports / health domain router (P3-5).
from backend.api.reports import router as reports_router
from backend.node_settings import NodeSettingsStore
from backend.rpc.configured import ConfiguredBitcoinRPCClient
from backend.reporting.requests import CoverageRequests
from backend.version import APP_VERSION
from backend.address_discovery import AddressDiscovery
from backend.discovery.address_scan import AddressScanManager
from backend.discovery.wallet_scan import WalletScanSessions
from backend.api.wallet_discovery import create_wallet_discovery_router
from backend.electrs import ElectrsClient
from backend.persistence.database import Database
from backend.rpc.bitcoin import (
    NODE_UNREACHABLE_MESSAGE,
    BitcoinRPCClient,
    NodeUnreachableError,
    RpcError,
    RpcTimeoutError,
)
from backend.tracing.errors import TraceNotFoundError

logger = logging.getLogger("lineage.api")


FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"

settings = get_settings()
db = Database(settings.database_path)
rpc_client = ConfiguredBitcoinRPCClient(settings)

from backend.tracing.engine import TraceEngine  # noqa: E402

# Acquisition Review + Disposals v1
# (ACQUISITION_REVIEW_AND_DISPOSALS_V1_REQUIREMENTS.md): the app-level
# exception handlers below need the two Disposal error classes; the routes
# live in backend/api/acquisitions.py (P3-3).
from backend.acquisitions.disposal_service import (  # noqa: E402
    DisposalNotFoundError,
    DisposalValidationError,
)

engine = TraceEngine(db, rpc_client, max_depth=settings.max_trace_depth)
coverage_requests = CoverageRequests()

# Blockchain Analysis mode (BLOCKCHAIN_ANALYSIS_MODE_REQUIREMENTS.md): an
# ownership-independent, spend-state-driven walker, additive to the Forward
# Provenance engine. Uses the engine's stable readonly helpers only.
from backend.tracing.analysis import BlockchainAnalysisWalker  # noqa: E402

analysis = BlockchainAnalysisWalker(
    engine,
    max_hops=settings.analysis_max_hops,
    max_transactions=settings.analysis_max_transactions,
    max_outpoints=settings.analysis_max_outpoints,
)

# Background executor for Blockchain Analysis walks
# (BLOCKCHAIN_ANALYSIS_ASYNC_UX_PERFORMANCE_FIX_REQUIREMENTS.md). Long
# spend-state walks never run on the request path; each job runs in a bounded
# worker thread owning its own loop-bound Database/RPC/engine/walker.
from backend.worker import AnalysisWorker, recover_stale_analysis_jobs  # noqa: E402

analysis_worker = AnalysisWorker(settings)

# Reverse-mode address discovery (address -> candidate outpoints). Pure read,
# no ownership evidence. The Electrs fallback is only used when Core has no
# blockfilter index and ELECTRS_URL is configured.
electrs_client = ElectrsClient(settings.electrs_url, timeout=settings.electrs_timeout)
address_discovery = AddressDiscovery(rpc_client, electrs=electrs_client)

# Persistent address-discovery scan lifecycle
# (REVERSE_ADDRESS_DISCOVERY_LIFECYCLE_FIX_REQUIREMENTS.md): a long scanblocks
# discovery is a durable QUEUED/RUNNING/COMPLETE/CANCELLED/FAILED job that the
# browser polls - never one long request. Isolated from AnalysisWorker.
address_scan_manager = AddressScanManager(db, rpc_client, discovery=address_discovery)
wallet_scan_sessions = WalletScanSessions(rpc_client, db)
from backend.inventory import InventoryRefresh
inventory_refresh = InventoryRefresh(db, rpc_client)


def inventory_manager():
    global inventory_refresh
    if inventory_refresh.db is not db or inventory_refresh.rpc is not rpc_client:
        inventory_refresh = InventoryRefresh(db, rpc_client)
    return inventory_refresh


def wallet_sessions():
    # Tests replace the app's DB/RPC; production owns one immutable pair.
    global wallet_scan_sessions
    if wallet_scan_sessions.db is not db or wallet_scan_sessions.rpc is not rpc_client:
        wallet_scan_sessions = WalletScanSessions(rpc_client, db)
    return wallet_scan_sessions


async def invalidate_wallet_sessions():
    await wallet_sessions().invalidate()
    await inventory_manager().invalidate()
    wallet_sessions().restart_required = True
    inventory_manager().restart_required = True
    await db.db.execute("UPDATE wallet_inventory_scopes SET scan_mode='STALE_NODE'")
    await db.db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.connect()
    # Any QUEUED/RUNNING jobs left by a previous process are dead: mark them
    # FAILED and the traces they were walking ERROR so the UI offers a re-check.
    await recover_stale_analysis_jobs(db)
    # Address-discovery scans survive a restart when Core is still scanning
    # them (scenario A); otherwise their completion is resolved or they are
    # failed honestly (scenario B). A Core scan with no app job stays foreign.
    try:
        await address_scan_manager.reconcile_on_startup()
    except Exception:
        logger.exception("address-discovery job reconciliation failed at startup")
    analysis_worker.start()
    try:
        yield
    finally:
        await inventory_manager().close()
        await wallet_sessions().close()
        await address_scan_manager.close()
        analysis_worker.shutdown(wait=True)
        await electrs_client.close()
        await db.close()
        await rpc_client.close()


app = FastAPI(title="Bitcoin Lineage Tracker", version="0.1.0", lifespan=lifespan)
app.include_router(create_node_settings_router(NodeSettingsStore(Settings.from_env()), settings,
                                              on_saved=invalidate_wallet_sessions))
app.include_router(create_wallet_discovery_router(wallet_sessions))


class UnconfiguredNodeGuard:
    """Pure ASGI: leave disconnect events available to bounded report requests."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if (scope["type"] == "http"
                and isinstance(engine.rpc, ConfiguredBitcoinRPCClient)
                and engine.rpc.configuration_error() and path.startswith("/api/")
                # B4: the checklist explains an unconfigured node itself.
                and path not in {"/api/health", "/api/settings", "/api/getting-started"}
                and not path.startswith("/api/node-settings")):
            response = JSONResponse(status_code=503, content={
                "detail": "Bitcoin Core is not configured. Open Node Settings."})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class UnavailableLog:
    """Pure ASGI: log every 503 with endpoint, reason and trace (FIX-OWN-1).

    The outpoint is part of the path; the trace comes from ``trace_id``.
    Other query parameters (e.g. wallet labels) are not logged, and address
    segments outside provenance/report paths are redacted (B5-07).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        status, body = None, bytearray()

        async def logging_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body" and status == 503:
                body.extend(message.get("body", b"")[:4096 - len(body)])
                if not message.get("more_body"):
                    logger.warning("503 %s %s%s: %s", scope["method"], _logged_path(scope["path"]),
                                   _trace_scope(scope), _unavailable_reason(bytes(body)))
            await send(message)

        await self.app(scope, receive, logging_send)


# B5-07: provenance and report reads keep their full path (TXIDs/outpoints
# are accepted in the operator log, S-5); elsewhere address segments are
# replaced, never logged (§12).
_FULL_PATH_PREFIXES = ("/api/provenance", "/api/reports")
_ADDRESS_PARENTS = {"addresses", "evidence", "address"}
_ADDRESS_SEGMENT = re.compile(
    r"(?:(?:bc|tb|bcrt)1[0-9a-z]{6,87}|[13mn2][1-9A-HJ-NP-Za-km-z]{25,39})", re.IGNORECASE
)
_TXID_SEGMENT = re.compile(r"[0-9a-fA-F]{64}")


def _printable(text):
    """B5-08: control characters (percent-decoded ``%0A`` etc.) escaped, one log line."""
    return "".join(c if c.isprintable() else c.encode("unicode_escape").decode("ascii") for c in text)


def _logged_path(path):
    path = _printable(path)
    if path.startswith(_FULL_PATH_PREFIXES):
        return path
    segments = path.split("/")
    for i, segment in enumerate(segments):
        after_parent = i > 0 and segments[i - 1] in _ADDRESS_PARENTS and segment not in {"", "scan"}
        if _TXID_SEGMENT.fullmatch(segment):
            continue
        if after_parent or _ADDRESS_SEGMENT.fullmatch(segment):
            segments[i] = "<address>"
    return "/".join(segments)


def _trace_scope(scope):
    trace = parse_qs(scope.get("query_string", b"").decode("latin-1")).get("trace_id")
    return f" (trace_id={_printable(trace[0])})" if trace else ""


def _unavailable_reason(body):
    try:
        data = json.loads(body)
    except ValueError:
        return "no reason given"
    if isinstance(data, dict):
        # The detail names what is unavailable and, via unavailable(), its reason.
        return str(data.get("detail") or data.get("reason") or "no reason given")
    return "no reason given"


app.add_middleware(UnconfiguredNodeGuard)
# Outside the node guard, so its 503 is logged as well.
app.add_middleware(UnavailableLog)
# Cross-site non-GET requests are rejected (403) before any other handling
# (P1-7, L-13).
app.add_middleware(SameOriginGuard)
# Added last = outermost: requests for an unknown Host (DNS rebinding) are
# rejected (400) before the same-origin check and everything else (SEC-05).
app.add_middleware(HostAllowlistGuard)


@app.exception_handler(RpcError)
async def _rpc_error_handler(_: Request, exc: RpcError):
    logger.warning("RPC error: %s", exc.message)
    if isinstance(exc, TraceNotFoundError):
        return JSONResponse(status_code=404, content={"detail": "Trace not found."})
    if isinstance(exc, NodeUnreachableError):
        return JSONResponse(status_code=503, content={"detail": NODE_UNREACHABLE_MESSAGE})
    if isinstance(exc, RpcTimeoutError):
        return JSONResponse(status_code=504, content={"detail": "Bitcoin Core request timed out; retry."})
    return JSONResponse(status_code=400, content={"detail": exc.message})


@app.exception_handler(sqlite3.OperationalError)
async def _sqlite_operational_error_handler(_: Request, exc: sqlite3.OperationalError):
    # aiosqlite re-raises the stdlib sqlite3 exceptions unchanged
    # (``aiosqlite.OperationalError is sqlite3.OperationalError``).
    # SQLITE_BUSY / SQLITE_BUSY_SNAPSHOT both carry "database is locked".
    if "database is locked" in str(exc):
        logger.warning("SQLite busy: %s", exc)
        return JSONResponse(status_code=503, content={"detail": "Database busy, please retry."})
    raise exc  # unchanged generic 500 behaviour for every other OperationalError


@app.exception_handler(DisposalValidationError)
async def _disposal_validation_handler(_: Request, exc: DisposalValidationError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(DisposalNotFoundError)
async def _disposal_not_found_handler(_: Request, exc: DisposalNotFoundError):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


# ---------------------------------------------------------------------------
# Reports / exports / health domain (backend/api/reports.py, P3-5)
# ---------------------------------------------------------------------------
# Registered here, where ``GET /api/health`` used to be declared, so the
# global route resolution order is unchanged. The provenance report and
# settings routes (declared after the dashboard router before) share no path
# with the traces / acquisitions / inventory / provenance / dashboard routers
# (``/api/outpoints/{txid}/{vout}/acquisition-provenance`` vs
# ``/api/outpoints/{txid}/{vout}/classify`` differ in their static tail), so
# they resolve identically from here.

app.include_router(reports_router)


# ---------------------------------------------------------------------------
# Traces / Analyze domain (backend/api/traces.py, P3-2)
# ---------------------------------------------------------------------------
# Registered here, where ``POST /api/analyze`` used to be declared, so the
# global route resolution order is unchanged.

app.include_router(traces_router)


# ---------------------------------------------------------------------------
# Acquisitions / Disposals / Exchange Exits domain (backend/api/acquisitions.py,
# P3-3)
# ---------------------------------------------------------------------------
# Registered here, where ``GET /api/acquisitions`` used to be declared, so the
# global route resolution order is unchanged (the moved routes keep their
# relative order inside the router).

app.include_router(acquisitions_router)


# ---------------------------------------------------------------------------
# Inventory / Documentation Coverage domain (backend/api/inventory.py, P3-4)
# ---------------------------------------------------------------------------
# Registered here, where ``GET /api/reports/documentation-coverage`` /
# ``GET /api/inventory`` used to be declared, so the global route resolution
# order is unchanged. The inventory refresh / export routes (declared after
# the provenance and dashboard routers before) share no path with those
# routers, so they resolve identically from here.

app.include_router(inventory_router)


from backend.api.provenance import create_provenance_router
app.include_router(create_provenance_router(lambda: (db, engine), documentation_coverage_endpoint, _check_sensitive))
from backend.api.dashboard import create_dashboard_router
app.include_router(create_dashboard_router(lambda: (db, engine)))


# ---------------------------------------------------------------------------
# Registry / Ownership domain (backend/api/registry.py, P3-4)
# ---------------------------------------------------------------------------
# Registered here, where ``POST /api/registry/import`` used to be declared, so
# the global route resolution order is unchanged (the moved routes keep their
# relative order inside the router).

app.include_router(registry_router)

# Getting-started checklist (B4): registered last, so the resolution order of
# every existing route is unchanged.
from backend.api.getting_started import router as getting_started_router  # noqa: E402
app.include_router(getting_started_router)


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

STATIC_DIR = FRONTEND_DIR / "static"

# Cache-busted index.html: all /static/ href/src attributes get ?v=APP_VERSION
# appended at serve time.  This is the single source of truth for the asset
# cache-bust version – bump APP_VERSION in backend/version.py on each release.
_STATIC_ASSET_RE = re.compile(r'((?:src|href)="/static/[^"?]+)')
_INDEX_HTML_PATH = FRONTEND_DIR / "static" / "index.html"


def _render_index_html() -> str:
    """Read index.html and rewrite every local asset URL with ?v=APP_VERSION."""
    html = _INDEX_HTML_PATH.read_text(encoding="utf-8")
    html = _STATIC_ASSET_RE.sub(lambda m: m.group(1) + f"?v={APP_VERSION}", html)
    return html.replace("{{APP_VERSION}}", APP_VERSION)


class _VersionedStaticFiles(StaticFiles):
    """Serves /static/ assets; requests carrying a ?v= parameter receive an
    immutable long-lived Cache-Control header so the browser never re-fetches
    a particular version of an asset."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = super().file_response(full_path, stat_result, scope, status_code)
        if status_code == 200 and b"v=" in scope.get("query_string", b""):
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable"
            )
        return response


if STATIC_DIR.exists():
    app.mount("/static", _VersionedStaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def index() -> HTMLResponse:
    return HTMLResponse(
        content=_render_index_html(),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )
