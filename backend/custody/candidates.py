"""Continuation candidate search + explainable scoring.

After a custodial boundary exists, the app searches the local known-address
registry for later owned UTXOs that could be withdrawals. No xpub and no
external services are used: blockchain data comes only from the local Bitcoin
Core RPC (scantxoutset of the current UTXO set) plus outpoints already cached
in the local SQLite database.

Candidates are suggestions with provenance ``INFERRED``. They are never a
fact, never persisted, and never promoted to a bridge without explicit user
confirmation.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.models.schemas import (
    CustodyCandidate,
    Provenance,
    SpendStatus,
)
from backend.rpc.bitcoin import sats

logger = logging.getLogger("lineage.custody")


def _wallet_map(registry_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        (r.get("address") or "").strip(): r
        for r in registry_rows if r.get("address")
    }


async def _scan_local_utxo_set(rpc, wallet_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Scan the local node's UTXO set for registered addresses."""
    addresses = [a for a in wallet_map if a]
    if not addresses:
        return []
    descriptors = [f"addr({a})" for a in addresses]
    try:
        res = await rpc.scantxoutset("start", descriptors)
    except Exception as exc:  # noqa: BLE001 - feature must degrade gracefully
        logger.info("scantxoutset unavailable; using DB-cached candidates only: %s", exc)
        res = {}
    out: list[dict[str, Any]] = []
    for u in res.get("unspents") or []:
        txid = u.get("txid")
        vout = u.get("vout")
        if not txid or vout is None:
            continue
        spk = u.get("scriptPubKey") or {}
        addr = spk.get("address") or (spk.get("addresses") or [None])[0]
        registry = wallet_map.get((addr or "").strip(), {})
        out.append({
            "txid": txid,
            "vout": int(vout),
            "address": addr,
            "amount_sats": sats(u.get("amount")),
            "block_height": u.get("height"),
            "block_time": None,
            "spend_status": SpendStatus.UNSPENT,
            "wallet_name": registry.get("wallet_name") or "default",
            "label": registry.get("label") or "",
        })
    return out


def _from_db_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "txid": row.get("txid"),
        "vout": row.get("vout"),
        "address": row.get("address"),
        "amount_sats": row.get("amount_sats"),
        "block_height": row.get("block_height"),
        "block_time": row.get("block_time"),
        "spend_status": row.get("spend_status") or SpendStatus.UNKNOWN,
        "wallet_name": row.get("registry_wallet")
        or (row.get("wallet_name") or "default"),
        "label": row.get("registry_label") or (row.get("label") or ""),
    }


def score_candidate(
    candidate: dict[str, Any],
    deposit_sats: int | None,
    boundary_time: int | None,
    all_candidates: dict[tuple[str, int], dict[str, Any]],
) -> tuple[int, list[str]]:
    """Explainable 0..100 score for a potential custody-bridge destination.

    Signal weights (spec section 5):
      A. time proximity        (+25/+15/+8)
      B. amount similarity     (+15/+8)
      C. known-owned dest      (+40)
      D. unique candidate      (+10)
      E. transaction fan-in    (+10)
    """
    score = 0
    reasons: list[str] = []

    amount = candidate.get("amount_sats")
    candidate_time = candidate.get("block_time")

    # C. Known-owned destination.
    if candidate.get("address"):
        score += 40
        reasons.append("owned destination")

    # B. Amount similarity (no exact equality required - fees/splits/merges).
    if deposit_sats and amount:
        ratio = min(amount, deposit_sats) / max(amount, deposit_sats)
        if ratio >= 0.7:
            score += 15
            reasons.append(f"amount differs by only {abs(100 - ratio * 100):.1f}%")
        elif ratio >= 0.4:
            score += 8
            reasons.append(f"amount is within {(1 - ratio) * 100:.0f}% of the deposit")

    # A. Time proximity to the deposit.
    if boundary_time and candidate_time:
        hours = max(0, (candidate_time - boundary_time) / 3600.0)
        per_day = hours / 24.0
        if hours <= 24:
            score += 25
            reasons.append(f"{hours:.0f} hours after custodial deposit")
        elif per_day <= 3:
            score += 15
            reasons.append(f"{per_day:.1f} days after custodial deposit")
        else:
            score += 8
            reasons.append(f"{per_day:.0f} days after custodial deposit")

    # D. Unique candidate: no other candidate within 40% of the deposit value.
    if deposit_sats:
        similar = [
            key for key, other in all_candidates.items()
            if other is not candidate
            and other.get("amount_sats")
            and (min(other["amount_sats"], deposit_sats) / max(other["amount_sats"], deposit_sats)) >= 0.4
        ]
        if not similar:
            score += 10
            reasons.append("no competing similar owned output")

    # E. Transaction fan-in (service-like wallet behavior).
    input_count = candidate.get("input_count") or 0
    if input_count >= 5:
        score += 10
        reasons.append(f"high fan-in withdrawal transaction ({input_count} inputs)")

    return min(100, score), reasons


async def find_candidates(
    db, rpc, trace_id: int, boundary: dict[str, Any]
) -> list[CustodyCandidate]:
    """Search the registry for possible continuation outputs after a boundary.

    ``boundary`` is a dict from ``db.get_custodial_boundary`` / ``__by_id``.
    """
    registry_rows = await db.search_known_addresses("")
    wallet_map = _wallet_map(registry_rows)

    boundary_op = await db.get_outpoint(boundary["txid"], boundary["vout"])
    boundary_time = (boundary_op or {}).get("block_time")
    fallback_amount = (boundary_op or {}).get("amount_sats")

    deposit_sats = await _traced_contribution(db, trace_id, boundary["txid"])

    candidates: dict[tuple[str, int], dict[str, Any]] = {}

    scanned = await _scan_local_utxo_set(rpc, wallet_map)
    for c in scanned:
        candidates[(c["txid"], c["vout"])] = c

    db_rows = await db.find_registered_outpoints_after(boundary_time)
    for row in db_rows:
        key = (row.get("txid"), row.get("vout"))
        if not all(key):
            continue
        cand = _from_db_row(row)
        existing = candidates.get(key)
        if existing:
            existing.update({k: v for k, v in cand.items() if v is not None})
        else:
            candidates[key] = cand

    if not deposit_sats:
        deposit_sats = fallback_amount

    results: list[CustodyCandidate] = []
    for (txid, vout), cand in candidates.items():
        # Only outputs created strictly after the boundary.
        if boundary_time is not None:
            created = cand.get("block_time")
            if created is None:
                created = await _resolve_creation_time(db, rpc, txid)
                cand["block_time"] = created
            if (created or 0) <= boundary_time:
                continue

        cand["input_count"] = await _resolve_input_count(db, rpc, txid)
        score, reasons = score_candidate(cand, deposit_sats, boundary_time, candidates)
        results.append(CustodyCandidate(
            boundary_id=boundary.get("id"),
            txid=txid,
            vout=vout,
            outpoint=f"{txid}:{vout}",
            address=cand.get("address"),
            amount_sats=cand.get("amount_sats"),
            block_time=cand.get("block_time"),
            block_height=cand.get("block_height"),
            spend_status=cand.get("spend_status") or SpendStatus.UNKNOWN,
            wallet_name=cand.get("wallet_name"),
            provenance=Provenance.INFERRED,
            confidence_score=score,
            reasons=reasons,
        ))

    results.sort(key=lambda c: c.confidence_score, reverse=True)
    return results


async def _traced_contribution(db, trace_id: int, txid: str) -> int | None:
    """Amount of the traced lineage that entered the service in ``txid``.

    Sources are the trace edges feeding ``txid`` (the boundary deposit). The
    amount is what was deposited regardless of ownership classification: a
    trace start may be UNKNOWN-owned yet the traced value still entered the
    service, so ownership rows are not consulted here."""
    edges = await db.get_edges(trace_id)
    total = 0
    seen: set[tuple[str, int]] = set()
    found = False
    for e in edges:
        if e.spending_txid != txid:
            continue
        src = (e.source_txid, e.source_vout)
        if src in seen:
            continue
        seen.add(src)
        op = await db.get_outpoint(e.source_txid, e.source_vout)
        if op and op.get("amount_sats"):
            total += op["amount_sats"]
            found = True
    return total if found else None


async def _resolve_input_count(db, rpc, txid: str) -> int:
    try:
        tx = await db.get_transaction(txid)
        raw = tx.get("raw_json") if tx else None
        if isinstance(raw, dict) and raw.get("vin") is not None:
            return len(raw.get("vin") or [])
    except Exception:  # noqa: BLE001
        pass
    try:
        raw = await rpc.get_raw_transaction(txid, verbose=True)
        return len(raw.get("vin") or [])
    except Exception:  # noqa: BLE001
        return 0


async def _resolve_creation_time(db, rpc, txid: str) -> int | None:
    try:
        tx = await db.get_transaction(txid)
        if tx and tx.get("block_time"):
            return int(tx["block_time"])
    except Exception:  # noqa: BLE001
        pass
    try:
        raw = await rpc.get_raw_transaction(txid, verbose=True)
        blockhash = raw.get("blockhash")
        if blockhash:
            header = await rpc.get_block_header(blockhash, verbose=True)
            return header.get("time")
    except Exception:  # noqa: BLE001
        pass
    return None