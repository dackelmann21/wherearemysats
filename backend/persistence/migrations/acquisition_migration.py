"""Forward-only v19 migration (Acquisition recovery table).

The recovery table is an inert snapshot, never an active domain model. It
preserves superseded user data and links without exposing a legacy API.
"""
import json


async def migrate_acquisition_anchors(connection):
    await connection.commit()
    await connection.execute("BEGIN IMMEDIATE")
    try:
        await connection.execute("""CREATE TABLE legacy_acquisition_recovery (
            record_type TEXT NOT NULL, legacy_id INTEGER NOT NULL,
            payload TEXT NOT NULL, PRIMARY KEY(record_type, legacy_id))""")
        for table in ("acquisitions", "exchange_exit_sources", "disposal_acquisition_links"):
            cursor = await connection.execute(f"SELECT rowid AS recovery_id, * FROM {table}")
            for row in await cursor.fetchall():
                await connection.execute(
                    "INSERT INTO legacy_acquisition_recovery VALUES (?, ?, ?)",
                    (table, row["recovery_id"], json.dumps(dict(row))))
        await connection.execute("""CREATE TABLE acquisition_anchors_v19 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            acquisition_date TEXT, source TEXT, reference TEXT, notes TEXT,
            origin_txid TEXT NOT NULL,
            origin_vout INTEGER NOT NULL CHECK(origin_vout >= 0),
            origin_address TEXT,
            origin_amount_sats INTEGER NOT NULL CHECK(origin_amount_sats >= 0),
            origin_block_height INTEGER, origin_block_time INTEGER,
            trace_id INTEGER, trace_status TEXT,
            status TEXT NOT NULL DEFAULT 'RESOLVING',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(origin_txid, origin_vout))""")
        await connection.execute("""INSERT INTO acquisition_anchors_v19
            SELECT id, acquisition_date, broker, reference, notes,
                   lower(origin_txid), origin_vout, origin_address,
                   origin_amount_sats, origin_block_height, origin_block_time,
                   trace_id, trace_status, status, created_at, updated_at
            FROM acquisitions WHERE origin_type = 'ONCHAIN'
                AND origin_txid IS NOT NULL AND origin_vout IS NOT NULL
                AND origin_amount_sats IS NOT NULL""")
        # Preserve independent Exits/Disposals. Remove only links to records
        # which cannot be on-chain anchors; all original links are recoverable.
        for table in ("exchange_exit_sources", "disposal_acquisition_links"):
            await connection.execute(f"DELETE FROM {table} WHERE acquisition_id NOT IN "
                                     "(SELECT id FROM acquisition_anchors_v19)")
        cursor = await connection.execute("SELECT seq FROM sqlite_sequence WHERE name='acquisitions'")
        sequence = await cursor.fetchone()
        await connection.execute("DROP TABLE acquisitions")
        await connection.execute("ALTER TABLE acquisition_anchors_v19 RENAME TO acquisitions")
        if sequence:
            await connection.execute("UPDATE sqlite_sequence SET seq=max(seq, ?) WHERE name='acquisitions'", (sequence[0],))
        await connection.execute("CREATE INDEX idx_acquisitions_status ON acquisitions(status)")
        await connection.execute("PRAGMA user_version=19")
        await connection.commit()
    except BaseException:
        await connection.rollback()
        raise
