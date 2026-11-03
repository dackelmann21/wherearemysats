"""Forward-only v25: UNIQUE index on ``exchange_exits.txid`` (P2-N3-Rest).

``migrate`` only bumps ``user_version`` to 25 (marker; the schema itself is
unchanged). ``ensure_unique_index`` runs on EVERY connect: with no duplicate
exit TXIDs it creates ``ux_exchange_exits_txid``; with legacy duplicates the
app still starts without the index and a warning names each duplicate TXID
with its Exit ids. Once the duplicates are deleted, the next start creates
the index (idempotent). The service check (P2-5b) stays the primary guard;
the index is the DB-level fallback for writes that bypass it.
"""

import logging

logger = logging.getLogger("lineage.db")

_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_exchange_exits_txid "
    "ON exchange_exits(txid)"
)


async def migrate(conn):
    await conn.execute("PRAGMA user_version = 25")
    await conn.commit()


async def ensure_unique_index(conn):
    cursor = await conn.execute(
        "SELECT txid, GROUP_CONCAT(id) AS ids FROM "
        "(SELECT id, txid FROM exchange_exits ORDER BY id) "
        "GROUP BY txid HAVING COUNT(*) > 1 ORDER BY txid"
    )
    duplicates = await cursor.fetchall()
    if duplicates:
        for row in duplicates:
            logger.warning(
                "exchange_exits: exit transaction %s is used by Exits %s; "
                "unique index ux_exchange_exits_txid not created - delete the "
                "duplicate Exits, the next start creates the index",
                row[0], row[1],
            )
        return
    await conn.execute(_INDEX_SQL)
    await conn.commit()
