"""One query source for Acquisition-position custody stops and explained spends (D-15).

``descendant_summary`` reads these facts either for one Acquisition or, via the
reporting ``PositionBatch``, for every Acquisition at once. Both paths use the
SQL below so their filters cannot drift apart:

- Exit deposit stops require an explicit source link to the Acquisition;
  explicit ``custodial_boundaries`` apply to every Acquisition.
- Explained spends are Exit sources and disposal blockchain evidence linked to
  the Acquisition.
"""

from collections import defaultdict

from backend.reporting.read_data import rows

# FROM/WHERE shared by both paths; per-Acquisition callers append a filter.
_EXIT_DEPOSITS = (
    "FROM exchange_exit_sources s JOIN exchange_exits e ON e.id = s.exchange_exit_id "
    "JOIN exchange_exit_outputs o ON o.exchange_exit_id = e.id WHERE o.is_exchange_deposit = 1")
CUSTODIAL_BOUNDARIES_SQL = "SELECT txid, vout FROM custodial_boundaries"
EXIT_DEPOSIT_BOUNDARIES_SQL = "SELECT s.acquisition_id, e.txid, o.vout " + _EXIT_DEPOSITS
_EXPLAINED_SOURCES = (
    "SELECT acquisition_id, source_txid, source_vout FROM exchange_exit_sources{exit_filter} "
    "UNION SELECT l.acquisition_id, e.source_txid, e.source_vout FROM disposal_blockchain_evidence e "
    "JOIN disposal_acquisition_links l ON l.disposal_id = e.disposal_id{link_filter}")
EXPLAINED_SOURCES_SQL = _EXPLAINED_SOURCES.format(exit_filter="", link_filter="")


async def custody_boundaries_for(db, acquisition_id):
    """Exit deposit stops linked to this Acquisition plus explicit boundaries."""
    return {(r["txid"], r["vout"]) for r in await rows(db,
        "SELECT e.txid, o.vout " + _EXIT_DEPOSITS + " AND s.acquisition_id = ? UNION "
        + CUSTODIAL_BOUNDARIES_SQL, (acquisition_id,))}


async def explained_sources_for(db, acquisition_id):
    return {(r["source_txid"], r["source_vout"]) for r in await rows(db, _EXPLAINED_SOURCES.format(
        exit_filter=" WHERE acquisition_id = ?", link_filter=" WHERE l.acquisition_id = ?"),
        (acquisition_id, acquisition_id))}


async def custodial_boundaries(db):
    return {(r["txid"], r["vout"]) for r in await rows(db, CUSTODIAL_BOUNDARIES_SQL)}


async def exit_deposit_boundaries_by_acquisition(db, *, limit=100_000):
    result = defaultdict(set)
    for r in await rows(db, EXIT_DEPOSIT_BOUNDARIES_SQL, limit=limit):
        result[r["acquisition_id"]].add((r["txid"], r["vout"]))
    return result


async def explained_sources_by_acquisition(db, *, limit=100_000):
    result = defaultdict(set)
    for r in await rows(db, EXPLAINED_SOURCES_SQL, limit=limit):
        result[r["acquisition_id"]].add((r["source_txid"], r["source_vout"]))
    return result
