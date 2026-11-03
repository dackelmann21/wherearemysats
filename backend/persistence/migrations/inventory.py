"""Forward-only schema v20: wallet inventory membership tables.

Moved unchanged from ``backend.persistence.inventory`` (P3-6); the inventory
persistence itself (``store_snapshot`` / ``save_refresh``) stays there.
"""


async def migrate(conn):
    await conn.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS wallet_inventory_scopes (
            address TEXT PRIMARY KEY REFERENCES known_addresses(address) ON DELETE CASCADE,
            scanned_at TEXT NOT NULL, block_height INTEGER NOT NULL,
            bestblock TEXT NOT NULL, scan_mode TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS wallet_inventory_outputs (
            txid TEXT NOT NULL, vout INTEGER NOT NULL,
            address TEXT NOT NULL REFERENCES wallet_inventory_scopes(address) ON DELETE CASCADE,
            PRIMARY KEY(txid, vout),
            FOREIGN KEY(txid, vout) REFERENCES outpoints(txid, vout)
        );
        CREATE INDEX IF NOT EXISTS idx_inventory_address ON wallet_inventory_outputs(address);
        CREATE TABLE IF NOT EXISTS wallet_inventory_policy (
            discovery_id INTEGER PRIMARY KEY REFERENCES wallet_discoveries(id) ON DELETE CASCADE,
            policy_json TEXT NOT NULL
        );
        PRAGMA user_version = 20;
        COMMIT;
    """)
    await conn.commit()
