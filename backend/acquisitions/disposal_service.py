"""Disposal service (ACQUISITION_REVIEW_AND_DISPOSALS_V1, v0.10.0).

Economic disposal records are separate from Acquisitions. This module owns
validation, fee/net computation, allocation checks against Acquisition amounts,
and optional blockchain evidence verification.

SPENT != SOLD: an output being spent is a blockchain fact and never creates a
Disposal automatically.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from backend.validation import canonical_txid
from backend.models.schemas import (
    Acquisition,
    Disposal,
    DisposalAllocationCreate,
    DisposalCandidate,
    DisposalCreate,
    DisposalEvidence,
    DisposalEvidenceCreate,
    DisposalType,
    DisposalUpdate,
)
from .service import (
    _BTC_SATS,
    _fmt_decimal,
    acquisition_descendant_summary,
    AcquisitionValidationError,
)
from .disposal_numbers import _normalize_currency_string, _parse_simple_decimal


class DisposalValidationError(Exception):
    """Input validation failure for a Disposal (maps to HTTP 400)."""


class DisposalNotFoundError(Exception):
    """Requested Disposal or dependent Acquisition was not found (HTTP 404)."""


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _strip_optional(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _parse_disposal_date(value: Any) -> str:
    s = str(value or "").strip()
    try:
        date.fromisoformat(s)
    except ValueError as exc:
        raise DisposalValidationError(
            "The disposal date is required and must be YYYY-MM-DD."
        ) from exc
    return s


def _parse_disposal_time(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        time.fromisoformat(s)
    except ValueError as exc:
        raise DisposalValidationError(
            "The disposal time must be a valid HH:MM or HH:MM:SS value."
        ) from exc
    return s


def _parse_disposal_btc(value: Any) -> tuple[str, int]:
    if value is None or not str(value).strip():
        raise DisposalValidationError("The disposal BTC amount is required.")
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise DisposalValidationError("The disposal BTC amount must be a number.") from exc
    if d <= 0:
        raise DisposalValidationError("The disposal BTC amount must be greater than zero.")
    sats = d * _BTC_SATS
    if sats != sats.to_integral_value():
        raise DisposalValidationError(
            "The disposal BTC amount supports at most 8 decimal places (whole satoshis)."
        )
    return _fmt_decimal(d), int(sats.to_integral_value())


def _btc_sats(value: str) -> int:
    """Return satoshis for an already-validated BTC decimal string."""
    return int((Decimal(value) * _BTC_SATS).to_integral_value())


def _parse_disposal_eur(value: Any, label: str = "Gross proceeds (EUR)") -> tuple[str, Decimal]:
    try:
        d = Decimal(_normalize_currency_string(value, label))
    except AcquisitionValidationError as exc:
        raise DisposalValidationError(str(exc)) from exc
    if d < 0:
        raise DisposalValidationError(f"{label} cannot be negative.")
    return _fmt_decimal(d), d


def _parse_disposal_fee_percent(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    try:
        pct = _parse_simple_decimal(value, "Fees (%)")
    except AcquisitionValidationError as exc:
        raise DisposalValidationError(str(exc)) from exc
    if pct < 0:
        raise DisposalValidationError("The fee percent cannot be negative.")
    if pct > Decimal("100"):
        raise DisposalValidationError("The fee percent cannot exceed 100.")
    if pct != pct.quantize(Decimal("0.0001")):
        raise DisposalValidationError(
            "The fee percent supports at most 4 decimal places."
        )
    return _fmt_decimal(pct)


def _parse_sale_price(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    try:
        raw = _normalize_currency_string(value, "Sale price (EUR/BTC)")
    except AcquisitionValidationError as exc:
        raise DisposalValidationError(str(exc)) from exc
    d = Decimal(raw)
    if d < 0:
        raise DisposalValidationError("The sale price cannot be negative.")
    return _fmt_decimal(d)


def _compute_fees_and_net(
    gross_decimal: Decimal, fee_percent_str: str | None
) -> tuple[str | None, str]:
    """Return (fees_eur, net_proceeds_eur) as normalized strings."""
    fees = None
    if fee_percent_str:
        pct = Decimal(fee_percent_str)
        fees = (gross_decimal * pct / Decimal(100)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    net = (gross_decimal - (fees or Decimal(0))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return (_fmt_decimal(fees) if fees is not None else None, _fmt_decimal(net))


# ---------------------------------------------------------------------------
# Allocation and evidence validation
# ---------------------------------------------------------------------------


async def _validate_allocations(
    db: Any,
    disposal_sats: int,
    allocations: list[DisposalAllocationCreate],
    disposal_id: int | None = None,
) -> list[tuple[int, int, int, int]]:
    """Validate + combine acquisition allocations into sat-level rows.

    Returns 4-tuples (acquisition_id, allocated_sats, disposed_sats,
    network_fee_sats). Legacy allocations (2-element lists, e.g. hand-created
    SALE disposals without blockchain linkage) read back as
    disposed_sats == allocated_sats, network_fee_sats == 0, preserving the
    v0.10.x semantics. Allocations carrying explicit disposed/fee detail (from
    Exchange Exit v0.11.0) are preserved exactly so a miner fee is NEVER
    silently re-fused into the disposed satoshis.
    """
    if not allocations:
        raise DisposalValidationError(
            "At least one acquisition allocation is required."
        )

    combined: dict[int, list[int]] = {}
    for item in allocations:
        acq_id = item.acquisition_id
        sats = item.allocated_sats
        if not isinstance(acq_id, int) or acq_id <= 0:
            raise DisposalValidationError(
                "Each allocation must reference a positive acquisition id."
            )
        if not isinstance(sats, int) or sats <= 0:
            raise DisposalValidationError(
                "Each allocation must be a positive whole number of satoshis."
            )
        disposed = item.disposed_sats if item.disposed_sats is not None else sats
        fee = item.network_fee_sats if item.network_fee_sats is not None else 0
        if not isinstance(disposed, int) or disposed <= 0:
            raise DisposalValidationError("disposed_sats must be a positive value.")
        if not isinstance(fee, int) or fee < 0:
            raise DisposalValidationError("network_fee_sats cannot be negative.")
        if disposed + fee <= 0:
            raise DisposalValidationError(
                "disposed_sats + network_fee_sats must be positive."
            )
        prev = combined.get(acq_id)
        if prev is None:
            combined[acq_id] = [disposed, fee]
        else:
            combined[acq_id] = [prev[0] + disposed, prev[1] + fee]

    allocated_total = sum(v[0] + v[1] for v in combined.values())
    if allocated_total != disposal_sats:
        raise DisposalValidationError(
            f"Allocations must sum to the disposal BTC amount "
            f"({disposal_sats} sats, got {allocated_total})."
        )

    current_map: dict[int, int] = {}
    if disposal_id:
        for row in await db.get_disposal_allocations(disposal_id):
            current_map[row["acquisition_id"]] = row["allocated_sats"]

    result: list[tuple[int, int, int, int]] = []
    for acq_id, (disposed, fee) in combined.items():
        acq = await db.get_acquisition(acq_id)
        if acq is None:
            raise DisposalValidationError(f"Acquisition {acq_id} does not exist.")
        acq_sats = _btc_sats(acq["btc_amount"])
        existing = await db.get_acquisition_allocated_sats(acq_id)
        current = current_map.get(acq_id, 0)
        if existing - current + disposed + fee > acq_sats:
            raise DisposalValidationError(
                f"Allocation for acquisition {acq_id} exceeds its remaining BTC amount."
            )
        result.append((acq_id, disposed + fee, disposed, fee))
    return result


async def _validate_evidence(
    engine: Any, db: Any, evidences: list[DisposalEvidenceCreate]
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for ev in evidences:
        stx = _strip_optional(ev.source_txid)
        sv = ev.source_vout
        st = _strip_optional(ev.spending_txid)

        if not stx and sv is None and not st:
            # Explicitly empty evidence item is not useful in v1.
            raise DisposalValidationError(
                "Evidence requires source_txid, source_vout and spending_txid."
            )
        if not stx or sv is None or not st:
            raise DisposalValidationError(
                "Evidence requires source_txid, source_vout and spending_txid."
            )

        try:
            stx = canonical_txid(stx)
            st = canonical_txid(st)
        except ValueError:
            raise DisposalValidationError(
                "Transaction IDs must be 64-character hexadecimal strings."
            ) from None

        spend = await engine.resolve_spend_state(stx, sv)
        if spend.get("spend_status") != "SPENT" or spend.get("spending_txid") != st:
            raise DisposalValidationError(
                f"Spending transaction {st} does not spend {stx}:{sv}."
            )

        normalized.append(
            {
                "source_txid": stx,
                "source_vout": sv,
                "spending_txid": st,
                "destination_vout": ev.destination_vout,
                "destination_address": _strip_optional(ev.destination_address),
                "amount_sats": ev.amount_sats,
            }
        )
    return normalized


# ---------------------------------------------------------------------------
# Response builder
# ---------------------------------------------------------------------------


async def _build_disposal_response(db: Any, row: dict[str, Any]) -> Disposal:
    allocations = [
        DisposalAllocationCreate(
            acquisition_id=a["acquisition_id"],
            allocated_sats=a["allocated_sats"],
        )
        for a in await db.get_disposal_allocations(row["id"])
    ]
    evidence = [
        DisposalEvidence(
            id=e["id"],
            disposal_id=e["disposal_id"],
            source_txid=e["source_txid"],
            source_vout=e["source_vout"],
            spending_txid=e["spending_txid"],
            destination_vout=e["destination_vout"],
            destination_address=e["destination_address"],
            amount_sats=e["amount_sats"],
            created_at=e["created_at"],
        )
        for e in await db.get_disposal_evidence(row["id"])
    ]

    gross = Decimal(row["gross_proceeds_eur"])
    btc = Decimal(row["btc_amount"])
    effective_price: str | None = None
    if btc > 0:
        effective_price = _fmt_decimal(
            (gross / btc).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )

    return Disposal(
        id=row["id"],
        disposal_type=row["disposal_type"],
        disposal_date=row["disposal_date"],
        disposal_time=row["disposal_time"],
        btc_amount=row["btc_amount"],
        gross_proceeds_eur=row["gross_proceeds_eur"],
        sale_price_eur_per_btc=row["sale_price_eur_per_btc"],
        fee_percent=row["fee_percent"],
        fees_eur=row["fees_eur"],
        net_proceeds_eur=row["net_proceeds_eur"],
        effective_sale_price_eur_per_btc=effective_price,
        broker=row["broker"],
        reference=row["reference"],
        notes=row["notes"],
        allocations=allocations,
        blockchain_evidence=evidence,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------


async def create_disposal(db: Any, engine: Any, payload: DisposalCreate) -> Disposal:
    if payload.disposal_type != DisposalType.SALE:
        raise DisposalValidationError(
            "Only SALE disposals are supported in this version."
        )

    disposal_date = _parse_disposal_date(payload.disposal_date)
    disposal_time = _parse_disposal_time(payload.disposal_time)
    btc_amount, btc_sats = _parse_disposal_btc(payload.btc_amount)
    gross_amount, gross_decimal = _parse_disposal_eur(payload.gross_proceeds_eur)
    fee_percent = _parse_disposal_fee_percent(payload.fee_percent)
    sale_price = _parse_sale_price(payload.sale_price_eur_per_btc)
    fees_eur, net_proceeds_eur = _compute_fees_and_net(gross_decimal, fee_percent)

    allocation_pairs = await _validate_allocations(db, btc_sats, payload.allocations)
    evidence = await _validate_evidence(engine, db, payload.blockchain_evidence)

    # Row, allocation links and evidence as ONE isolated transaction
    # (P1-9b): a failure in between never leaves a Disposal without its links
    # or evidence. Validation (reads, RPC) runs before the write lock; the
    # allocation check is repeated under the lock (P2-5, R3-08) so a
    # concurrent write cannot slip in between check and insert.
    async with db.write_transaction() as tx_db:
        allocation_pairs = await _validate_allocations(
            tx_db, btc_sats, payload.allocations
        )
        row = await tx_db.create_disposal(
            commit=False,
            disposal_type=DisposalType.SALE,
            disposal_date=disposal_date,
            disposal_time=disposal_time,
            btc_amount=btc_amount,
            gross_proceeds_eur=gross_amount,
            sale_price_eur_per_btc=sale_price,
            fee_percent=fee_percent,
            fees_eur=fees_eur,
            net_proceeds_eur=net_proceeds_eur,
            broker=_strip_optional(payload.broker),
            reference=_strip_optional(payload.reference),
            notes=_strip_optional(payload.notes),
        )
        await tx_db.replace_disposal_links(row["id"], allocation_pairs, commit=False)
        await tx_db.replace_disposal_evidence(row["id"], evidence, commit=False)

    return await _build_disposal_response(db, row)


async def update_disposal(
    db: Any, engine: Any, disposal_id: int, payload: DisposalUpdate
) -> Disposal:
    existing = await db.get_disposal(disposal_id)
    if existing is None:
        raise DisposalNotFoundError(f"Disposal {disposal_id} not found.")

    disposal_date = existing["disposal_date"]
    if payload.disposal_date is not None:
        disposal_date = _parse_disposal_date(payload.disposal_date)

    disposal_time = existing["disposal_time"]
    if payload.disposal_time is not None:
        disposal_time = _parse_disposal_time(payload.disposal_time)

    btc_amount = existing["btc_amount"]
    btc_sats = _btc_sats(btc_amount)
    if payload.btc_amount is not None:
        btc_amount, btc_sats = _parse_disposal_btc(payload.btc_amount)

    gross_amount = existing["gross_proceeds_eur"]
    gross_decimal = Decimal(gross_amount)
    if payload.gross_proceeds_eur is not None:
        gross_amount, gross_decimal = _parse_disposal_eur(payload.gross_proceeds_eur)

    fee_percent = existing["fee_percent"]
    if payload.fee_percent is not None:
        fee_percent = _parse_disposal_fee_percent(payload.fee_percent)

    sale_price = existing["sale_price_eur_per_btc"]
    if payload.sale_price_eur_per_btc is not None:
        sale_price = _parse_sale_price(payload.sale_price_eur_per_btc)

    fees_eur, net_proceeds_eur = _compute_fees_and_net(gross_decimal, fee_percent)

    changes: dict[str, Any] = {
        "disposal_date": disposal_date,
        "disposal_time": disposal_time,
        "btc_amount": btc_amount,
        "gross_proceeds_eur": gross_amount,
        "sale_price_eur_per_btc": sale_price,
        "fee_percent": fee_percent,
        "fees_eur": fees_eur,
        "net_proceeds_eur": net_proceeds_eur,
        "broker": _strip_optional(payload.broker)
        if payload.broker is not None
        else existing["broker"],
        "reference": _strip_optional(payload.reference)
        if payload.reference is not None
        else existing["reference"],
        "notes": _strip_optional(payload.notes)
        if payload.notes is not None
        else existing["notes"],
    }

    # Validate everything first (reads, RPC), then write fields, links and
    # evidence as ONE isolated transaction (P1-9b): an invalid allocation or
    # evidence, or a failure in between, leaves the Disposal unchanged.
    allocation_pairs = None
    if payload.allocations is not None:
        allocation_pairs = await _validate_allocations(
            db, btc_sats, payload.allocations, disposal_id=disposal_id
        )

    evidence = None
    if payload.blockchain_evidence is not None:
        evidence = await _validate_evidence(engine, db, payload.blockchain_evidence)

    # The allocation check is repeated under the lock (P2-5, R3-08).
    async with db.write_transaction() as tx_db:
        row = await tx_db.update_disposal(disposal_id, commit=False, **changes)
        if row is None:
            raise DisposalNotFoundError(f"Disposal {disposal_id} not found.")
        if allocation_pairs is not None:
            allocation_pairs = await _validate_allocations(
                tx_db, btc_sats, payload.allocations, disposal_id=disposal_id
            )
            await tx_db.replace_disposal_links(disposal_id, allocation_pairs, commit=False)
        if evidence is not None:
            await tx_db.replace_disposal_evidence(disposal_id, evidence, commit=False)

    return await _build_disposal_response(db, row)


async def get_disposal(db: Any, disposal_id: int) -> Disposal:
    row = await db.get_disposal(disposal_id)
    if row is None:
        raise DisposalNotFoundError(f"Disposal {disposal_id} not found.")
    return await _build_disposal_response(db, row)


async def list_disposals(db: Any) -> list[Disposal]:
    rows = await db.list_disposals()
    return [await _build_disposal_response(db, r) for r in rows]


async def delete_disposal(db: Any, disposal_id: int) -> bool:
    ok = await db.delete_disposal(disposal_id)
    if not ok:
        raise DisposalNotFoundError(f"Disposal {disposal_id} not found.")
    return True


async def disposal_candidates(
    db: Any, engine: Any, acq_id: int
) -> list[DisposalCandidate]:
    """Return suggested blockchain evidence for an ONCHAIN acquisition.

    Spent terminals and unresolved OWNED source spends are returned, including
    sources with successor edges.
    """
    acq = await db.get_acquisition(acq_id)
    if acq is None:
        raise DisposalNotFoundError(f"Acquisition {acq_id} not found.")

    descendant = await acquisition_descendant_summary(db, engine, acq)
    candidates = {(term["txid"], term["vout"]): term for term in
                  descendant.spent_terminals + descendant.unresolved_spends}
    return [
        DisposalCandidate(
            source_txid=term["txid"],
            source_vout=term["vout"],
            spending_txid=term["spending_txid"],
            address=term.get("address"),
            amount_sats=term.get("amount_sats"),
        )
        for term in candidates.values()
    ]
