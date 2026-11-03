"""Forward-only schema v21: annotations, never persisted classification."""


async def migrate(conn):
    await conn.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS no_acquisition_annotations (
            network TEXT NOT NULL,
            txid TEXT NOT NULL,
            vout INTEGER NOT NULL CHECK(vout >= 0),
            source TEXT,
            note TEXT,
            revision INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            PRIMARY KEY(network, txid, vout)
        );
        PRAGMA user_version = 21;
        COMMIT;
    """)
