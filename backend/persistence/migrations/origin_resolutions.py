"""Forward-only v22: explicit review decisions; never backfill from annotations."""


async def migrate(conn):
    await conn.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS provenance_origin_resolutions (
            txid TEXT NOT NULL CHECK(length(txid)=64 AND txid=lower(txid)
                AND txid NOT GLOB '*[^0-9a-f]*'),
            vout INTEGER NOT NULL CHECK(vout >= 0),
            resolution_type TEXT NOT NULL CHECK(resolution_type='NO_ACQUISITION'),
            comment TEXT CHECK(length(comment)<=4000),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            PRIMARY KEY(txid, vout)
        );
        CREATE TRIGGER IF NOT EXISTS origin_resolution_acquisition_conflict
        BEFORE INSERT ON provenance_origin_resolutions
        WHEN EXISTS (SELECT 1 FROM acquisitions
            WHERE origin_txid=NEW.txid AND origin_vout=NEW.vout)
        BEGIN SELECT RAISE(ABORT, 'Origin already has an Acquisition'); END;
        CREATE TRIGGER IF NOT EXISTS acquisition_origin_resolution_conflict
        BEFORE INSERT ON acquisitions
        WHEN EXISTS (SELECT 1 FROM provenance_origin_resolutions
            WHERE txid=NEW.origin_txid AND vout=NEW.origin_vout)
        BEGIN SELECT RAISE(ABORT, 'Remove No-Acquisition resolution before creating an Acquisition'); END;
        CREATE TRIGGER IF NOT EXISTS acquisition_origin_resolution_update_conflict
        BEFORE UPDATE OF origin_txid, origin_vout ON acquisitions
        WHEN EXISTS (SELECT 1 FROM provenance_origin_resolutions
            WHERE txid=NEW.origin_txid AND vout=NEW.origin_vout)
        BEGIN SELECT RAISE(ABORT, 'Origin has a No-Acquisition resolution'); END;
        CREATE TRIGGER IF NOT EXISTS origin_resolution_identity_immutable
        BEFORE UPDATE OF txid, vout ON provenance_origin_resolutions
        BEGIN SELECT RAISE(ABORT, 'Origin resolution identity is immutable'); END;
        PRAGMA user_version = 22;
        COMMIT;
    """)
