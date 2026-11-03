"""Forward-only v24: key access flag per Registry entry (FIX-REG-1, REG-1).

Adds ``known_addresses.key_access`` (INTEGER NOT NULL DEFAULT 1). Existing
entries keep today's behavior (key access yes). An entry with 0 stays address
ownership evidence (SK-4b) but is not scanned and never counts in holdings.
"""


async def migrate(conn):
    await conn.commit()
    await conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = await conn.execute("PRAGMA table_info(known_addresses)")
        if "key_access" not in {str(r[1]) for r in await cursor.fetchall()}:
            await conn.execute(
                "ALTER TABLE known_addresses ADD COLUMN key_access INTEGER NOT NULL DEFAULT 1")
        await conn.execute("PRAGMA user_version = 24")
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise
