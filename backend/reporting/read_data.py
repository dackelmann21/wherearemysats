"""Bulk, non-persisting inputs for derived reports; no competing ownership rules."""

import asyncio
import re
from collections import defaultdict

from backend.models.schemas import OutputInfo, TransactionInfo


OWNERSHIP_EVIDENCE = {
    "USER", "KNOWN_ADDRESS", "ADDRESS_REGISTRY", "DESCRIPTOR", "XPUB",
    "MANUAL_IMPORT", "XPUB_DISCOVERY", "USER_CONFIRMED",
}


_TXID = re.compile(r'[0-9a-f]{64}')


def valid_vout(vout):
    return type(vout) is int and vout >= 0


def valid_input(vin):
    """One validation for a cached ``raw_json`` vin reference (D-20).

    Exact canonical identity only: a lower-case 64-hex TXID and a non-negative
    integer VOUT (``bool`` is rejected). Coinbase inputs carry no reference.
    """
    return (isinstance(vin, dict) and isinstance(vin.get('txid'), str) and _TXID.fullmatch(vin['txid']) is not None
            and valid_vout(vin.get('vout')))


def input_keys(raw):
    """Sorted distinct valid ``(txid, vout)`` input references of a cached transaction."""
    if not isinstance(raw, dict) or not isinstance(raw.get('vin'), list):
        return []
    return sorted({(i['txid'], i['vout']) for i in raw['vin'] if valid_input(i)})


class ReadBudgetExceeded(Exception):
    """Incomplete processing must never be represented as a complete score."""


async def rows(db, sql, params=(), *, limit=100_000):
    result = []
    async with db.db.execute(sql, params) as cursor:
        while batch := await cursor.fetchmany(256):
            result.extend(dict(r) for r in batch)
            if len(result) > limit:
                raise ReadBudgetExceeded("Read-model size limit exceeded.")
            await asyncio.sleep(0)
    return result


async def selected_rows(db, table, column, values, *, limit=100_000, order_by=None):
    """All rows of ``table`` (``values`` None) or those whose ``column`` is in ``values``.

    ``order_by`` is an optional column name (code literal) applied to every
    query; with a filter, rows are ordered within each batch of 400 values,
    so the order holds per ``column`` value, not across the whole result.
    """
    ordered = f" ORDER BY {order_by}" if order_by else ""
    if values is None:
        return await rows(db, f"SELECT * FROM {table}{ordered}", limit=limit)
    values = list(set(values))
    result = []
    for offset in range(0, len(values), 400):
        batch = values[offset:offset + 400]
        result.extend(await rows(db, f"SELECT * FROM {table} WHERE {column} IN "
                                 f"({','.join('?' for _ in batch)}){ordered}", batch, limit=limit))
        if len(result) > limit:
            raise ReadBudgetExceeded("Read-model size limit exceeded.")
    return result


async def bounded_map(keys, operation, concurrency=8):
    """Unique keys, fixed worker count, and joined cleanup on error/cancellation."""
    pending = iter(dict.fromkeys(keys))
    result = {}

    async def worker():
        for key in pending:
            result[key] = await operation(key)

    tasks = [asyncio.create_task(worker()) for _ in range(max(1, concurrency))]
    try:
        await asyncio.gather(*tasks)
        return result
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class ReadData:
    @classmethod
    async def load(cls, db, engine, txids=None, *, limit=100_000):
        self = cls()
        stored = await selected_rows(db, "outpoints", "txid", txids, limit=limit)
        owned = await selected_rows(db, "ownership", "txid", txids, limit=limit)
        self.outpoints = {(r["txid"], r["vout"]): r for r in stored}
        self.ownership = {(r["txid"], r["vout"]): r for r in owned}
        addresses = {r["address"] for r in stored if r.get("address")}
        self.known = {r["address"]: r for r in await selected_rows(
            db, "known_addresses", "address", addresses, limit=limit)}
        self.evidence = defaultdict(list)
        for r in await selected_rows(db, "address_evidence", "address", addresses, limit=limit):
            self.evidence[r["address"]].append(r)
        self.outputs = {}
        for index, key in enumerate(self.outpoints.keys() | self.ownership.keys()):
            if index >= limit:
                raise ReadBudgetExceeded("Read-model size limit exceeded.")
            row = self.outpoints.get(key, self.ownership.get(key))
            out = OutputInfo(txid=key[0], vout=key[1], outpoint=f"{key[0]}:{key[1]}",
                             address=row.get("address"), value_sats=row.get("amount_sats"))
            await engine.enrich_outputs_readonly(TransactionInfo(txid=key[0], outputs=[out]), data=self)
            self.outputs[key] = out
            if index % 128 == 0:
                await asyncio.sleep(0)
        return self

    async def get_outpoint(self, txid, vout):
        return self.outpoints.get((txid, vout))

    async def get_ownership(self, txid, vout):
        return self.ownership.get((txid, vout))

    async def get_known_address(self, address):
        return self.known.get(address)

    async def is_known_address(self, address):
        return address in self.known

    async def get_address_evidence(self, address):
        return self.evidence.get(address, [])


def evidence_owned(out):
    return out.ownership_status == "OWNED" and out.classification_source in OWNERSHIP_EVIDENCE


async def custody_boundaries(db):
    return {(r["txid"], r["vout"]) for r in await rows(db,
        "SELECT e.txid, o.vout FROM exchange_exits e JOIN exchange_exit_outputs o "
        "ON o.exchange_exit_id = e.id WHERE o.is_exchange_deposit = 1 "
        "UNION SELECT txid, vout FROM custodial_boundaries")}


async def revision(db):
    # Same-connection writes plus commits by other connections (analysis worker).
    async with db.db.execute("PRAGMA data_version") as cursor:
        version = (await cursor.fetchone())[0]
    return db.db.total_changes, version
