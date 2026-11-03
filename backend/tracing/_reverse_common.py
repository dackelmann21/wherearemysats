"""Shared helpers for the two reverse walkers.

Used by ``ReverseBlockchainAnalysisWalker`` (reverse_analysis.py) and
``ReverseProvenanceWalker`` (reverse_provenance.py). Only the parts that were
identical in both walkers live here; the BFS loops stay in their modules
because their traversal rules differ (ownership-neutral vs ownership-aware).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from backend.models.schemas import (
    OwnershipStatus,
    RelationshipType,
    TraceEdge,
    TransactionTopology,
)
from backend.rpc.bitcoin import (
    BlockNotSyncedError,
    NodeUnreachableError,
    RpcError,
    RpcTimeoutError,
)
from backend.tracing.provenance import (
    provenance_for_output,
    tx_topology,
)

# Whole-transaction topology vocabulary -> edge relationship vocabulary.
TOPOLOGY_TO_RELATIONSHIP = {
    TransactionTopology.DIRECT: RelationshipType.DIRECT,
    TransactionTopology.SPLIT: RelationshipType.SPLIT,
    TransactionTopology.MERGE: RelationshipType.MERGED,
    TransactionTopology.MIXED: RelationshipType.MIXED,
    TransactionTopology.UNKNOWN: RelationshipType.UNKNOWN,
}


async def persist_tx_context(db: Any, tx: Any) -> None:
    """Persist whole-transaction topology + per-output provenance facts.

    Same facts the forward engine and forward analysis persist, computed with
    the effective ownership of THIS transaction's outputs so the honest
    structural reading (EXACT / MIXED / AMBIGUOUS / UNKNOWN) holds. Ownership
    is informational only here; it never controls what is persisted.
    """
    tx_top = tx_topology(tx)
    owned_keys = {
        f"{tx.txid}:{o.vout}"
        for o in tx.outputs
        if o.ownership_status == OwnershipStatus.OWNED
    }
    for out_o in tx.outputs:
        prov_state, prov_reasons = provenance_for_output(tx, out_o.vout, owned_keys)
        await db.upsert_outpoint(
            {
                "txid": out_o.txid,
                "vout": out_o.vout,
                "address": out_o.address,
                "amount_sats": out_o.value_sats,
                "script_pubkey": out_o.script_pubkey,
                "script_type": out_o.script_type,
                "block_height": out_o.block_height,
                "block_time": out_o.block_time,
                "spend_status": out_o.spend_status,
                "provenance_state": prov_state,
                "transaction_topology": tx_top,
                "likely_batch": bool(tx.likely_batch),
                "provenance_reasons": prov_reasons,
            }
        )


async def finalize_relationships(
    db: Any,
    load_transaction: Callable[[str], Awaitable[Any]],
    trace_id: int,
) -> None:
    """Assign relationship types from WHOLE-transaction topology.

    Reverse edges only ever destination a single traced output, so traced-edge
    counts would always report one output (a merge would read as DIRECT).
    Whole-transaction topology (DIRECT / SPLIT / MERGE / MIXED) is the correct
    structural read.
    """
    edges = await db.get_edges(trace_id)
    if not edges:
        return
    by_tx: dict[str, list[TraceEdge]] = {}
    for e in edges:
        by_tx.setdefault(e.spending_txid, []).append(e)
    for spending_txid, tx_edges in by_tx.items():
        try:
            stx = await load_transaction(spending_txid)
            rel = TOPOLOGY_TO_RELATIONSHIP.get(
                tx_topology(stx), RelationshipType.UNKNOWN
            )
        except (NodeUnreachableError, RpcTimeoutError, BlockNotSyncedError):
            # Node outage / auth / timeout / IBD is not missing history: fail the job.
            raise
        except RpcError:
            rel = RelationshipType.UNKNOWN
        for e in tx_edges:
            e.relationship_type = rel
            await db.upsert_edge(e)


def merge_seeds(
    seeds: list[tuple[str, int]],
    extra_seeds: list[tuple[str, int]] | None,
) -> list[tuple[str, int]]:
    """Merge deliberate continuation anchors into the rewalk seed list.

    Without ``extra_seeds`` the seed list is returned unchanged (no dedup; the
    walk's visited set deduplicates). With ``extra_seeds`` the combined list
    is deduplicated keeping first-seen order.
    """
    if not extra_seeds:
        return seeds
    seen: set[tuple[str, int]] = set()
    merged: list[tuple[str, int]] = []
    for s in list(seeds) + list(extra_seeds):
        if s not in seen:
            seen.add(s)
            merged.append(s)
    return merged
