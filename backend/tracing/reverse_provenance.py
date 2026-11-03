"""Reverse Provenance mode walker.

REVERSE_PROVENANCE_REQUIREMENTS_v0.6.0.md. An isolated, ownership-aware
reverse walker next to the frozen Forward Provenance engine and the
frozen forward BlockchainAnalysisWalker.

It answers: starting from a concrete outpoint (``TXID:VOUT``), which earlier
owned UTXOs and transactions form the user's personal on-chain ancestry?

Core invariant:

    Trace target != ownership evidence.

Starting a reverse trace never classifies the target. The walker reuses the
engine's stable, read-only helpers (``engine.load_transaction``) so ownership
is resolved through the existing evidence / Known Address Registry pipeline.

Traversal rules (spec section "Traversal algorithm"):
  - Inspect every input of the producing transaction of a traced outpoint.
  - Resolve each input's previous outpoint.
  - Persist full transaction/input/output context.
  - Recurse automatically ONLY into predecessor outpoints whose effective
    ownership is OWNED.
  - UNKNOWN predecessors are visible ancestry context: they never auto-expand
    and never block personal-provenance completeness. COMPLETE means every
    currently confirmed OWNED branch is fully resolved; classifying an UNKNOWN
    as OWNED is optional and simply extends that branch on the next walk.
  - EXTERNAL / LIKELY_OWNED predecessors are visible context, never recursed,
    never blocking.
  - Confirmed custodial boundaries and CUSTODIAL_DEPOSIT classifications stop
    on-chain traversal (ownership != custody is preserved).

No exact sat attribution is ever invented: forward graph direction stays
``older inputs -> transaction -> newer outputs`` and hop metadata is tracked
independently of visual depth.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any, Optional

from backend.models.schemas import (
    OwnershipStatus,
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

logger = logging.getLogger("lineage.reverse_provenance")


class ReverseProvenanceWalker:
    """Ownership-aware backward walker for REVERSE_PROVENANCE traces.

    Edge orientation reuses the forward ``trace_edges`` shape exactly:
    ``source`` = older predecessor outpoint, ``spending_txid`` = the producing
    transaction, ``destination`` = the newer traced outpoint. ``hop_number``
    stores the REVERSE discovery hop of the DESTINATION (target = 0, its
    producing-transaction predecessor inputs = 1, deeper = 2, ...).
    """

    def __init__(
        self,
        engine,
        max_hops: int = 32,
        max_transactions: int = 1000,
        max_outpoints: int = 2500,
    ):
        self.engine = engine
        self.db = engine.db
        self._max_hops = max_hops
        self._max_transactions = max_transactions
        self._max_outpoints = max_outpoints

    # ------------------------------------------------------------------
    # Stable read-only proxy over the shared engine.
    # ------------------------------------------------------------------

    async def load_transaction(self, txid: str) -> Any:
        return await self.engine.load_transaction(txid)

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
        """Create a REVERSE_PROVENANCE trace session and walk it backward."""
        start_tx = await self.load_transaction(start_txid)
        if start_vout is None or not any(o.vout == start_vout for o in start_tx.outputs):
            raise RpcError("The supplied VOUT does not exist in this transaction.")

        # Reverse Provenance performs NO ownership classification at the
        # target: the start outpoint is a traversal seed, never evidence
        # (TRACE TARGET != OWNERSHIP EVIDENCE). No ownership row is written.
        trace = await self.db.create_trace(
            name=name,
            start_txid=start_txid,
            start_address=start_address,
            start_vout=start_vout,
            trace_mode="REVERSE_PROVENANCE",
        )
        walk = await self.rewalk(trace.id)
        fresh = await self.db.get_trace(trace.id)
        if fresh:
            trace = fresh
        logger.info(
            "reverse trace created id=%d target=%s:%d ending=%s",
            trace.id, start_txid, start_vout, walk.get("ending_condition"),
        )
        return trace, walk

    async def rewalk(
        self,
        trace_id: int,
        extra_seeds: Optional[list[tuple[str, int]]] = None,
        progress_callback: Any | None = None,
    ) -> dict[str, Any]:
        """Re-compute the reverse provenance graph for a trace.

        ``extra_seeds`` adds deliberate continuation anchors (persisted for
        future rewalks by the caller). Rewalk is idempotent: edges are rebuilt
        from scratch and previously-visited outpoints are deduplicated.
        """
        trace = await self.db.get_trace(trace_id)
        if not trace:
            raise TraceNotFoundError()

        seeds = self._resolve_seeds(trace)
        seeds.extend(await self._resolve_persisted_seeds(trace_id))
        seeds = merge_seeds(seeds, extra_seeds)

        walk = await self._walk_backward(trace_id, seeds, progress_callback=progress_callback)
        status = await self._decide_status(trace_id, walk)
        await self.db.update_trace(trace_id, status=status)
        return walk

    # ------------------------------------------------------------------
    # Seed resolution
    # ------------------------------------------------------------------

    def _resolve_seeds(self, trace: Trace) -> list[tuple[str, int]]:
        """Reverse seeds = the trace target, plus any persisted continuation
        seeds merged in ``rewalk`` (the async DB read lives there)."""
        if trace.start_vout is not None:
            return [(trace.start_txid, trace.start_vout)]
        return []

    async def _resolve_persisted_seeds(self, trace_id: int) -> list[tuple[str, int]]:
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

        boundaries = {
            (b["txid"], int(b["vout"]))
            for b in await self.db.get_custodial_boundaries(trace_id)
        }

        visited: set[tuple[str, int]] = set()
        visited_txs: set[str] = set()
        pending_unknown: list[tuple[str, int]] = []
        context_external: list[tuple[str, int]] = []
        custody_boundaries: list[tuple[str, int]] = []
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
                limit_reason = "reverse hop limit reached (%d)" % self._max_hops
                break
            if len(visited) >= self._max_outpoints:
                limit_reason = "reverse outpoint limit reached (%d)" % self._max_outpoints
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
                # Unavailable historical data: a terminal context reason, never
                # a walk-crashing error and never a fabricated lineage claim.
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

                # Limit checked BEFORE resolving the predecessor (same order as
                # Reverse Blockchain Analysis): when it trips, the predecessor
                # is neither loaded nor persisted nor recorded as unresolved.
                if len(visited_txs) >= self._max_transactions:
                    limit_reason = "reverse transaction limit reached (%d)" % self._max_transactions
                    break

                pred_ownership, pred_resolved, pred_is_coinbase = await self._resolve_predecessor(
                    inp.txid, inp.vout
                )
                if not pred_resolved:
                    unresolved.append(prev_key)
                    continue

                # Edge: older predecessor outpoint -> producing tx -> newer
                # traced outpoint. hop_number = reverse hop of the destination.
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

                if pred_is_coinbase:
                    # The predecessor is a coinbase output: a natural terminal
                    # origin. Show it, never block on it, never walk deeper.
                    if inp.txid not in coinbase_origins:
                        coinbase_origins.append(inp.txid)
                    continue

                if prev_key in boundaries or pred_ownership == OwnershipStatus.CUSTODIAL_DEPOSIT:
                    # Known custody boundary: exact lineage stops there, as
                    # context only. Never fabricate off-chain ancestry.
                    custody_boundaries.append(prev_key)
                    continue
                if pred_ownership == OwnershipStatus.OWNED:
                    # Personal lineage: recurse backward automatically.
                    frontier.append((dhop + 1, inp.txid, inp.vout))
                elif pred_ownership == OwnershipStatus.UNKNOWN:
                    # Visible ancestry context: never auto-expanded, never
                    # blocking. CLASSIFYING an UNKNOWN as OWNED is optional
                    # and just extends that branch on a later walk.
                    if prev_key not in pending_unknown:
                        pending_unknown.append(prev_key)
                else:
                    # EXTERNAL / LIKELY_OWNED: visible context, not recursed,
                    # not blocking.
                    context_external.append(prev_key)

        await finalize_relationships(self.db, self.load_transaction, trace_id)
        await self.engine._detect_ownership_custody_conflicts(trace_id)

        return {
            "visited_outpoints": [f"{t}:{v}" for t, v in visited],
            "pending_unknown": [f"{t}:{v}" for t, v in pending_unknown],
            "context_external": [f"{t}:{v}" for t, v in context_external],
            "custody_boundaries": [f"{t}:{v}" for t, v in custody_boundaries],
            "coinbase_origins": coinbase_origins,
            "unresolved": [f"{t}:{v}" for t, v in unresolved],
            "ending_condition": (
                "REVERSE_LIMIT_REACHED" if limit_reason else "COMPLETE"
            ),
            "limit_reason": limit_reason,
            "counts": {
                "transactions": len(visited_txs),
                "outpoints": len(visited),
                "hops": max_hop_seen,
            },
        }

    async def _resolve_predecessor(
        self, txid: str, vout: int
    ) -> tuple[str, bool, bool]:
        """Resolve the effective ownership and terminal status of a predecessor.

        Returns ``(ownership_status, resolved, is_coinbase)``. Ownership is
        read from the predecessor's producing transaction output so registry /
        evidence / user-confirmed state (and legacy TRACE_START downgrades) are
        exactly the effective values the engine would show. ``is_coinbase`` is
        ``True`` when the predecessor's producing transaction is itself a
        coinbase transaction (a natural terminal origin).
        """
        try:
            pred_tx = await self.load_transaction(txid)
        except (NodeUnreachableError, RpcTimeoutError, BlockNotSyncedError):
            # Node outage / auth / timeout / IBD is not missing history: fail the job.
            raise
        except RpcError:
            return OwnershipStatus.UNKNOWN, False, False
        await persist_tx_context(self.db, pred_tx)
        pred_out = next((o for o in pred_tx.outputs if o.vout == vout), None)
        if pred_out is None:
            return OwnershipStatus.UNKNOWN, False, False
        is_coinbase = any(
            inp.prev_outpoint == "coinbase" for inp in pred_tx.inputs
        )
        return pred_out.ownership_status, True, is_coinbase

    async def _decide_status(self, trace_id: int, walk: dict[str, Any]) -> str:
        # Same priority as the forward engine: a reached safety limit wins,
        # then a PENDING ownership/custody conflict requires review
        # (NEEDS_REVIEW). The conflict rows stay listed either way.
        if walk.get("ending_condition") == "REVERSE_LIMIT_REACHED":
            return TraceStatus.REVERSE_LIMIT_REACHED
        conflicts = await self.db.list_ownership_custody_conflicts(
            trace_id, status="PENDING"
        )
        if conflicts:
            return TraceStatus.NEEDS_REVIEW
        # UNKNOWN predecessors are optionally-classifiable ancestry context,
        # never a completed-trace blocker: they surface in the walk data /
        # summary / UI as informational counts and never force a
        # NEEDS_CLASSIFICATION status. Classification is not required for a
        # trace to be COMPLETE.
        return TraceStatus.COMPLETE