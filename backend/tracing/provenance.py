"""Provenance and transaction-topology classification.

Bitcoin proves *consumed outpoints* and *created outputs*. In a transaction
with several inputs and/or several outputs, it does NOT prove which input
"became" which output. This module makes that distinction explicit:

``transaction_topology`` is about the shape of the transaction itself
(DIRECT / SPLIT / MERGE / MIXED). It is a pure blockchain fact and never
depends on ownership.

``provenance_state`` is the attribution confidence for ONE destination output
of a transaction:

* EXACT     - exactly one non-coinbase input AND exactly one meaningful
              output. The provenance of that output is mathematically exact.
* MIXED     - the transaction carries at least one owned output, but the
              input->output attribution is not unique (multi-in/multi-out).
* AMBIGUOUS - the transaction has NO owned output (continuation cannot be
              pinned down), or it has 2+ owned outputs (multiple possible
              continuations).
* UNKNOWN   - not computable / the output is not a meaningful value script.

Topology and batch structure are informational only. They must NEVER produce
custody suggestions: fan-in, merge shape, batch structure or pooled value
growth alone are not evidence that funds left the user's control (section 12).
"""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable

from backend.models.schemas import ProvenanceState, TransactionTopology

# Outputs that carry no spendable value / meaningful attribution.
_MEANINGLESS_SCRIPT_TYPES = {"nulldata", "nonstandard"}


def non_coinbase_inputs(tx: Any) -> list[Any]:
    """Return the real (non-coinbase) inputs of a transaction."""
    return [i for i in (tx.inputs or []) if i.txid or i.prev_outpoint != "coinbase"]


def meaningful_outputs(tx: Any) -> list[Any]:
    """Outputs that are value-bearing and attributed to a spendable script."""
    return [
        o
        for o in (tx.outputs or [])
        if (o.value_sats or 0) > 0 and (o.script_type or "") not in _MEANINGLESS_SCRIPT_TYPES
    ]


def tx_topology(tx: Any) -> str:
    """Classify the topology of a whole transaction (never per-output)."""
    n_in = len(non_coinbase_inputs(tx))
    n_out = len(meaningful_outputs(tx))
    if n_in == 0 or n_out == 0:
        return TransactionTopology.UNKNOWN
    if n_in == 1 and n_out == 1:
        return TransactionTopology.DIRECT
    if n_in == 1:
        return TransactionTopology.SPLIT
    if n_out == 1:
        return TransactionTopology.MERGE
    return TransactionTopology.MIXED


def batch_signals(tx: Any) -> tuple[bool, list[str]]:
    """Heuristic batch/workonomics indicator for a transaction.

    Returns ``(likely_batch, reasons)``. This is informational for the UI,
    never custody evidence. A batch is a multi-input/multi-output transaction
    (or a transaction with many distinct destination addresses).
    """
    outs = meaningful_outputs(tx)
    n_in = len(non_coinbase_inputs(tx))
    n_out = len(outs)
    addrs = {o.address for o in outs if o.address}

    reasons: list[str] = []
    likely = False
    if n_in >= 2 and n_out >= 2:
        likely = True
        reasons.append(f"{n_in} inputs merged into {n_out} outputs")
    if n_out >= 4 and len(addrs) >= 4:
        likely = True
        reasons.append(f"{n_out} outputs to {len(addrs)} distinct addresses")
    if likely and len(addrs) >= 2 and not any("distinct" in r for r in reasons):
        reasons.append(f"{len(addrs)} distinct destination addresses")
    return likely, reasons


def provenance_for_output(tx: Any, vout: int, owned_output_keys: set[str]) -> tuple[str, list[str]]:
    """Provenance state + explaining reasons for one destination output.

    ``owned_output_keys`` holds the outpoint keys (``"txid:vout"``) of the
    outputs of ``tx`` that are OWNED in the current trace.
    """
    target = next((o for o in (tx.outputs or []) if o.vout == vout), None)
    if not target:
        return ProvenanceState.UNKNOWN, [
            f"output {vout} does not exist in this transaction"
        ]
    if (target.value_sats or 0) <= 0 or (target.script_type or "") in _MEANINGLESS_SCRIPT_TYPES:
        return ProvenanceState.UNKNOWN, [
            "output carries no meaningful value placeholder (nulldata/nonstandard)"
        ]

    ins = non_coinbase_inputs(tx)
    outs = meaningful_outputs(tx)
    n_in = len(ins)
    n_out = len(outs)

    if n_in == 1 and n_out == 1:
        return ProvenanceState.EXACT, [
            "single input and single output: input->output provenance is exact"
        ]

    owned_here = {
        o.vout for o in (tx.outputs or []) if f"{tx.txid}:{o.vout}" in owned_output_keys
    }
    if not owned_here:
        return ProvenanceState.AMBIGUOUS, [
            "this transaction has no owned output; exact continuation cannot be established"
        ]
    if len(owned_here) >= 2:
        return ProvenanceState.AMBIGUOUS, [
            f"{len(owned_here)} owned outputs in this transaction; multiple possible continuations"
        ]

    if n_in == 1 and n_out > 1:
        reason = f"one input split across {n_out} outputs; per-output attribution is not unique"
    elif n_in > 1 and n_out == 1:
        reason = f"{n_in} inputs merged into one output; per-input attribution is not provable"
    else:
        reason = f"{n_in} inputs and {n_out} outputs; per-input attribution is not provable"
    return ProvenanceState.MIXED, [reason]

def longest_path_levels(edges: Iterable[Any]) -> dict[tuple[str, int], int]:
    """Longest materialized DAG path level per outpoint ``(txid, vout)``.

    Reproduces the display-column pass of the forward engine
    (``TraceEngine._finalize_relationships``) exactly: a Kahn topological
    pass over the edge set (outpoint -> outpoint, duplicate pairs counted
    once) starting from every outpoint without an incoming edge at level 0;
    each destination gets ``max(level(source) + 1)`` over its predecessors.
    Outpoints that never reach indegree 0 (only possible on a malformed,
    cyclic edge set) may be missing from the result.
    """
    children: dict[tuple[str, int], set[tuple[str, int]]] = {}
    indegree: dict[tuple[str, int], int] = {}
    for edge in edges:
        source = (edge.source_txid, edge.source_vout)
        dest = (edge.destination_txid, edge.destination_vout)
        indegree.setdefault(source, 0)
        if dest not in children.setdefault(source, set()):
            children[source].add(dest)
            indegree[dest] = indegree.get(dest, 0) + 1
    queue = deque(key for key, count in indegree.items() if count == 0)
    levels = {key: 0 for key in queue}
    while queue:
        source = queue.popleft()
        for dest in children.get(source, ()):
            levels[dest] = max(levels.get(dest, 0), levels[source] + 1)
            indegree[dest] -= 1
            if indegree[dest] == 0:
                queue.append(dest)
    return levels


def renumber_hops_longest_path(edges: list[Any]) -> list[Any]:
    """Set each edge's ``hop_number`` to the longest-path level of its source.

    Display columns follow the longest materialized DAG path, whereas
    traversal budgets use the shortest (BFS / discovery) path, so a merge
    reached via branches of different depth and its successor never collapse
    into one timeline column. An edge whose source has no computed level keeps
    its ``hop_number`` (same fallback as the forward engine). Mutates and
    returns ``edges``.
    """
    levels = longest_path_levels(edges)
    for edge in edges:
        edge.hop_number = levels.get((edge.source_txid, edge.source_vout), edge.hop_number)
    return edges
