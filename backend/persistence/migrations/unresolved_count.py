"""Forward-only v23: unresolved predecessor count per analysis job (P1-3c, SK-3).

Adds the nullable ``analysis_jobs.unresolved_count`` (no default). Existing
jobs keep NULL ("not recorded", never 0).
"""


async def migrate(conn):
    await conn.commit()
    await conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = await conn.execute("PRAGMA table_info(analysis_jobs)")
        if "unresolved_count" not in {str(r[1]) for r in await cursor.fetchall()}:
            await conn.execute("ALTER TABLE analysis_jobs ADD COLUMN unresolved_count INTEGER")
        await conn.execute("PRAGMA user_version = 23")
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise
