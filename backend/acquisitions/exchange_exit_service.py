"""Exchange Exit service (EXCHANGE_EXIT_REQUIREMENTS.md v0.11.0).

An Exchange Exit is the user-confirmed blockchain boundary where BTC leaves
self-custody to an exchange. It is a first-class domain object:

* The on-chain facts are owned here: the exit transaction (txid), the OWNED
  source outpoints that fed it (exchange_exit_sources), and the exit's output
  layout with confirmed exchange deposits (exchange_exit_outputs).
* An Exchange Exit never creates a Disposal. Independent historical Disposal
  records remain separate; a SPENT output is never a sale by itself.

SPENT != SOLD: this module deliberately does NOT walk past the boundary. The
exchange's later spends of the deposit output (which are NOT owned by us) are
never followed for the ACQUISITION's disposal scope - the one-hop-too-far
regression. Generic Blockchain Analysis (backend/tracing/analysis.py) still
traverses freely past the exit for ownership-detection purposes.

The objective transaction miner fee is separate from selected output value.
Legacy source fields disposed_sats and network_fee_sats represent the user's
recorded boundary allocation, never blockchain-proven sat identity or a sale.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from datetime import date, datetime, time, timezone
from typing import Any

from backend.rpc.bitcoin import RpcError
from backend.validation import canonical_txid
from backend.models.schemas import (
    Acquisition,
    AcquisitionDescendant,
    ExchangeExit,
    ExchangeExitCandidate,
    ExchangeExitCandidateAcquisition,
    ExchangeExitCandidateInput,
    ExchangeExitCandidateLoadError,
    ExchangeExitCandidateOutput,
    ExchangeExitCreate,
    ExchangeExitOutput,
    ExchangeExitOutputSelection,
    ExchangeExitSource,
    ExchangeExitSourceCreate,
    ExchangeExitUpdate,
)
from .disposal_service import _btc_sats, _strip_optional
from backend.tracing.engine import (
    OwnershipStatus,
    SpendStatus,
)

logger = logging.getLogger(__name__)

# The exit tx's own outputs are classified by ownership only as far as WE can
# tell from OUR trace; anything not provably owned by us is a possible deposit.
# We deliberately never claim an external output is "exchange" by address
# heuristics - the user confirms which non-owned outputs are the deposit.


def _tx_to_dict_payload(tx: Any) -> Any:
    """Render an engine transaction payload into the dict-shaped form this
    module consumes (``o["vout"]`` / ``o.get("ownership_status")`` on
    inputs/outputs plus attribute access to total/block scalars).

    The real TraceEngine returns pydantic ``TransactionInfo`` (inputs are
    ``InputInfo``, outputs are ``OutputInfo`` models); this module and its unit
    test doubles are dict-shaped. We normalize ONLY at this ingestion boundary
    so the historical ownership/classification payloads (and the in-memory
    DummyTx doubles) pass through byte-for-byte unchanged - keeping unit
    behaviour identical whichever engine is behind the facade.
    """
    outputs = getattr(tx, "outputs", None)
    if outputs is None:
        return tx
    if outputs and isinstance(outputs[0], dict):
        return tx

    inputs: list[dict[str, Any]] = []
    for i in tx.inputs:
        inputs.append(
            {
                "txid": getattr(i, "txid", None),
                "vout": getattr(i, "vout", None),
                "value": getattr(i, "value", None),
                "value_sats": getattr(i, "value_sats", None),
                "address": getattr(i, "address", None),
                "ownership_status": getattr(
                    i, "ownership_status", OwnershipStatus.UNKNOWN
                ),
                "prev_outpoint": getattr(i, "prev_outpoint", None),
                "spending_txid": getattr(i, "spending_txid", None),
            }
        )
    out_dicts: list[dict[str, Any]] = []
    for o in tx.outputs:
        value_sats = getattr(o, "value_sats", None)
        out_dicts.append(
            {
                "txid": getattr(o, "txid", None),
                "vout": getattr(o, "vout", None),
                "outpoint": getattr(o, "outpoint", None),
                "value": getattr(o, "value", None),
                "value_sats": value_sats,
                "amount_sats": value_sats if value_sats is not None else getattr(o, "value", None),
                "address": getattr(o, "address", None),
                "ownership_status": getattr(
                    o, "ownership_status", OwnershipStatus.UNKNOWN
                ),
                "spend_status": getattr(o, "spend_status", SpendStatus.UNKNOWN),
                "spending_txid": getattr(o, "spending_txid", None),
                "block_height": getattr(o, "block_height", None),
                "block_time": getattr(o, "block_time", None),
            }
        )
    ns = __import__("types").SimpleNamespace(
        txid=getattr(tx, "txid", None),
        inputs=inputs,
        outputs=out_dicts,
        input_total_sats=getattr(tx, "input_total_sats", None),
        output_total_sats=getattr(tx, "output_total_sats", None),
        block_height=getattr(tx, "block_height", None),
        block_time=getattr(tx, "block_time", None),
        fee_sats=getattr(tx, "fee_sats", None),
    )
    return ns


class ExchangeExitValidationError(Exception):
    """Input/blockchain validation failure for an Exchange Exit (HTTP 400)."""


class ExchangeExitConflictError(Exception):
    """The write conflicts with existing Exchange Exit data (HTTP 409)."""


class ExchangeExitNotFoundError(Exception):
    """Requested Exchange Exit does not exist (HTTP 404)."""


_EXIT_TIME_RE = re.compile(r"\d{2}:\d{2}(:\d{2})?")


def _validate_exit_date(value: Any) -> str:
    """Exit date is a required ISO calendar date (YYYY-MM-DD).

    Mirrors the Acquisition date validation; only *input* is validated, stored
    legacy values are read and exported unchanged (CSV-escaped on export).
    """
    text = str(value or "").strip()
    try:
        if date.fromisoformat(text).isoformat() != text:
            raise ValueError()
    except ValueError as exc:
        raise ExchangeExitValidationError("Exit date must be YYYY-MM-DD.") from exc
    return text


def _validate_exit_time(value: Any) -> str | None:
    """Optional exit time as HH:MM or HH:MM:SS (HTML time input format)."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if not _EXIT_TIME_RE.fullmatch(text):
            raise ValueError()
        time.fromisoformat(text)
    except ValueError as exc:
        raise ExchangeExitValidationError("Exit time must be HH:MM or HH:MM:SS.") from exc
    return text


def _parse_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ExchangeExitValidationError(f"Invalid whole number: {value!r}")


def _derive_suggested_exit_date(block_time: Any) -> str | None:
    """Proposed Exchange Exit date for a candidate: the calendar date (UTC) of
    the CONFIRMED block that contains the spending transaction.

    The Exchange Exit boundary is the spending transaction of the last relevant
    self-custody source, so the spending transaction's own block date is the
    only blockchain-derived date that may be proposed. The
    acquisition/purchase date, the later spend of the exchange output and the
    current system time are all explicitly NOT used.

    Returns None when the spending transaction has no confirmed block timestamp
    (unconfirmed/mempool or unresolved block header). A date is never invented
    and never substituted: the UI then leaves the field empty and requires the
    user to choose one.
    """
    if block_time is None or block_time == "":
        return None
    try:
        timestamp = int(block_time)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()


def _derive_fee_sats(
    input_total_sats: int | None,
    output_total_sats: int | None,
    explicit_fee: int | None,
) -> int | None:
    """Miner fee for the exit = input_total - output_total.

    A supplied fee is only a consistency assertion; blockchain totals remain
    authoritative and the value must match exactly.
    """
    if explicit_fee is not None:
        if explicit_fee < 0:
            raise ExchangeExitValidationError("The exit fee cannot be negative.")
        if (
            input_total_sats is not None
            and output_total_sats is not None
            and explicit_fee != input_total_sats - output_total_sats
        ):
            raise ExchangeExitValidationError(
                "The supplied miner fee does not match the blockchain fee "
                "(input_total - output_total)."
            )
        return explicit_fee
    if input_total_sats is not None and output_total_sats is not None:
        implied = input_total_sats - output_total_sats
        if implied < 0:
            raise ExchangeExitValidationError(
                "Exit transaction outputs exceed its inputs; cannot derive a fee."
            )
        return implied
    return None


# ---------------------------------------------------------------------------
# Candidate generation (BLOCKCHAIN_SCOPE_BOUNDARY_V1). An Exchange Exit
# candidate for an Acquisition = a transaction that spends one or more OWNED
# terminal outpoints of that acquisition AND has at least one value-bearing
# output that is NOT provably owned by us (the possible exchange deposit).
# We stop there: the spent/fully-spent downstream of that output (which is not
# ours) is NEVER walked for the acquisition's disposal scope.
# ---------------------------------------------------------------------------


def _classify_output(
    out: dict[str, Any], owned_statuses: set[str]
) -> ExchangeExitCandidateOutput:
    status = out.get("ownership_status") or OwnershipStatus.UNKNOWN
    is_owned = status in owned_statuses
    amount_sats = _parse_optional_int(out.get("amount_sats"))
    possible_deposit = (
        (not is_owned)
        and amount_sats is not None
        and int(amount_sats) > 0
    )
    return ExchangeExitCandidateOutput(
        vout=int(out["vout"]),
        address=out.get("address"),
        amount_sats=amount_sats,
        ownership_status=status,
        possible_deposit=possible_deposit,
    )


async def _resolve_source_ownership(
    db: Any, engine: Any, acquisition_id: int, source_txid: str, source_vout: int
) -> tuple[str, bool, bool]:
    """Strict per-source ownership gate for Exchange Exit creation.

    Returns (ownership_status, is_owned, is_origin) for a source outpoint.
    Unloadable source funding txs are NOT exempt (strict enforcement): they
    resolve as not-owned, so only the narrow UNKNOWN acquisition-origin
    fallback with explicit user confirmation may carry them. This never writes
    anything to the Known Address Registry.
    """
    status = OwnershipStatus.UNKNOWN
    is_owned = False
    try:
        src_tx = _tx_to_dict_payload(await engine.load_transaction(source_txid))
        src_out = next(
            (o for o in src_tx.outputs if int(o["vout"]) == int(source_vout)),
            None,
        )
        if src_out is not None:
            status = src_out.get("ownership_status") or OwnershipStatus.UNKNOWN
            is_owned = status in (
                OwnershipStatus.OWNED,
                OwnershipStatus.LIKELY_OWNED,
            )
    except Exception:
        pass

    is_origin = False
    acq = await db.get_acquisition(acquisition_id)
    if acq is None:
        # P2-5b: exchange_exit_sources has no FK; a source must not reference
        # a non-existent Acquisition.
        raise ExchangeExitValidationError(
            f"Acquisition {acquisition_id} does not exist."
        )
    if acq is not None:
        acq_origin_txid = acq.get("origin_txid")
        acq_origin_vout = acq.get("origin_vout")
        is_origin = (
            acq_origin_txid is not None
            and acq_origin_vout is not None
            and str(acq_origin_txid) == source_txid
            and int(acq_origin_vout) == int(source_vout)
        )
    return status, is_owned, is_origin


async def build_exchange_exit_candidates(
    db: Any, engine: Any, acquisition: Acquisition,
    load_errors: list[ExchangeExitCandidateLoadError] | None = None,
) -> list[ExchangeExitCandidate]:
    """Build candidate Exchange Exit transactions for an Acquisition anchor.

    ``load_errors`` (FIX-UI-6): when the caller passes a list, every candidate
    or source transaction the node could not deliver is reported there as
    (txid, reason) instead of only being skipped silently. Candidate discovery
    and filters are unchanged; successfully loaded candidates are returned
    either way and one unloadable transaction never fails the whole call.
    """

    def _report_load_error(txid: str, exc: Exception) -> None:
        if load_errors is None or any(e.txid == txid for e in load_errors):
            return
        load_errors.append(ExchangeExitCandidateLoadError(
            txid=txid, reason=str(exc) or type(exc).__name__))

    descendants = await acquisition_descendants_for_candidate(
        db, engine, acquisition
    )
    if not descendants:
        return []

    # Scope: only ONE hop. The spending txs of the acquisition's owned terminal
    # outpoints are candidate exits - NOT their downstream spends.
    # Collect the candidate spending txids in discovery order.
    candidate_txids: dict[str, None] = {}
    owned_statuses = {OwnershipStatus.OWNED, OwnershipStatus.LIKELY_OWNED}

    # The Acquisition origin (trace start) is DIRECTLY tied to tracked
    # provenance even when its ownership status is UNKNOWN (TRACE TARGET !=
    # OWNERSHIP EVIDENCE): the start outpoint was a traversal seed, not an
    # ownership statement. It therefore qualifies as an exit source for THIS
    # acquisition alone, subject to explicit user confirmation
    # (ownership_confirmed). No arbitrary UNKNOWN spent terminal ever does.
    origin_txid = getattr(acquisition, "origin_txid", None)
    origin_vout = getattr(acquisition, "origin_vout", None)

    def _terminal_is_origin(term_txid: str, term_vout: int) -> bool:
        return (
            origin_txid is not None
            and origin_vout is not None
            and str(origin_txid) == str(term_txid)
            and int(origin_vout) == int(term_vout)
        )

    # Re-derive the terminal leaves + spent leaves directly (the summary does
    # not carry enough to resolve each spending tx's output layout without
    # reloading, but acquisition_descendant_summary already gives spent_terminals
    # with spending_txid - reuse it so we never fabricate candidate txids).
    summary = await acquisition_descendant_summary_result(db, engine, acquisition)
    covered_keys: set[tuple[str, int]] = set()
    candidate_sources = {(term["txid"], term["vout"]): term for term in
                         summary.spent_terminals + summary.unresolved_spends
                         + summary.owned_spends}
    for term in candidate_sources.values():
        spending_txid = term.get("spending_txid")
        if not spending_txid:
            continue

        # ONE-HOP-TOO-FAR FIX: a spent_terminal that is not an OWNED/LIKELY_OWNED
        # output of ours is a downstream non-owned output (e.g. the exchange's
        # later spend of the deposit). Only OWNED source outpoints - plus the
        # Acquisition's own origin outpoint (narrow UNKNOWN fallback, above) -
        # define an Exchange Exit boundary for this acquisition.
        try:
            src_tx = _tx_to_dict_payload(await engine.load_transaction(term["txid"]))
        except Exception as exc:
            # The source tx could not be loaded, so this spending tx cannot be
            # qualified as a candidate. Report instead of skipping silently.
            _report_load_error(term["txid"], exc)
            continue
        try:
            src_out = next(
                (o for o in src_tx.outputs if int(o["vout"]) == int(term["vout"])),
                None,
            )
            if src_out is None:
                continue
            term_status = (
                src_out.get("ownership_status") or OwnershipStatus.UNKNOWN
            )
            term_owned = term_status in owned_statuses
            term_origin = _terminal_is_origin(term["txid"], int(term["vout"]))
            if not (term_owned or term_origin):
                continue
        except Exception:
            continue

        candidate_txids.setdefault(spending_txid)
        covered_keys.add((term["txid"], int(term["vout"])))

    # The origin outpoint is often a trace SOURCE (its edge points into the
    # exit's deposit output), so it is NOT a leaf and never appears in
    # spent_terminals. Resolve its spender directly from persisted spend facts
    # and offer the exit as a candidate. Its later spend is never walked
    # (one-hop-too-far), and any spending tx with only owned outputs is still
    # filtered out below as not-an-exit.
    if (
        origin_txid is not None
        and origin_vout is not None
        and (origin_txid, int(origin_vout)) not in covered_keys
    ):
        origin_spend = await db.get_spend(origin_txid, int(origin_vout))
        origin_spending_txid = (
            origin_spend.get("spending_txid") if origin_spend else None
        )
        if origin_spending_txid:
            origin_amount = getattr(acquisition, "origin_amount_sats", None)
            if origin_amount is None:
                try:
                    origin_tx = _tx_to_dict_payload(await engine.load_transaction(origin_txid))
                    origin_out = next(
                        (
                            o
                            for o in origin_tx.outputs
                            if int(o["vout"]) == int(origin_vout)
                        ),
                        None,
                    )
                    if origin_out is not None:
                        origin_amount = origin_out.get("amount_sats")
                except Exception:
                    origin_amount = None
            if origin_amount is not None:
                # A resolvable origin amount is required to price the candidate
                # input; without it no candidate is offered.
                candidate_txids.setdefault(origin_spending_txid)

    if not candidate_txids:
        return []

    # FX-1: the entry Acquisition only DISCOVERS the candidate transactions
    # above. Their sources are always every input of the exit transaction, with
    # class and anchors from the same provenance evaluation as
    # exit_provenance_rows, so every involved Acquisition sees the same list.
    loaded: dict[str, Any] = {}
    for spending_txid in candidate_txids:
        # FX-2: a recorded Exit is never offered again, whether this
        # Acquisition is an attributed source or only an unattributed anchor.
        if await db.get_exchange_exit_by_txid(spending_txid) is not None:
            continue
        try:
            loaded[spending_txid] = _tx_to_dict_payload(
                await engine.load_transaction(spending_txid)
            )
        except Exception as exc:
            # Candidate tx could not be loaded (e.g. mempool race, missing on
            # the node). Report (FIX-UI-6) rather than skip silently; never
            # fabricate an exit from partial data or fail the whole response.
            _report_load_error(spending_txid, exc)
            continue
    inputs_by_exit = {
        txid: [(i["txid"], int(i["vout"])) for i in tx.inputs]
        for txid, tx in loaded.items()
    }
    per_input, per_exit = await _exit_input_evidence(db, engine, inputs_by_exit)

    result: list[ExchangeExitCandidate] = []
    for spending_txid, tx in loaded.items():
        output_candidates = [_classify_output(o, owned_statuses) for o in tx.outputs]
        has_deposit = any(o.possible_deposit for o in output_candidates)
        if not has_deposit:
            # Every value output is provably owned by us -> an internal
            # consolidation/merge, NOT an exit. Never suggested.
            continue

        input_candidates = [
            await _candidate_input(db, engine, tx_input, per_input[key], owned_statuses)
            for tx_input, key in zip(tx.inputs, inputs_by_exit[spending_txid])
        ]

        # Involved sats per acquisition = the amounts of the inputs the
        # evaluation attributes to exactly that Acquisition (economic quantity
        # heading to the exchange, before miner fee split). Inputs without a
        # single Acquisition anchor are shown but counted for none.
        acquisition_candidates: list[ExchangeExitCandidateAcquisition] = []
        for i in input_candidates:
            if i.acquisition_id is None:
                continue
            entry = next(
                (a for a in acquisition_candidates if a.acquisition_id == i.acquisition_id),
                None,
            )
            if entry is None:
                entry = ExchangeExitCandidateAcquisition(
                    acquisition_id=i.acquisition_id,
                    acquisition_date=i.acquisition_date,
                    source=i.source,
                )
                acquisition_candidates.append(entry)
            entry.involved_sats += int(i.amount_sats or 0)

        result.append(
            ExchangeExitCandidate(
                txid=spending_txid,
                block_height=getattr(tx, "block_height", None),
                block_time=getattr(tx, "block_time", None),
                suggested_exit_date=_derive_suggested_exit_date(
                    getattr(tx, "block_time", None)
                ),
                inputs=input_candidates,
                outputs=output_candidates,
                fee_sats=_derive_fee_sats(
                    getattr(tx, "input_total_sats", None),
                    getattr(tx, "output_total_sats", None),
                    None,
                ),
                acquisitions=acquisition_candidates,
                mixed=len(acquisition_candidates) > 1,
                isolation_ok=bool(
                    output_candidates
                    and len([o for o in output_candidates if o.possible_deposit]) == 1
                ),
                provenance_class=per_exit[spending_txid].provenance_class,
            )
        )

    # FIX-EXIT-5: candidates are offered chronologically ascending (block
    # height, then txid); unconfirmed candidates (no block) come last. Pure
    # ordering - filters and contents are unchanged.
    result.sort(
        key=lambda c: (c.block_height is None, c.block_height or 0, c.txid)
    )
    return result


async def _exit_input_evidence(
    db: Any, engine: Any, inputs_by_exit: dict[str, list[tuple[str, int]]]
) -> tuple[dict, dict]:
    """Read-only provenance evidence for candidate exit inputs (FX-1)."""
    import asyncio
    import time

    from backend.config import get_settings
    from backend.reporting.exit_provenance import exit_input_evidence
    from backend.reporting.read_data import ReadBudgetExceeded

    settings = get_settings()
    deadline = time.monotonic() + settings.coverage_timeout
    try:
        async with asyncio.timeout(settings.coverage_timeout):
            return await exit_input_evidence(
                db, engine, inputs_by_exit, settings, deadline
            )
    except TimeoutError as exc:
        raise ReadBudgetExceeded(
            "Exit candidate provenance budget exceeded; retry."
        ) from exc


def _single_acquisition(evidence: Any) -> bool:
    """The evaluation names exactly one Acquisition and nothing else (no
    No-Acquisition anchor, no other unresolved origin)."""
    return (
        len(evidence.acquisitions) == 1
        and not evidence.no_acquisition_anchors
        and evidence.unresolved_origins <= evidence.acquisition_origins
    )


async def _reject_sources_without_single_acquisition(
    db: Any, engine: Any, txid: str, keys: list[tuple[str, int]]
) -> None:
    """FX-2 on the server: a source with an amount must be an input the
    evaluation attributes to exactly one Acquisition, read like the editor
    and ``exit_provenance_rows``. A MIXED or multi-Acquisition input is
    saved without an amount (left out of the sources), never under one
    Acquisition. Applies to creation only; stored exits are not re-checked.
    """
    if not keys:
        return
    per_input, _ = await _exit_input_evidence(db, engine, {txid: keys})
    for key in keys:
        evidence = per_input[key]
        if evidence.acquisitions and not _single_acquisition(evidence):
            raise ExchangeExitConflictError(
                f"Exit input {key[0]}:{key[1]} is not attributed to a single "
                f"Acquisition ({evidence.provenance_class}, anchors "
                f"{sorted(evidence.acquisitions)}); it cannot carry an amount "
                "under one Acquisition. Save it without an amount."
            )


async def _candidate_input(
    db: Any,
    engine: Any,
    tx_input: dict[str, Any],
    evidence: Any,
    owned_statuses: set[str],
    *,
    persist_ownership: bool = True,
) -> ExchangeExitCandidateInput:
    """One exit input with its evaluated class and anchors (FX-1).

    The input becomes an own source of an Acquisition only when the evaluation
    names exactly that one Acquisition and nothing else (no No-Acquisition
    anchor, no other unresolved origin), and the input is OWNED/LIKELY_OWNED or
    is that Acquisition's own origin outpoint (narrow UNKNOWN fallback with
    user confirmation). No other attribution is invented: a MIXED or
    multi-Acquisition input shows all anchors and belongs to none.
    """
    txid, vout = tx_input["txid"], int(tx_input["vout"])
    status = OwnershipStatus.UNKNOWN
    amount_sats = _parse_optional_int(tx_input.get("value_sats"))
    if amount_sats is None:
        amount_sats = _parse_optional_int(tx_input.get("value"))
    address = tx_input.get("address")
    try:
        # The write path keeps the engine default; reads pass False (B5-02).
        options = {} if persist_ownership else {"persist_ownership": False}
        src_tx = _tx_to_dict_payload(await engine.load_transaction(txid, **options))
        src_out = next((o for o in src_tx.outputs if int(o["vout"]) == vout), None)
        if src_out is not None:
            status = src_out.get("ownership_status") or OwnershipStatus.UNKNOWN
            if amount_sats is None:
                amount_sats = _parse_optional_int(src_out.get("amount_sats"))
            address = address or src_out.get("address")
    except Exception:
        pass

    acquisition = None
    ownership_confirmed = False
    if _single_acquisition(evidence):
        (aid,) = evidence.acquisitions
        row = await db.get_acquisition(aid)
        is_origin = row is not None and (
            row.get("origin_txid") == txid
            and row.get("origin_vout") is not None
            and int(row["origin_vout"]) == vout
        )
        if row is not None and (status in owned_statuses or is_origin):
            acquisition = row
            ownership_confirmed = is_origin and status not in owned_statuses

    return ExchangeExitCandidateInput(
        txid=txid,
        vout=vout,
        amount_sats=amount_sats,
        address=address,
        acquisition_id=acquisition["id"] if acquisition else None,
        acquisition_date=acquisition["acquisition_date"] if acquisition else None,
        source=acquisition.get("source") if acquisition else None,
        ownership_status=status,
        ownership_confirmed=ownership_confirmed,
        provenance_class=evidence.provenance_class,
        anchor_acquisition_ids=sorted(evidence.acquisitions),
        no_acquisition_anchor_outpoints=[
            f"{k[0]}:{k[1]}" for k in sorted(evidence.no_acquisition_anchors)
        ],
        # FX-2: anchored OWNED input attributed to no single Acquisition.
        # EXTERNAL/UNKNOWN inputs are never a source, attributed or not.
        unattributed=(
            acquisition is None
            and bool(evidence.acquisitions)
            and status in owned_statuses
        ),
    )


async def _unattributed_inputs(
    db: Any, engine: Any, txid: str, tx: Any, source_keys: set[tuple[str, int]]
) -> tuple[list[dict[str, Any]], int]:
    """FX-2: exit inputs outside the payload sources, read like the editor.

    Returns the rows to store for unattributed inputs (one per Acquisition
    anchor, no amount) and the sats of all inputs that are attributed to no
    single Acquisition (unattributed, EXTERNAL/UNKNOWN, unanchored). An input
    the evaluation attributes to one Acquisition but the payload left out adds
    nothing: it stays a FX-1 source and is never stored as unattributed.
    """
    others = [i for i in tx.inputs if (i["txid"], int(i["vout"])) not in source_keys]
    if not others:
        return [], 0
    keys = [(i["txid"], int(i["vout"])) for i in others]
    per_input, _ = await _exit_input_evidence(db, engine, {txid: keys})
    owned_statuses = {OwnershipStatus.OWNED, OwnershipStatus.LIKELY_OWNED}
    rows: list[dict[str, Any]] = []
    unattributed_sats = 0
    for tx_input, key in zip(others, keys):
        candidate = await _candidate_input(
            db, engine, tx_input, per_input[key], owned_statuses
        )
        if candidate.acquisition_id is not None:
            continue
        if candidate.amount_sats is None:
            raise ExchangeExitValidationError(
                f"Exit input {key[0]}:{key[1]} has no resolvable amount."
            )
        unattributed_sats += candidate.amount_sats
        if not candidate.unattributed:
            continue
        rows.extend(
            {
                "acquisition_id": aid,
                "source_txid": key[0],
                "source_vout": key[1],
                "source_amount_sats": candidate.amount_sats,
                "disposed_sats": 0,
                "network_fee_sats": 0,
                "ownership_status": candidate.ownership_status,
                "ownership_confirmed": 0,
            }
            for aid in candidate.anchor_acquisition_ids
        )
    return rows, unattributed_sats


async def acquisition_descendants_for_candidate(
    db: Any, engine: Any, acquisition: Acquisition
) -> AcquisitionDescendant | None:
    """Return the acquisition's current descendant summary (used to scope
    candidate exits to known owned terminals)."""
    return await acquisition_descendant_summary_result(db, engine, acquisition)


async def acquisition_descendant_summary_result(
    db: Any, engine: Any, acquisition: Acquisition
) -> AcquisitionDescendant | None:
    from backend.acquisitions.service import acquisition_descendant_summary

    try:
        # acquisition_descendant_summary expects a single row dict, not the
        # Acquisition response model (which has no .get()). Resolve the row so
        # the real endpoint can actually surface candidates.
        row = await db.get_acquisition(acquisition.id)
        if row is None:
            return None
        return await acquisition_descendant_summary(db, engine, row)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Direct (single-acquisition) exit creation
# ---------------------------------------------------------------------------


async def create_exchange_exit(
    db: Any, engine: Any, payload: ExchangeExitCreate
) -> ExchangeExit:
    """Create a confirmed Exchange Exit + its on-chain source/output rows.

    One or more source outpoints (each owned, already linked to acquisitions)
    must be provided. The exit tx must be a real, loadable transaction whose
    inputs include all the sources and whose outputs include the confirmed
    exchange deposits.
    """
    try:
        txid = canonical_txid(payload.txid or "")
    except ValueError:
        raise ExchangeExitValidationError(
            "Exchange Exit txid must be a 64-character hex string."
        ) from None

    exit_date = _validate_exit_date(payload.exit_date)
    exit_time = _validate_exit_time(payload.exit_time)

    try:
        tx = _tx_to_dict_payload(await engine.load_transaction(txid))
    except Exception as exc:
        raise ExchangeExitValidationError(
            f"Cannot load exchange exit transaction {txid}: {exc}"
        ) from exc

    # Validate the exit's owned inputs (sources) against the loaded transaction.
    tx_inputs = {(i["txid"], i["vout"]): i for i in tx.inputs}
    source_rows: list[dict[str, Any]] = []
    alloc_pairs: list[tuple] = []
    evidence_rows: list[dict[str, Any]] = []
    disposed_total_sats = 0
    network_fee_total_sats = 0

    for src in payload.sources:
        try:
            source_txid = canonical_txid(src.source_txid or "")
        except ValueError:
            raise ExchangeExitValidationError(
                f"Invalid source outpoint txid: {src.source_txid!r}"
            ) from None
        source_vout = src.source_vout
        input_key = (source_txid, source_vout)
        tx_input = tx_inputs.get(input_key)
        if tx_input is None:
            raise ExchangeExitValidationError(
                f"Source outpoint {source_txid}:{source_vout} is not an input of "
                f"exit transaction {txid}."
            )
        source_amount_sats = _parse_optional_int(tx_input.get("value_sats"))
        if source_amount_sats is None:
            source_amount_sats = _parse_optional_int(tx_input.get("value"))
        if source_amount_sats is None or source_amount_sats <= 0:
            raise ExchangeExitValidationError(
                f"Source outpoint {source_txid}:{source_vout} has no resolvable amount."
            )
        disposed_sats = src.disposed_sats
        network_fee_sats = src.network_fee_sats or 0
        if disposed_sats <= 0:
            raise ExchangeExitValidationError(
                "Each source must dispose a positive number of sats."
            )
        if disposed_sats + network_fee_sats > source_amount_sats:
            raise ExchangeExitValidationError(
                f"Source {source_txid}:{source_vout} cannot dispose "
                f"{disposed_sats} sats + fee {network_fee_sats} sats: only "
                f"{source_amount_sats} sats are available."
            )
        # Strict per-source ownership gate (v0.11.2): every source must be
        # provably owned (OWNED/LIKELY_OWNED source output) OR the acquisition's
        # own origin outpoint with explicit user confirmation of self-custody
        # (ownership_confirmed). Unloadable sources are not exempt. The
        # confirmation is per-Exit only and is never written to the Known
        # Address Registry; the historical ownership classification stays as-is.
        source_status, source_is_owned, source_is_origin = (
            await _resolve_source_ownership(
                db, engine, src.acquisition_id, source_txid, source_vout
            )
        )
        if not source_is_owned:
            if not source_is_origin:
                raise ExchangeExitValidationError(
                    f"Source outpoint {source_txid}:{source_vout} is neither an "
                    "OWNED output nor the acquisition's own origin outpoint; "
                    "refusing to record the Exchange Exit."
                )
            if not src.ownership_confirmed:
                raise ExchangeExitValidationError(
                    f"Source outpoint {source_txid}:{source_vout} ownership is "
                    "not independently established; confirm self-custody of "
                    "this origin outpoint to record the Exchange Exit."
                )
        disposed_total_sats += disposed_sats
        network_fee_total_sats += network_fee_sats
        source_rows.append(
            {
                "acquisition_id": src.acquisition_id,
                "source_txid": source_txid,
                "source_vout": source_vout,
                "source_amount_sats": source_amount_sats,
                "disposed_sats": disposed_sats,
                "network_fee_sats": network_fee_sats,
                "ownership_status": source_status,
                # 1 only when the source relied on the UNKNOWN-origin user
                # confirmation; established OWNED sources stay 0 so read-back
                # never conflates blockchain evidence with a confirmation.
                "ownership_confirmed": 1
                if (source_is_origin and not source_is_owned and src.ownership_confirmed)
                else 0,
            }
        )
        # Allocation drives the acquisition's remaining self-custody position;
        # the fee split is accounting metadata, not per-input chain evidence.
        alloc_pairs.append(
            {
                "acquisition_id": src.acquisition_id,
                "allocated_sats": disposed_sats + network_fee_sats,
                "disposed_sats": disposed_sats,
                "network_fee_sats": network_fee_sats,
            }
        )
        evidence_rows.append(
            {
                "source_txid": source_txid,
                "source_vout": source_vout,
                "spending_txid": txid,
            }
        )

    await _reject_sources_without_single_acquisition(
        db, engine, txid,
        [(r["source_txid"], r["source_vout"]) for r in source_rows],
    )
    unattributed_rows, unattributed_sats = await _unattributed_inputs(
        db, engine, txid, tx,
        {(r["source_txid"], r["source_vout"]) for r in source_rows},
    )
    if not source_rows and not unattributed_rows:
        raise ExchangeExitValidationError(
            "At least one owned source outpoint (input) is required."
        )

    # Optionally require the supplied sources to cover ALL owned inputs of the
    # exit so no owned sats escape the boundary unaccounted. Simplest v1 rule:
    # they must at least be a subset; a stricter all-owned-inputs check would
    # need the acquisition's full trace, which we deliberately leave to the
    # Disposal allocation validator.
    # (Leave permissive in v1 - users may exit from multiple acquisitions in
    # one tx, so not every owned input must belong to the same acquisition.)

    # Verify the exchange deposit outputs are real value-bearing outputs of the
    # exit and mark them on the outputs table.
    tx_outputs = {int(o["vout"]): o for o in tx.outputs}
    confirmed_deposits: list[dict[str, Any]] = []
    selected_vouts: set[int] = set()
    for sel in payload.output_selection:
        if not sel.is_exchange_deposit:
            continue
        vout = sel.vout
        if vout in selected_vouts:
            raise ExchangeExitValidationError(
                f"Deposit output vout {vout} was selected more than once."
            )
        selected_vouts.add(vout)
        out = tx_outputs.get(vout)
        if out is None:
            raise ExchangeExitValidationError(
                f"Selected deposit output vout {vout} does not exist in exit {txid}."
            )
        amount_sats = _parse_optional_int(out.get("amount_sats"))
        if amount_sats is None or amount_sats <= 0:
            raise ExchangeExitValidationError(
                f"Selected deposit output vout {vout} has no resolvable amount."
            )
        ownership_status = out.get("ownership_status") or OwnershipStatus.UNKNOWN
        if ownership_status in {OwnershipStatus.OWNED, OwnershipStatus.LIKELY_OWNED}:
            raise ExchangeExitValidationError(
                f"Selected deposit output vout {vout} is owned change, not an "
                "exchange deposit candidate."
            )
        confirmed_deposits.append(
            {
                "vout": vout,
                "address": out.get("address"),
                "amount_sats": amount_sats,
                "ownership_status": ownership_status,
                "is_exchange_deposit": 1,
            }
        )

    if not confirmed_deposits:
        raise ExchangeExitValidationError(
            "At least one exchange deposit output must be selected."
        )
    transferred_sats = sum(d["amount_sats"] for d in confirmed_deposits)
    # FX-2: the part not attributed to an Acquisition may only come from exit
    # inputs that are attributed to no single Acquisition. Without such inputs
    # the attributed sources must cover the deposit exactly (FX-1).
    if disposed_total_sats > transferred_sats or (
        disposed_total_sats != transferred_sats and not unattributed_sats
    ):
        raise ExchangeExitValidationError(
            "Transferred sats must equal the sum of selected exchange deposit outputs."
        )

    input_total = _parse_optional_int(getattr(tx, "input_total_sats", None))
    output_total = _parse_optional_int(getattr(tx, "output_total_sats", None))
    fee_sats = _derive_fee_sats(input_total, output_total, payload.fee_sats)
    if fee_sats is None:
        raise ExchangeExitValidationError(
            "The complete transaction input and output totals are required to derive the miner fee."
        )
    if network_fee_total_sats > fee_sats or (
        network_fee_total_sats != fee_sats and not unattributed_sats
    ):
        raise ExchangeExitValidationError(
            "Per-acquisition miner-fee allocations must sum to the full transaction fee."
        )
    not_attributed_sats = (
        transferred_sats - disposed_total_sats + fee_sats - network_fee_total_sats
    )
    if not_attributed_sats > unattributed_sats:
        raise ExchangeExitValidationError(
            f"{not_attributed_sats} sats of the deposit and fee are not attributed "
            f"to an Acquisition, but the exit inputs that are attributed to no "
            f"single Acquisition hold only {unattributed_sats} sats."
        )
    source_rows += unattributed_rows

    # Any owned input not listed as a source is tracked as retained change if it
    # is a value output that is still UNSPENT by us (kept, not disposed). For v1
    # we store ONLY the confirmed deposits + mark ownership on all outputs.
    exit_output_rows: list[dict[str, Any]] = []
    for out in tx.outputs:
        o = _classify_output(out, {OwnershipStatus.OWNED, OwnershipStatus.LIKELY_OWNED})
        exit_output_rows.append(
            {
                "vout": o.vout,
                "address": out.get("address"),
                "amount_sats": o.amount_sats,
                "ownership_status": o.ownership_status,
                "is_exchange_deposit": 1
                if any(d["vout"] == o.vout for d in confirmed_deposits)
                and next(
                    (d for d in confirmed_deposits if d["vout"] == o.vout),
                    None,
                )["is_exchange_deposit"]
                else 0,
            }
        )

    # Create the exit + its source/output rows as ONE isolated transaction
    # (P1-9, L-06): a failure in between never leaves an Exit without its
    # sources (allocation) or deposit outputs (position boundary).
    try:
        async with db.write_transaction() as tx_db:
            # P2-5b: duplicate-TXID and per-Acquisition cap checks run under
            # the write lock (BEGIN IMMEDIATE), so concurrent creations cannot
            # both pass. RPC work stays above.
            await _check_exit_unique_and_cap(tx_db, txid, source_rows)
            exit_row = await tx_db.create_exchange_exit(
                commit=False,
                txid=txid,
                exit_date=exit_date,
                exit_time=exit_time,
                broker=payload.broker,
                reference=payload.reference,
                notes=payload.notes,
                block_height=getattr(tx, "block_height", None),
                block_time=getattr(tx, "block_time", None),
                input_total_sats=input_total,
                output_total_sats=output_total,
                fee_sats=fee_sats,
                verified=1 if tx is not None else 0,
            )
            await tx_db.replace_exchange_exit_sources(
                exit_row["id"], source_rows, commit=False
            )
            await tx_db.replace_exchange_exit_outputs(
                exit_row["id"], exit_output_rows, commit=False
            )
    except sqlite3.IntegrityError as exc:
        # P2-N3-Rest fallback: a write path that bypassed the P2-5b check hits
        # the DB-level unique index (ux_exchange_exits_txid, reported by
        # SQLite as the column); map it to the SAME 409 conflict.
        if "exchange_exits.txid" not in str(exc):
            raise
        other = await db.get_other_exchange_exit_by_txid(txid)
        if other is None:
            raise
        raise ExchangeExitConflictError(
            f"Exchange Exit {other['id']} already uses exit transaction {txid}."
        ) from exc

    # A Wallet Exit is an OWNERSHIP-boundary record (EXIT != SALE, EXIT !=
    # DISPOSAL): recorded once, it NEVER creates, links or invents a Disposal.
    # A Disposal is a separate, explicitly-managed SALE domain record created
    # by the user through the disposal workflow; an Exit is the on-chain
    # Ownership-evidence documentation only (sources/outputs/evidence rows).
    # No economic fields are ever derived here and no linked disposal_id is
    # back-patched onto the exit.
    return await _get_exchange_exit(db, exit_row["id"])


async def _check_exit_unique_and_cap(
    db: Any, txid: str, source_rows: list[dict[str, Any]]
) -> None:
    """P2-5b: reject a second Exit for the same exit TXID and Exit-source
    allocations beyond the Acquisition amount (summed over all Exits).
    Disposals are not counted. Raises ``ExchangeExitConflictError`` (409);
    a vanished Acquisition raises ``ExchangeExitValidationError`` (400)."""
    other = await db.get_other_exchange_exit_by_txid(txid)
    if other is not None:
        raise ExchangeExitConflictError(
            f"Exchange Exit {other['id']} already uses exit transaction {txid}."
        )
    requested: dict[int, int] = {}
    for row in source_rows:
        requested[row["acquisition_id"]] = (
            requested.get(row["acquisition_id"], 0)
            + row["disposed_sats"]
            + row["network_fee_sats"]
        )
    for acq_id, sats in requested.items():
        acq = await db.get_acquisition(acq_id)
        if acq is None:
            raise ExchangeExitValidationError(
                f"Acquisition {acq_id} does not exist."
            )
        acq_sats = _btc_sats(acq["btc_amount"])
        already = await db.get_exit_allocated_sats(acq_id)
        if already + sats > acq_sats:
            raise ExchangeExitConflictError(
                f"Exchange Exit allocations of acquisition {acq_id} would "
                f"exceed its amount: {already} sats already allocated by other "
                f"Exits + {sats} sats requested > {acq_sats} sats."
            )


async def _get_exit_outputs_with_conflicts(
    db: Any, exit_id: int
) -> list[ExchangeExitOutput]:
    rows = await db.get_exchange_exit_outputs(exit_id)
    result: list[ExchangeExitOutput] = []
    for row in rows:
        conflict = False
        if row.get("is_exchange_deposit") and row.get("address"):
            conflict = await db.get_known_address(row["address"]) is not None
        result.append(ExchangeExitOutput(**row, ownership_conflict=conflict))
    return result


async def _get_exchange_exit(db: Any, exit_id: int) -> ExchangeExit:
    row = await db.get_exchange_exit(exit_id)
    if row is None:
        raise ExchangeExitNotFoundError(f"Exchange Exit {exit_id} not found.")
    sources = await db.get_exchange_exit_sources(exit_id)
    outputs = await _get_exit_outputs_with_conflicts(db, exit_id)
    return ExchangeExit(
        id=row["id"],
        txid=row["txid"],
        exit_date=row["exit_date"],
        exit_time=row.get("exit_time"),
        broker=_strip_optional(row.get("broker")),
        reference=_strip_optional(row.get("reference")),
        notes=_strip_optional(row.get("notes")),
        block_height=row.get("block_height"),
        block_time=row.get("block_time"),
        input_total_sats=row.get("input_total_sats"),
        output_total_sats=row.get("output_total_sats"),
        fee_sats=row.get("fee_sats"),
        verified=row.get("verified") if "verified" in row else row.get("verification_ok", False),
        disposal_id=row.get("disposal_id"),
        sources=[ExchangeExitSource(**s) for s in sources],
        outputs=outputs,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def get_exchange_exit(
    db: Any, exit_id: int, engine: Any | None = None
) -> ExchangeExit:
    """One Exit; with an engine also the read-only FX-2 allocation check."""
    exit_ = await _get_exchange_exit(db, exit_id)
    if engine is not None:
        exit_.allocation_check, exit_.allocation_differences = (
            await stored_allocation_check(db, engine, exit_)
        )
    return exit_


def _short_outpoint(txid: str, vout: int) -> str:
    return f"{txid[:12]}…:{vout}"


def _evaluated_text(candidate: ExchangeExitCandidateInput) -> str:
    anchors = [f"#{aid}" for aid in candidate.anchor_acquisition_ids] + [
        _short_outpoint(*op.rsplit(":", 1))
        for op in candidate.no_acquisition_anchor_outpoints
    ]
    return (candidate.provenance_class or "UNRESOLVED") + (
        ": " + ", ".join(anchors) if anchors else ""
    )


def _stored_text(attributed: list[int], anchors: list[int]) -> str:
    parts = [f"attributed to Acquisition #{aid}" for aid in attributed]
    if anchors:
        parts.append(
            "not attributed (anchors " + ", ".join(f"#{aid}" for aid in anchors) + ")"
        )
    return " and ".join(parts)


async def stored_allocation_check(
    db: Any, engine: Any, exit_: ExchangeExit
) -> tuple[str, list[str]]:
    """FX-2, read-only: compare the stored attribution of an Exit with the
    current provenance evaluation of its inputs (the editor's reading).

    Reports differences such as a MIXED input stored under one Acquisition
    (legacy) or a unique input missing from the Exit. Never writes; whether
    and how to re-save is left to the user.
    """
    try:
        # A plain read: never persist ownership reconciliation (L-10, B5-02).
        tx = _tx_to_dict_payload(
            await engine.load_transaction(exit_.txid, persist_ownership=False)
        )
        keys = [(i["txid"], int(i["vout"])) for i in tx.inputs]
        per_input, _ = await _exit_input_evidence(db, engine, {exit_.txid: keys})
        owned_statuses = {OwnershipStatus.OWNED, OwnershipStatus.LIKELY_OWNED}
        candidates = {
            key: await _candidate_input(
                db, engine, tx_input, per_input[key], owned_statuses,
                persist_ownership=False,
            )
            for tx_input, key in zip(tx.inputs, keys)
        }
    except Exception:
        # Advisory only: an unavailable evaluation never blocks reading.
        return "UNAVAILABLE", []

    stored: dict[tuple[str, int], tuple[list[int], list[int]]] = {}
    for src in exit_.sources:
        attributed, anchors = stored.setdefault(
            (src.source_txid, src.source_vout), ([], [])
        )
        if src.disposed_sats + src.network_fee_sats > 0:
            attributed.append(src.acquisition_id)
        else:
            anchors.append(src.acquisition_id)

    notes: list[str] = []
    for key in sorted(stored.keys() - candidates.keys()):
        notes.append(
            f"{_short_outpoint(*key)}: stored, but not an input of the exit transaction."
        )
    for key, c in candidates.items():
        attributed, anchors = (sorted(x) for x in stored.get(key, ([], [])))
        recorded = bool(attributed or anchors)
        where = _short_outpoint(*key)
        if c.acquisition_id is not None:
            if attributed == [c.acquisition_id] and not anchors:
                continue
            notes.append(
                f"{where}: stored as {_stored_text(attributed, anchors)}; the on-chain "
                f"evaluation attributes it to Acquisition #{c.acquisition_id}."
                if recorded else
                f"{where}: the on-chain evaluation attributes it to Acquisition "
                f"#{c.acquisition_id}; it is not recorded in this Exit."
            )
        elif c.unattributed:
            if not attributed and anchors == c.anchor_acquisition_ids:
                continue
            notes.append(
                f"{where}: stored as {_stored_text(attributed, anchors)}; the on-chain "
                f"evaluation reads it as not attributed ({_evaluated_text(c)})."
                if recorded else
                f"{where}: not attributed ({_evaluated_text(c)}) per the on-chain "
                "evaluation; it is not recorded in this Exit."
            )
        elif recorded:
            notes.append(
                f"{where}: stored as {_stored_text(attributed, anchors)}; the on-chain "
                f"evaluation reads it as not a source ({_evaluated_text(c)}, "
                f"{c.ownership_status})."
            )
    return ("DIFFERS" if notes else "MATCHES"), notes


async def get_exchange_exit_by_txid(db: Any, txid: str) -> ExchangeExit | None:
    try:
        txid = canonical_txid(txid)
    except ValueError:
        return None
    row = await db.get_exchange_exit_by_txid(txid)
    if row is None:
        return None
    return await _get_exchange_exit(db, row["id"])


async def list_exchange_exits(db: Any) -> list[ExchangeExit]:
    rows = await db.list_exchange_exits()
    result: list[ExchangeExit] = []
    for row in rows:
        sources = await db.get_exchange_exit_sources(row["id"])
        outputs = await _get_exit_outputs_with_conflicts(db, row["id"])
        result.append(
            ExchangeExit(
                id=row["id"],
                txid=row["txid"],
                exit_date=row["exit_date"],
                exit_time=row.get("exit_time"),
                broker=_strip_optional(row.get("broker")),
                reference=_strip_optional(row.get("reference")),
                notes=_strip_optional(row.get("notes")),
                block_height=row.get("block_height"),
                block_time=row.get("block_time"),
                input_total_sats=row.get("input_total_sats"),
                output_total_sats=row.get("output_total_sats"),
                fee_sats=row.get("fee_sats"),
                verified=row.get("verified") if "verified" in row else row.get("verification_ok", False),
                disposal_id=row.get("disposal_id"),
                sources=[ExchangeExitSource(**s) for s in sources],
                outputs=outputs,
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        )
    return result


# B5-10: total time for the back-fill of one call (GETs run it outside any
# report timeout); Exits not reached stay NULL until the next call.
FX8_REFRESH_BUDGET = 10.0
# (exit id, txid, kind) already warned about in this process: one line each.
_fx8_warned: set[tuple[int, str, str]] = set()


def _warn_fx8_once(row: dict[str, Any], kind: str, message: str, *args: Any) -> None:
    key = (row["id"], row["txid"], kind)
    if key not in _fx8_warned:
        _fx8_warned.add(key)
        logger.warning(message, row["id"], row["txid"], *args)


async def refresh_unconfirmed_exit_blocks(db: Any, engine: Any) -> int:
    """FX-8 (owner decision 2026-10-29): back-fill block height/time of Exits
    recorded while their exit transaction was unconfirmed.

    Runs before every year filter (exit-years list, CSV v2 export, Blockchain
    History Report). Only Exits with ``block_height IS NULL`` are loaded, at
    most once per Exit and run, through ``TraceEngine.load_transaction`` (P2-N8
    re-check). Only ``block_height``/``block_time`` (and ``updated_at``) are
    written; ``exit_date`` and user fields stay. Node unavailable (same outage
    types as R3-06, all ``RpcError``) or still unconfirmed: the Exit stays
    unchanged, a warning is logged (once per Exit, state and process), the
    caller continues. All loads of one call share ``FX8_REFRESH_BUDGET``; Exits
    not reached stay unchanged (B5-10). Returns the number of Exits updated.
    """
    if engine is None:
        return 0
    pending = [row for row in await db.list_exchange_exits() if row.get("block_height") is None]
    updated = 0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + FX8_REFRESH_BUDGET
    for index, row in enumerate(pending):
        remaining = deadline - loop.time()
        try:
            if remaining <= 0:
                raise TimeoutError
            async with asyncio.timeout(remaining):
                # Runs inside GETs: blockchain facts only, no ownership (L-10).
                tx = await engine.load_transaction(row["txid"], persist_ownership=False)
        except TimeoutError:
            logger.warning(
                "block back-fill budget of %gs exhausted; %d unconfirmed exchange "
                "exit(s) stay out of year filters until the next request",
                FX8_REFRESH_BUDGET, len(pending) - index,
            )
            break
        except RpcError as exc:
            _warn_fx8_once(
                row, "error",
                "could not refresh block of unconfirmed exchange exit %s (tx %s): %s", exc,
            )
            continue
        height, block_time = getattr(tx, "block_height", None), getattr(tx, "block_time", None)
        if height is None or block_time is None:
            recheck_failed = getattr(engine, "unconfirmed_recheck_failed", None)
            if recheck_failed is not None and recheck_failed(row["txid"]):
                _warn_fx8_once(
                    row, "unreachable",
                    "exchange exit %s (tx %s): node not reachable, block not checked; "
                    "it stays out of year filters",
                )
            else:
                _warn_fx8_once(
                    row, "unconfirmed",
                    "exchange exit %s (tx %s) is not confirmed on-chain yet; "
                    "it stays out of year filters",
                )
            continue
        async with db.write_transaction() as tx_db:
            current = await tx_db.get_exchange_exit(row["id"])
            if current is None or current.get("block_height") is not None:
                continue
            await tx_db.update_exchange_exit(
                row["id"], commit=False, block_height=height, block_time=block_time
            )
        updated += 1
    return updated


async def delete_exchange_exit(db: Any, exit_id: int) -> bool:
    """Delete the Exchange Exit + its Exit-specific on-chain rows.

    Acquisition history is persistent: this removes the Exit classification and
    its Exit-specific sources/outputs/allocations ONLY. The Acquisition, its
    trace, the cached blockchain provenance needed to rediscover the candidate,
    Known Address Registry entries, unrelated Disposals and other Exchange
    Exits are all left untouched. Exit-specific self-custody confirmation lives
    on the source rows and disappears with them; it never became global
    ownership evidence.

    Position state is derived, never stored: the Acquisition re-derives from the
    remaining facts through the canonical position-state logic on the next read.
    A DISPOSED / PARTIALLY_DISPOSED acquisition therefore returns to whatever
    the remaining evidence supports (for the usual confirmed-exit case,
    NEEDS_REVIEW) instead of being removed or hidden.

    A Disposal is never deleted with an Exit (SK-1, Exchange Exit !=
    Disposal). Both link directions - ``disposals.exchange_exit_id`` and the
    legacy ``exchange_exits.disposal_id`` (schema v17/v18, never written by
    the current create path) - are handled alike in
    ``Database.delete_exchange_exit``: the linked Disposal keeps its economic
    row but loses the allocation links and evidence that would otherwise keep
    the Acquisition pinned to DISPOSED after the Exit classification is gone.
    A stale legacy link is ignored. Everything happens in ONE isolated
    transaction (P1-9).

    This path must never invoke the Acquisition cleanup
    (``Database.delete_acquisition`` / ``delete_all_acquisitions`` /
    ``_delete_acquisition_rows``). Deleting an Exit is not deleting an
    Acquisition. An integration test pins that boundary through this API.
    """
    if await db.get_exchange_exit(exit_id) is None:
        return False
    return await db.delete_exchange_exit(exit_id)


async def update_exchange_exit(
    db: Any, engine: Any, exit_id: int, payload: ExchangeExitUpdate
) -> ExchangeExit:
    """Update the *metadata* scalars of an Exchange Exit.

    Only TRUE metadata is mutable here: exit_date / exit_time / broker /
    reference / notes. These never touch miner-fee attribution or the
    disposed-vs-network-fee split.

    The objective transaction fee is immutable. Metadata edits never rewrite
    blockchain evidence, create a Disposal or change source allocations.
    """
    existing = await _get_exchange_exit(db, exit_id)

    immutable = ["fee_sats"]
    for field in immutable:
        if getattr(payload, field, None) is not None:
            raise ExchangeExitValidationError(
                f"{field} is immutable on an Exchange Exit once created; "
                "delete and recreate the exit to correct blockchain evidence."
            )

    changes: dict[str, Any] = {}
    if payload.exit_date is not None:
        changes["exit_date"] = _validate_exit_date(payload.exit_date)
    if payload.exit_time is not None:
        changes["exit_time"] = _validate_exit_time(payload.exit_time)
    if payload.broker is not None:
        changes["broker"] = _strip_optional(payload.broker)
    if payload.reference is not None:
        changes["reference"] = _strip_optional(payload.reference)
    if payload.notes is not None:
        changes["notes"] = _strip_optional(payload.notes)

    if not changes:
        return existing

    # P2-5b: another Exit with the same exit TXID (legacy duplicate) blocks the
    # edit; own row does not count. Checked under the write lock.
    async with db.write_transaction() as tx_db:
        other = await tx_db.get_other_exchange_exit_by_txid(
            existing.txid, exclude_exit_id=exit_id
        )
        if other is not None:
            raise ExchangeExitConflictError(
                f"Exchange Exit {other['id']} already uses exit transaction "
                f"{existing.txid}."
            )
        await tx_db.update_exchange_exit(exit_id, commit=False, **changes)
    return await _get_exchange_exit(db, exit_id)
