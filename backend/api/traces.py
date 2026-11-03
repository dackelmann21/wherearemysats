"""Traces / Analyze domain router (consolidation package P3-2).

Routes for transaction analysis, reverse-mode address discovery and scans,
traces (create / list / get / delete / select / continue / follow /
analysis-job / summary / export), outpoint classification, annotations and
trace-scoped custody (custodial boundaries, custody candidates, declined
suggestions, custody bridges), plus the helpers only these routes use
(``_build_full_trace``, ``_compute_summary``, ``_enrich_input_context``, ...).

The application's runtime objects (database, engine, RPC client, analysis
worker, settings, ...) remain module globals of ``backend.api.server`` so
tests can keep replacing them there. This module resolves them late-bound
through :func:`backend.api.runtime.state` at call time (``srv = state()`` at
the top of each endpoint / helper) and never caches them at import time.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response

from backend.address_discovery import (
    AddressDiscovery,
    AddressDiscoveryUnavailableError,
    InvalidAddressError,
)
from backend.api.http_common import (
    _check_sensitive,
    _get_trace_or_404,
    _ownership_custody_conflict_model,
    _validate_txid,
)
from backend.api.runtime import state
from backend.custody import candidates as custody_candidates
from backend.custody import detect as custody_detect
from backend.models.schemas import (
    AddressDiscoveryResult,
    AddressOutput,
    AddressScanJob,
    AddressScanStart,
    AddressScanStatus,
    AnalysisFollowRequest,
    AnalysisJob,
    AnalysisJobEnqueued,
    AnalysisJobLimits,
    AnalysisJobProgress,
    AnalysisJobResponse,
    Annotation,
    AnalyzeResponse,
    ClassificationSource,
    ClassificationUpdate,
    CustodialBoundary,
    CustodialBoundaryCreate,
    CustodialDismissal,
    CustodialDismissalCreate,
    CustodyBridge,
    CustodyBridgeConfirm,
    CustodyBridgeSource,
    CustodyCandidate,
    FullTrace,
    InputInfo,
    OutpointsRequest,
    OutputInfo,
    OwnershipStatus,
    Provenance,
    RelationshipType,
    Trace,
    TraceCreate,
    TraceCreateResponse,
    TraceEdge,
    TraceExport,
    TraceInputRole,
    TraceMode,
    TraceStatus,
    TraceSummary,
    TransactionInfo,
)
from backend.persistence.database import Database
from backend.reporting.csv_exports import safe_text
from backend.rpc.bitcoin import (
    NODE_UNREACHABLE_MESSAGE,
    BlockNotSyncedError,
    NodeUnreachableError,
    RpcError,
    RpcTimeoutError,
)
from backend.tracing.engine import TraceEngine
from backend.tracing.provenance import provenance_for_output, tx_topology

logger = logging.getLogger("lineage.api")

router = APIRouter()


# ---------------------------------------------------------------------------
# Transaction analysis
# ---------------------------------------------------------------------------


@router.post("/api/analyze", response_model=AnalyzeResponse)
async def analyze(payload: TraceCreate) -> AnalyzeResponse:
    srv = state()
    txid = _validate_txid(payload.start_txid)
    _check_sensitive(payload.start_address, "address")
    if payload.start_vout is not None and payload.start_vout < 0:
        raise HTTPException(status_code=400, detail="VOUT must be >= 0.")

    result = await srv.engine.analyze_start(txid, payload.start_address, payload.start_vout)
    return AnalyzeResponse(
        trace_id=None,
        transaction=result.get("transaction"),
        matched_vout=result.get("matched_vout"),
    )


@router.get("/api/transactions/{txid}", response_model=TransactionInfo)
async def get_transaction(
    txid: str, response: Response, provenance: Optional[Literal["neutral"]] = None
) -> TransactionInfo:
    srv = state()
    response.headers['Cache-Control'] = 'no-store'
    txid = _validate_txid(txid)
    # Plain read: never persist ownership reconciliation (L-10).
    tx = await srv.engine.load_transaction(txid, persist_ownership=False)
    if provenance == "neutral":
        # P4-N1: the output dialog of an analysis trace shows the same
        # ownership-neutral provenance as its table and export, computed into
        # a copy (the engine shares ``tx`` via its memory cache), never written.
        tx = tx.model_copy(deep=True)
        topology = tx_topology(tx)
        for o in tx.outputs:
            o.provenance_state, o.provenance_reasons = provenance_for_output(tx, o.vout, set())
            o.transaction_topology = topology
    from backend.reporting.origin_resolution import OriginResolutions
    (await OriginResolutions.load(srv.db)).enrich(tx.outputs)
    await _attach_spending_txids(srv, tx.outputs)
    return tx


SPENDER_LOOKUP_TIMEOUT = 5.0


async def _attach_spending_txids(srv, outputs: list[OutputInfo]) -> None:
    """Fill ``spending_txid`` for the output dialog's "spent by" link (FIX-UI-3).

    The outpoint cache row carries no spending txid; a walked spend is in
    ``spends``. Outputs marked SPENT only because a later transaction that
    consumes them was loaded have no such row, so they are looked up with one
    read-only txospenderindex call. Null when the spend stays unknown; when
    the node is unreachable or the lookup times out, ``spending_lookup_error``
    says so (B5-04).
    """
    missing = []
    for out in outputs:
        spend = await srv.db.get_spend(out.txid, out.vout)
        out.spending_txid = spend["spending_txid"] if spend else None
        out.spending_lookup_error = None
        if out.spending_txid is None and out.spend_status == "SPENT":
            missing.append(out)
    if not missing:
        return
    # Bounded so a slow node never holds the dialog for the full RPC timeout.
    try:
        resp = await asyncio.wait_for(
            srv.engine.rpc.get_tx_spending_prevout(
                [(o.txid, o.vout) for o in missing], mempool_only=False
            ),
            timeout=SPENDER_LOOKUP_TIMEOUT,
        )
    except (NodeUnreachableError, RpcTimeoutError, asyncio.TimeoutError) as exc:
        reason = (NODE_UNREACHABLE_MESSAGE if isinstance(exc, NodeUnreachableError)
                  else "Spender lookup timed out.")
        for out in missing:
            out.spending_lookup_error = reason
        return
    except RpcError:
        return
    spenders = {(e.get("txid"), e.get("vout")): e.get("spendingtxid") for e in resp or []}
    for out in missing:
        out.spending_txid = spenders.get((out.txid, out.vout))


# ---------------------------------------------------------------------------
# Reverse-mode address discovery
# ---------------------------------------------------------------------------
# REVERSE_PROVENANCE / BLOCKCHAIN_ANALYSIS_REVERSE: resolve every locally
# discoverable outpoint paying to a Bitcoin address. Read-only, local only, no
# ownership evidence, no trace creation.


@router.get(
    "/api/reverse/address/{address}/outputs",
    response_model=AddressDiscoveryResult,
)
async def reverse_address_outputs(address: str) -> AddressDiscoveryResult:
    """Return all locally discoverable outputs that pay to *address*.

    Included purely for reverse-mode target selection. Never writes ownership
    evidence, outpoint, or trace data. Sources: Bitcoin Core scanblocks
    (blockfilter index) or, when configured, local Electrs fallback.
    """
    srv = state()
    candidate = (address or "").strip()
    if not candidate:
        raise HTTPException(status_code=400, detail="address is required")
    _check_sensitive(candidate, "address")
    svc = AddressDiscovery(srv.rpc_client, electrs=srv.electrs_client)
    try:
        outputs, source = await svc.resolve_address_outputs(candidate)
    except InvalidAddressError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AddressDiscoveryUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except NodeUnreachableError as exc:
        raise HTTPException(status_code=503, detail=NODE_UNREACHABLE_MESSAGE) from exc
    except BlockNotSyncedError as exc:
        raise HTTPException(
            status_code=503,
            detail="Bitcoin Core is still in initial block download.",
        ) from exc
    except RpcTimeoutError as exc:
        raise HTTPException(status_code=504, detail="Bitcoin Core address scan timed out.") from exc

    return AddressDiscoveryResult(
        address=candidate,
        discovery_source=source,
        outputs=[
            AddressOutput(
                txid=out["txid"],
                vout=out["vout"],
                outpoint=f'{out["txid"]}:{out["vout"]}',
                address=out["address"],
                value_sats=out["value_sats"],
                spend_status=out["spend_status"],
                spending_txid=out.get("spending_txid"),
                block_height=out.get("block_height"),
                block_time=out.get("block_time"),
            )
            for out in outputs
        ],
    )


# Persistent address-discovery scan lifecycle. A reverse-mode address -> outputs discovery is
# a durable job the browser polls. Bitcoin Core's ``scanblocks start`` is
# SYNCHRONOUS (it returns only after the scan completes/aborts), so the manager
# runs it in a background task and the endpoints only ever touch
# ``scanblocks status`` (poll/reconnect) / ``scanblocks abort`` (stop). While a
# scan is active the app never issues a second scanblocks start, and a Core scan
# the app did not own is reported as a neutral foreign scan. The reconnect
# endpoint never returns a terminal (COMPLETE/CANCELLED/FAILED) job - those are
# history, not current UI state - so a fresh Start Trace page always starts
# visually clean.


@router.post("/api/reverse/address/scan", response_model=AddressScanStatus)
async def reverse_address_scan_start(payload: AddressScanStart) -> AddressScanStatus:
    """Start (or resume) an address-discovery scan for a mainnet address."""
    srv = state()
    candidate = (payload.address or "").strip()
    if not candidate:
        raise HTTPException(status_code=400, detail="address is required")
    _check_sensitive(candidate, "address")
    try:
        return await srv.address_scan_manager.start_scan(candidate)
    except InvalidAddressError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/reverse/address/scan", response_model=AddressScanStatus)
async def reverse_address_scan_active() -> AddressScanStatus:
    """Reconnect query: the active app-owned job (QUEUED/RUNNING) or
    foreign-scan info when Bitcoin Core alone is running a scan. Terminal
    COMPLETE/CANCELLED/FAILED jobs are historical and are never returned, so a
    fresh Start Trace page starts clean instead of restoring stale UI state."""
    srv = state()
    return await srv.address_scan_manager.active_status()


@router.get("/api/reverse/address/scan/{job_id}", response_model=AddressScanJob)
async def reverse_address_scan_poll(job_id: int) -> AddressScanJob:
    """Poll the live progress / terminal state of one address-discovery scan."""
    srv = state()
    job = await srv.address_scan_manager.poll_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Address scan not found.")
    return job


@router.post("/api/reverse/address/scan/{job_id}/abort", response_model=AddressScanJob)
async def reverse_address_scan_abort(job_id: int) -> AddressScanJob:
    """Stop an app-owned scan: scanblocks abort -> CANCELLED (nothing deleted)."""
    srv = state()
    job = await srv.address_scan_manager.abort_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Address scan not found.")
    return job


# ---------------------------------------------------------------------------
# Traces
# ---------------------------------------------------------------------------


@router.post("/api/traces", status_code=201)
async def create_trace(payload: TraceCreate) -> Response:
    srv = state()
    txid = _validate_txid(payload.start_txid)
    _check_sensitive(payload.name, "name")
    _check_sensitive(payload.start_address, "address")
    if payload.start_vout is not None and payload.start_vout < 0:
        raise HTTPException(status_code=400, detail="VOUT must be >= 0.")

    mode = (payload.trace_mode or TraceMode.FORWARD_PROVENANCE).strip().upper()
    if mode not in (
        TraceMode.FORWARD_PROVENANCE,
        TraceMode.BLOCKCHAIN_ANALYSIS,
        TraceMode.REVERSE_PROVENANCE,
        TraceMode.BLOCKCHAIN_ANALYSIS_REVERSE,
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Trace mode '{mode}' is not available yet.",
        )

    is_reverse = mode in (
        TraceMode.REVERSE_PROVENANCE,
        TraceMode.BLOCKCHAIN_ANALYSIS_REVERSE,
    )

    if mode in (
        TraceMode.BLOCKCHAIN_ANALYSIS,
        TraceMode.BLOCKCHAIN_ANALYSIS_REVERSE,
        TraceMode.REVERSE_PROVENANCE,
    ):
        # Validate the start outpoint synchronously (same RPC checks the
        # walker would make); then hand the expensive walk to the background
        # worker and return 202 so the request never blocks on it.
        try:
            start_tx = await srv.engine.load_transaction(txid)
        except RpcError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        # Reverse modes anchor on one exact outpoint: start_vout is required
        # and the output must exist. OP_RETURN / provably unspendable outputs
        # are not legitimate personal-trace / ancestry targets.
        target_out = None
        if payload.start_vout is not None:
            target_out = next(
                (o for o in start_tx.outputs if o.vout == payload.start_vout), None
            )
            if target_out is None:
                raise HTTPException(
                    status_code=400,
                    detail="The supplied VOUT does not exist in this transaction.",
                )
        if is_reverse and payload.start_vout is None:
            raise HTTPException(
                status_code=400,
                detail="Reverse modes require an exact TXID:VOUT target (start_vout).",
            )
        if is_reverse and target_out is not None:
            script_type = (target_out.script_type or "").lower()
            script_pubkey = target_out.script_pubkey or ""
            if script_type == "nulldata" or script_pubkey.startswith("6a"):
                raise HTTPException(
                    status_code=400,
                    detail="OP_RETURN / provably unspendable outputs cannot be used "
                           "as reverse trace targets.",
                )

        trace = await srv.db.create_trace(
            name=payload.name,
            start_txid=txid,
            start_address=payload.start_address,
            start_vout=payload.start_vout,
            trace_mode=mode,
        )
        job = await srv.db.create_analysis_job(trace.id, operation="CREATE")
        try:
            srv.analysis_worker.submit(
                job_id=job["id"], trace_id=trace.id, operation="CREATE"
            )
        except Exception:
            logger.exception("failed to submit analysis job for trace %d", trace.id)
            await srv.db.update_analysis_job_state(
                job["id"], "FAILED", error_summary="Analysis worker is unavailable. Please retry."
            )
            await srv.db.update_trace(trace.id, status="ERROR")
            job = await srv.db.get_analysis_job(job["id"])

        return JSONResponse(
            status_code=202,
            content=TraceCreateResponse(
                id=trace.id,
                trace_id=trace.id,
                trace_mode=trace.trace_mode,
                status="IN_PROGRESS",
                analysis_job=AnalysisJob(**job),
            ).model_dump(),
        )

    # Forward Provenance: synchronous walk on the request path (unchanged).
    trace, _walk = await srv.engine.create_trace(
        name=payload.name,
        start_txid=txid,
        start_address=payload.start_address,
        start_vout=payload.start_vout,
    )
    return JSONResponse(status_code=201, content=trace.model_dump())


@router.get("/api/traces")
async def list_traces() -> list[Trace]:
    srv = state()
    return await srv.db.list_traces()


@router.delete("/api/traces", status_code=204)
async def delete_all_traces() -> Response:
    srv = state()
    await srv.db.delete_all_traces()
    return Response(status_code=204)


@router.get("/api/traces/{trace_id}", response_model=FullTrace)
async def get_trace(trace_id: int, response: Response) -> FullTrace:
    srv = state()
    response.headers['Cache-Control'] = 'no-store'
    trace = await srv.db.get_trace(trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found.")

    full = await _build_full_trace(trace, srv.engine)
    full.unresolved_count = await srv.db.get_latest_finished_unresolved_count(trace_id)
    return full


@router.delete("/api/traces/{trace_id}", status_code=204)
async def delete_trace(trace_id: int) -> Response:
    srv = state()
    trace = await srv.db.get_trace(trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found.")
    await srv.db.delete_trace(trace_id)
    return Response(status_code=204)


@router.post("/api/traces/{trace_id}/select")
async def select_outputs(trace_id: int, payload: OutpointsRequest) -> dict[str, Any]:
    """Manually select output(s) of the start transaction to trace."""
    srv = state()
    trace = await srv.db.get_trace(trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found.")
    txid = _validate_txid(payload.txid)

    tx = await srv.engine.load_transaction(txid)
    valid = {o.vout for o in tx.outputs}
    for v in payload.vouts:
        if v not in valid:
            raise HTTPException(status_code=400, detail=f"VOUT {v} does not exist in this transaction.")

    for v in payload.vouts:
        await srv.engine.classify_output(
            trace_id,
            txid,
            v,
            "OWNED",
            classification_source="USER",
            announce=False,
        )

    # Forward Provenance re-walks synchronously here. Analysis / reverse modes
    # own their traversal on the background worker and never use this path
    # (reverse traces anchor on one exact outpoint at creation).
    if trace.trace_mode in (
        TraceMode.BLOCKCHAIN_ANALYSIS,
        TraceMode.BLOCKCHAIN_ANALYSIS_REVERSE,
        TraceMode.REVERSE_PROVENANCE,
    ):
        return {"message": f"{len(payload.vouts)} output(s) selected."}

    walk = await srv.engine.continue_trace(trace_id)
    return {"message": f"{len(payload.vouts)} output(s) selected.", "walk": walk}


async def _register_owned_output_address(txid, vout, payload):
    srv = state()
    if payload.key_access is not None and not payload.register_owned_address:
        raise HTTPException(status_code=400, detail="Key access applies only to an address confirmation.")
    if payload.register_owned_address:
        if payload.ownership_status != "OWNED" or payload.classification_source != "USER":
            raise HTTPException(status_code=400, detail="Address confirmation requires an explicit USER OWNED decision.")
        output = await srv.db.get_outpoint(txid, vout)
        if not output or not output.get("address"):
            raise HTTPException(status_code=400, detail="This cached output has no address to confirm.")
        existing = await srv.db.get_known_address(output["address"])
        wallet = (existing or {}).get("wallet_name") or payload.wallet_name or "Manually confirmed"
        _check_sensitive(wallet, "wallet_name")
        # REG-1: a new entry takes the chosen key access; an existing entry is
        # only ever marked "no key access" here, never set back (Known Addresses).
        await srv.db.add_known_address(output["address"], wallet_name=wallet,
                                       key_access=payload.key_access is not False)
        if existing and payload.key_access is False:
            await srv.db.set_known_address_key_access(output["address"], False)
        await srv.db.add_address_evidence(output["address"], wallet, "USER_CONFIRMED")
        async with srv.db.db.execute("""SELECT o.wallet_name FROM ownership o JOIN outpoints p
            ON p.txid=o.txid AND p.vout=o.vout WHERE p.address=?
            AND o.ownership_status='EXTERNAL' AND o.classification_source='USER'
            AND NOT (o.txid=? AND o.vout=?)""", (output["address"], txid, vout)) as cursor:
            contradictions = await cursor.fetchall()
        for contradiction in contradictions:
            await srv.db.record_ownership_contradiction(output["address"],
                user_wallet=contradiction["wallet_name"] or "", evidence_wallet=wallet,
                detail="Explicit EXTERNAL output conflicts with USER_CONFIRMED address ownership.")



def _check_manual_classification(payload: ClassificationUpdate) -> None:
    """Only manual decisions are settable through the classify endpoints.

    Discovery/import evidence (e.g. XPUB_DISCOVERY) and custody states
    (CUSTODIAL_DEPOSIT) must come from their own workflows (L-08).
    """
    if payload.ownership_status not in ("OWNED", "EXTERNAL", "UNKNOWN"):
        raise HTTPException(status_code=400, detail="Use the trace custody workflow for custody decisions.")
    if payload.classification_source not in ("USER", "UNKNOWN") or (
            payload.classification_source == "UNKNOWN" and payload.ownership_status != "UNKNOWN"):
        raise HTTPException(status_code=400, detail="A manual classification or reset is required.")
    if payload.remove_address_confirmation and (payload.register_owned_address or (
            payload.ownership_status, payload.classification_source) not in (("EXTERNAL", "USER"), ("UNKNOWN", "UNKNOWN"))):
        raise HTTPException(status_code=400,
                            detail="Removing the address confirmation applies only to EXTERNAL or a reset.")


async def _remove_address_confirmation(txid, vout, payload):
    """FIX-OWN-1: the same operation as deleting the entry in Known Addresses."""
    if not payload.remove_address_confirmation:
        return
    srv = state()
    output = await srv.db.get_outpoint(txid, vout)
    if output and output.get("address") and await srv.db.delete_known_address(output["address"]):
        logger.info("known address confirmation removed for %s:%d before reclassification", txid, vout)


async def _classified(txid, vout, result):
    """Classification result plus the effective ownership, so an override by an
    address confirmation is never silent (FIX-OWN-1)."""
    return dict(result, effective_ownership=await state().engine.outpoint_ownership_state(txid, vout))


@router.post("/api/outpoints/{txid}/{vout}/classify")
async def classify_saved_outpoint(txid: str, vout: int, payload: ClassificationUpdate):
    srv = state()
    txid = _validate_txid(txid)
    if vout < 0 or not await srv.db.get_outpoint(txid, vout):
        raise HTTPException(status_code=404, detail="Cached output not found.")
    existing = await srv.db.get_ownership(txid, vout)
    if existing and existing['ownership_status'] == 'CUSTODIAL_DEPOSIT':
        raise HTTPException(status_code=409, detail="Use the trace custody workflow for custody decisions.")
    _check_manual_classification(payload)
    await _register_owned_output_address(txid, vout, payload)
    await _remove_address_confirmation(txid, vout, payload)
    return await _classified(txid, vout, await srv.engine.classify_output(None, txid, vout, payload.ownership_status,
        classification_source=payload.classification_source, wallet_name=payload.wallet_name,
        confidence=payload.confidence, announce=False))


@router.post("/api/traces/{trace_id}/outpoints/{txid}/{vout}/classify")
async def classify_outpoint(
    trace_id: int, txid: str, vout: int, payload: ClassificationUpdate
) -> dict[str, Any]:
    srv = state()
    txid = _validate_txid(txid)
    trace = await srv.db.get_trace(trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found.")

    if vout < 0:
        raise HTTPException(status_code=400, detail="VOUT must be >= 0.")
    _check_manual_classification(payload)

    # Reverse modes run their walks on the background worker. Classification
    # only writes evidence here; the walk extends when the user presses
    # Re-check / continue (async 202). Never trigger the engine's synchronous
    # forward re-walk for a reverse trace.
    await _register_owned_output_address(txid, vout, payload)
    await _remove_address_confirmation(txid, vout, payload)

    if trace.trace_mode in (
        TraceMode.REVERSE_PROVENANCE,
        TraceMode.BLOCKCHAIN_ANALYSIS_REVERSE,
    ):
        return await _classified(txid, vout, await srv.engine.classify_output(
            trace_id,
            txid,
            vout,
            payload.ownership_status,
            classification_source=payload.classification_source,
            wallet_name=payload.wallet_name,
            confidence=payload.confidence,
            announce=False,
        ))

    return await _classified(txid, vout, await srv.engine.classify_output(
        trace_id,
        txid,
        vout,
        payload.ownership_status,
        classification_source=payload.classification_source,
        wallet_name=payload.wallet_name,
        confidence=payload.confidence,
    ))


@router.post("/api/traces/{trace_id}/continue")
async def continue_trace(trace_id: int) -> dict[str, Any]:
    srv = state()
    trace = await srv.db.get_trace(trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found.")
    if trace.trace_mode in (
        TraceMode.BLOCKCHAIN_ANALYSIS,
        TraceMode.BLOCKCHAIN_ANALYSIS_REVERSE,
        TraceMode.REVERSE_PROVENANCE,
    ):
        if await srv.db.has_active_analysis_job(trace_id):
            raise HTTPException(
                status_code=409,
                detail="An analysis is already running or queued for this trace.",
            )
        job = await srv.db.create_analysis_job(trace_id, operation="CONTINUE")
        try:
            srv.analysis_worker.submit(job_id=job["id"], trace_id=trace_id, operation="CONTINUE")
        except Exception:
            logger.exception("failed to submit re-check job for trace %d", trace_id)
            await srv.db.update_analysis_job_state(
                job["id"], "FAILED", error_summary="Analysis worker is unavailable. Please retry."
            )
            await srv.db.update_trace(trace_id, status="ERROR")
            job = await srv.db.get_analysis_job(job["id"])
        return JSONResponse(
            status_code=202,
            content=AnalysisJobEnqueued(message="Re-check scheduled.", analysis_job=AnalysisJob(**job)).model_dump(),
        )
    walk = await srv.engine.continue_trace(trace_id)
    return {"walk": walk}


@router.post("/api/traces/{trace_id}/follow")
async def follow_output(trace_id: int, payload: AnalysisFollowRequest) -> dict[str, Any]:
    """Deliberately extend a Blockchain Analysis trace by following the
    spending transaction of a spent output (spec section 10).

    Validates the target outpoint synchronously (same RPC checks the walker
    makes), then schedules the re-walk on the background worker: the spend
    walk itself never blocks the request. Returns 409 while another analysis
    is already queued/running for the trace.
    """
    srv = state()
    txid = _validate_txid(payload.txid)
    if payload.vout < 0:
        raise HTTPException(status_code=400, detail="VOUT must be >= 0.")
    trace = await srv.db.get_trace(trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found.")
    if trace.trace_mode not in (
        TraceMode.BLOCKCHAIN_ANALYSIS,
        TraceMode.BLOCKCHAIN_ANALYSIS_REVERSE,
    ):
        if trace.trace_mode == TraceMode.REVERSE_PROVENANCE:
            raise HTTPException(
                status_code=400,
                detail="The follow action is not available for Reverse Provenance "
                       "traces; use Re-check / continue to extend after new "
                       "ownership evidence.",
            )
        raise HTTPException(
            status_code=400,
            detail="The follow action is only available for Blockchain Analysis traces.",
        )
    if await srv.db.has_active_analysis_job(trace_id):
        raise HTTPException(
            status_code=409,
            detail="An analysis is already running or queued for this trace.",
        )

    follow_tx = await srv.engine.load_transaction(txid)
    if not any(o.vout == payload.vout for o in follow_tx.outputs):
        raise HTTPException(
            status_code=400,
            detail="The supplied VOUT does not exist in this transaction.",
        )

    follow_target = (txid, payload.vout)
    if trace.trace_mode == TraceMode.BLOCKCHAIN_ANALYSIS:
        # Forward analysis: the followed outpoint must be spent - there is a
        # spending transaction to walk into.
        spend = await srv.engine.resolve_spend_state(txid, payload.vout)
        if spend.get("spend_status") != "SPENT" or not spend.get("spending_txid"):
            raise HTTPException(
                status_code=400,
                detail="This output is not spent - there is no spending transaction to follow.",
            )
    elif trace.trace_mode == TraceMode.BLOCKCHAIN_ANALYSIS_REVERSE:
        # Reverse analysis: deliberately continue backward from a chosen
        # ancestor. The ancestor may be spent or unspent; it only needs to be
        # a resolvable output whose producing-transaction lineage we extend.
        await srv.db.add_reverse_seed(trace_id, txid, payload.vout)

    job = await srv.db.create_analysis_job(trace_id, operation="FOLLOW")
    try:
        srv.analysis_worker.submit(
            job_id=job["id"],
            trace_id=trace_id,
            operation="FOLLOW",
            follow_target=follow_target,
        )
    except Exception:
        logger.exception("failed to submit follow job for trace %d", trace_id)
        await srv.db.update_analysis_job_state(
            job["id"], "FAILED", error_summary="Analysis worker is unavailable. Please retry."
        )
        await srv.db.update_trace(trace_id, status="ERROR")
        job = await srv.db.get_analysis_job(job["id"])
    return JSONResponse(
        status_code=202,
        content=AnalysisJobEnqueued(message="Follow scheduled.", analysis_job=AnalysisJob(**job)).model_dump(),
    )


@router.get("/api/traces/{trace_id}/analysis-job", response_model=AnalysisJobResponse)
async def get_analysis_job(trace_id: int) -> AnalysisJobResponse:
    """Poll endpoint for the background analysis worker (spec section 9.1).

    Returns the latest job state for a Blockchain Analysis trace so the
    frontend can render QUEUED/RUNNING progress and react to the terminal
    states COMPLETE / LIMIT_REACHED / FAILED. Cheap: only SQLite state +
    configured limits, never a walk.
    """
    srv = state()
    trace = await srv.db.get_trace(trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found.")
    if trace.trace_mode not in (
        TraceMode.BLOCKCHAIN_ANALYSIS,
        TraceMode.BLOCKCHAIN_ANALYSIS_REVERSE,
        TraceMode.REVERSE_PROVENANCE,
    ):
        raise HTTPException(
            status_code=404,
            detail="No analysis job exists for this trace.",
        )
    job = await srv.db.get_latest_analysis_job(trace_id)
    if not job:
        raise HTTPException(
            status_code=404,
            detail="No analysis job exists for this trace.",
        )
    if trace.trace_mode == TraceMode.REVERSE_PROVENANCE:
        limits = AnalysisJobLimits(
            transactions=srv.settings.reverse_provenance_max_transactions,
            outpoints=srv.settings.reverse_provenance_max_outpoints,
            hops=srv.settings.reverse_provenance_max_hops,
        )
    else:
        limits = AnalysisJobLimits(
            transactions=srv.settings.analysis_max_transactions,
            outpoints=srv.settings.analysis_max_outpoints,
            hops=srv.settings.analysis_max_hops,
        )
    return AnalysisJobResponse(
        trace_id=trace_id,
        job_id=job["id"],
        state=job["state"],
        operation=job["operation"],
        error_summary=job["error_summary"],
        progress=AnalysisJobProgress(
            transactions=job["progress_tx"],
            outpoints=job["progress_outpoints"],
            hop=job["progress_hop"],
        ),
        limits=limits,
    )


@router.get("/api/traces/{trace_id}/summary", response_model=TraceSummary)
async def trace_summary(trace_id: int) -> TraceSummary:
    srv = state()
    trace = await srv.db.get_trace(trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found.")
    full = await _build_full_trace(trace, srv.engine)
    if not full.summary:
        raise HTTPException(status_code=404, detail="No summary available.")
    return full.summary


@router.get("/api/traces/{trace_id}/export")
async def export_trace(trace_id: int, response: Response = None) -> TraceExport:
    srv = state()
    if response is not None:
        response.headers['Cache-Control'] = 'no-store'
    trace = await srv.db.get_trace(trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found.")
    full = await _build_full_trace(trace, srv.engine)
    return TraceExport(
        schema_version="0.3",
        trace=full.trace,
        transactions=full.transactions,
        outpoints=full.outpoints,
        edges=full.edges,
        annotations=full.annotations,
        summary=full.summary,
        forward_stop_reason=full.forward_stop_reason,
        forward_stopped_outpoints=full.forward_stopped_outpoints,
        forward_max_depth=full.forward_max_depth,
        custodial_boundaries=full.custodial_boundaries,
        custody_bridges=full.custody_bridges,
        dismissed_candidates=full.dismissed_candidates,
        ownership_custody_conflicts=full.ownership_custody_conflicts,
    )


@router.get("/api/traces/{trace_id}/export.csv")
async def export_trace_csv(trace_id: int) -> Response:
    data = await export_trace(trace_id)
    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow(["type", "txid", "vout", "address", "amount_sats", "spend_status",
                     "ownership", "source_txid", "destination_txid", "destination_vout",
                     "hop", "relationship", "block_height", "block_time",
                     "ownership_source", "provenance_state", "transaction_topology",
                     "likely_batch", "provenance_reasons", "fee_sats", "fee_rate"])
    for t in data.transactions:
        writer.writerow(["transaction", t.txid, "", "", "", "", "", "", "", "", "",
                         "", t.block_height, t.block_time, "", "",
                         t.transaction_topology or "UNKNOWN", 1 if t.likely_batch else 0, "",
                         t.fee_sats if t.fee_sats is not None else "",
                         t.fee_rate if t.fee_rate is not None else ""])
    for o in data.outpoints:
        writer.writerow(["outpoint", o.txid, o.vout, safe_text(o.address), o.value_sats,
                         o.spend_status, safe_text(o.ownership_status), "", "", "", "", "",
                         o.block_height, o.block_time, safe_text(o.classification_source),
                         o.provenance_state or "UNKNOWN", o.transaction_topology or "UNKNOWN",
                         1 if o.likely_batch else 0, safe_text("; ".join(o.provenance_reasons or [])),
                         "", ""])
    for e in data.edges:
        # Custody bridges are logical links, NEVER ordinary spend edges.
        # They are written as their own row type so a consumer can never
        # confuse them with exact on-chain lineage.
        if e.relationship_type == "CUSTODY_BRIDGE":
            writer.writerow([
                "custody_bridge", e.destination_txid, e.destination_vout, "", "",
                "", e.provenance, e.source_txid + ":" + str(e.source_vout),
                e.destination_txid, e.destination_vout, e.hop_number,
                "CUSTODY_BRIDGE", "", "", "", "", "", 0, "", "", "",
            ])
            continue
        writer.writerow(["edge", e.spending_txid, "", "", "", "", "", e.source_txid,
                         e.destination_txid, e.destination_vout, e.hop_number,
                         e.relationship_type, "", "", "", "", "", 0, "", "", ""])
    for b in data.custodial_boundaries:
        writer.writerow([
            "custodial_boundary", b.txid, b.vout, "", "", "", "CUSTODIAL_DEPOSIT",
            "", "", "", "", "EXACT_LINEAGE_ENDS", "", "", "USER", "", "", 0, "",
            "", "",
        ])
    for d in data.dismissed_candidates:
        writer.writerow([
            "custodial_dismissal", d.txid, d.vout, "", "", "", d.confidence,
            safe_text("; ".join(d.reasons)), d.txid, "", "", "DECLINED_CANDIDATE", "", "",
            "", "", "", 0, "", "", "",
        ])

    content = buf.getvalue()
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=trace-{trace_id}.csv"},
    )


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------


@router.post("/api/annotations", response_model=Annotation, status_code=201)
async def add_annotation(payload: dict[str, Any]) -> Annotation:
    srv = state()
    entity_type = str(payload.get("entity_type", "")).strip()
    entity_id = str(payload.get("entity_id", "")).strip()
    tag = str(payload.get("tag", "UNKNOWN")).strip().upper()
    note = payload.get("note")

    if entity_type not in ("transaction", "outpoint", "trace"):
        raise HTTPException(status_code=400, detail="Invalid entity_type.")
    if not entity_id:
        raise HTTPException(status_code=400, detail="entity_id is required.")
    _check_sensitive(entity_id)
    _check_sensitive(note, "note")

    return await srv.db.add_annotation(entity_type, entity_id, tag, note)


@router.get("/api/annotations")
async def list_annotations() -> list[Annotation]:
    srv = state()
    return await srv.db.list_all_annotations()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _enrich_input_context(
    trace: Trace,
    transactions: list[TransactionInfo],
    edges: list,
    data_db: Database,
    trace_engine: TraceEngine,
) -> None:
    """Attach per-input merge/consolidation context to trace transactions.

    Merge / consolidation input context. For
    every non-coinbase input of every traced transaction, expose the exact
    input UTXO (``prev_txid:vout`` is already on ``InputInfo``), the ownership
    state of the referenced previous output, and a display-only role:

    - LINEAGE_INPUT      the tracked outpoint through which the current trace
                         reached this spending transaction (exactly the edge
                         ``source_txid:source_vout`` with ``spending_txid``
                         equal to this transaction).
    - KNOWN_TRACE_INPUT  a co-input whose outpoint is present in ANOTHER
                         stored trace (linkable via ``related_trace_ids``).
    - CO_INPUT           everything else: transaction context only.

    This is purely transactional context. ``edges`` (and therefore the trace's
    provenance/depth/ownership semantics) are never touched: a co-input is
    shown but can never become an ancestor of this Acquisition trace, and no
    sat-level attribution (FIFO/LIFO/proportional) is implied.
    """
    # Outpoints the CURRENT trace tracks INTO each spending transaction.
    lineage_by_tx: dict[str, set[str]] = {}
    for e in edges:
        if not e.spending_txid:
            continue
        lineage_by_tx.setdefault(e.spending_txid, set()).add(
            f"{e.source_txid}:{e.source_vout}"
        )

    # Every stored trace's outpoints -> unrelated-trace link targets.
    trace_index = await data_db.get_outpoint_trace_index()
    current_id = trace.id
    known_others: dict[str, list[int]] = {}
    for key, ids in trace_index.items():
        others = [tid for tid in ids if tid != current_id]
        if others:
            known_others[key] = others

    for tx in transactions:
        if not tx.inputs:
            continue
        enriched: list[InputInfo] = []
        for i in tx.inputs:
            if not i.txid or i.prev_outpoint == "coinbase":
                enriched.append(i)
                continue
            key = f"{i.txid}:{i.vout}"
            ctx = InputInfo(
                txid=i.txid,
                vout=i.vout,
                prev_outpoint=i.prev_outpoint,
                sequence=i.sequence,
                value=i.value,
                value_sats=i.value_sats,
                script_pubkey=i.script_pubkey,
                script_type=i.script_type,
                address=i.address,
                spending_txid=i.spending_txid,
                our_trace_spend=i.our_trace_spend,
            )
            if key in lineage_by_tx.get(tx.txid, ()):
                ctx.trace_role = TraceInputRole.LINEAGE_INPUT
            elif key in known_others:
                ctx.trace_role = TraceInputRole.KNOWN_TRACE_INPUT
            else:
                ctx.trace_role = TraceInputRole.CO_INPUT
            ctx.related_trace_ids = sorted(set(trace_index.get(key, [])))
            own = await data_db.get_ownership(i.txid, i.vout)
            cached = await data_db.get_outpoint(i.txid, i.vout)
            # Without a cached outpoint fall back to the input's own address
            # (owner decision 2026-10-11): ``InputInfo.address`` is set only
            # by ``TraceEngine._populate_io_from_raw`` from the previous
            # output's scriptPubKey as decoded by the node (``getrawtransaction``
            # verbose of the previous tx, from the raw_json cache or RPC);
            # coinbase / unfetchable previous outputs carry ``None``.
            address = (cached.get("address") if cached else None) or i.address
            if address:
                # R2-01: effective (read-time) ownership, same derivation as
                # every other read path; read-only, no ownership or
                # contradiction writes (OWNED from registry evidence is not
                # persisted since P2-8).
                ref = OutputInfo(
                    txid=i.txid, vout=i.vout, outpoint=key,
                    address=address,
                )
                await trace_engine.effective_ownership(ref, own, data=data_db)
                ctx.ownership_status = ref.ownership_status
                ctx.classification_source = ref.classification_source
                ctx.wallet_name = ref.wallet_name
            elif own:
                ctx.ownership_status = (
                    own.get("ownership_status") or OwnershipStatus.UNKNOWN
                )
                ctx.classification_source = (
                    own.get("classification_source") or ClassificationSource.UNKNOWN
                )
                ctx.wallet_name = own.get("wallet_name")
            enriched.append(ctx)
        tx.inputs = enriched


async def _build_full_trace(
    trace: Trace, trace_engine: TraceEngine | None = None, *,
    _allow_forward_reconcile: bool = True,
) -> FullTrace:
    srv = state()
    se = trace_engine or srv.engine
    data_db = se.db if trace_engine is not None else srv.db
    edges = await data_db.get_edges(trace.id)
    boundaries = await data_db.get_custodial_boundaries(trace.id)
    bridges = await data_db.get_custody_bridges(trace.id)
    is_forward = trace.trace_mode == TraceMode.FORWARD_PROVENANCE
    forward_seeds = set(await se._collect_owned_start_points(trace)) if is_forward else set()
    refresh_keys = {
        key for e in edges for key in (
            (e.source_txid, e.source_vout), (e.destination_txid, e.destination_vout)
        )
    } | forward_seeds

    txids: list[str] = [trace.start_txid]
    for e in edges:
        txids.append(e.spending_txid)
        txids.append(e.source_txid)
        txids.append(e.destination_txid)
    for b in bridges:
        txids.append(b["destination_txid"])

    # Reading an ownership-neutral analysis trace never persists ownership (R-02).
    persist = trace.trace_mode not in (TraceMode.BLOCKCHAIN_ANALYSIS, TraceMode.BLOCKCHAIN_ANALYSIS_REVERSE)
    transactions: list[TransactionInfo] = []
    outpoints = []
    seen_tx: set[str] = set()
    for txid in txids:
        if txid in seen_tx:
            continue
        seen_tx.add(txid)
        try:
            tx = await se.load_transaction(txid, persist_ownership=persist)
            # P4-N1 (option B): the outpoints cache may hold provenance an
            # ownership-aware walker computed; analysis traces show the
            # ownership-neutral reading, computed here and never written.
            # The engine shares ``tx`` via its memory cache, so the neutral
            # values go into a copy owned by this response.
            if not persist:
                tx = tx.model_copy(deep=True)
            transactions.append(tx)
            neutral_top = None if persist else tx_topology(tx)
            for o in tx.outputs:
                if not persist:
                    o.provenance_state, o.provenance_reasons = provenance_for_output(
                        tx, o.vout, set()
                    )
                    o.transaction_topology = neutral_top
                # refresh spend status for outpoints interesting to us
                if (o.txid, o.vout) in refresh_keys:
                    try:
                        sp = await se.resolve_spend_state(o.txid, o.vout)
                        o.spend_status = sp.get("spend_status")
                        o.spending_txid = sp.get("spending_txid")
                    except RpcError:
                        pass
                outpoints.append(o)
        except RpcError as exc:
            logger.warning("failed to load tx %s for trace %d: %s", txid, trace.id, exc)

    missing, depth_limited = [], []
    forward_state = {}
    if is_forward:
        forward_state = se.materialized_forward_state(
            edges, outpoints, boundaries, forward_seeds,
        )
        missing = forward_state["missing_continuation"]
        depth_limited = forward_state["depth_limited_outpoints"]
        if (missing and _allow_forward_reconcile
                and trace.status != TraceStatus.FORWARD_LIMIT_REACHED):
            await se.continue_trace(trace.id)
            refreshed = await data_db.get_trace(trace.id)
            return await _build_full_trace(
                refreshed, se, _allow_forward_reconcile=False,
            )

    dismissals = await data_db.get_custodial_dismissals(trace.id)
    ownership_custody_conflicts = await data_db.list_ownership_custody_conflicts(trace.id)
    annotations = await data_db.list_all_annotations()

    await _enrich_input_context(trace, transactions, edges, data_db, se)
    from backend.reporting.origin_resolution import OriginResolutions
    (await OriginResolutions.load(data_db)).enrich(outpoints)

    pending = bool(forward_state.get("pending_unknown"))
    has_blocking_conflicts = any(r.get("status") == "PENDING" for r in ownership_custody_conflicts)

    # ONE canonical effective trace state. The persisted ``trace.status`` alone
    # can lag current ownership. Re-derive classification from the reachable
    # owned frontier, never all displayed UNKNOWN outputs or a sticky historical
    # NEEDS_CLASSIFICATION flag. Persist so list/detail/export agree.
    #
    # Blockchain Analysis has its
    # own status semantics: COMPLETE / ANALYSIS_LIMIT_REACHED / ERROR are
    # decided and persisted by the analysis walker. Its graph is deliberately
    # ownership-agnostic, so the forward ownership-frontier
    # reconciliation must NOT apply (following never requires classification).
    # The reverse modes work identically: REVERSE_PROVENANCE decides COMPLETE /
    # REVERSE_LIMIT_REACHED itself (UNKNOWN predecessors are optionally
    # classifiable context and never force NEEDS_CLASSIFICATION) and
    # BLOCKCHAIN_ANALYSIS_REVERSE decides COMPLETE / ANALYSIS_LIMIT_REACHED;
    # neither must be force-reconciled by the forward logic (their graphs
    # legitimately contain many UNKNOWN contextual outpoints).
    is_analysis = trace.trace_mode in (
        TraceMode.BLOCKCHAIN_ANALYSIS,
        TraceMode.BLOCKCHAIN_ANALYSIS_REVERSE,
        TraceMode.REVERSE_PROVENANCE,
    )
    if is_analysis:
        effective_status = trace.status
        if (
            trace.trace_mode == TraceMode.REVERSE_PROVENANCE
            and trace.status == TraceStatus.NEEDS_CLASSIFICATION
        ):
            # v0.6.0 semantic fix: UNKNOWN context is non-blocking, so a
            # persisted NEEDS_CLASSIFICATION on a reverse trace is stale state
            # from an earlier release (the walker never emits it anymore).
            # Normalize to COMPLETE so the banner/classification-required
            # messaging can never stick to a resolved reverse trace.
            effective_status = TraceStatus.COMPLETE
    elif depth_limited or trace.status == TraceStatus.FORWARD_LIMIT_REACHED:
        effective_status = TraceStatus.FORWARD_LIMIT_REACHED
    elif missing:
        effective_status = TraceStatus.ERROR
    elif trace.status == TraceStatus.NEEDS_REVIEW or has_blocking_conflicts:
        effective_status = TraceStatus.NEEDS_REVIEW
    elif pending:
        effective_status = TraceStatus.NEEDS_CLASSIFICATION
    elif (forward_state.get("ended_at_custody_boundary")
          or trace.status == TraceStatus.COMPLETE_AT_CUSTODY_BOUNDARY):
        effective_status = TraceStatus.COMPLETE_AT_CUSTODY_BOUNDARY
    else:
        effective_status = TraceStatus.COMPLETE

    if effective_status != trace.status:
        updated = await data_db.update_trace(trace.id, status=effective_status)
        if updated:
            trace = updated

    summary = await _compute_summary(trace, edges, transactions)
    needs_classification = effective_status == TraceStatus.NEEDS_CLASSIFICATION
    needs_review = effective_status == TraceStatus.NEEDS_REVIEW

    # Custody bridges are logical/economic links, never on-chain spend edges.
    # They are surfaced in the same edge list for the graph/UI/export but
    # carry relationship_type=CUSTODY_BRIDGE + a non-EXACT provenance so they
    # can never be mistaken for exact Bitcoin lineage.
    if bridges:
        max_hop = max((e.hop_number for e in edges), default=-1)
        for b in bridges:
            edges.append(TraceEdge(
                trace_id=trace.id,
                source_txid=b.get("boundary_txid") or trace.start_txid,
                source_vout=b.get("boundary_vout") or 0,
                spending_txid="",
                destination_txid=b["destination_txid"],
                destination_vout=b["destination_vout"],
                hop_number=max_hop + 1,
                relationship_type=RelationshipType.CUSTODY_BRIDGE,
                provenance=b.get("provenance") or Provenance.USER_CONFIRMED,
            ))

    full = FullTrace(
        trace=trace,
        transactions=transactions,
        outpoints=outpoints,
        edges=edges,
        annotations=annotations,
        summary=summary,
        needs_classification=needs_classification,
        needs_review=needs_review,
        forward_stop_reason=(
            "MAX_TRACE_DEPTH" if effective_status == TraceStatus.FORWARD_LIMIT_REACHED
            else "CONTINUATION_UNAVAILABLE" if missing else None
        ),
        forward_stopped_outpoints=depth_limited or missing,
        forward_max_depth=se.max_depth if is_forward else None,
        custodial_boundaries=[
            CustodialBoundary(
                id=int(b["id"]),
                trace_id=int(b["trace_id"]),
                txid=b["txid"],
                vout=int(b["vout"]),
                service_name=b.get("service_name") or "",
                note=b.get("note"),
                created_at=str(b.get("created_at") or ""),
            )
            for b in boundaries
        ],
        custody_bridges=[
            CustodyBridge(
                id=int(b["id"]),
                trace_id=int(b["trace_id"]),
                destination_txid=b["destination_txid"],
                destination_vout=int(b["destination_vout"]),
                provenance=b.get("provenance") or Provenance.USER_CONFIRMED,
                confidence_score=b.get("confidence_score"),
                reasons=list(b.get("reasons") or []),
                service_name=b.get("service_name") or "",
                note=b.get("note"),
                created_at=str(b.get("created_at") or ""),
                boundary_id=b.get("boundary_id"),
                boundary_txid=b.get("boundary_txid"),
                boundary_vout=b.get("boundary_vout"),
                sources=[
                    CustodyBridgeSource(
                        boundary_id=s.get("boundary_id"),
                        txid=s.get("txid"),
                        vout=s.get("vout"),
                    )
                    for s in (b.get("sources") or [])
                ],
            )
            for b in bridges
        ],
        dismissed_candidates=[
            CustodialDismissal(
                id=int(d["id"]),
                trace_id=int(d["trace_id"]),
                txid=d["txid"],
                vout=int(d["vout"]),
                reasons=list(d.get("reasons") or []),
                confidence=d.get("confidence") or "likely",
                service_name=d.get("service_name") or "",
                note=d.get("note"),
                created_at=str(d.get("created_at") or ""),
            )
            for d in dismissals
        ],
        ownership_custody_conflicts=[
            _ownership_custody_conflict_model(r) for r in ownership_custody_conflicts
        ],
    )
    # Blockchain Analysis never runs heuristic custody/boundary detection on
    # the graph: the analysis is ownership-neutral and must not suggest
    # custodial deposits or escalate outpoints (spec sections 9, 12).
    if not is_analysis:
        full.possible_boundaries = custody_detect.detect_possible_boundaries(
            full,
            dismissed={(d["txid"], int(d["vout"])) for d in dismissals},
        )
    return full


async def _compute_summary(
    trace: Trace,
    edges: list,
    transactions: list[TransactionInfo],
) -> TraceSummary:
    start_amount = None
    if trace.start_vout is not None:
        for t in transactions:
            if t.txid == trace.start_txid:
                for o in t.outputs:
                    if o.vout == trace.start_vout:
                        start_amount = o.value_sats
                        break

    txids = {trace.start_txid}
    for e in edges:
        txids.add(e.spending_txid)
        txids.add(e.source_txid)
        txids.add(e.destination_txid)

    outpoint_by_key: dict[str, Any] = {}
    for t in transactions:
        for o in t.outputs:
            outpoint_by_key[f"{o.txid}:{o.vout}"] = o

# Reverse modes: summary counts
    # describe the discovered ancestry graph. Each edge source is an ancestor
    # outpoint; ownership buckets are informational (never traversal gates).
    if trace.trace_mode in (
        TraceMode.REVERSE_PROVENANCE,
        TraceMode.BLOCKCHAIN_ANALYSIS_REVERSE,
    ):
        graph_keys = set()
        ancestor_keys: list[str] = []
        seen_ancestors: set[str] = set()
        for e in edges:
            graph_keys.add(f"{e.source_txid}:{e.source_vout}")
            graph_keys.add(f"{e.destination_txid}:{e.destination_vout}")
            src = f"{e.source_txid}:{e.source_vout}"
            if src not in seen_ancestors:
                seen_ancestors.add(src)
                ancestor_keys.append(src)
        if trace.start_vout is not None:
            graph_keys.add(f"{trace.start_txid}:{trace.start_vout}")

        current_utxos: set[str] = set()
        unknown_outputs: set[str] = set()
        for key in graph_keys:
            o = outpoint_by_key.get(key)
            if not o:
                continue
            if o.spend_status == "UNKNOWN":
                unknown_outputs.add(key)
            elif o.spend_status == "UNSPENT":
                current_utxos.add(key)

        owned_ancestors = unknown_predecessors = external_predecessors = 0
        oldest_height: Optional[int] = None
        oldest_time: Optional[int] = None
        for t in transactions:
            h = t.block_height
            ts = t.block_time
            if h is not None and (oldest_height is None or h < oldest_height):
                oldest_height = h
            if ts is not None and (oldest_time is None or ts < oldest_time):
                oldest_time = ts
        for key in ancestor_keys:
            o = outpoint_by_key.get(key)
            if not o:
                continue
            status = o.ownership_status
            if status == OwnershipStatus.OWNED:
                owned_ancestors += 1
            elif status == OwnershipStatus.UNKNOWN:
                unknown_predecessors += 1
            elif status == OwnershipStatus.EXTERNAL:
                external_predecessors += 1

        merges = sum(1 for e in edges if e.relationship_type in ("MERGED", "MIXED"))
        splits = sum(1 for e in edges if e.relationship_type == "SPLIT")
        target_outpoint = (
            f"{trace.start_txid}:{trace.start_vout}"
            if trace.start_vout is not None
            else None
        )
        # Followed = transactions whose inputs the walk resolved (the target
        # and every edge destination), matching the job counter. Source-only
        # transactions - a coinbase origin or a Reverse Provenance context
        # input - are discovered, not followed (FIX-UI-3).
        followed_txids = {trace.start_txid} | {e.destination_txid for e in edges}
        return TraceSummary(
            trace_id=trace.id,
            status=trace.status,
            start_txid=trace.start_txid,
            start_amount_sats=start_amount,
            transactions_followed=len(followed_txids),
            tracked_branches=len(ancestor_keys),
            current_utxos=len(current_utxos),
            unknown_outputs=len(unknown_outputs),
            merges=merges,
            splits=splits,
            target_outpoint=target_outpoint,
            owned_ancestors=owned_ancestors,
            unknown_predecessors=unknown_predecessors,
            external_predecessors=external_predecessors,
            oldest_block_height=oldest_height,
            oldest_block_time=oldest_time,
        )

    # Blockchain Analysis mode:
    # summary counts describe the analysed graph, not the user's wallet.
    # Ownership never gates anything here - "current UTXOs" are the spent /
    # unspent outpoints the walk actually visited, "unknown outputs" are graph
    # outpoints whose spend state could not be resolved.
    if trace.trace_mode == TraceMode.BLOCKCHAIN_ANALYSIS:
        graph_keys = set()
        for e in edges:
            graph_keys.add(f"{e.source_txid}:{e.source_vout}")
            graph_keys.add(f"{e.destination_txid}:{e.destination_vout}")
        if trace.start_vout is not None:
            graph_keys.add(f"{trace.start_txid}:{trace.start_vout}")
        elif trace.start_address:
            for t in transactions:
                if t.txid == trace.start_txid:
                    for o in t.outputs:
                        if o.address == trace.start_address:
                            graph_keys.add(f"{o.txid}:{o.vout}")

        current_utxos: set[str] = set()
        unknown_outputs: set[str] = set()
        for key in graph_keys:
            o = outpoint_by_key.get(key)
            if not o:
                continue
            if o.spend_status == "UNKNOWN":
                unknown_outputs.add(key)
            elif o.spend_status == "UNSPENT":
                current_utxos.add(key)

        merges = sum(1 for e in edges if e.relationship_type in ("MERGED", "MIXED"))
        splits = sum(1 for e in edges if e.relationship_type == "SPLIT")
        branches = {f"{e.source_txid}:{e.source_vout}" for e in edges}
        return TraceSummary(
            trace_id=trace.id,
            status=trace.status,
            start_txid=trace.start_txid,
            start_amount_sats=start_amount,
            transactions_followed=len(txids),
            tracked_branches=len(branches),
            current_utxos=len(current_utxos),
            unknown_outputs=len(unknown_outputs),
            merges=merges,
            splits=splits,
        )

    owned_keys = set()
    for e in edges:
        owned_keys.add(f"{e.source_txid}:{e.source_vout}")
        owned_keys.add(f"{e.destination_txid}:{e.destination_vout}")

    current_utxos: set[str] = set()
    unknown_outputs: set[str] = set()
    for key in owned_keys:
        o = outpoint_by_key.get(key)
        if not o:
            continue
        if o.spend_status == "UNKNOWN":
            if o.ownership_status == "OWNED":
                current_utxos.add(key)
        elif o.spend_status == "UNSPENT" and o.ownership_status == "OWNED":
            current_utxos.add(key)
        elif o.spend_status == "SPENT" and o.ownership_status == "UNKNOWN":
            unknown_outputs.add(key)

    merges = sum(1 for e in edges if e.relationship_type in ("MERGED", "MIXED"))
    splits = sum(1 for e in edges if e.relationship_type == "SPLIT")

    branches = {f"{e.source_txid}:{e.source_vout}" for e in edges}
    branches.update(current_utxos)

    return TraceSummary(
        trace_id=trace.id,
        status=trace.status,
        start_txid=trace.start_txid,
        start_amount_sats=start_amount,
        transactions_followed=len(txids),
        tracked_branches=len(branches),
        current_utxos=len(current_utxos),
        unknown_outputs=len(unknown_outputs),
        merges=merges,
        splits=splits,
    )


# ---------------------------------------------------------------------------
# Custodial boundaries & custody bridges
# ---------------------------------------------------------------------------


async def _require_outpoint(txid: str, vout: int) -> TransactionInfo:
    srv = state()
    txid = _validate_txid(txid)
    if vout < 0:
        raise HTTPException(status_code=400, detail="VOUT must be >= 0.")
    tx = await srv.engine.load_transaction(txid)
    if not any(o.vout == vout for o in tx.outputs):
        raise HTTPException(status_code=400, detail="VOUT does not exist in this transaction.")
    return tx


@router.get("/api/traces/{trace_id}/custodial-boundaries", response_model=list[CustodialBoundary])
async def list_custodial_boundaries(trace_id: int) -> list[CustodialBoundary]:
    srv = state()
    await _get_trace_or_404(trace_id)
    rows = await srv.db.get_custodial_boundaries(trace_id)
    return [
        CustodialBoundary(
            id=int(r["id"]), trace_id=int(r["trace_id"]), txid=r["txid"],
            vout=int(r["vout"]), service_name=r.get("service_name") or "",
            note=r.get("note"), created_at=str(r.get("created_at") or ""),
        )
        for r in rows
    ]


@router.post("/api/traces/{trace_id}/custodial-boundaries", response_model=dict, status_code=201)
async def create_custodial_boundary(
    trace_id: int, payload: CustodialBoundaryCreate
) -> dict[str, Any]:
    """Mark an outpoint as CUSTODIAL_DEPOSIT. Exact lineage ends here."""
    srv = state()
    await _get_trace_or_404(trace_id)
    txid = _validate_txid(payload.txid)
    tx = await _require_outpoint(txid, payload.vout)
    _check_sensitive(payload.service_name, "service_name")
    _check_sensitive(payload.note, "note")

    await srv.engine.classify_output(
        trace_id,
        txid,
        payload.vout,
        "CUSTODIAL_DEPOSIT",
        classification_source="USER",
        announce=False,
    )
    boundary = await srv.db.create_custodial_boundary(
        trace_id,
        txid,
        payload.vout,
        service_name=payload.service_name or "",
        note=payload.note,
    )
    # Ownership that only existed because exact lineage was propagated through
    # this boundary must be re-evaluated before the graph is rebuilt.
    reset = await srv.engine.repair_downstream_ownership(
        trace_id, txid, payload.vout
    )
    walk = await srv.engine.continue_trace(trace_id)
    return {
        "boundary": {
            "id": boundary["id"], "trace_id": boundary["trace_id"],
            "txid": boundary["txid"], "vout": boundary["vout"],
            "service_name": boundary.get("service_name") or "",
            "note": boundary.get("note"),
            "created_at": str(boundary.get("created_at") or ""),
        },
        "reset_outpoints": reset,
        "walk": walk,
    }


@router.delete("/api/traces/{trace_id}/custodial-boundaries/{boundary_id}", status_code=204)
async def delete_custodial_boundary(trace_id: int, boundary_id: int) -> Response:
    srv = state()
    await _get_trace_or_404(trace_id)
    boundary = await srv.db.get_custodial_boundary_by_id(boundary_id)
    if not boundary or int(boundary["trace_id"]) != trace_id:
        raise HTTPException(status_code=404, detail="Boundary not found.")
    await srv.engine.remove_custodial_classification(
        trace_id, boundary["txid"], int(boundary["vout"])
    )
    return Response(status_code=204)


@router.delete(
    "/api/traces/{trace_id}/outpoints/{txid}/{vout}/custody", status_code=204
)
async def delete_outpoint_custody_classification(
    trace_id: int, txid: str, vout: int
) -> Response:
    """Remove the explicit CUSTODIAL_DEPOSIT classification of an outpoint.

    This is the "Remove custodial boundary" action regardless of whether a
    confirmed boundary record exists: the boundary (if any) is deleted, the
    custody classification is dropped, and the trace is re-evaluated from the
    remaining evidence (recomputed ownership, provenance, downstream tracing
    and completion status).
    """
    srv = state()
    await _get_trace_or_404(trace_id)
    txid = _validate_txid(txid)
    await _require_outpoint(txid, vout)
    await srv.engine.remove_custodial_classification(trace_id, txid, vout)
    return Response(status_code=204)


@router.get(
    "/api/traces/{trace_id}/custodial-boundaries/{boundary_id}/candidates",
    response_model=list[CustodyCandidate],
)
async def get_custody_candidates(
    trace_id: int, boundary_id: int
) -> list[CustodyCandidate]:
    """Ranked continuation candidates for a boundary (provenance INFERRED)."""
    srv = state()
    await _get_trace_or_404(trace_id)
    boundary = await srv.db.get_custodial_boundary_by_id(boundary_id)
    if not boundary or int(boundary["trace_id"]) != trace_id:
        raise HTTPException(status_code=404, detail="Boundary not found.")
    return await custody_candidates.find_candidates(
        srv.db, srv.rpc_client, trace_id, boundary
    )


@router.post(
    "/api/traces/{trace_id}/custodial-suggestions/{txid}/{vout}/decline",
    response_model=CustodialDismissal,
    status_code=201,
)
async def decline_custodial_suggestion(
    trace_id: int,
    txid: str,
    vout: int,
    payload: CustodialDismissalCreate,
) -> CustodialDismissal:
    """Record a user's "decline" for a heuristic suggestion.

    The persisted decision keeps the same candidate from being suggested
    again on every re-check. It is never stored in the boundary or bridge
    tables and never touches on-chain trace data, so it can be reset later.
    """
    srv = state()
    await _get_trace_or_404(trace_id)
    txid = _validate_txid(txid)
    tx = await _require_outpoint(txid, vout)
    _check_sensitive(payload.note, "note")
    if payload.confidence not in ("strong", "likely"):
        raise HTTPException(status_code=400, detail="Confidence must be 'strong' or 'likely'.")

    row = await srv.db.create_custodial_dismissal(
        trace_id,
        txid,
        vout,
        reasons=payload.reasons,
        confidence=payload.confidence,
        service_name=payload.service_name,
        note=payload.note,
    )
    return CustodialDismissal(
        id=int(row["id"]),
        trace_id=int(row["trace_id"]),
        txid=row["txid"],
        vout=int(row["vout"]),
        reasons=list(row.get("reasons") or []),
        confidence=row.get("confidence") or "likely",
        service_name=row.get("service_name") or "",
        note=row.get("note"),
        created_at=str(row.get("created_at") or ""),
    )


@router.delete(
    "/api/traces/{trace_id}/custodial-suggestions/{txid}/{vout}/decline",
    status_code=204,
)
async def reset_custodial_suggestion(trace_id: int, txid: str, vout: int) -> Response:
    """Reset a previous decline so the suggestion can be offered again."""
    srv = state()
    await _get_trace_or_404(trace_id)
    txid = _validate_txid(txid)
    tx = await _require_outpoint(txid, vout)
    await srv.db.delete_custodial_dismissal(trace_id, txid, vout)
    await srv.engine.continue_trace(trace_id)
    return Response(status_code=204)


@router.get("/api/traces/{trace_id}/custody-bridges", response_model=list[CustodyBridge])
async def list_custody_bridges(trace_id: int) -> list[CustodyBridge]:
    srv = state()
    await _get_trace_or_404(trace_id)
    rows = await srv.db.get_custody_bridges(trace_id)
    return [
        CustodyBridge(
            id=int(r["id"]), trace_id=int(r["trace_id"]),
            destination_txid=r["destination_txid"],
            destination_vout=int(r["destination_vout"]),
            provenance=r.get("provenance") or Provenance.USER_CONFIRMED,
            confidence_score=r.get("confidence_score"),
            reasons=list(r.get("reasons") or []),
            service_name=r.get("service_name") or "",
            note=r.get("note"),
            created_at=str(r.get("created_at") or ""),
            boundary_id=r.get("boundary_id"),
            boundary_txid=r.get("boundary_txid"),
            boundary_vout=r.get("boundary_vout"),
            sources=[
                CustodyBridgeSource(
                    boundary_id=s.get("boundary_id"), txid=s.get("txid"), vout=s.get("vout"),
                )
                for s in (r.get("sources") or [])
            ],
        )
        for r in rows
    ]


@router.post("/api/traces/{trace_id}/custody-bridges", response_model=dict, status_code=201)
async def confirm_custody_bridge(
    trace_id: int, payload: CustodyBridgeConfirm
) -> dict[str, Any]:
    """User explicitly confirms a logical continuation (never exact lineage)."""
    srv = state()
    await _get_trace_or_404(trace_id)
    boundary = await srv.db.get_custodial_boundary_by_id(payload.boundary_id)
    if not boundary or int(boundary["trace_id"]) != trace_id:
        raise HTTPException(status_code=404, detail="Boundary not found.")
    destination_txid = _validate_txid(payload.destination_txid)
    tx = await _require_outpoint(destination_txid, payload.destination_vout)

    dest = next((o for o in tx.outputs if o.vout == payload.destination_vout), None)
    if not dest:
        raise HTTPException(status_code=400, detail="Destination output not found.")
    if dest.address and not await srv.db.is_known_address(dest.address):
        raise HTTPException(
            status_code=400,
            detail="Destination is not a known-owned address (add it to the registry first).",
        )
    if dest.address is None:
        raise HTTPException(
            status_code=400,
            detail="Destination output has no address (op_return / custom script).",
        )

    bridge = await srv.db.create_custody_bridge(
        trace_id=trace_id,
        boundary_ids=[int(boundary["id"])],
        destination_txid=destination_txid,
        destination_vout=payload.destination_vout,
        provenance=Provenance.USER_CONFIRMED,
        confidence_score=payload.confidence,
        reasons=[],
        service_name=boundary.get("service_name") or "",
        note=boundary.get("note"),
    )

    # The user confirmed this withdrawal output belongs to them: persist OWNED
    # so the walk resumes exact tracing from it.
    await srv.engine.classify_output(
        trace_id,
        destination_txid,
        payload.destination_vout,
        "OWNED",
        classification_source="USER",
        announce=False,
    )
    walk = await srv.engine.continue_trace(trace_id)
    return {"bridge": bridge, "walk": walk}


@router.delete("/api/traces/{trace_id}/custody-bridges/{bridge_id}", status_code=204)
async def delete_custody_bridge(trace_id: int, bridge_id: int) -> Response:
    """Remove a bridge. The Bitcoin trace data stays intact."""
    srv = state()
    await _get_trace_or_404(trace_id)
    removed = await srv.db.delete_custody_bridge(bridge_id)
    if not removed:
        raise HTTPException(status_code=404, detail="Custody bridge not found.")
    await srv.engine.continue_trace(trace_id)
    return Response(status_code=204)


