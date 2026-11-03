"""Independent scan membership; blockchain facts stay in the canonical cache.

Only scanners write membership. Acquisition/trace mutations never call here.
No watch key, descriptor, or unused derivation range is persisted.

``population_keys`` and ``metadata`` are the pure read side of the same
tables; reporting imports them from here so it never depends on the
scanner-facing ``backend.inventory`` module.
"""
import json
from datetime import datetime, timezone

import aiosqlite

from backend.reporting.read_data import rows

# Scan definitions: Registry entries with key access only (REG-1). An entry
# without key access stays ownership evidence but is never scanned or held.
SCAN_DEFINITIONS = ("SELECT address, wallet_name FROM known_addresses "
                    "WHERE key_access = 1 ORDER BY address")


async def population_keys(db, *, limit=100_000):
    scanned = await rows(db, "SELECT i.txid, i.vout FROM wallet_inventory_outputs i "
                         "JOIN wallet_inventory_scopes s ON s.address=i.address "
                         "WHERE s.scan_mode != 'STALE_NODE' AND NOT EXISTS (SELECT 1 FROM known_addresses k "
                         "WHERE k.address=i.address AND k.key_access = 0)", limit=limit)
    return {(r["txid"], r["vout"]) for r in scanned}


async def metadata(db):
    scopes = await rows(db, "SELECT * FROM wallet_inventory_scopes ORDER BY address")
    definitions = await rows(db, SCAN_DEFINITIONS)
    without_key = {r["address"] for r in await rows(db,
        "SELECT address FROM known_addresses WHERE key_access = 0")}
    covered = {s["address"] for s in scopes if s["scan_mode"] != "STALE_NODE"} - without_key
    policies = await rows(db, "SELECT d.id, d.wallet_name, d.network, d.script_type, "
        "d.start_height, d.end_height, d.gap_limit, d.branch0_last_index, d.branch1_last_index, p.policy_json "
        "FROM wallet_discoveries d LEFT JOIN wallet_inventory_policy p ON p.discovery_id=d.id")
    for p in policies:
        p["scan_policy"] = json.loads(p.pop("policy_json") or "{}")
    return {"scanned_address_count": len(covered), "configured_address_count": len(definitions),
            "refresh_required": any(d["address"] not in covered for d in definitions),
            "oldest_scan_at": min((s["scanned_at"] for s in scopes), default=None),
            "newest_scan_at": max((s["scanned_at"] for s in scopes), default=None),
            "snapshot_heights": sorted({s["block_height"] for s in scopes}),
            "wallets": sorted({d["wallet_name"] for d in definitions + policies}), "discoveries": policies,
            "scope": "Confirmed receipts on known wallet scripts; mempool spends are checked live. "
                     "New derivation ranges require another wallet discovery. A finite scan does not prove completeness."}


async def store_snapshot(writer, addresses, snapshot):
    """Caller owns an isolated transaction. Replace only successfully scanned scopes."""
    now = snapshot.get("observed_at") or datetime.now(timezone.utc).isoformat()
    for tx in snapshot["transactions"]:
        cursor = await writer.db.execute("SELECT input_total_sats, fee_sats FROM transactions WHERE txid=?", (tx["txid"],))
        existing = await cursor.fetchone()
        existing = dict(existing) if existing else {}
        await writer.upsert_transaction({**existing, **tx}, commit=False)
    for out in snapshot["outputs"]:
        await writer.upsert_outpoint(out, commit=False)
    allowed = set()
    for address in sorted(set(addresses)):
        cursor = await writer.db.execute("SELECT scanned_at FROM wallet_inventory_scopes WHERE address=?", (address,))
        previous = await cursor.fetchone()
        if previous and previous["scanned_at"] > now:
            continue  # A newer refresh won while discovery awaited confirmation.
        allowed.add(address)
        await writer.db.execute("""INSERT INTO wallet_inventory_scopes
            VALUES (?, ?, ?, ?, ?) ON CONFLICT(address) DO UPDATE SET
            scanned_at=excluded.scanned_at, block_height=excluded.block_height,
            bestblock=excluded.bestblock, scan_mode=excluded.scan_mode""",
            (address, now, snapshot["height"], snapshot["bestblock"], snapshot["mode"]))
        await writer.db.execute("DELETE FROM wallet_inventory_outputs WHERE address=?", (address,))
    for out in snapshot["outputs"]:
        if out["address"] in allowed and out["spend_status"] == "UNSPENT":
            await writer.db.execute("INSERT OR REPLACE INTO wallet_inventory_outputs VALUES (?, ?, ?)",
                                    (out["txid"], out["vout"], out["address"]))


async def save_refresh(db, definitions, snapshot):
    from backend.persistence.database import Database, DiscoveryRegistryChanged
    writer = Database(db.path)
    writer.db = await aiosqlite.connect(db.path)
    writer.db.row_factory = aiosqlite.Row
    try:
        await writer.db.execute("PRAGMA foreign_keys=ON")
        await writer.db.execute("PRAGMA busy_timeout=5000")
        await writer.db.execute("BEGIN IMMEDIATE")
        cursor = await writer.db.execute(SCAN_DEFINITIONS)
        if [dict(r) for r in await cursor.fetchall()] != definitions:
            raise DiscoveryRegistryChanged()
        await store_snapshot(writer, [d["address"] for d in definitions], snapshot)
        await writer.db.commit()
    except BaseException:
        await writer.db.rollback()
        raise
    finally:
        await writer.close()
