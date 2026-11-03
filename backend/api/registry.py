"""Registry / Ownership domain router (consolidation package P3-4).

Routes for the Known Address Registry (import / list / get / delete /
batches / batch delete / wallet-groups / stats / delete-all), the one-time
xpub wallet discovery records (plan / preview / run / list / get / delete),
address evidence (list / get / delete), explicit address ownership
confirmation, address conflicts (list / resolve) and the trace-scoped
ownership/custody conflicts (list / resolve), plus the helpers only these
routes use (``_discovery_config``, ``_discovery_info_model``,
``_evidence_model``, ``_conflict_model``).

The application's runtime objects (database, engine, RPC client, the
``wallet_sessions()`` / ``inventory_manager()`` accessors, ...) remain module
globals of ``backend.api.server`` so tests can keep replacing them there.
This module resolves them late-bound through :func:`backend.api.runtime.state`
at call time (``srv = state()`` at the top of each endpoint) and never caches
them at import time.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from backend.api.http_common import (
    _check_sensitive,
    _get_trace_or_404,
    _ownership_custody_conflict_model,
)
from backend.api.runtime import state
from backend.discovery.service import DiscoveryConfig, DiscoveryError, WalletDiscovery
from backend.models.schemas import (
    AddressConflict,
    BatchRecord,
    ClassificationSource,
    DiscoveryInfo,
    DiscoveryPlan,
    DiscoveryRequest,
    DiscoveryRunResult,
    EvidenceItem,
    KnownAddress,
    KnownAddressImportRequest,
    KnownAddressImportResult,
    KnownAddressQuery,
    KnownAddressUpdate,
    OwnershipCustodyConflict,
    OwnershipStatus,
)
from backend.registry.address_validation import classify_address
from backend.registry.import_parser import parse_import

logger = logging.getLogger("lineage.api")

router = APIRouter()


# ---------------------------------------------------------------------------
# Known Address Registry
# ---------------------------------------------------------------------------


@router.post("/api/registry/import", response_model=KnownAddressImportResult)
async def registry_import(payload: KnownAddressImportRequest) -> KnownAddressImportResult:
    """Import addresses from pasted text or an explicit row list."""
    srv = state()
    content = (payload.content or "").strip()
    rows = payload.rows
    source = str(payload.source or "PASTE").strip().upper() or "PASTE"
    wallet_name = str(payload.wallet_name or "default").strip() or "default"
    batch_name = str(payload.name or "").strip() or f"Import {len(content)} chars"

    parsed = []
    blank_count = 0
    if rows:
        for row in rows:
            parsed.append(
                {
                    "address": str(row.get("address", "")).strip(),
                    "label": str(row.get("label", "")).strip(),
                    "wallet_name": str(row.get("wallet_name", "") or wallet_name).strip(),
                    "error": "",
                }
            )
    else:
        if not content:
            raise HTTPException(status_code=400, detail="No import content supplied.")
        pr = parse_import(content)
        parsed = [
            {"address": r.address, "label": r.label,
             "wallet_name": r.wallet_name or wallet_name, "error": r.error}
            for r in pr.rows
        ]
        blank_count = pr.blank_count
        batch_name = str(payload.name or "").strip() or f"Paste import, {len(pr.rows)} lines"

    valid_rows: list[dict] = []
    invalid_count = 0
    for r in parsed:
        addr = (r.get("address") or "").strip()
        if not addr:
            blank_count += 1
            continue
        if r.get("error"):
            invalid_count += 1
            continue
        if not classify_address(addr):
            invalid_count += 1
            continue
        valid_rows.append(r)

    batch = await srv.db.create_known_address_batch(
        name=batch_name,
        wallet_name=wallet_name,
        source=f"IMPORT:{source}",
        address_count=len(valid_rows),
        invalid_count=invalid_count,
    )
    batch_id = batch["id"] if batch else None
    for r in valid_rows:
        r["batch_id"] = batch_id
        r["key_access"] = payload.key_access
        if not r.get("address_type"):
            r["address_type"] = classify_address(r["address"]) or ""

    stats = await srv.db.bulk_insert_known_addresses(valid_rows)
    imported = stats.get("imported", 0)
    duplicates = stats.get("duplicates", 0)
    conflicts = stats.get("conflicts", 0)

    # Every imported address records WHY it is owned (section 2 evidence
    # source MANUAL_IMPORT). Evidence attaches to the existing canonical row;
    # an address is never duplicated just because more claims exist.
    for r in valid_rows:
        await srv.db.add_address_evidence(
            address=r["address"],
            wallet_name=r.get("wallet_name") or wallet_name,
            source="MANUAL_IMPORT",
            batch_id=batch_id,
        )

    if batch_id is not None:
        await srv.db.set_batch_import_counts(batch_id, imported, duplicates)

    return KnownAddressImportResult(
        imported=imported,
        duplicates=duplicates,
        conflicts=conflicts,
        total=len(valid_rows),
    )


@router.get("/api/registry/addresses", response_model=list[KnownAddress])
async def registry_list(query: str = "", wallet_name: str = "") -> list[KnownAddress]:
    srv = state()
    q = KnownAddressQuery(query=query or None, wallet_name=wallet_name or None)
    records = await srv.db.search_known_addresses(query=q.query or "")
    if q.wallet_name:
        records = [r for r in records if r.get("wallet_name") == q.wallet_name]
    return [_known_address_model(r) for r in records]


def _known_address_model(record: dict[str, Any]) -> KnownAddress:
    return KnownAddress(
        id=int(record["id"]),
        address=record["address"],
        wallet_name=record.get("wallet_name") or "default",
        label=record.get("label") or "",
        batch_id=record.get("batch_id"),
        address_type=record.get("address_type") or "",
        created_at=str(record.get("created_at") or ""),
        key_access=bool(record.get("key_access", 1)),
    )


@router.get("/api/registry/addresses/{address:path}", response_model=KnownAddress)
async def registry_get(address: str) -> KnownAddress:
    srv = state()
    record = await srv.db.get_known_address(address.strip())
    if not record:
        raise HTTPException(status_code=404, detail="Address is not in the registry.")
    return _known_address_model(record)


@router.patch("/api/registry/addresses/{address:path}", response_model=KnownAddress)
async def registry_update(address: str, payload: KnownAddressUpdate) -> KnownAddress:
    """Set key access (REG-1). Ownership evidence is unchanged either way."""
    srv = state()
    address = address.strip()
    if not await srv.db.set_known_address_key_access(address, payload.key_access):
        raise HTTPException(status_code=404, detail="Address is not in the registry.")
    return _known_address_model(await srv.db.get_known_address(address))


@router.delete("/api/registry/addresses/{address:path}", status_code=204)
async def registry_delete(address: str) -> Response:
    srv = state()
    removed = await srv.db.delete_known_address(address.strip())
    if not removed:
        raise HTTPException(status_code=404, detail="Address is not in the registry.")
    return Response(status_code=204)


@router.get("/api/registry/batches", response_model=list[BatchRecord])
async def registry_batches() -> list[BatchRecord]:
    srv = state()
    out = []
    for r in await srv.db.list_known_address_batches():
        out.append(BatchRecord(
            id=int(r["id"]),
            name=r.get("name") or "",
            wallet_name=r.get("wallet_name") or "default",
            source=r.get("source") or "",
            address_count=int(r.get("address_count") or 0),
            invalid_count=int(r.get("invalid_count") or 0),
            duplicate_count=int(r.get("duplicate_count") or 0),
            imported_count=int(r.get("imported_count") or 0),
            created_at=str(r.get("created_at") or ""),
        ))
    return out


@router.delete("/api/registry/batches/{batch_id}", status_code=204)
async def registry_batch_delete(batch_id: int) -> Response:
    srv = state()
    if batch_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid batch id.")
    removed = await srv.db.delete_known_address_batch(batch_id)
    return Response(status_code=204)


@router.get("/api/registry/wallet-groups")
async def registry_wallet_groups() -> list[dict]:
    srv = state()
    return await srv.db.list_wallet_groups()


@router.get("/api/registry/stats")
async def registry_stats() -> dict[str, int]:
    srv = state()
    return {"known_addresses": await srv.db.count_known_addresses()}


@router.delete("/api/registry/addresses")
async def delete_all_bitcoin_addresses() -> dict[str, int]:
    """Reset the Known Address Registry and import/discovery evidence.

    Deletes all registered/imported/discovered addresses, import batches,
    wallet-discovery metadata, scan jobs and MANUAL_IMPORT/XPUB_DISCOVERY
    address evidence. USER_CONFIRMED evidence and explicit per-outpoint
    classifications survive. Ownership derived from the registry is reset to
    UNKNOWN; no outpoint becomes EXTERNAL solely because evidence was removed.
    """
    srv = state()
    try:
        sessions = srv.wallet_sessions()
        async with sessions.lock:
            sessions.invalidate_locked()
            await srv.inventory_manager().invalidate()
            result = await srv.db.delete_all_bitcoin_addresses()
    except Exception:
        logger.exception("delete all bitcoin addresses failed")
        raise HTTPException(status_code=500, detail="Failed to delete registry entries.")
    logger.info("delete all bitcoin addresses: %s", {k: v for k, v in result.items()})
    return result


# ---------------------------------------------------------------------------
# One-time wallet discovery (WALLET_XPUB_DISCOVERY_REQUIREMENTS.md)
#
# PRIVACY: the extended public key is accepted by parse/preview/run, used in
# memory only, and never returned or stored. All responses carry a redacted
# key_hint. Input that looks like a private key / seed is rejected first.
# ---------------------------------------------------------------------------


def _discovery_config(payload: DiscoveryRequest) -> DiscoveryConfig:
    return DiscoveryConfig(
        key=payload.key,
        wallet_name=payload.wallet_name,
        label=payload.label,
        key_type=payload.key_type,
        network=payload.network,
        script_type=payload.script_type,
        gap_limit=payload.gap_limit,
        start_height=payload.start_height,
        end_height=payload.end_height,
        scan_mode=payload.scan_mode,
    )


def _discovery_info_model(row: dict[str, Any], jobs: list[dict[str, Any]]) -> DiscoveryInfo:
    return DiscoveryInfo(
        id=int(row["id"]),
        wallet_name=row.get("wallet_name") or "",
        label=row.get("label") or "",
        key_type=row.get("key_type") or "",
        network=row.get("network") or "",
        script_type=row.get("script_type") or "",
        account_path=row.get("account_path") or "",
        gap_limit=int(row.get("gap_limit") or 20),
        start_height=row.get("start_height"),
        end_height=row.get("end_height"),
        scan_mode=row.get("scan_mode") or "scanblocks",
        status=row.get("status") or "COMPLETED",
        trace_count=int(row.get("trace_count") or 0),
        branch0_found=int(row.get("branch0_found") or 0),
        branch1_found=int(row.get("branch1_found") or 0),
        branch0_last_index=int(row.get("branch0_last_index", -1)),
        branch1_last_index=int(row.get("branch1_last_index", -1)),
        found_vout_count=int(row.get("found_vout_count") or 0),
        unexpected_vout_count=int(row.get("unexpected_vout_count") or 0),
        created_at=str(row.get("created_at") or ""),
        finished_at=row.get("finished_at"),
        jobs=jobs,
    )


def _evidence_model(r: dict[str, Any]) -> EvidenceItem:
    return EvidenceItem(
        id=int(r["id"]),
        address=r["address"],
        wallet_name=r.get("wallet_name") or "default",
        source=r.get("source") or "MANUAL_IMPORT",
        derivation_path=r.get("derivation_path") or "",
        branch_index=r.get("branch_index"),
        address_index=r.get("address_index"),
        key_type=r.get("key_type") or "",
        discovery_id=r.get("discovery_id"),
        batch_id=r.get("batch_id"),
        first_seen_at=r.get("first_seen_at"),
        last_seen_at=r.get("last_seen_at"),
        created_at=str(r.get("created_at") or ""),
    )


def _conflict_model(r: dict[str, Any]) -> AddressConflict:
    return AddressConflict(
        id=int(r["id"]),
        address=r["address"],
        wallet_name=r.get("wallet_name") or "",
        evidence_wallet_name=r.get("evidence_wallet_name") or "",
        reason=r.get("reason") or "",
        detail=r.get("detail") or "",
        status=r.get("status") or "PENDING",
        created_at=str(r.get("created_at") or ""),
        resolved_at=r.get("resolved_at"),
    )


@router.post("/api/discovery/plan", response_model=DiscoveryPlan)
async def discovery_plan(payload: DiscoveryRequest) -> DiscoveryPlan:
    """Validate an extended public key and describe the planned scan.

    Never performs a scan and never returns the key. Input that looks like a
    private key / seed phrase is rejected up front.
    """
    srv = state()
    _check_sensitive(payload.key, "key")
    if not (payload.key or "").strip():
        raise HTTPException(status_code=400, detail="An extended public key is required.")
    try:
        return WalletDiscovery(srv.rpc_client, srv.db).build_plan(_discovery_config(payload))
    except DiscoveryError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/api/discovery/preview", response_model=DiscoveryRunResult)
async def discovery_preview() -> DiscoveryRunResult:
    raise HTTPException(status_code=410, detail="Use the scan/review/confirm discovery session workflow.")


@router.post("/api/discovery/run", response_model=DiscoveryRunResult)
async def discovery_run() -> DiscoveryRunResult:
    raise HTTPException(status_code=410, detail="Import requires a retained discovery session and review revision.")


@router.get("/api/discovery", response_model=list[DiscoveryInfo])
async def list_discoveries() -> list[DiscoveryInfo]:
    srv = state()
    rows = await srv.db.list_wallet_discoveries()
    jobs = await srv.db.list_scan_jobs()
    by_discovery: dict[int, list[dict[str, Any]]] = {}
    for j in jobs:
        by_discovery.setdefault(int(j["discovery_id"]), []).append(j)
    return [_discovery_info_model(r, by_discovery.get(int(r["id"]), [])) for r in rows]


@router.get("/api/discovery/{discovery_id}", response_model=DiscoveryInfo)
async def get_discovery(discovery_id: int) -> DiscoveryInfo:
    srv = state()
    row = await srv.db.get_wallet_discovery(discovery_id)
    if not row:
        raise HTTPException(status_code=404, detail="Discovery not found.")
    jobs = await srv.db.list_scan_jobs(discovery_id)
    return _discovery_info_model(row, jobs)


@router.delete("/api/discovery/{discovery_id}", status_code=204)
async def delete_discovery(discovery_id: int) -> Response:
    srv = state()
    row = await srv.db.get_wallet_discovery(discovery_id)
    if not row:
        raise HTTPException(status_code=404, detail="Discovery not found.")
    removed = await srv.db.delete_wallet_discovery(discovery_id)
    return Response(status_code=204)


@router.get("/api/registry/evidence", response_model=list[EvidenceItem])
async def list_evidence(wallet_name: str = "", source: str = "") -> list[EvidenceItem]:
    srv = state()
    rows = await srv.db.list_address_evidence(wallet_name or None, source or None)
    return [_evidence_model(r) for r in rows]


@router.get("/api/registry/evidence/{address}", response_model=list[EvidenceItem])
async def get_address_evidence(address: str) -> list[EvidenceItem]:
    srv = state()
    rows = await srv.db.get_address_evidence(address.strip())
    return [_evidence_model(r) for r in rows]


@router.delete("/api/registry/evidence/{address}/{source}", status_code=204)
async def delete_evidence(
    address: str, source: str, wallet_name: str = "default"
) -> Response:
    """Remove one evidence row. The registry address is kept unless it becomes
    evidence-free and unused (see discovery delete semantics)."""
    srv = state()
    src = source.strip().upper()
    if src not in ("MANUAL_IMPORT", "XPUB_DISCOVERY", "USER_CONFIRMED"):
        raise HTTPException(status_code=400, detail="Unknown evidence source.")
    removed = await srv.db.delete_address_evidence(address.strip(), wallet_name.strip() or "default", src)
    if not removed:
        raise HTTPException(status_code=404, detail="Evidence row not found.")
    return Response(status_code=204)


@router.post("/api/registry/addresses/{address:path}/confirm", status_code=201)
async def confirm_address_ownership(
    address: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Record explicit user confirmation that an address is owned."""
    srv = state()
    address = address.strip()
    record = await srv.db.get_known_address(address)
    if not record:
        raise HTTPException(status_code=404, detail="Address is not in the registry.")
    wallet = str((payload or {}).get("wallet_name") or record.get("wallet_name") or "default")
    _check_sensitive(wallet, "wallet_name")
    await srv.db.add_address_evidence(address, wallet, "USER_CONFIRMED")
    return {"address": address, "wallet_name": wallet, "source": "USER_CONFIRMED"}


@router.get("/api/registry/conflicts", response_model=list[AddressConflict])
async def list_conflicts(status: str = "") -> list[AddressConflict]:
    srv = state()
    rows = await srv.db.list_address_conflicts(status or None)
    return [_conflict_model(r) for r in rows]


@router.post("/api/registry/conflicts/{conflict_id}/resolve")
async def resolve_conflict(conflict_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    srv = state()
    resolution = str((payload or {}).get("resolution", "") or "").strip().upper()
    if resolution not in ("KEEP", "SHARED", "MOVED"):
        raise HTTPException(status_code=400, detail="resolution must be KEEP, SHARED or MOVED.")
    ok = await srv.db.resolve_address_conflict(conflict_id, resolution)
    if not ok:
        raise HTTPException(status_code=404, detail="Conflict not found.")
    return {"conflict_id": conflict_id, "resolution": resolution}


# ---------------------------------------------------------------------------
# Ownership / custody conflicts
#
# Direct ownership evidence (MANUAL_IMPORT / XPUB_DISCOVERY / USER_CONFIRMED)
# + a confirmed CUSTODIAL_DEPOSIT boundary on the same outpoint is an explicit
# contradiction. It is surfaced as an OWNERSHIP_CUSTODY_CONFLICT requiring
# review and is NEVER auto-resolved on either side. Resolution is explicit:
#   KEEP_BOUNDARY  - the custody boundary stays; exact lineage ends there.
#   REMOVE_BOUNDARY- only the custody classification/boundary is removed; the
#                    UTXO, the tx graph and the wallet ownership evidence stay.
# ---------------------------------------------------------------------------


@router.get(
    "/api/traces/{trace_id}/ownership-custody-conflicts",
    response_model=list[OwnershipCustodyConflict],
)
async def list_ownership_custody_conflicts(
    trace_id: int, status: str = ""
) -> list[OwnershipCustodyConflict]:
    srv = state()
    await _get_trace_or_404(trace_id)
    rows = await srv.db.list_ownership_custody_conflicts(trace_id, status or None)
    return [_ownership_custody_conflict_model(r) for r in rows]


@router.post(
    "/api/traces/{trace_id}/ownership-custody-conflicts/{conflict_id}/resolve",
    response_model=dict,
)
async def resolve_ownership_custody_conflict(
    trace_id: int, conflict_id: int, payload: dict[str, Any]
) -> dict[str, Any]:
    """Resolve an ownership/custody conflict by explicit user decision."""
    srv = state()
    resolution = str((payload or {}).get("resolution", "") or "").strip().upper()
    if resolution not in ("KEEP_BOUNDARY", "REMOVE_BOUNDARY"):
        raise HTTPException(
            status_code=400,
            detail="resolution must be KEEP_BOUNDARY or REMOVE_BOUNDARY.",
        )
    await _get_trace_or_404(trace_id)
    row = await srv.db.get_ownership_custody_conflict_by_id(conflict_id)
    if not row or int(row["trace_id"]) != trace_id:
        raise HTTPException(status_code=404, detail="Conflict not found.")
    if row.get("status") != "PENDING":
        detail = f"Conflict is already resolved ({row.get('status')})."
        raise HTTPException(status_code=409, detail=detail)

    if resolution == "KEEP_BOUNDARY":
        await srv.db.resolve_ownership_custody_conflict(conflict_id, "KEEP_BOUNDARY")
        walk = await srv.engine.continue_trace(trace_id)
        return {"conflict_id": conflict_id, "resolution": resolution, "walk": walk}

    # REMOVE_BOUNDARY: delete the boundary object and drop ONLY the custody
    # classification. The UTXO + tx graph and the wallet ownership evidence are
    # preserved; the next walk re-applies OWNED from the registry.
    boundary_id = row.get("boundary_id")
    if boundary_id is not None:
        boundary = await srv.db.get_custodial_boundary_by_id(int(boundary_id))
        if boundary and int(boundary["trace_id"]) == trace_id:
            await srv.db.delete_custodial_boundary(int(boundary["id"]))
    await srv.db.set_ownership(
        row["txid"], int(row["vout"]), OwnershipStatus.UNKNOWN, ClassificationSource.UNKNOWN
    )
    await srv.db.resolve_ownership_custody_conflict(conflict_id, "BOUNDARY_REMOVED")
    walk = await srv.engine.continue_trace(trace_id)
    return {"conflict_id": conflict_id, "resolution": resolution, "walk": walk}
