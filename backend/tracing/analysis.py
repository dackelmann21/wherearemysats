"""Blockchain Analysis mode walker.

A strictly additive module next to
the frozen Forward Provenance engine (spec section 14).

The walk is *ownership-independent*: an arbitrary transaction, address or
output can be investigated and every SPEND branch is followed automatically by
spend-state resolution, regardless of whether the outputs are owned. Following
a spending transaction NEVER classifies an output and NEVER writes ownership
evidence - ownership rows are only ever produced by the Forward Provenance
engine through registries / xpub / user confirmation (spec sections 3, 7, 9).

This walker reuses the engine's stable, read-only helpers
(``engine.load_transaction`` and ``engine.resolve_spend_state``) and the
topology / provenance fact helpers from ``backend.tracing.provenance``. It
implements its own BFS so safety limits (hops / transactions / outpoints) and
the ``follow`` re-walk semantics stay self-contained (spec section 6).

Provenance/topology shown for analysis outpoints are the same *facts* the
existing engine computes and may already display - computed here with the
empty ownership set, which is the honest structural reading: a single-in /
single-out transaction is mathematically EXACT, everything else without owned
outputs is AMBIGUOUS, nulldata/nonstandard outputs are UNKNOWN. No sat-level
attribution is ever claimed and nothing is upgraded because a user followed a
branch (spec sections 11, 12, 2).
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, Optional

from backend.models.schemas import (
    RelationshipType,
    SpendStatus,
    Trace,
    TraceEdge,
    TraceStatus,
)
from backend.rpc.bitcoin import (
    BlockNotSyncedError,
    NodeUnreachableError,
    RpcError,
    RpcTimeoutError,
)
from backend.tracing.errors import TraceNotFoundError
from backend.tracing.provenance import (
    batch_signals,
    meaningful_outputs,
    provenance_for_output,
    renumber_hops_longest_path,
    tx_topology,
)

logger = logging.getLogger("lineage.analysis")


class BlockchainAnalysisWalker:
    """Spend-state-driven forward walker for BLOCKCHAIN_ANALYSIS traces."""

    def __init__(self, engine, max_hops: int = 8, max_transactions: int = 200,
                 max_outpoints: int = 500):
        self.engine = engine
        self.db = engine.db
        self._max_hops = max_hops
        self._max_transactions = max_transactions
        self._max_outpoints = max_outpoints

    # ------------------------------------------------------------------
    # Stable read-only proxies over the shared engine.
    # ------------------------------------------------------------------

    async def load_transaction(self, txid: str) -> Any:
        # Ownership-neutral (L-10): effective ownership is derived in memory
        # only; no reconciliation / contradiction row is persisted.
        return await self.engine.load_transaction(txid, persist_ownership=False)

    async def resolve_spend_state(self, txid: str, vout: int) -> dict[str, Any]:
        return await self.engine.resolve_spend_state(txid, vout)

    # ------------------------------------------------------------------
    # Trace lifecycle
    # ------------------------------------------------------------------

    async def create_trace(
        self,
        name: str,
        start_txid: str,
        start_address: Optional[str],
        start_vout: Optional[int],
    ) -> tuple[Trace, dict[str, Any]]:
        """Create a BLOCKCHAIN_ANALYSIS trace session and walk it."""
        start_tx = await self.load_transaction(start_txid)
        if start_vout is not None and not any(
            o.vout == start_vout for o in start_tx.outputs
        ):
            raise RpcError("The supplied VOUT does not exist in this transaction.")

        # Blockchain Analysis performs NO ownership classification at the
        # start: the start outpoint(s) are pure traversal SEEDS. No ownership
        # row is ever written for them by the walker.
        trace = await self.db.create_trace(
            name=name,
            start_txid=start_txid,
            start_address=start_address,
            start_vout=start_vout,
            trace_mode="BLOCKCHAIN_ANALYSIS",
        )
        walk = await self.rewalk(trace.id)
        # rewalk updated the stored status (COMPLETE / ANALYSIS_LIMIT_REACHED);
        # return a fresh trace so callers/API see the final state.
        fresh = await self.db.get_trace(trace.id)
        if fresh:
            trace = fresh
        logger.info(
            "analysis trace created id=%d txid=%s ending=%s",
            trace.id, start_txid, walk.get("ending_condition"),
        )
        return trace, walk

    async def rewalk(
        self,
        trace_id: int,
        extra_seeds: Optional[list[tuple[str, int]]] = None,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        """Re-compute the analysis graph for a trace.

        ``extra_seeds`` adds outpoints to the walk seeds (used by ``follow``).
        The re-walk is idempotent: edges are rebuilt from scratch and
        previously-visited outpoints are deduplicated.
        """
        trace = await self.db.get_trace(trace_id)
        if not trace:
            raise TraceNotFoundError()

        seeds = await self._resolve_seeds(trace)
        if extra_seeds:
            seen: set[tuple[str, int]] = set()
            merged: list[tuple[str, int]] = []
            for s in list(seeds) + list(extra_seeds):
                if s not in seen:
                    seen.add(s)
                    merged.append(s)
            seeds = merged

        walk = await self._walk(trace_id, seeds, progress_callback=progress_callback)
        status = self._decide_status(walk)
        await self.db.update_trace(trace_id, status=status)
        return walk

    async def follow(self, trace_id: int, txid: str, vout: int, progress_callback: Any | None = None) -> dict[str, Any]:
        """Follow the spending transaction of a spent output (spec section 10).

        Re-anchors the followed outpoint at hop 0 so a limit-truncated graph
        can be deliberately extended. Always idempotent and never changes any
        ownership state.
        """
        trace = await self.db.get_trace(trace_id)
        if not trace:
            raise TraceNotFoundError()
        if trace.trace_mode != "BLOCKCHAIN_ANALYSIS":
            raise RpcError(
                "The follow action is only available for Blockchain Analysis traces."
            )

        tx = await self.load_transaction(txid)
        if not any(o.vout == vout for o in tx.outputs):
            raise RpcError("The supplied VOUT does not exist in this transaction.")
        spend = await self.resolve_spend_state(txid, vout)
        if spend.get("spend_status") != "SPENT" or not spend.get("spending_txid"):
            raise RpcError(
                "This output is not spent - there is no spending transaction to follow."
            )

        return await self.rewalk(trace_id, extra_seeds=[(txid, vout)], progress_callback=progress_callback)

    # ------------------------------------------------------------------
    # Seed resolution
    # ------------------------------------------------------------------

    async def _resolve_seeds(self, trace: Trace) -> list[tuple[str, int]]:
        """Resolve the start outpoint(s) of an analysis trace.

        Exact VOUT defines a single seed; address matches its outputs; no
        address and no VOUT means whole-transaction analysis over the
        meaningful (value-bearing) outputs.
        """
        tx = await self.load_transaction(trace.start_txid)
        if trace.start_vout is not None:
            if not any(o.vout == trace.start_vout for o in tx.outputs):
                raise RpcError(
                    "The supplied VOUT does not exist in this transaction."
                )
            return [(trace.start_txid, trace.start_vout)]
        if trace.start_address:
            matches = [o.vout for o in tx.outputs if o.address == trace.start_address]
            if not matches:
                raise RpcError(
                    "The supplied Bitcoin address does not match any output in this transaction."
                )
            return [(trace.start_txid, vout) for vout in matches]
        return [
            (trace.start_txid, o.vout) for o in meaningful_outputs(tx)
        ]

    # ------------------------------------------------------------------
    # Walk
    # ------------------------------------------------------------------

    async def _walk(self, trace_id: int, seeds: list[tuple[str, int]], progress_callback: Any | None = None) -> dict[str, Any]:
        await self.db.delete_trace_edges(trace_id)

        visited: set[tuple[str, int]] = set()
        visited_txs: set[str] = set()
        pending_unknown: list[tuple[str, int]] = []
        terminal_utxos: list[tuple[str, int]] = []
        max_hop_seen = 0
        limit_reason: Optional[str] = None

        frontier: deque[tuple[int, str, int]] = deque(
            (0, txid, vout) for txid, vout in seeds
        )

        while frontier and limit_reason is None:
            hop, txid, vout = frontier.popleft()
            key = (txid, vout)
            if key in visited:
                continue

            if hop > self._max_hops:
                limit_reason = "hop limit reached (%d)" % self._max_hops
                break
            if len(visited) >= self._max_outpoints:
                limit_reason = "outpoint limit reached (%d)" % self._max_outpoints
                break

            visited.add(key)
            visited_txs.add(txid)
            max_hop_seen = max(max_hop_seen, hop)

            if progress_callback is not None:
                await progress_callback(
                    transactions=len(visited_txs),
                    outpoints=len(visited),
                    hop=max_hop_seen,
                )

            try:
                tx = await self.load_transaction(txid)
            except (NodeUnreachableError, RpcTimeoutError, BlockNotSyncedError):
                # Node outage / auth / timeout / IBD is not missing history: fail the job.
                raise
            except RpcError:
                # Chain view hiccup: keep the outpoint pending instead of
                # discarding the partial walk collected so far.
                pending_unknown.append(key)
                continue

            try:
                spend = await self.resolve_spend_state(txid, vout)
            except (NodeUnreachableError, RpcTimeoutError, BlockNotSyncedError):
                raise
            except RpcError:
                # Spend state not resolvable (e.g. gettxout or a non-index
                # gettxspendingprevout failure): same pending treatment as an
                # unloadable source transaction (D-11).
                pending_unknown.append(key)
                continue
            spend_status = spend.get("spend_status")

            if spend_status == SpendStatus.UNSPENT:
                terminal_utxos.append(key)
                await self.db.update_outpoint_spend_status(txid, vout, "UNSPENT")
                continue

            if spend_status == SpendStatus.UNKNOWN or not spend.get("spending_txid"):
                pending_unknown.append(key)
                continue

            spending_txid = spend["spending_txid"]
            if len(visited_txs) >= self._max_transactions:
                limit_reason = "transaction limit reached (%d)" % self._max_transactions
                break

            try:
                spending_tx = await self.load_transaction(spending_txid)
            except (NodeUnreachableError, RpcTimeoutError, BlockNotSyncedError):
                raise
            except RpcError:
                # The spend itself is a resolved blockchain fact (already
                # cached by resolve_spend_state), but the spending transaction
                # cannot be loaded: no edge and no successor can be built, so
                # the source stays pending instead of failing the whole walk
                # (D-11). It is not counted as a visited transaction.
                pending_unknown.append(key)
                continue
            visited_txs.add(spending_txid)

            # Persist whole-transaction topology + per-output provenance facts
            # for every output of the spending transaction (same facts the
            # forward engine displays, computed without ownership context).
            tx_top = tx_topology(spending_tx)
            is_batch, _reasons = batch_signals(spending_tx)
            is_batch_int = 1 if is_batch else 0
            for out_o in spending_tx.outputs:
                # P4-4: the outpoints row is a cache shared with the
                # ownership-aware walkers (forward engine, reverse walkers).
                # A provenance value they already computed (anything but the
                # NULL / UNKNOWN default; UNKNOWN is tx-intrinsic and equal
                # for every writer) is kept: the empty-owned-set reading must
                # not clobber it. Topology / batch flag are whole-transaction
                # facts and are always written.
                existing = await self.db.get_outpoint(out_o.txid, out_o.vout)
                keep_provenance = bool(
                    existing
                    and existing.get("provenance_state") not in (None, "UNKNOWN")
                )
                row = {
                    "txid": out_o.txid,
                    "vout": out_o.vout,
                    "address": out_o.address,
                    "amount_sats": out_o.value_sats,
                    "script_pubkey": out_o.script_pubkey,
                    "script_type": out_o.script_type,
                    "block_height": out_o.block_height,
                    "block_time": out_o.block_time,
                    "spend_status": out_o.spend_status,
                    "transaction_topology": tx_top,
                    "likely_batch": is_batch_int,
                }
                if not keep_provenance:
                    # When kept, the in-memory values already mirror the
                    # stored row (applied from the cache by load_transaction).
                    prov_state, prov_reasons = provenance_for_output(
                        spending_tx, out_o.vout, set()
                    )
                    out_o.provenance_state = prov_state
                    out_o.provenance_reasons = prov_reasons
                    row["provenance_state"] = prov_state
                    row["provenance_reasons"] = prov_reasons
                out_o.transaction_topology = tx_top
                out_o.likely_batch = bool(is_batch_int)
                await self.db.upsert_outpoint(row)

            # Mark the traced source outpoint SPENT (blockchain fact cache).
            for out in tx.outputs:
                if out.vout == vout:
                    out.spend_status = SpendStatus.SPENT
                    out.spending_txid = spending_txid
                    out.spending_vin = spend.get("vin_index")
                    await self.db.upsert_outpoint(
                        {
                            "txid": out.txid,
                            "vout": out.vout,
                            "address": out.address,
                            "amount_sats": out.value_sats,
                            "script_pubkey": out.script_pubkey,
                            "script_type": out.script_type,
                            "block_height": out.block_height,
                            "block_time": out.block_time,
                            "spend_status": "SPENT",
                        }
                    )
                    break

            # Edge into every successor output so the spending transaction is
            # rendered with its FULL input/output context (spec section 11:
            # merge/consolidation inputs stay visible, no sat attribution).
            targets = [o for o in meaningful_outputs(spending_tx)]
            if not targets:
                # A fee-burn / all-nulldata spender: render the transaction but
                # do not fabricate terminals for meaningless outputs.
                targets = list(spending_tx.outputs)
            else:
                for o in targets:
                    frontier.append((hop + 1, spending_txid, o.vout))
            for o in targets:
                edge = TraceEdge(
                    trace_id=trace_id,
                    source_txid=txid,
                    source_vout=vout,
                    spending_txid=spending_txid,
                    destination_txid=spending_txid,
                    destination_vout=o.vout,
                    hop_number=hop,
                    relationship_type=RelationshipType.UNKNOWN,
                )
                await self.db.upsert_edge(edge)

        await self._finalize_relationships(trace_id)

        return {
            "visited_outpoints": [f"{t}:{v}" for t, v in visited],
            "pending_unknown": [f"{t}:{v}" for t, v in pending_unknown],
            "terminal_utxos": [f"{t}:{v}" for t, v in terminal_utxos],
            "ending_condition": (
                "ANALYSIS_LIMIT_REACHED" if limit_reason else "COMPLETE"
            ),
            "limit_reason": limit_reason,
            "counts": {
                "transactions": len(visited_txs),
                "outpoints": len(visited),
                "hops": max_hop_seen,
            },
        }

    async def _finalize_relationships(self, trace_id: int) -> None:
        """Structural relationship types per spending transaction.

        Identical rules to the forward engine, driven purely by traced-input
        multiplicity and whole-transaction shape (no ownership involved).
        Display hops follow the longest materialized DAG path like the engine
        (D-10): the BFS walk writes shortest-path hops, so without the
        renumbering a merge reached via branches of different depth and its
        successor collapse into one hop-view column. Every edge is upserted
        below, which persists the renumbered ``hop_number``.
        """
        edges = await self.db.get_edges(trace_id)
        if not edges:
            return
        renumber_hops_longest_path(edges)

        by_tx: dict[str, list[TraceEdge]] = {}
        for e in edges:
            by_tx.setdefault(e.spending_txid, []).append(e)

        for spending_txid, tx_edges in by_tx.items():
            source_set = {(e.source_txid, e.source_vout) for e in tx_edges}
            dest_set = {(e.destination_txid, e.destination_vout) for e in tx_edges}
            n_in = len(source_set)
            n_out = len(dest_set)

            external_inputs = 0
            try:
                stx = await self.load_transaction(spending_txid)
                total_inputs = len(
                    [i for i in stx.inputs if i.txid or i.prev_outpoint != "coinbase"]
                )
                external_inputs = max(0, total_inputs - n_in)
            except (NodeUnreachableError, RpcTimeoutError, BlockNotSyncedError):
                # Node outage / auth / timeout / IBD is not missing history: fail the job.
                raise
            except RpcError:
                external_inputs = 0

            if n_in == 0:
                rel = RelationshipType.UNKNOWN
            elif n_in > 1 and n_out > 1:
                rel = RelationshipType.MIXED
            elif n_in > 1:
                rel = RelationshipType.MERGED
            elif n_out > 1:
                rel = RelationshipType.SPLIT
            elif external_inputs > 0:
                rel = RelationshipType.MIXED
            else:
                rel = RelationshipType.DIRECT

            for e in tx_edges:
                e.relationship_type = rel
                await self.db.upsert_edge(e)

    def _decide_status(self, walk: dict[str, Any]) -> str:
        if walk.get("ending_condition") == "ANALYSIS_LIMIT_REACHED":
            return TraceStatus.ANALYSIS_LIMIT_REACHED
        return TraceStatus.COMPLETE