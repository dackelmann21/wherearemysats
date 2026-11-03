"""User-declared Acquisition anchors; blockchain facts come only from the node.

An anchor documents provenance, not proof of purchase or ownership.
Manual entry, CSV import and trace actions share this service.
"""
from __future__ import annotations

import logging
from sqlite3 import IntegrityError
from datetime import date
from decimal import Decimal

from backend.models.schemas import (
    Acquisition, AcquisitionCreate, AcquisitionUpdate, AcquisitionDescendant,
    AcquisitionDisposalSummary, AcquisitionExchangeExitSummary,
    AcquisitionOriginCandidate, AcquisitionStatus, PositionState, TraceStatus,
)
from backend.validation import canonical_txid
from backend.registry.address_validation import is_valid_mainnet_address
from backend.rpc.bitcoin import NODE_UNREACHABLE_MESSAGE, NodeUnreachableError, RpcError, sats
from backend.acquisitions.position import PositionContext, descendant_summary

logger = logging.getLogger(__name__)
_BTC_SATS = Decimal(100_000_000)
_TRACE_TO_ACQ_STATUS = {
    TraceStatus.COMPLETE: AcquisitionStatus.COMPLETE,
    TraceStatus.COMPLETE_AT_CUSTODY_BOUNDARY: AcquisitionStatus.COMPLETE,
    TraceStatus.NEEDS_CLASSIFICATION: AcquisitionStatus.REVIEW_REQUIRED,
    TraceStatus.NEEDS_REVIEW: AcquisitionStatus.REVIEW_REQUIRED,
    TraceStatus.IN_PROGRESS: AcquisitionStatus.TRACING,
    TraceStatus.ANALYSIS_LIMIT_REACHED: AcquisitionStatus.REVIEW_REQUIRED,
    TraceStatus.REVERSE_LIMIT_REACHED: AcquisitionStatus.REVIEW_REQUIRED,
    TraceStatus.FORWARD_LIMIT_REACHED: AcquisitionStatus.REVIEW_REQUIRED,
    TraceStatus.ERROR: AcquisitionStatus.TRACE_FAILED,
}


class AcquisitionValidationError(Exception):
    """Clean user-facing validation failure."""


class AcquisitionSelectionRequiredError(Exception):
    def __init__(self, candidates):
        super().__init__("Select the exact output to anchor the Acquisition.")
        self.candidates = candidates


class AcquisitionDuplicateError(Exception):
    """An anchor already exists for this exact outpoint."""


def _fmt_decimal(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _metadata(payload) -> dict:
    fields = payload.model_dump(include={"acquisition_date", "source", "reference", "notes"},
                                exclude_unset=True)
    if "acquisition_date" in fields:
        value = (fields["acquisition_date"] or "").strip()
        if value:
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            except ValueError as exc:
                raise AcquisitionValidationError("Acquisition date must be YYYY-MM-DD.") from exc
        fields["acquisition_date"] = value or None
    for key in ("source", "reference", "notes"):
        if key in fields:
            fields[key] = (fields[key] or "").strip() or None
    return fields


def _validate_txid(txid: str) -> str:
    try:
        return canonical_txid(str(txid or ""))
    except ValueError:
        raise AcquisitionValidationError("The transaction ID must be a 64-character hexadecimal string.") from None


async def origin_candidates(engine, txid: str) -> list[dict]:
    """Read-only resolution, including CSV preview (zero database writes)."""
    txid = _validate_txid(txid)
    try:
        raw = await engine.rpc.get_raw_transaction(txid, verbose=True)
    except NodeUnreachableError:
        raise  # a stopped node is not a missing transaction (FIX-OWN-1b)
    except RpcError as exc:
        raise AcquisitionValidationError("The transaction could not be found on the local Bitcoin node.") from exc
    if not isinstance(raw, dict) or raw.get("txid", txid).lower() != txid:
        raise AcquisitionValidationError("The node returned an invalid transaction.")
    height = timestamp = None
    if raw.get("blockhash"):
        header = await engine.rpc.get_block_header(raw["blockhash"], verbose=True)
        height, timestamp = header.get("height"), header.get("time")
    result = []
    for output in raw.get("vout", []):
        script = output.get("scriptPubKey") or {}
        addresses = script.get("addresses") or []
        address = script.get("address") or (addresses[0] if len(addresses) == 1 else None)
        amount = sats(output.get("value"))
        if amount is None or amount < 0:
            raise AcquisitionValidationError("The node returned an invalid output amount.")
        result.append(AcquisitionOriginCandidate(
            vout=output["n"], amount_sats=amount, address=address,
            block_height=height, block_time=timestamp,
        ).model_dump())
    return result


async def resolve_origin(engine, txid, address=None, origin_vout=None) -> dict:
    txid = _validate_txid(txid)
    address = (address or "").strip() or None
    if address and not is_valid_mainnet_address(address):
        raise AcquisitionValidationError("The Bitcoin address is invalid or not a mainnet address.")
    if origin_vout is None and address is None:
        raise AcquisitionValidationError("An exact VOUT (or Bitcoin address to resolve) is required.")
    outputs = await origin_candidates(engine, txid)
    matches = [o for o in outputs if address is None or o["address"] == address]
    if origin_vout is not None:
        matches = [o for o in matches if o["vout"] == origin_vout]
    if not matches:
        raise AcquisitionValidationError("The selected VOUT or Bitcoin address does not match any output of this transaction.")
    if len(matches) != 1:
        raise AcquisitionSelectionRequiredError(matches)
    out = matches[0]
    return dict(origin_txid=txid, origin_vout=out["vout"], origin_address=out["address"],
                origin_amount_sats=out["amount_sats"], origin_block_height=out["block_height"],
                origin_block_time=out["block_time"])


async def acquisition_descendant_summary(db, engine, acquisition, context=None) -> AcquisitionDescendant:
    return await descendant_summary(db, engine, acquisition, context)


def _acq_status_from_trace_status(trace_status):
    return _TRACE_TO_ACQ_STATUS.get(trace_status or "", AcquisitionStatus.TRACE_FAILED)


_POSITION_COUNT_STATES = (PositionState.CURRENT, PositionState.NEEDS_REVIEW,
                        PositionState.PARTIALLY_DISPOSED, PositionState.DISPOSED, PositionState.TRACING)
_TRACING_STATUSES = {AcquisitionStatus.TRACING, AcquisitionStatus.RESOLVING, TraceStatus.IN_PROGRESS}


def derive_position_state(origin_amount_sats, allocated_sats, acq_status, trace_status, descendant):
    if origin_amount_sats > 0 and allocated_sats >= origin_amount_sats:
        return PositionState.DISPOSED
    if descendant is not None and descendant.fully_exited:
        # FIX-EXIT-3: complete On-Chain exit, also via 0/0 rows (allocated_sats stays 0).
        return PositionState.DISPOSED
    if allocated_sats > 0:
        return PositionState.PARTIALLY_DISPOSED
    if (acq_status or "") in _TRACING_STATUSES or (trace_status or "") in _TRACING_STATUSES:
        return PositionState.TRACING
    if descendant is not None and (descendant.needs_review or descendant.spend_unknown):
        return PositionState.NEEDS_REVIEW
    if descendant is not None and descendant.unspent_terminal_count >= 1:
        return PositionState.CURRENT
    return PositionState.NEEDS_REVIEW


def position_state_from_row(row, allocated_sats, descendant):
    return derive_position_state(row["origin_amount_sats"], allocated_sats,
                                 row.get("status"), row.get("trace_status"), descendant)


async def _create_and_link_trace(db, engine, acq_id, acq_date, origin):
    acq_date = acq_date or "undated"
    name = f"Acquisition {acq_date} {origin['origin_txid'][:8]}:{origin['origin_vout']}"
    try:
        trace, _walk = await engine.create_trace(
            name=name, start_txid=origin["origin_txid"], start_address=None,
            start_vout=origin["origin_vout"], trace_mode="FORWARD_PROVENANCE")
    except Exception:
        logger.warning("Acquisition %s forward trace creation failed", acq_id)
        row = await db.update_acquisition(acq_id, status=AcquisitionStatus.TRACE_FAILED, trace_status=None)
        return row, "The forward trace could not be created."
    status = _acq_status_from_trace_status(trace.status)
    row = await db.update_acquisition(acq_id, trace_id=trace.id, trace_status=trace.status, status=status)
    return row, ("The linked trace needs classification or review."
                 if status == AcquisitionStatus.REVIEW_REQUIRED else None)


async def preview_acquisition(db, engine, payload: AcquisitionCreate) -> dict:
    try:
        metadata = _metadata(payload)
        origin = await resolve_origin(engine, payload.txid, payload.address, payload.vout)
    except AcquisitionSelectionRequiredError as exc:
        return dict(status="REVIEW_REQUIRED", candidates=exc.candidates)
    except NodeUnreachableError:
        return dict(status="INVALID", error=NODE_UNREACHABLE_MESSAGE)
    except (AcquisitionValidationError, RpcError) as exc:
        return dict(status="INVALID", error=str(exc))
    existing = await db.get_acquisition_by_origin(origin["origin_txid"], origin["origin_vout"])
    from backend.reporting.origin_resolution import OriginResolutions
    if (origin['origin_txid'], origin['origin_vout']) in (await OriginResolutions.load(db)).decisions:
        return dict(status='INVALID', error='Remove the No-Acquisition resolution before creating an Acquisition.')
    return dict(status="DUPLICATE" if existing else "READY", **metadata, **origin,
                btc_amount=_fmt_decimal(Decimal(origin["origin_amount_sats"]) / _BTC_SATS),
                existing_acquisition_id=existing["id"] if existing else None,
                existing_acquisition_date=existing["acquisition_date"] if existing else None)


async def _row_to_acquisition(db, engine, row, warn=None, context=None) -> Acquisition:
    descendant = await acquisition_descendant_summary(db, engine, row, context)
    allocated = await db.get_acquisition_allocated_sats(row["id"])
    disposals = [AcquisitionDisposalSummary(
        disposal_id=d["id"], disposal_date=d["disposal_date"], disposal_type=d["disposal_type"],
        allocated_sats=int(d["allocated_sats"] or 0), disposed_sats=d.get("disposed_sats"),
        network_fee_sats=d.get("network_fee_sats"), gross_proceeds_eur=d["gross_proceeds_eur"],
        fees_eur=d["fees_eur"], net_proceeds_eur=d["net_proceeds_eur"], broker=d["broker"],
    ) for d in await db.get_acquisition_disposals(row["id"])]
    exits = [AcquisitionExchangeExitSummary(
        exit_id=e["id"], txid=e["txid"], exit_date=e["exit_date"], exit_time=e.get("exit_time"),
        allocated_sats=int(e["allocated_sats"] or 0), disposed_sats=int(e["disposed_sats"] or 0),
        network_fee_sats=int(e["network_fee_sats"] or 0), fee_sats=int(e["fee_sats"] or 0),
        broker=e.get("broker"), reference=e.get("reference"), notes=e.get("notes"),
        verified=bool(e.get("verified")), disposal_id=e.get("disposal_id"),
        unattributed_inputs=int(e.get("unattributed_inputs") or 0),
    ) for e in await db.get_acquisition_exchange_exits(row["id"])]
    return Acquisition(**row, descendant=descendant, warning=warn,
        position_state=position_state_from_row(row, allocated, descendant), allocated_sats=allocated,
        disposals=disposals, disposal_count=len(disposals), exchange_exits=exits, exchange_exit_count=len(exits))


async def create_acquisition(db, engine, payload: AcquisitionCreate) -> Acquisition:
    metadata = _metadata(payload)
    origin = await resolve_origin(engine, payload.txid, payload.address, payload.vout)
    if await db.get_acquisition_by_origin(origin["origin_txid"], origin["origin_vout"]):
        raise AcquisitionDuplicateError("An Acquisition already exists for this exact outpoint.")
    from backend.reporting.origin_resolution import OriginResolutions
    if (origin['origin_txid'], origin['origin_vout']) in (await OriginResolutions.load(db)).decisions:
        raise AcquisitionDuplicateError('Remove the No-Acquisition resolution before creating an Acquisition.')
    try:
        row = await db.create_acquisition(**metadata, **origin, status=AcquisitionStatus.TRACING)
    except IntegrityError as exc:
        # The unique constraint also arbitrates overlapping manual/CSV saves.
        await db.db.rollback()
        if await db.get_acquisition_by_origin(origin["origin_txid"], origin["origin_vout"]):
            raise AcquisitionDuplicateError("An Acquisition already exists for this exact outpoint.") from exc
        if 'No-Acquisition' in str(exc):
            raise AcquisitionDuplicateError('Remove the No-Acquisition resolution before creating an Acquisition.') from exc
        raise
    row, warning = await _create_and_link_trace(db, engine, row["id"], row["acquisition_date"], origin)
    return await _row_to_acquisition(db, engine, row, warn=warning)


async def update_acquisition(db, engine, acq_id, payload: AcquisitionUpdate) -> Acquisition:
    if not await db.get_acquisition(acq_id):
        raise RpcError("Acquisition not found.")
    row = await db.update_acquisition(acq_id, **_metadata(payload))
    return await _row_to_acquisition(db, engine, row)


async def delete_acquisition(db, acq_id) -> bool:
    return await db.delete_acquisition(acq_id)


async def retrace_acquisition(db, engine, acq_id) -> Acquisition:
    row = await db.get_acquisition(acq_id)
    if not row:
        raise RpcError("Acquisition not found.")
    if row["trace_id"] is not None and await db.get_trace(row["trace_id"]):
        await engine.continue_trace(row["trace_id"])
        live = await db.get_trace(row["trace_id"])
        row = await db.update_acquisition(acq_id, status=_acq_status_from_trace_status(live.status),
                                          trace_status=live.status)
        return await _row_to_acquisition(db, engine, row)
    row, warning = await _create_and_link_trace(db, engine, acq_id, row["acquisition_date"], row)
    return await _row_to_acquisition(db, engine, row, warn=warning)


async def get_acquisition_response(db, engine, acq_id) -> Acquisition:
    row = await db.get_acquisition(acq_id)
    if not row:
        raise RpcError("Acquisition not found.")
    return await _row_to_acquisition(db, engine, row)


async def list_acquisitions_response(db, engine, position_state=None) -> list[Acquisition]:
    context = PositionContext()
    result = []
    for row in await db.list_acquisitions():
        acquisition = await _row_to_acquisition(db, engine, row, context=context)
        if position_state is None or acquisition.position_state == position_state:
            result.append(acquisition)
    return result


async def position_counts_response(db, engine) -> dict[str, int]:
    counts = {state: 0 for state in _POSITION_COUNT_STATES}
    rows = await db.list_acquisitions()
    survey = await db.get_allocation_survey()
    context = PositionContext()
    for row in rows:
        descendant = await acquisition_descendant_summary(db, engine, row, context)
        counts[position_state_from_row(row, int(survey.get(row["id"], 0)), descendant)] += 1
    counts["total"] = len(rows)
    return counts
