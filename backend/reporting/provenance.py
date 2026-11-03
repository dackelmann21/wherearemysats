"""Read-only Acquisition provenance for managed outputs in saved traces.

Reuse Coverage's graph, evidence frontier, custody stops and work limits.
This report includes historical outputs, so it never claims a current balance.
UNANCHORED is neither an ownership classification nor a stored output flag.
"""
import asyncio
import time

from backend.config import get_settings
from backend.reporting.coverage import (
    DocumentationGraph, _ancestry, acquisition_provenance, documentation_context,
)
from backend.reporting.read_data import ReadBudgetExceeded, ReadData, evidence_owned, revision, rows


async def graph_for_outputs(db, engine, keys, settings, deadline):
    data = await ReadData.load(db, engine, txids={k[0] for k in keys},
                               limit=settings.coverage_max_outpoints)
    anchors, boundaries, required, conflicts = await documentation_context(db, data.outputs)
    transactions = await _ancestry(db, keys, set(anchors) | boundaries | conflicts,
                                  settings.coverage_max_transactions)
    graph = DocumentationGraph(transactions, anchors, boundaries, required, conflicts, deadline=deadline)
    return graph, data


async def managed_provenance(db, engine, *, trace_id=None, outpoint=None, state=None):
    settings = get_settings()
    deadline = time.monotonic() + settings.coverage_timeout
    try:
        async with asyncio.timeout(settings.coverage_timeout):
            before = await revision(db)
            index = await db.get_outpoint_trace_index() if outpoint is None else {}
            if outpoint is None:
                # Older address-seeded traces can be terminal and have no
                # edges or explicit start_vout. Resolve only their recorded
                # start address inside the recorded transaction, from cache.
                for seed in await rows(db, """SELECT t.id, o.txid, o.vout
                    FROM traces t JOIN outpoints o
                      ON o.txid=t.start_txid AND o.address=t.start_address
                    WHERE t.start_vout IS NULL AND t.start_address IS NOT NULL""",
                    limit=settings.coverage_max_outpoints):
                    ids = index.setdefault(f"{seed['txid']}:{seed['vout']}", [])
                    if seed['id'] not in ids:
                        ids.append(seed['id'])
                        ids.sort()
            keys = {outpoint} if outpoint is not None else {
                (key.rsplit(':', 1)[0], int(key.rsplit(':', 1)[1]))
                for key, ids in index.items() if trace_id is None or trace_id in ids
            }
            if len(keys) > settings.coverage_max_outpoints:
                raise ReadBudgetExceeded('Managed output limit exceeded.')
            graph, data = await graph_for_outputs(db, engine, keys, settings, deadline)
            result = []
            for key in sorted(keys):
                out = data.outputs.get(key)
                if out is None or not evidence_owned(out) or key in graph.boundaries:
                    continue
                evidence = graph.evaluate(key)
                provenance = acquisition_provenance(evidence.state)
                if state is not None and state != provenance:
                    continue
                result.append(dict(txid=key[0], vout=key[1], outpoint=out.outpoint,
                    acquisition_provenance=provenance, documentation_state=evidence.state,
                    provenance_link_state=evidence.link_state, acquisition_ids=sorted(evidence.acquisitions),
                    trace_ids=index.get(out.outpoint, [])))
                await asyncio.sleep(0)
            if await revision(db) != before:
                raise ReadBudgetExceeded('Evidence changed; retry.')
            return result
    except TimeoutError as exc:
        raise ReadBudgetExceeded('Provenance execution budget exceeded; retry.') from exc
