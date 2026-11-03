"""Blockchain Analysis - Reverse walker.

REVERSE_BLOCKCHAIN_ANALYSIS_REQUIREMENTS_v0.6.0.md. An isolated, ownership-
neutral reverse ancestry walker for BLOCKCHAIN_ANALYSIS_REVERSE traces.

It answers: starting from any Bitcoin outpoint (``TXID:VOUT``), what on-chain
transaction ancestry leads to this output?

Ownership is display metadata ONLY and never controls traversal: every
resolvable predecessor input is followed backward regardless of whether the
predecessor is OWNED / UNKNOWN / EXTERNAL. The walk stops only at natural
terminals (coinbase origins, unavailable historical data), hard safety limits
or actual errors. A coinbase predecessor is recorded as an origin as soon as
it is discovered and is never walked itself (same rule as Reverse
Provenance).

Bitcoin transaction relationships alone drive the walk. The existing
``analysis_jobs`` table is reused, so this mode gets the same
QUEUED/RUNNING/COMPLETE/LIMIT_REACHED/FAILED background life cycle as forward
Blockchain Analysis; the worker dispatches by trace mode.

Graph orientation stays chronological (``older inputs -> transaction -> newer
outputs``); only discovery runs backward. ``hop_number`` on each edge stores
the reverse discovery hop of the edge's DESTINATION (target = 0, its
producing-transaction predecessor inputs = 1, deeper = 2, ...).
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, Optional

from backend.models.schemas import (
    RelationshipType,
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
from backend.tracing._reverse_common import (
    finalize_relationships,
    merge_seeds,
    persist_tx_context,
)
from backend.tracing.errors import TraceNotFoundError

logger = logging.getLogger("lineage.reverse_analysis")


class ReverseBlockchainAnalysisWalker:
    """Ownership-neutral backward walker for BLOCKCHAIN_ANALYSIS_REVERSE.

    Edge orientation reuses the forward ``trace_edges`` shape exactly:
    ``source`` = older predecessor outpoint, ``spending_txid`` = the producing
    transaction, ``destination`` = the newer traced outpoint.
    """

    def __init__(
        self,
        engine,
        max_hops: int = 8,
        max_transactions: int = 200,
        max_outpoints: int = 500,
    ):
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
        """Create a BLOCKCHAIN_ANALYSIS_REVERSE trace session and walk it."""
        start_tx = await self.load_transaction(start_txid)
        if start_vout is None or not any(o.vout == start_vout for o in start_tx.outputs):
            raise RpcError("The supplied VOUT does not exist in this transaction.")

        # Ownership must never gate eligibility and starting never creates
        # ownership evidence: the target is a pure traversal seed.
        trace = await self.db.create_trace(
            name=name,
            start_txid=start_txid,
            start_address=start_address,
            start_vout=start_vout,
            trace_mode="BLOCKCHAIN_ANALYSIS_REVERSE",
        )
        walk = await self.rewalk(trace.id)
        fresh = await self.db.get_trace(trace.id)
        if fresh:
            trace = fresh
        logger.info(
            "reverse analysis trace created id=%d target=%s:%d ending=%s",
            trace.id, start_txid, start_vout, walk.get("ending_condition"),
        )
        return trace, walk

    async def rewalk(
        self,
        trace_id: int,
        extra_seeds: Optional[list[tuple[str, int]]] = None,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        """Re-run / extend the reverse analysis using persisted state/seeds.

        ``extra_seeds`` adds deliberate continuation anchors. Rewalk is
        idempotent: edges are rebuilt from scratch and previously-visited
        outpoints are deduplicated (reconvergent ancestry stored once).
        """
        trace = await self.db.get_trace(trace_id)
        if not trace:
            raise TraceNotFoundError()

        seeds = self._resolve_seeds(trace)
        seeds.extend(await self._collect_persisted_seeds(trace_id))
        seeds = merge_seeds(seeds, extra_seeds)

        walk = await self._walk_backward(trace_id, seeds, progress_callback=progress_callback)
        await self.db.update_trace(trace_id, status=self._decide_status(walk))
        return walk

    async def follow(self, trace_id: int, txid: str, vout: int, progress_callback: Any | None = None) -> dict[str, Any]:
        """Deliberately continue backward from a chosen ancestor outpoint.

        The ancestor becomes a persisted continuation seed (the target is kept,
        so the already-discovered graph is never discarded), then the graph is
        rebuilt with both anchors.
        """
        trace = await self.db.get_trace(trace_id)
        if not trace:
            raise TraceNotFoundError()
        tx = await self.load_transaction(txid)
        if not any(o.vout == vout for o in tx.outputs):
            raise RpcError("The supplied VOUT does not exist in this transaction.")

        await self.db.add_reverse_seed(trace_id, txid, vout)
        return await self.rewalk(trace_id, progress_callback=progress_callback)

    # ------------------------------------------------------------------
    # Seed resolution
    # ------------------------------------------------------------------

    def _resolve_seeds(self, trace: Trace) -> list[tuple[str, int]]:
        if trace.start_vout is not None:
            return [(trace.start_txid, trace.start_vout)]
        return []

    async def _collect_persisted_seeds(self, trace_id: int) -> list[tuple[str, int]]:
        return [(r["txid"], r["vout"]) for r in await self.db.list_reverse_seeds(trace_id)]

    # ------------------------------------------------------------------
    # Walk
    # ------------------------------------------------------------------

    async def _walk_backward(
        self,
        trace_id: int,
        seeds: list[tuple[str, int]],
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        await self.db.delete_trace_edges(trace_id)

        visited: set[tuple[str, int]] = set()
        visited_txs: set[str] = set()
        coinbase_origins: list[str] = []
        unresolved: list[tuple[str, int]] = []
        max_hop_seen = 0
        limit_reason: Optional[str] = None

        frontier: deque[tuple[int, str, int]] = deque(
            (0, txid, vout) for txid, vout in seeds
        )

        while frontier and limit_reason is None:
            dhop, txid, vout = frontier.popleft()
            key = (txid, vout)
            if key in visited:
                continue

            if dhop > self._max_hops:
                limit_reason = "hop limit reached (%d)" % self._max_hops
                break
            if len(visited) >= self._max_outpoints:
                limit_reason = "outpoint limit reached (%d)" % self._max_outpoints
                break

            visited.add(key)
            visited_txs.add(txid)
            max_hop_seen = max(max_hop_seen, dhop)

            if progress_callback is not None:
                await progress_callback(
                    transactions=len(visited_txs),
                    outpoints=len(visited),
                    hop=max_hop_seen,
                )

            try:
                producing_tx = await self.load_transaction(txid)
            except (NodeUnreachableError, RpcTimeoutError, BlockNotSyncedError):
                # Node outage / auth / timeout / IBD is not missing history: fail the job.
                raise
            except RpcError:
                unresolved.append(key)
                continue

            await persist_tx_context(self.db, producing_tx)

            for inp in producing_tx.inputs:
                if inp.prev_outpoint == "coinbase":
                    # The producing transaction itself is a coinbase: record
                    # it as a terminal origin; there is no prior outpoint to
                    # follow and no fake prevout to fabricate.
                    if txid not in coinbase_origins:
                        coinbase_origins.append(txid)
                    continue

                prev_key = (inp.txid, inp.vout)
                if limit_reason is not None:
                    break

                if len(visited_txs) >= self._max_transactions:
                    limit_reason = "transaction limit reached (%d)" % self._max_transactions
                    break

                try:
                    pred_tx = await self.load_transaction(inp.txid)
                except (NodeUnreachableError, RpcTimeoutError, BlockNotSyncedError):
                    # Node outage / auth / timeout / IBD is not missing history: fail the job.
                    raise
                except RpcError:
                    # Unavailable historical data for the predecessor's own
                    # producing transaction: a terminal context reason, never a
                    # walk-crashing error (same rule as the tracked outpoint's
                    # producing transaction above).
                    unresolved.append(prev_key)
                    continue
                await persist_tx_context(self.db, pred_tx)
                if not any(o.vout == inp.vout for o in pred_tx.outputs):
                    unresolved.append(prev_key)
                    continue

                await self.db.upsert_edge(
                    TraceEdge(
                        trace_id=trace_id,
                        source_txid=inp.txid,
                        source_vout=inp.vout,
                        spending_txid=txid,
                        destination_txid=txid,
                        destination_vout=vout,
                        hop_number=dhop,
                        relationship_type=RelationshipType.UNKNOWN,
                    )
                )
                if prev_key in visited:
                    # Same ancestor already discovered via another traced
                    # branch: keep the linkage edge, never re-walk the node.
                    continue
                if any(i.prev_outpoint == "coinbase" for i in pred_tx.inputs):
                    # The predecessor is a coinbase output: a natural terminal
                    # origin (same rule as Reverse Provenance). Recorded now,
                    # never enqueued, so it is not counted as a visited
                    # outpoint/transaction/hop and cannot trip a limit.
                    if inp.txid not in coinbase_origins:
                        coinbase_origins.append(inp.txid)
                    continue
                # Every resolvable predecessor is followed regardless of
                # ownership (ownership is metadata only).
                frontier.append((dhop + 1, inp.txid, inp.vout))

        await finalize_relationships(self.db, self.load_transaction, trace_id)

        return {
            "visited_outpoints": [f"{t}:{v}" for t, v in visited],
            "coinbase_origins": coinbase_origins,
            "unresolved": [f"{t}:{v}" for t, v in unresolved],
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

    def _decide_status(self, walk: dict[str, Any]) -> str:
        if walk.get("ending_condition") == "ANALYSIS_LIMIT_REACHED":
            return TraceStatus.ANALYSIS_LIMIT_REACHED
        return TraceStatus.COMPLETE