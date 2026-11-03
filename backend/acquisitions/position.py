"""Evidence-backed acquisition positions, distinct from Documentation Coverage.

Fresh spend facts live only in a small TTL cache, or in a supplied request-local
Inventory observation map. Ownership/custody/economics are always re-read; no
position/review state is persisted by this read-model.
"""

import asyncio
import time
from collections import defaultdict

from backend.acquisitions.position_queries import custody_boundaries_for, explained_sources_for
from backend.config import get_settings
from backend.models.schemas import AcquisitionDescendant
from backend.reporting.read_data import (
    ReadBudgetExceeded, ReadData, bounded_map,
    evidence_owned, revision,
)
from backend.rpc.bitcoin import RpcError


class PositionContext:
    """One time budget across an entire list/count request, not per acquisition."""
    def __init__(self, settings=None, *, current_outputs=None):
        self.settings = settings or get_settings()
        self.deadline = time.monotonic() + self.settings.position_timeout
        self.current_outputs = current_outputs
        self._report_spends = None
        self.saved_facts = self.saved_outputs = self.batch = self.summaries = None

    async def spends(self, db, engine, keys):
        if self.saved_facts is not None:
            return {key: self.saved_facts.get(key, {'spend_status': 'UNKNOWN', 'spending_txid': None})
                    for key in keys}
        token = await revision(db)
        cache = (getattr(engine, "_position_spends", None) if self.current_outputs is None
                 else self._report_spends)
        if cache is None or cache[0] != token:
            cache = (token, {})
            if self.current_outputs is None:
                engine._position_spends = cache
            else:
                self._report_spends = cache
        values = cache[1]
        now = time.monotonic()
        result, missing = {}, []
        for key in keys:
            entry = values.get(key)
            if entry and (self.current_outputs is not None or now - entry[0] < self.settings.position_spend_ttl):
                result[key] = entry[1]
            else:
                missing.append(key)

        async def check(key):
            # Include mempool. Neither a cached UNSPENT flag nor a trace edge
            # establishes that this output is still a current holding.
            if self.current_outputs is None:
                current = await engine.rpc.get_txout(*key, include_mempool=True)
            else:
                if key not in self.current_outputs:
                    self.current_outputs[key] = await engine.rpc.get_txout(*key, include_mempool=True)
                current = self.current_outputs[key]
            if current is not None:
                return {"spend_status": "UNSPENT", "spending_txid": None}
            spend = await db.get_spend(*key)
            if not spend or not spend.get("spending_txid"):
                matches = await engine.rpc.get_tx_spending_prevout([key], mempool_only=False)
                spend = {"spending_txid": matches[0].get("spendingtxid")} if matches else {}
            return {"spend_status": "SPENT" if spend.get("spending_txid") else "UNKNOWN",
                    "spending_txid": spend.get("spending_txid")}

        if missing:
            async with asyncio.timeout(max(0, self.deadline - time.monotonic())):
                fresh = await bounded_map(missing, check,
                    self.settings.coverage_rpc_concurrency if self.current_outputs is not None else 8)
            if len(values) + len(fresh) > 10_000:
                values.clear()
            for key, fact in fresh.items():
                if len(values) < 10_000:
                    values[key] = (time.monotonic(), fact)
            result.update(fresh)
        return result


async def descendant_summary(db, engine, acquisition, context=None):
    context = context or PositionContext()
    summaries = context.summaries
    if summaries is not None and acquisition['id'] in summaries:
        return summaries[acquisition['id']]
    try:
        async with asyncio.timeout(max(0, context.deadline - time.monotonic())):
            result = await _summary(db, engine, acquisition, context)
            if summaries is not None:
                summaries[acquisition['id']] = result
            return result
    except (TimeoutError, RpcError, ReadBudgetExceeded):
        # Fail closed for CURRENT, but do not manufacture an economic disposal.
        return AcquisitionDescendant(spend_unknown=True, needs_review=True,
            note="Current ownership/spend facts could not be fully verified; retry or re-check the trace.")


def open_movements(descendant):
    """Combined open-movement display list (FIX-UI-6/FIX-UI-7): unresolved and
    partial unexplained spends, chronological by spending block time (undated
    last, then by outpoint). Presentation order only; same ordering as the
    acquisitions page (format.js openMovementText callers)."""
    items = list(descendant.unresolved_spends) + list(descendant.partial_unexplained_spends)
    return sorted(items, key=lambda s: (s.get("spending_block_time") is None,
                                        s.get("spending_block_time") or 0,
                                        f"{s.get('txid')}:{s.get('vout')}"))


def _traversable_owned(out):
    """Existing effective OWNED classification carries an Acquisition path.

    This is not independent wallet membership: Acquisition endpoint qualification
    still uses evidence_owned(); managed Inventory uses independent scans.
    """
    return out is not None and out.ownership_status == "OWNED"


async def _position_custody_boundaries(db, acquisition_id):
    """Exit deposit stops require an explicit source link to this Acquisition.

    Preserved/source-less Exits remain historical facts, not implicit links to
    re-imported Acquisitions. Explicit custodial_boundaries retain their existing
    scope; the global reporting boundary helper is intentionally separate.
    """
    return await custody_boundaries_for(db, acquisition_id)


async def _summary(db, engine, acquisition, context):
    trace_id = acquisition.get("trace_id")
    if trace_id is None:
        return AcquisitionDescendant(note="No linked trace yet.")
    batch = context.batch
    trace = batch.traces.get(trace_id) if batch else await db.get_trace(trace_id)
    if not trace:
        return AcquisitionDescendant(note="The linked trace no longer exists.")
    edges = batch.edges.get(trace_id, []) if batch else await db.get_edges(trace_id)
    origin = (acquisition.get("origin_txid"), acquisition.get("origin_vout"))
    keys = {origin} if origin[0] is not None and origin[1] is not None else set()
    children = defaultdict(set)
    for edge in edges:
        source = (edge.source_txid, edge.source_vout)
        dest = (edge.destination_txid, edge.destination_vout)
        keys.update((source, dest))
        children[source].add(dest)
    bridges = batch.bridges.get(trace_id, set()) if batch else set(await db.custody_bridge_destinations(trace_id))
    keys.update(bridges)
    data = batch.data_for(keys) if batch else await ReadData.load(db, engine, {key[0] for key in keys})
    boundaries = (batch.boundaries(acquisition['id']) if batch else
                  await _position_custody_boundaries(db, acquisition["id"]))
    boundaries |= {key for key, out in data.outputs.items() if out.ownership_status == "CUSTODIAL_DEPOSIT"}

    # Walk custody scope, not generic graph reachability. UNKNOWN/EXTERNAL
    # successors remain context and cannot carry the position further downstream.
    seeds = {origin} if origin in keys else set()
    seeds.update(bridges)
    reachable, context_keys = set(), set()
    pending = list(seeds)
    while pending:
        key = pending.pop()
        if key in reachable or key in boundaries:
            continue
        reachable.add(key)
        out = data.outputs.get(key)
        if key not in seeds and not _traversable_owned(out):
            continue
        for dest in children[key]:
            context_keys.add(dest)
            target = data.outputs.get(dest)
            if _traversable_owned(target) and dest not in boundaries:
                pending.append(dest)
    # Check spends/continuation on every effective OWNED branch, including
    # PROPAGATED intermediates; source metadata must not fragment the path.
    owned = {key for key in reachable if _traversable_owned(data.outputs.get(key))}
    # The UNKNOWN origin is useful only as optional blockchain evidence. Its
    # spend facts never promote it (or its successors) to OWNED.
    check_keys = owned | ({origin} if origin in reachable else set())
    facts = await context.spends(db, engine, check_keys)
    # Discard stale graph continuations when the source is now unspent, or its
    # current spender differs from the transaction represented by the edges.
    live_reachable, pending = set(), list(seeds)
    while pending:
        key = pending.pop()
        if key in live_reachable or key not in reachable:
            continue
        live_reachable.add(key)
        fact = facts.get(key, {})
        if fact.get("spend_status") == "SPENT":
            pending.extend(k for k in children[key] if k in owned and k[0] == fact["spending_txid"])
    owned &= live_reachable
    check_keys &= live_reachable
    unspent = [key for key in owned if facts[key]["spend_status"] == "UNSPENT"
               and evidence_owned(data.outputs[key])]
    unspent.sort(key=lambda k: (data.outpoints.get(k, {}).get("block_height") or 0, k))
    unknown = any(facts[key]["spend_status"] == "UNKNOWN" for key in owned)

    explained = (batch.explained.get(acquisition['id'], set()) if batch else
                 await explained_sources_for(db, acquisition["id"]))
    unresolved, spent_terminals, owned_spends, partial_unexplained, open_side = [], [], [], [], False
    for key in sorted(check_keys):
        fact = facts[key]
        if fact["spend_status"] != "SPENT" or key in explained:
            continue
        successors = {k for k in children[key] if k[0] == fact["spending_txid"]}
        # Established OWNED successors carry the position; UNKNOWN/EXTERNAL
        # siblings remain context. Every owned successor is checked in turn,
        # so a resolved branch cannot hide another owned branch's unresolved
        # spend. Without owned continuation retain explicit custody/economic
        # review: EXTERNAL/topology alone still cannot explain a spent source.
        owned_continuation = bool(successors & owned)
        foreign = [k for k in successors
                   if k not in boundaries and not _traversable_owned(data.outputs.get(k))
                   and (data.outputs[k].value_sats != 0 if k in data.outputs else True)]
        foreign_side = bool(foreign)
        unexplained = not owned_continuation and (not successors or foreign_side)
        # An unexplained spend with an owned continuation may still leave a
        # non-owned, non-boundary sibling that could be in own custody.
        open_side |= owned_continuation and foreign_side
        out = data.outputs.get(key)
        # FIX-UI-6: presentation data naming the open movement. The spending
        # block time comes from the spending tx's outputs already in the read
        # model; exit_candidate is a UI hint only (trace-local successors show
        # a non-owned, non-boundary value output) — the Exit candidates API
        # remains authoritative and no ownership/custody claim is made.
        spending_block_time = next(
            (data.outpoints[k]["block_time"] for k in sorted(successors)
             if data.outpoints.get(k, {}).get("block_time") is not None), None)
        item = dict(txid=key[0], vout=key[1], spending_txid=fact["spending_txid"],
                    address=out.address if out else None, amount_sats=out.value_sats if out else None,
                    spending_block_time=spending_block_time,
                    exit_candidate=bool(successors) and foreign_side)
        owned_spends.append(item)
        if not children[key]:
            spent_terminals.append(item)
        if key in owned and unexplained:
            unresolved.append(item)
        # FIX-UI-7: the FX-3b open-side case (own change AND a foreign value
        # output) is named as an open movement too. Same facts, display only.
        if key in owned and owned_continuation and foreign_side:
            known = [data.outputs[k].value_sats for k in foreign
                     if k in data.outputs and data.outputs[k].value_sats is not None]
            partial_unexplained.append(dict(item, foreign_amount_sats=sum(known) if known else None))

    # Every spent endpoint is explained (unresolved is empty) and nothing is
    # unspent or unverified; at least one spent source is an explained Exit,
    # and no unexplained spend leaves an open foreign side output.
    fully_exited = (not unresolved and not open_side and not unknown and not unspent
                    and not any(facts[k]["spend_status"] == "UNSPENT" for k in check_keys)
                    and any(facts[k]["spend_status"] == "SPENT" and k in explained for k in check_keys))

    leaves = (reachable | context_keys) - {k for k, dests in children.items() if dests}
    primary = data.outputs[unspent[0]] if unspent else None
    mixed = any(e.relationship_type in ("MERGED", "MIXED") for e in edges)
    mixed |= any(data.outputs[k].provenance_state == "MIXED" for k in owned)
    note = ""
    if unresolved:
        note = "An owned source was spent without a resolved custody/economic classification."
    elif not unspent:
        note = "No current evidence-backed owned unspent descendant."
    elif mixed:
        note = "Current descendant UTXOs include mixed inputs; exact sat-level attribution is not claimed."
    return AcquisitionDescendant(
        terminal_count=len(leaves), unspent_terminal_count=len(unspent),
        verified_unspent_outpoints=[{
            "txid": key[0], "vout": key[1],
            "address": data.outputs[key].address if key in data.outputs else None,
            "amount_sats": data.outputs[key].value_sats if key in data.outputs else None,
        } for key in sorted(check_keys) if facts[key]["spend_status"] == "UNSPENT"
            and (key == origin or key in unspent)],
        primary_terminal_txid=primary.txid if primary else None,
        primary_terminal_vout=primary.vout if primary else None,
        primary_terminal_address=primary.address if primary else None,
        primary_terminal_amount_sats=primary.value_sats if primary else None,
        mixed=mixed, spend_unknown=unknown, fully_exited=fully_exited, needs_review=bool(unresolved) or unknown,
        unresolved_spends=unresolved, partial_unexplained_spends=partial_unexplained,
        spent_terminals=spent_terminals, owned_spends=owned_spends, note=note)
