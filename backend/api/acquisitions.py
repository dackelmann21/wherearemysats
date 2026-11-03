"""Acquisitions / Disposals / Exchange Exits domain router (consolidation
package P3-3).

Routes for user-declared Acquisitions (list / create / get / update / delete /
delete-all / retrace / origin candidates / position counts / CSV export /
two-phase CSV import), Disposal candidates and Disposals, and Exchange Exits
(candidates / CRUD / by-txid / CSV exports), plus the helpers only these
routes use (``_csv_response``, ``_parse_and_preview_rows``,
``_preview_counts``, ``_POSITION_STATES``).

The application's runtime objects (database, engine, ...) remain module
globals of ``backend.api.server`` so tests can keep replacing them there.
This module resolves them late-bound through :func:`backend.api.runtime.state`
at call time (``srv = state()`` at the top of each endpoint / helper) and
never caches them at import time. The ``DisposalValidationError`` /
``DisposalNotFoundError`` exception handlers stay app-level in ``server.py``.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from backend.acquisitions.csv_import import parse_acquisition_csv
from backend.acquisitions.disposal_service import (
    create_disposal,
    delete_disposal,
    disposal_candidates as service_disposal_candidates,
    get_disposal,
    list_disposals,
    update_disposal,
)
from backend.acquisitions.exchange_exit_service import (
    ExchangeExitConflictError,
    ExchangeExitNotFoundError,
    ExchangeExitValidationError,
    build_exchange_exit_candidates,
    create_exchange_exit,
    delete_exchange_exit,
    get_exchange_exit,
    get_exchange_exit_by_txid,
    list_exchange_exits,
    update_exchange_exit,
)
from backend.acquisitions.service import (
    AcquisitionDuplicateError,
    AcquisitionSelectionRequiredError,
    AcquisitionValidationError,
    create_acquisition as service_create_acquisition,
    delete_acquisition as service_delete_acquisition,
    get_acquisition_response,
    list_acquisitions_response,
    position_counts_response,
    origin_candidates,
    preview_acquisition as service_preview_acquisition,
    retrace_acquisition as service_retrace_acquisition,
    update_acquisition as service_update_acquisition,
)
from backend.api.http_common import _check_sensitive
from backend.api.runtime import state
from backend.api.security_input import canonical_txid, detect_sensitive_input
from backend.models.schemas import (
    Acquisition,
    AcquisitionCreate,
    AcquisitionImportPreviewRequest,
    AcquisitionImportRequest,
    AcquisitionImportResult,
    AcquisitionImportRowResult,
    AcquisitionImportRowStatus,
    AcquisitionOriginCandidate,
    AcquisitionUpdate,
    Disposal,
    DisposalCandidate,
    DisposalCreate,
    DisposalUpdate,
    ExchangeExit,
    ExchangeExitCandidateList,
    ExchangeExitCandidateLoadError,
    ExchangeExitCreate,
    ExchangeExitUpdate,
    PositionCounts,
)
from backend.reporting.csv_exports import export_csv, ExportValidationError
from backend.reporting.read_data import ReadBudgetExceeded
from backend.rpc.bitcoin import RpcError

logger = logging.getLogger("lineage.api")

router = APIRouter()


_POSITION_STATES = {
    "CURRENT",
    "NEEDS_REVIEW",
    "PARTIALLY_DISPOSED",
    "DISPOSED",
    "TRACING",
}


# ---------------------------------------------------------------------------
# Acquisitions (ACQUISITION_TRACKING_V1_REQUIREMENTS.md)
#
# User-declared Acquisition provenance anchored to an exact on-chain
# outpoint. Saving one automatically creates + runs a normal forward provenance
# trace from that outpoint (View Trace / descendant summary). NOT a tax engine:
# no disposals, FIFO, cost-basis or gain/loss is computed or stored.
# ---------------------------------------------------------------------------


@router.get("/api/acquisitions", response_model=list[Acquisition])
async def list_acquisitions_endpoint(
    position_state: Optional[str] = Query(None),
) -> list[Acquisition]:
    srv = state()
    if position_state is not None and position_state not in _POSITION_STATES:
        raise HTTPException(status_code=400, detail="Invalid position_state.")
    return await list_acquisitions_response(srv.db, srv.engine, position_state)


@router.post("/api/acquisitions", response_model=Acquisition, status_code=201)
async def create_acquisition_endpoint(payload: AcquisitionCreate) -> Acquisition:
    srv = state()
    _check_sensitive(payload.source, "source")
    _check_sensitive(payload.reference, "reference")
    _check_sensitive(payload.notes, "notes")
    try:
        return await service_create_acquisition(srv.db, srv.engine, payload)
    except AcquisitionValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AcquisitionDuplicateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AcquisitionSelectionRequiredError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "detail": str(exc),
                "candidates": exc.candidates,
            },
        ) from exc


@router.get("/api/acquisitions/position-counts", response_model=PositionCounts)
async def position_counts_endpoint() -> PositionCounts:
    srv = state()
    return PositionCounts(**await position_counts_response(srv.db, srv.engine))

async def _csv_response(kind, **filters):
    srv = state()
    try:
        content = await export_csv(kind, srv.db, srv.engine, **filters)
    except ExportValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ReadBudgetExceeded as exc:
        raise HTTPException(status_code=503, detail='CSV export unavailable; please retry.') from exc
    return Response(content, media_type='text/csv', headers={
        'Content-Disposition': f'attachment; filename="wams-{kind}.csv"',
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
    })


@router.get('/api/acquisitions/export.csv')
async def acquisitions_csv_export_endpoint():
    return await _csv_response('acquisitions')


@router.get("/api/acquisitions/outputs/{txid}", response_model=list[AcquisitionOriginCandidate])
async def acquisition_outputs_endpoint(txid: str):
    srv = state()
    try:
        return await origin_candidates(srv.engine, txid)
    except AcquisitionValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/acquisitions/{acq_id}", response_model=Acquisition)
async def get_acquisition_endpoint(acq_id: int) -> Acquisition:
    srv = state()
    try:
        return await get_acquisition_response(srv.db, srv.engine, acq_id)
    except RpcError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail="Acquisition not found.") from exc
        raise


@router.patch("/api/acquisitions/{acq_id}", response_model=Acquisition)
async def update_acquisition_endpoint(acq_id: int, payload: AcquisitionUpdate) -> Acquisition:
    srv = state()
    _check_sensitive(payload.source, "source")
    _check_sensitive(payload.reference, "reference")
    _check_sensitive(payload.notes, "notes")
    try:
        return await service_update_acquisition(srv.db, srv.engine, acq_id, payload)
    except AcquisitionValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AcquisitionDuplicateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AcquisitionSelectionRequiredError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "detail": str(exc),
                "candidates": exc.candidates,
            },
        ) from exc
    except RpcError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail="Acquisition not found.") from exc
        raise


@router.delete("/api/acquisitions/{acq_id}", status_code=204)
async def delete_acquisition_endpoint(acq_id: int) -> Response:
    srv = state()
    if not await service_delete_acquisition(srv.db, acq_id):
        raise HTTPException(status_code=404, detail="Acquisition not found.")
    return Response(status_code=204)


@router.delete("/api/acquisitions")
async def delete_all_acquisitions_endpoint() -> dict[str, int]:
    """Reset the Acquisition dataset so the same CSV can be imported again.

    Removes every active on-chain Acquisition, its Disposal and
    Exchange Exit allocation links, and the Acquisition-owned forward traces.
    Independent application data is preserved: Exchange Exits (user-confirmed
    custody boundaries) and Disposals (independent economic records) keep
    their own rows, ownership-neutral Blockchain Analysis traces and jobs
    survive, the blockchain cache, Known Address Registry, address evidence,
    wallet discovery and settings are untouched, and no ownership
    classification is re-derived as a side effect.

    One canonical, atomic backend operation - never N single deletes. The
    response carries counts only, no addresses or outpoints.
    """
    srv = state()
    try:
        result = await srv.db.delete_all_acquisitions()
    except Exception:
        logger.exception("delete all acquisitions failed")
        raise HTTPException(status_code=500, detail="Failed to delete acquisitions.")
    logger.info("delete all acquisitions: %s", dict(result))
    return result


@router.post("/api/acquisitions/{acq_id}/retrace", response_model=Acquisition)
async def retrace_acquisition_endpoint(acq_id: int) -> Acquisition:
    srv = state()
    try:
        return await service_retrace_acquisition(srv.db, srv.engine, acq_id)
    except RpcError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail="Acquisition not found.") from exc
        raise


@router.get(
    "/api/acquisitions/{acq_id}/disposal-candidates",
    response_model=list[DisposalCandidate],
)
async def disposal_candidates_endpoint(acq_id: int) -> list[DisposalCandidate]:
    srv = state()
    return await service_disposal_candidates(srv.db, srv.engine, acq_id)


# ---------------------------------------------------------------------------
# Disposals (ACQUISITION_REVIEW_AND_DISPOSALS_V1_REQUIREMENTS.md)
#
# Economic disposal records: a Disposal is a SALE of BTC allocated from one or
# more Acquisitions. Blockchain evidence is optional and verified, but SPENT is
# never SOLD.
# ---------------------------------------------------------------------------


@router.post("/api/disposals", response_model=Disposal, status_code=201)
async def create_disposal_endpoint(payload: DisposalCreate) -> Disposal:
    srv = state()
    _check_sensitive(payload.broker, "broker")
    return await create_disposal(srv.db, srv.engine, payload)


@router.get("/api/disposals", response_model=list[Disposal])
async def list_disposals_endpoint() -> list[Disposal]:
    srv = state()
    return await list_disposals(srv.db)


@router.get("/api/disposals/{disposal_id}", response_model=Disposal)
async def get_disposal_endpoint(disposal_id: int) -> Disposal:
    srv = state()
    return await get_disposal(srv.db, disposal_id)


@router.patch("/api/disposals/{disposal_id}", response_model=Disposal)
async def update_disposal_endpoint(
    disposal_id: int, payload: DisposalUpdate
) -> Disposal:
    srv = state()
    _check_sensitive(payload.broker, "broker")
    return await update_disposal(srv.db, srv.engine, disposal_id, payload)


@router.delete("/api/disposals/{disposal_id}", status_code=204)
async def delete_disposal_endpoint(disposal_id: int) -> Response:
    srv = state()
    await delete_disposal(srv.db, disposal_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Exchange Exits (EXCHANGE_EXIT_REQUIREMENTS.md / v0.11.0)
#
# The user-confirmed blockchain boundary where BTC leaves self-custody toward
# an exchange. SPENT != SOLD: an exit is (a) transaction facts (sources,
# outputs, miner fee) + (b) an OPTIONAL economic Disposal the user chooses to
# link. Never auto-created; the linked Disposal only exists after explicit
# user confirmation.
# ---------------------------------------------------------------------------


@router.post(
    "/api/acquisitions/{acquisition_id}/exchange-exit-candidates",
    response_model=ExchangeExitCandidateList,
)
async def exchange_exit_candidates_endpoint(
    acquisition_id: int,
) -> ExchangeExitCandidateList:
    srv = state()
    try:
        acquisition = await get_acquisition_response(srv.db, srv.engine, acquisition_id)
    except RpcError as exc:
        if "not found" in str(exc).lower():
            raise HTTPException(status_code=404, detail="Acquisition not found.") from exc
        raise
    try:
        # FIX-UI-6: candidate txs the node could not deliver are reported as
        # load errors instead of being skipped silently.
        load_errors: list[ExchangeExitCandidateLoadError] = []
        candidates = await build_exchange_exit_candidates(
            srv.db, srv.engine, acquisition, load_errors=load_errors
        )
        return ExchangeExitCandidateList(candidates=candidates, load_errors=load_errors)
    except ReadBudgetExceeded as exc:
        raise HTTPException(
            status_code=503, detail="Exit candidate provenance unavailable; please retry."
        ) from exc


@router.post("/api/exchange-exits", response_model=ExchangeExit, status_code=201)
async def create_exchange_exit_endpoint(payload: ExchangeExitCreate) -> ExchangeExit:
    srv = state()
    _check_sensitive(payload.broker, "broker")
    try:
        return await create_exchange_exit(srv.db, srv.engine, payload)
    except ExchangeExitValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ExchangeExitConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ReadBudgetExceeded as exc:
        raise HTTPException(
            status_code=503, detail="Exit input provenance unavailable; please retry."
        ) from exc


@router.get("/api/exchange-exits", response_model=list[ExchangeExit])
async def list_exchange_exits_endpoint() -> list[ExchangeExit]:
    srv = state()
    return await list_exchange_exits(srv.db)


@router.get("/api/exchange-exits/by-txid/{txid}", response_model=ExchangeExit)
async def get_exchange_exit_by_txid_endpoint(txid: str) -> ExchangeExit:
    srv = state()
    result = await get_exchange_exit_by_txid(srv.db, txid)
    if result is None:
        raise HTTPException(status_code=404, detail="Exchange Exit not found.")
    return result


@router.get('/api/exchange-exits/export.csv')
async def exits_csv_export_endpoint(
    from_date: Optional[str] = Query(None, alias='from'),
    to_date: Optional[str] = Query(None, alias='to'),
):
    """Confirmed block-time bounds, inclusive UTC days; unknown time only in all-time export."""
    return await _csv_response('exits', from_date=from_date, to_date=to_date)


@router.get('/api/exchange-exits/provenance-v1.csv')
async def exit_provenance_csv_endpoint(
    from_date: Optional[str] = Query(None, alias='from'),
    to_date: Optional[str] = Query(None, alias='to'),
):
    """Ordered on-chain hop evidence. Date bounds use confirmed UTC block time."""
    return await _csv_response('exit-provenance-v1', from_date=from_date, to_date=to_date)


@router.get("/api/exchange-exits/{exit_id}", response_model=ExchangeExit)
async def get_exchange_exit_endpoint(exit_id: int) -> ExchangeExit:
    srv = state()
    try:
        return await get_exchange_exit(srv.db, exit_id, srv.engine)
    except ExchangeExitNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/api/exchange-exits/{exit_id}", response_model=ExchangeExit)
async def update_exchange_exit_endpoint(
    exit_id: int, payload: ExchangeExitUpdate
) -> ExchangeExit:
    srv = state()
    _check_sensitive(payload.broker, "broker")
    try:
        return await update_exchange_exit(srv.db, srv.engine, exit_id, payload)
    except ExchangeExitNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ExchangeExitValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ExchangeExitConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/api/exchange-exits/{exit_id}", status_code=204)
async def delete_exchange_exit_endpoint(exit_id: int) -> Response:
    srv = state()
    if not await delete_exchange_exit(srv.db, exit_id):
        raise HTTPException(status_code=404, detail="Exchange Exit not found.")
    return Response(status_code=204)


async def _parse_and_preview_rows(
    content: str,
    selections: dict[str, int],
    default_source: str = "",
):
    """Shared two-phase engine: parse the CSV, then classify every row WITHOUT
    a single database write (no Acquisition, no Trace, no ownership evidence,
    no engine cache writes - read-only RPC resolution).

    ``default_source`` fills the source of rows whose ``source`` cell is
    missing or empty; a non-empty cell always wins (FIX-UI-4).

    Returns (parsed, list of (AcquisitionImportRowResult, Optional[AcquisitionCreate])),
    with the row status one of READY / DUPLICATE / INVALID / REVIEW_REQUIRED.
    Raises HTTP 400 for file-level problems (missing header, no data rows)."""
    srv = state()

    parsed = parse_acquisition_csv(content)
    if parsed.error:
        raise HTTPException(status_code=400, detail=parsed.error)
    if not parsed.rows:
        raise HTTPException(
            status_code=400, detail="The CSV contains no data rows (header only)."
        )

    default_source = (default_source or "").strip()
    prepared: list[tuple[AcquisitionImportRowResult, Optional[AcquisitionCreate]]] = []
    for r in parsed.rows:
        r.source = r.source or default_source
        rr = AcquisitionImportRowResult(
            line=r.line_number,
            status=AcquisitionImportRowStatus.INVALID,
            txid=r.txid or "",
            acquisition_date=r.acquisition_date,
            source=r.source,
            reference=r.reference,
            notes=r.notes,
        )
        payload: Optional[AcquisitionCreate] = None
        if r.error:
            rr.error = r.error
            prepared.append((rr, payload))
            continue

        sensitive = next((message for value in (r.source, r.reference, r.notes)
                          if (message := detect_sensitive_input(value))), None)
        if sensitive:
            rr.error = sensitive
            prepared.append((rr, payload))
            continue

        origin_vout: Optional[int] = None
        if str(rr.line) in selections:
            origin_vout = int(selections[str(rr.line)])
        elif r.origin_vout is not None:
            origin_vout = int(r.origin_vout)

        try:
            payload = AcquisitionCreate(
                acquisition_date=r.acquisition_date,
                source=r.source or None,
                reference=r.reference or None,
                notes=r.notes or None,
                txid=r.txid,
                address=r.address,
                vout=origin_vout,
            )
        except Exception as exc:  # noqa: BLE001 - pydantic field coercion
            rr.error = str(exc)
            prepared.append((rr, None))
            continue

        prev = await service_preview_acquisition(srv.db, srv.engine, payload)
        rr.status = prev["status"]
        rr.error = prev.get("error") or None
        rr.candidates = [
            AcquisitionOriginCandidate.model_validate(c) for c in (prev.get("candidates") or [])
        ]
        rr.existing_acquisition_id = prev.get("existing_acquisition_id")
        if prev["status"] == "READY":
            rr.origin_vout = prev["origin_vout"]
            rr.acquisition_date = prev.get("acquisition_date")
            rr.btc_amount = prev["btc_amount"]
            rr.source = prev.get("source") or ""
            rr.reference = prev.get("reference") or ""
            rr.notes = prev.get("notes") or ""
        prepared.append((rr, payload))

    return parsed, prepared


def _preview_counts(
    prepared: list[tuple[AcquisitionImportRowResult, Optional[AcquisitionCreate]]],
) -> dict[str, int]:
    counts = {"ready": 0, "duplicates": 0, "invalid": 0, "review_required": 0}
    for rr, _payload in prepared:
        if rr.status == "DUPLICATE":
            counts["duplicates"] += 1
        elif rr.status == "INVALID":
            counts["invalid"] += 1
        elif rr.status == "REVIEW_REQUIRED":
            counts["review_required"] += 1
        elif rr.status in ("READY", "IMPORTED"):
            counts["ready"] += 1  # IMPORTED implies it was a created READY row
    return counts


@router.post("/api/acquisitions/import/preview", response_model=AcquisitionImportResult)
async def acquisitions_csv_preview_endpoint(
    payload: AcquisitionImportPreviewRequest,
) -> AcquisitionImportResult:
    """Non-destructive preview phase (v0.8.0 CSV import).

    Parses + normalizes + validates + resolves every row and classifies it as
    READY / DUPLICATE / INVALID / REVIEW_REQUIRED. The preview NEVER creates an
    Acquisition, never creates a Trace, never writes ownership evidence and
    never changes database contents (all resolution is read-only RPC).

    ``selections`` (line -> chosen vout) resolves a REVIEW_REQUIRED row right
    in the preview: the row is revalidated and becomes READY without requiring
    a CSV edit or re-upload."""
    content = (payload.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="No CSV content supplied.")
    parsed, prepared = await _parse_and_preview_rows(
        content, payload.selections, payload.default_source)
    counts = _preview_counts(prepared)
    return AcquisitionImportResult(
        total=len(prepared),
        ready=counts["ready"],
        imported=0,
        duplicates=counts["duplicates"],
        invalid=counts["invalid"],
        review_required=counts["review_required"],
        failed=0,
        delimiter=parsed.delimiter,
        header_columns=parsed.header_columns,
        ignored_columns=parsed.ignored_columns,
        rows=[rr for rr, _p in prepared],
    )


@router.post("/api/acquisitions/import", response_model=AcquisitionImportResult)
async def acquisitions_csv_import_endpoint(
    payload: AcquisitionImportRequest,
) -> AcquisitionImportResult:
    """Create the READY rows the user explicitly selected from the preview.

    Only the line numbers in ``payload.lines`` are submitted for creation and
    only while they are still READY (a same-batch sibling can legitimately turn
    a later row into a DUPLICATE; such rows are skipped, never re-created).
    Each successful row goes through the exact same ``create_acquisition``
    service path as the manual form (one CSV row = one Acquisition + its normal
    Forward Provenance trace). One failed row never aborts the others."""
    srv = state()
    content = (payload.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="No CSV content supplied.")
    parsed, prepared = await _parse_and_preview_rows(
        content, payload.selections, payload.default_source)
    requested = {int(line) for line in payload.lines}
    imported = 0
    failed = 0

    for rr, create_payload in prepared:
        if rr.line not in requested or create_payload is None:
            continue
        if rr.status != AcquisitionImportRowStatus.READY:
            continue

        try:
            acq = await service_create_acquisition(srv.db, srv.engine, create_payload)
        except AcquisitionDuplicateError:
            # Two CSV rows with the same origin: the first was created, this
            # one correctly becomes a skipped duplicate - never an error.
            rr.status = AcquisitionImportRowStatus.DUPLICATE
            try:
                existing = await srv.db.get_acquisition_by_origin(
                    canonical_txid(create_payload.txid), int(rr.origin_vout if rr.origin_vout is not None else -1)
                )
                if existing:
                    rr.existing_acquisition_id = existing["id"]
                    rr.existing_acquisition_date = existing["acquisition_date"]
            except Exception:  # noqa: BLE001 - best-effort display metadata
                pass
        except AcquisitionSelectionRequiredError as exc:
            rr.status = AcquisitionImportRowStatus.REVIEW_REQUIRED
            rr.candidates = exc.candidates
        except AcquisitionValidationError as exc:
            failed += 1
            rr.status = AcquisitionImportRowStatus.IMPORT_FAILED
            rr.error = str(exc)
        except RpcError as exc:
            failed += 1
            rr.status = AcquisitionImportRowStatus.IMPORT_FAILED
            rr.error = exc.message
        else:
            imported += 1
            rr.status = AcquisitionImportRowStatus.IMPORTED
            rr.acquisition_id = acq.id
            rr.trace_id = acq.trace_id

    counts = _preview_counts(prepared)
    return AcquisitionImportResult(
        total=len(prepared),
        ready=counts["ready"],
        imported=imported,
        duplicates=counts["duplicates"],
        invalid=counts["invalid"],
        review_required=counts["review_required"],
        failed=failed,
        delimiter=parsed.delimiter,
        header_columns=parsed.header_columns,
        ignored_columns=parsed.ignored_columns,
        rows=[rr for rr, _p in prepared],
    )
