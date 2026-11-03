"""Path-based Provenance Coverage and strict ancestry diagnostics (§17).

The managed population comes only from independent wallet scan membership,
checked against Core's live UTXO set (including mempool
spends). Trace-created cached outpoints are never a wallet population source.
Documentation follows every input in the cached blockchain DAG,
not just the edges selected by an individual trace. An exact Acquisition anchor
ends a path; an Exit cuts it. No value is allocated between paths.

The primary metric counts each current UTXO with an established Acquisition
path once at its full current value. This is NOT a documented-sat fraction.
The existing whole-ancestry documentation buckets remain separate diagnostics.

An uncached, untracked input is an undocumented evidence frontier. Missing data
for a known owned/traced path, invalid transaction data, or a processing limit
is instead unresolved. This distinction avoids calling all absent documentation
NEEDS_REVIEW while never treating a truncated known lineage as complete.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Literal
from types import SimpleNamespace

from pydantic import BaseModel, Field

from backend.config import get_settings
from backend.reporting.read_data import (
    ReadBudgetExceeded, ReadData, bounded_map, custody_boundaries, input_keys,
    revision, rows, selected_rows, valid_vout,
)
from backend.rpc.bitcoin import sats
from backend.persistence.inventory import population_keys, metadata as inventory_metadata
from backend.models.schemas import OutputInfo
from backend.registry.wallet_attribution import effective_wallet_matches
from backend.reporting.context import ReportContext
from backend.reporting.wallet_positions import acquisition_locations, classify_locations
from backend.reporting.origin_resolution import OriginResolutions, provenance_class

CoverageState = Literal[
    "FULLY_DOCUMENTED", "PARTIALLY_DOCUMENTED", "UNDOCUMENTED", "NEEDS_REVIEW"
]
STATES = ("FULLY_DOCUMENTED", "PARTIALLY_DOCUMENTED", "UNDOCUMENTED", "NEEDS_REVIEW")
ProvenanceLinkState = Literal["PROVENANCE_LINKED", "UNANCHORED", "NEEDS_REVIEW"]
AcquisitionProvenance = Literal["ANCHORED", "PARTIAL", "UNANCHORED", "NEEDS_REVIEW"]


def acquisition_provenance(documentation_state: str) -> AcquisitionProvenance:
    """Whole-ancestry description, never an allocation of individual sats."""
    return {"FULLY_DOCUMENTED": "ANCHORED", "PARTIALLY_DOCUMENTED": "PARTIAL",
            "UNDOCUMENTED": "UNANCHORED", "NEEDS_REVIEW": "NEEDS_REVIEW"}[documentation_state]


class CoverageBucket(BaseModel):
    amount_sats: int = 0
    utxo_count: int = 0
    # Integer hundredths of a percent, rounded by largest remainder.
    percentage_bps: int | None = None


class WalletObservation(BaseModel):
    txid: str
    vout: int
    outpoint: str
    amount_sats: int
    provenance_state: str
    reasons: list[str] = Field(default_factory=list)
    wallet_match: bool = False
    wallet_matches: list[dict] = Field(default_factory=list)
    historical_wallet_evidence: list[dict] = Field(default_factory=list)
    wallet_membership_state: Literal["CURRENT_MANAGED", "ACQUISITION_NOT_IN_CURRENT_WALLET"] = "CURRENT_MANAGED"
    spend_status: Literal["UNSPENT"] = "UNSPENT"
    address: str | None = None
    script_pubkey: str | None = None
    block_height: int | None = None
    block_time: int | None = None
    confirmations: int | None = None
    ownership_status: str = "UNKNOWN"


class CoverageUTXO(WalletObservation):
    documentation_state: CoverageState
    acquisition_provenance: AcquisitionProvenance
    provenance_link_state: ProvenanceLinkState = Field(description=
        "Existence of a documented Acquisition path, independently of strict whole-ancestry state.")
    acquisition_ids: list[int] = Field(default_factory=list)


class DocumentationCoverage(BaseModel):
    population: Literal["CURRENT_WALLET_INVENTORY"] = "CURRENT_WALLET_INVENTORY"
    inventory: dict = Field(default_factory=dict)
    total_sats: int
    current_managed_sats: int
    documented_not_in_current_wallet_sats: int = 0
    documented_not_in_current_wallet_utxo_count: int = 0
    documented_not_in_current_wallet_utxos: list[CoverageUTXO] = Field(default_factory=list)
    acquisition_wallet_positions: list[dict] = Field(default_factory=list)
    acquisition_wallet_positions_complete: bool = True
    utxo_count: int
    provenance_linked_sats: int = Field(description=
        "Full current value of UTXOs with an established Acquisition path, each counted once; not sat allocation.")
    provenance_linked_utxo_count: int
    provenance_coverage_percentage_bps: int | None = Field(description=
        "BTC-weighted linked share of total_sats, rounded half-up to hundredths of a percent; null for zero holdings.")
    provenance_unresolved_sats: int
    provenance_unresolved_utxo_count: int
    unanchored_sats: int
    unanchored_utxo_count: int
    fully_documented_percentage_bps: int | None
    buckets: dict[CoverageState, CoverageBucket]
    utxos: list[CoverageUTXO]


@dataclass
class Evidence:
    acquisitions: set[int] = field(default_factory=set)
    undocumented: bool = False
    review: set[str] = field(default_factory=set)
    mixed: bool = False
    no_acquisition_anchors: set[tuple[str, int]] = field(default_factory=set)
    entry_frontier: bool = False
    acquisition_origins: set[tuple[str, int]] = field(default_factory=set)
    unresolved_origins: set[tuple[str, int]] = field(default_factory=set)
    # Exact causes captured by the evaluator, never inferred by the UI.
    review_issues: dict[tuple[str, int], set[tuple[str, str]]] = field(default_factory=dict)
    # Evaluator bookkeeping (L-12), not evidence: `truncated` marks a result
    # that hit a cycle or the depth limit below it (path-dependent); `height`
    # is the longest parent chain a complete result needed.
    truncated: bool = field(default=False, compare=False, repr=False)
    height: int = field(default=0, compare=False, repr=False)

    @property
    def provenance_class(self):
        return provenance_class(self)

    @property
    def link_state(self) -> ProvenanceLinkState:
        # A blocked path contributes no Acquisition IDs. A valid OTHER path
        # remains positive even when strict ancestry still requires review.
        if self.acquisitions:
            return "PROVENANCE_LINKED"
        return "NEEDS_REVIEW" if self.review else "UNANCHORED"

    @property
    def state(self) -> str:
        if self.review:
            return "NEEDS_REVIEW"
        if not self.acquisitions:
            return "UNDOCUMENTED"
        return "PARTIALLY_DOCUMENTED" if self.undocumented else "FULLY_DOCUMENTED"


class DocumentationGraph:
    """Memoized ancestry evidence, shared across all current outpoints.

    The memo never makes a result depend on evaluation order (L-12): a
    truncated result is never reused, and a complete result is reused only
    where its height still fits the depth limit at the current depth, i.e.
    exactly where a fresh evaluation would also have been complete.
    """

    max_depth = 200

    def __init__(self, transactions, anchors, boundaries, required, conflicts,
                 *, deadline=None, max_work=500_000, self_custody_outputs=None, capture_paths=False,
                 origin_resolutions=None):
        self.transactions = transactions
        self.anchors = anchors
        self.boundaries = boundaries
        self.required = required
        self.conflicts = conflicts
        self.memo = {}
        # Truncated results depend on the depth they were reached at (R-01);
        # reuse them only at exactly that depth, never across depths.
        self._truncated_memo = {}
        self.deadline = deadline
        self.max_work = max_work
        self.work = 0
        self.validated = {}
        # Optional stricter interpretation using the SAME DAG and traversal.
        # Legacy documentation/linkage retains its evidence-frontier semantics.
        self.self_custody_outputs = self_custody_outputs
        # Optional read-only evidence projection, not another tracing policy.
        # Capture only parent relationships actually visited by this evaluator.
        self.evaluated_parents = {} if capture_paths else None
        self.origin_resolutions = origin_resolutions

    def _charge(self, work=1):
        self.work += work
        if self.work > self.max_work or (self.deadline is not None and time.monotonic() >= self.deadline):
            raise ReadBudgetExceeded("Ancestry execution budget exceeded.")

    def _transaction_shape(self, txid, raw):
        if txid not in self.validated:
            vouts, coinbase = set(), False
            if isinstance(raw, dict) and isinstance(raw.get("vin"), list) and isinstance(raw.get("vout"), list):
                self._charge(len(raw["vin"]) + len(raw["vout"]))
                if raw["vin"] and all(isinstance(i, dict) for i in raw["vin"]) and all(isinstance(o, dict) for o in raw["vout"]):
                    vouts = {o["n"] for o in raw["vout"] if isinstance(o.get("n"), int)}
                    coinbase = any("coinbase" in i for i in raw["vin"])
            self.validated[txid] = (vouts, coinbase)
        return self.validated[txid]

    @staticmethod
    def complete_transaction(raw, vout):
        if not isinstance(raw, dict):
            return False
        inputs, outputs = raw.get("vin"), raw.get("vout")
        return (
            isinstance(inputs, list) and bool(inputs)
            and all(isinstance(i, dict) for i in inputs)
            and isinstance(outputs, list)
            and all(isinstance(o, dict) for o in outputs)
            and any(o.get("n") == vout for o in outputs)
        )

    def evaluate(self, key, visiting=None):
        self._charge()
        visiting = set() if visiting is None else visiting
        if key in visiting or len(visiting) >= self.max_depth:
            return Evidence(review={"Ancestry cycle or processing limit."}, unresolved_origins={key},
                            review_issues={key: {("EVIDENCE", "Ancestry cycle or processing limit.")}},
                            truncated=True)
        known = self.memo.get(key)
        if known is not None and not known.truncated and len(visiting) + known.height < self.max_depth:
            return known
        depth_key = (key, len(visiting))
        truncated = self._truncated_memo.get(depth_key)
        if truncated is not None:
            return truncated
        if self.evaluated_parents is not None:
            self.evaluated_parents[key] = set()
        strict = self.self_custody_outputs is not None
        out = self.self_custody_outputs.get(key) if strict else None
        owned = out is not None and out.ownership_status == "OWNED"
        inherited_review = set()
        resolved_no_acquisition = (strict and self.origin_resolutions is not None
                                   and key in self.origin_resolutions.decisions)
        if key in self.conflicts and not strict:
            result = Evidence(review={"Unresolved ownership/custody evidence conflict."})
        elif key in self.boundaries:
            # A later independently OWNED receipt needs its own documentation;
            # a generic analysis beyond the boundary cannot carry evidence over it.
            result = Evidence(undocumented=True, entry_frontier=strict)
        elif strict and out is not None and out.ownership_status == "EXTERNAL" and not resolved_no_acquisition:
            result = Evidence(entry_frontier=True)
        elif key in self.anchors:
            result = Evidence(acquisitions=set(self.anchors[key]), acquisition_origins={key})
        elif resolved_no_acquisition:
            result = Evidence(undocumented=True, no_acquisition_anchors={key})
        elif strict and not owned:
            result = Evidence(review={"Required ancestry ownership is not established."}, unresolved_origins={key})
        else:
            raw = self.transactions.get(key[0])
            if raw is None:
                result = (Evidence(review={"Required ancestry transaction is not available."})
                          if strict or key in self.required or key[0] in self.transactions
                          else Evidence(undocumented=True))
            elif key[1] not in self._transaction_shape(key[0], raw)[0]:
                result = Evidence(review={"Incomplete transaction input/output evidence."})
            elif self._transaction_shape(key[0], raw)[1]:
                result = Evidence(undocumented=True)
                if strict and owned:
                    result.unresolved_origins.add(key)
            else:
                result = Evidence(mixed=len(raw["vin"]) > 1)
                entry_keys = set()
                visiting.add(key)
                for vin in raw["vin"]:
                    # Same VOUT rule as input_keys(); the evaluator stays agnostic
                    # of TXID spelling (an unloaded reference is never evidence).
                    if not isinstance(vin.get("txid"), str) or not vin["txid"] or not valid_vout(vin.get("vout")):
                        result.review.add("Incomplete transaction input/output evidence.")
                        continue
                    parent_key = (vin["txid"], vin["vout"])
                    if self.evaluated_parents is not None:
                        self.evaluated_parents[key].add(parent_key)
                    parent = self.evaluate(parent_key, visiting)
                    result.acquisitions.update(parent.acquisitions)
                    result.acquisition_origins.update(parent.acquisition_origins)
                    result.unresolved_origins.update(parent.unresolved_origins)
                    result.undocumented |= parent.undocumented
                    result.review.update(parent.review)
                    inherited_review.update(parent.review)
                    for issue_key, issues in parent.review_issues.items():
                        result.review_issues.setdefault(issue_key, set()).update(issues)
                    result.mixed |= parent.mixed
                    result.no_acquisition_anchors.update(parent.no_acquisition_anchors)
                    result.truncated |= parent.truncated
                    result.height = max(result.height, parent.height + 1)
                    if strict and parent.entry_frontier and owned:
                        entry_keys.add(parent_key)
                visiting.remove(key)
                if entry_keys:
                    # A pure entry receive is one reviewable origin. For mixed
                    # input ancestry retain each boundary, never offer a blanket
                    # resolution on the consolidation that would hide anchors.
                    result.unresolved_origins.update({key} if not (
                        result.acquisitions or result.no_acquisition_anchors or result.unresolved_origins
                    ) else entry_keys)
        prior_review = set(result.review)
        if strict and not owned and not result.entry_frontier and not resolved_no_acquisition:
            result.review.add("Required ancestry ownership is not established.")
        if strict and key in self.conflicts:
            result.review.add("Unresolved ownership/custody evidence conflict.")
        if strict and key not in self.anchors and not resolved_no_acquisition and not result.entry_frontier:
            raw = self.transactions.get(key[0])
            if isinstance(raw, dict) and self.complete_transaction(raw, key[1]):
                inputs, outputs = raw["vin"], raw["vout"]
                parents = [(i.get("txid"), i.get("vout")) for i in inputs if "coinbase" not in i]
                output_ids = [o.get("n") for o in outputs]
                # Validate without discarding already established references.
                malformed = (any("coinbase" in i for i in inputs) and len(inputs) != 1)
                malformed |= any(not isinstance(n, int) or isinstance(n, bool) or n < 0 for n in output_ids)
                malformed |= len({str(n) for n in output_ids}) != len(output_ids)
                malformed |= len({str(p) for p in parents}) != len(parents)
                malformed |= raw.get("txid", key[0]) != key[0]
                if malformed:
                    result.review.add("Inconsistent transaction input/output evidence.")
        if strict and result.entry_frontier and owned:
            result.review.add("Current ownership conflicts with a custody boundary.")
        if strict:
            local_reasons = result.review - inherited_review - {"Provenance origin review is required."}
            if not owned and not result.entry_frontier and not resolved_no_acquisition:
                local_reasons.add("Required ancestry ownership is not established.")
            for reason in local_reasons:
                dimension = "OWNERSHIP" if reason == "Required ancestry ownership is not established." else "EVIDENCE"
                result.review_issues.setdefault(key, set()).add((dimension, reason))
            # A local evidence error is itself an unresolved frontier. Propagated
            # errors already carry their exact originating keys, not extra copies.
            if result.review and (not result.unresolved_origins or result.review - prior_review):
                result.unresolved_origins.add(key)
            if not (result.acquisition_origins or result.no_acquisition_anchors
                    or result.unresolved_origins or result.entry_frontier):
                result.unresolved_origins.add(key)
            if result.unresolved_origins:
                result.review.add("Provenance origin review is required.")
        # Keep a complete entry for read-only path projections (exit provenance)
        # rather than replacing it with a deeper, truncated re-evaluation.
        if known is None or known.truncated or not result.truncated:
            self.memo[key] = result
        if result.truncated:
            self._truncated_memo[depth_key] = result
        return result


async def documentation_coverage(db, engine, *, settings=None) -> DocumentationCoverage:
    settings = settings or get_settings()
    deadline = time.monotonic() + settings.coverage_timeout
    try:
        async with asyncio.timeout(settings.coverage_timeout):
            before = await revision(db)
            result = await _documentation_coverage(db, engine, settings, deadline)
            if await revision(db) != before:
                raise ReadBudgetExceeded("Evidence changed during report generation; retry.")
            return result
    except TimeoutError as exc:
        raise ReadBudgetExceeded("Coverage execution budget exceeded; retry.") from exc


async def _coverage_population_keys(db, *, limit=100_000):
    return await population_keys(db, limit=limit)


async def _ancestry(db, keys, stops, max_transactions, *, frontier_stops=None, transaction_budget=None):
    """Load only relevant immutable cached ancestry, in breadth-first SQL batches.

    No RPC and no trace/discovery writes. Missing transactions remain absent;
    cached NULL/invalid payloads retain their distinct unresolved meaning.
    The transaction budget counts stored rows; missing references remain bounded
    by the outpoint/work budgets and never become inferred evidence.
    """
    transactions, seen, queried, expanded = {}, set(), set(), set()
    frontier = set(keys)
    byte_count = 0
    for _ in range(200):
        if frontier_stops is not None:
            stops |= await frontier_stops(frontier - seen - stops)
        frontier -= seen | stops
        if not frontier:
            break
        seen.update(frontier)
        if len(seen) > 100_000:
            raise ReadBudgetExceeded("Ancestry size limit exceeded.")
        txids = {key[0] for key in frontier} - queried
        queried.update(txids)
        # References without a stored transaction are evidence frontiers,
        # not loaded transaction nodes. Keep them absent for the evaluator's
        # required/optional ancestry rules. The outpoint and graph-work caps
        # still bound wide missing frontiers; NULL/invalid stored rows count.
        ordered_txids = list(txids)
        for offset in range(0, len(ordered_txids), 400):
            batch = ordered_txids[offset:offset + 400]
            for row in await selected_rows(db, "transactions", "txid", batch, limit=400):
                if transaction_budget is not None:
                    transaction_budget.add(row["txid"])
                    if len(transaction_budget) > max_transactions:
                        raise ReadBudgetExceeded('Ancestry transaction limit exceeded across report batches.')
                if len(transactions) >= max_transactions:
                    raise ReadBudgetExceeded("Ancestry transaction limit exceeded.")
                raw = row.get("raw_json")
                byte_count += len(raw or "")
                if byte_count > 64 * 1024 * 1024:
                    raise ReadBudgetExceeded("Ancestry data limit exceeded.")
                try:
                    transactions[row["txid"]] = json.loads(raw) if raw else None
                except (TypeError, ValueError):
                    transactions[row["txid"]] = {}
                await asyncio.sleep(0)
        parents = set()
        for key in frontier:
            if key[0] in expanded:
                continue
            raw = transactions.get(key[0])
            if not DocumentationGraph.complete_transaction(raw, key[1]):
                continue
            expanded.add(key[0])
            parents.update(input_keys(raw))
        frontier = parents
        await asyncio.sleep(0)
    return transactions


async def documentation_context(db, outputs, *, origin_resolutions=None):
    """Shared evidence rules for holdings, historical outputs and CSV reports."""
    anchors = (origin_resolutions or await OriginResolutions.load(db)).anchors

    boundaries = await custody_boundaries(db)
    boundaries |= {key for key, out in outputs.items() if out.ownership_status == "CUSTODIAL_DEPOSIT"}
    conflicts = {(r["txid"], r["vout"]) for r in await rows(db,
        "SELECT txid, vout FROM ownership_custody_conflicts WHERE status = 'PENDING'"
    )}
    conflict_addresses = {r["address"] for r in await rows(db,
        "SELECT address FROM address_conflicts WHERE status = 'PENDING'"
    )}
    conflicts |= {k for k, o in outputs.items() if o.address in conflict_addresses}
    required = {key for key, o in outputs.items() if o.ownership_status == "OWNED"}
    for edge in await rows(db, "SELECT source_txid, source_vout, destination_txid, destination_vout FROM trace_edges", limit=1_000_000):
        required.add((edge["source_txid"], edge["source_vout"]))
        required.add((edge["destination_txid"], edge["destination_vout"]))
    return anchors, boundaries, required, conflicts


async def wallet_observation_context(db, engine, settings, deadline, *, population=None, observed_keys=None, acquisitions=None, report=None):
    """Shared Inventory and membership inputs, without any ancestry evaluation."""
    keys = (await _coverage_population_keys(db, limit=settings.coverage_max_outpoints)
            if population is None else population)
    # Reporting can evaluate bounded subsets using the same rules. The complete
    # saved population still determines managed versus outside membership.
    observation_keys = keys if observed_keys is None else observed_keys
    # One request-local node observation per outpoint, shared with the existing
    # Acquisition position evaluator. No historical ownership row adds a wallet.
    current_outputs = await bounded_map(sorted(observation_keys),
        lambda key: engine.rpc.get_txout(*key, include_mempool=True),
        settings.coverage_rpc_concurrency)
    report = report or ReportContext()
    position_candidates, locations, origins = await acquisition_locations(
        db, engine, current_outputs, settings, deadline, acquisitions=acquisitions, report=report)
    report_keys = observation_keys | set(position_candidates)
    data = await ReadData.load(db, engine,
        txids=sorted({txid for txid, _ in report_keys}), limit=settings.coverage_max_outpoints)
    outputs = data.outputs
    # An exact node-resolved anchor can predate or lack a materialized trace.
    # Represent its facts read-only; never create ownership or cache rows here.
    fallback_addresses = set()
    for key in report_keys - outputs.keys():
        a = origins.get(key)
        if a:
            outputs[key] = OutputInfo(txid=key[0], vout=key[1], outpoint=f"{key[0]}:{key[1]}",
                                     address=a.get("origin_address"), value_sats=a["origin_amount_sats"])
            if a.get("origin_address"):
                fallback_addresses.add(a["origin_address"])
    for known in await selected_rows(db, "known_addresses", "address", fallback_addresses):
        data.known[known["address"]] = known
    for evidence_row in await selected_rows(db, "address_evidence", "address", fallback_addresses - data.evidence.keys()):
        data.evidence[evidence_row["address"]].append(evidence_row)
    anchors, boundaries, required, conflicts = await documentation_context(db, outputs, origin_resolutions=report.origin_resolutions)
    address_conflicts = await selected_rows(db, "address_conflicts", "address", data.known)
    conflicts_by_address = {}
    for conflict in address_conflicts:
        conflicts_by_address.setdefault(conflict["address"], []).append(conflict)
    current_keys = [key for key in report_keys if current_outputs.get(key) is not None and key in outputs]
    observations = {}
    for key in current_keys:
        out = outputs[key]
        stored = data.outpoints.get(key, {})
        historical = data.evidence.get(out.address, [])
        managed = key in keys
        amount = sats(current_outputs[key]["value"])
        observations[key] = WalletObservation(
            txid=out.txid, vout=out.vout, outpoint=out.outpoint, amount_sats=amount,
            provenance_state=out.provenance_state,
            reasons=(["Cached output value differs from current node evidence."]
                     if out.value_sats is not None and amount != out.value_sats else []),
            wallet_match=managed,
            wallet_matches=(effective_wallet_matches(data.known.get(out.address), historical,
                conflicts_by_address.get(out.address, [])) if managed else []),
            historical_wallet_evidence=[{field: e.get(field) for field in
                ("id", "wallet_name", "source", "derivation_path", "branch_index", "address_index",
                 "discovery_id", "batch_id", "created_at")} for e in historical],
            wallet_membership_state="CURRENT_MANAGED" if managed else "ACQUISITION_NOT_IN_CURRENT_WALLET",
            address=out.address, script_pubkey=stored.get("script_pubkey"),
            block_height=stored.get("block_height"), block_time=stored.get("block_time"),
            confirmations=current_outputs[key].get("confirmations"), ownership_status=out.ownership_status)
    return SimpleNamespace(keys=keys, current_keys=current_keys, current_outputs=current_outputs,
        data=data, outputs=outputs, anchors=anchors, boundaries=boundaries, required=required,
        conflicts=conflicts, observations=observations, position_candidates=position_candidates,
        locations=locations)


def outside_current_wallets(observed, key, evidence_link):
    """The single "outside current wallets" predicate for a non-managed current output (D-05).

    Returns the Acquisition IDs whose position this output documents outside the
    current wallets, or None. A script that already belongs to a configured
    wallet is never claimed absent: its saved membership is stale, so the
    candidate Acquisitions get an inventory-refresh reason instead. A Registry
    entry without key access is never scanned (REG-1): it gets the normal
    lineage check like any other non-managed output.
    ``evidence_link`` returns ``(evidence, link_state)`` and is only called when
    the lineage is needed.
    """
    known = observed.data.known.get(observed.outputs[key].address)
    if known is not None and known.get("key_access", 1):
        for aid in observed.position_candidates[key]:
            observed.locations[aid]["reasons"].append("Known wallet script requires an inventory refresh.")
        return None
    evidence, link_state = evidence_link()
    connected = evidence.acquisitions & observed.position_candidates[key]
    if not connected or link_state != "PROVENANCE_LINKED" or key in observed.boundaries:
        return None
    return connected


async def _documentation_coverage(db, engine, settings, deadline, *, population=None, observed_keys=None, acquisitions=None, report=None) -> DocumentationCoverage:
    """Legacy full-ancestry diagnostic; keep its original evidence semantics."""
    report = report or ReportContext()
    observed = await wallet_observation_context(db, engine, settings, deadline,
        population=population, observed_keys=observed_keys, acquisitions=acquisitions, report=report)
    keys, current_keys, current_outputs = observed.keys, observed.current_keys, observed.current_outputs
    outputs = observed.outputs
    anchors, boundaries, required, conflicts = observed.anchors, observed.boundaries, observed.required, observed.conflicts
    locations = observed.locations
    transactions = await _ancestry(db, current_keys, set(anchors) | boundaries | conflicts,
                                    settings.coverage_max_transactions)
    # Historic ownership/conflict evidence still matters to provenance even
    # though it no longer supplies the wallet population. Load only the ancestry
    # being evaluated, including missing referenced inputs, not every cached
    # historical outpoint in the database.
    ancestry_txids = {key[0] for key in current_keys}
    for raw in transactions.values():
        ancestry_txids.update(txid for txid, _ in input_keys(raw))
    if ancestry_txids - {key[0] for key in outputs}:
        ancestry_data = await ReadData.load(db, engine, ancestry_txids, limit=settings.coverage_max_outpoints)
        _, ancestry_boundaries, ancestry_required, ancestry_conflicts = await documentation_context(db, ancestry_data.outputs, origin_resolutions=report.origin_resolutions)
        boundaries |= ancestry_boundaries
        required |= ancestry_required
        conflicts |= ancestry_conflicts
    graph = DocumentationGraph(transactions, anchors, boundaries, required, conflicts, deadline=deadline)
    utxos, outside = [], []
    for key in sorted(current_keys):
        await asyncio.sleep(0)
        out = outputs[key]
        current = current_outputs[key]
        amount = sats(current["value"])
        evidence = graph.evaluate(key)
        reasons = sorted(evidence.review)
        state = evidence.state
        link_state = evidence.link_state
        if out.value_sats is not None and amount != out.value_sats:
            state = "NEEDS_REVIEW"
            # This conflict concerns the current output itself, not a separate
            # ancestry branch. Keep the candidate IDs but withhold linkage.
            link_state = "NEEDS_REVIEW"
            reasons.append("Cached output value differs from current node evidence.")
        if not reasons:
            reasons = [{
                "FULLY_DOCUMENTED": "All relevant paths connect to documented Acquisition evidence.",
                "PARTIALLY_DOCUMENTED": "Documented and undocumented paths contribute; no within-UTXO fraction is assigned.",
                "UNDOCUMENTED": "No relevant path connects to documented Acquisition evidence in WhereAreMySats.",
            }[state]]
        structure = out.provenance_state
        if structure == "UNKNOWN" and evidence.mixed:
            structure = "MIXED"
        managed = key in keys
        connected = (evidence.acquisitions if managed else
                     outside_current_wallets(observed, key, lambda: (evidence, link_state)))
        if connected is None:
            continue
        item = CoverageUTXO(
            **{k: v for k, v in observed.observations[key].model_dump().items() if k not in ("reasons", "provenance_state")},
            documentation_state=state, provenance_state=structure,
            acquisition_provenance=acquisition_provenance(state),
            provenance_link_state=link_state,
            acquisition_ids=sorted(connected), reasons=reasons,
        )
        (utxos if managed else outside).append(item)

    buckets = {state: CoverageBucket() for state in STATES}
    for utxo in utxos:
        bucket = buckets[utxo.documentation_state]
        bucket.amount_sats += utxo.amount_sats
        bucket.utxo_count += 1
    total = sum(b.amount_sats for b in buckets.values())
    linked = [u for u in utxos if u.provenance_link_state == "PROVENANCE_LINKED"]
    unresolved = [u for u in utxos if u.provenance_link_state == "NEEDS_REVIEW"]
    unanchored = [u for u in utxos if u.provenance_link_state == "UNANCHORED"]
    linked_sats = sum(u.amount_sats for u in linked)
    if total:
        # Deterministic display rounding only; never an allocation of sats.
        for bucket in buckets.values():
            bucket.percentage_bps = bucket.amount_sats * 10000 // total
        remainder = 10000 - sum(b.percentage_bps for b in buckets.values())
        order = sorted(STATES, key=lambda s: -(buckets[s].amount_sats * 10000 % total))
        for state in order[:remainder]:
            buckets[state].percentage_bps += 1
    wallet_positions = classify_locations(locations, utxos, outside)
    return DocumentationCoverage(
        inventory=await inventory_metadata(db),
        total_sats=total, utxo_count=len(utxos), buckets=buckets, utxos=utxos,
        current_managed_sats=total,
        documented_not_in_current_wallet_sats=sum(u.amount_sats for u in outside),
        documented_not_in_current_wallet_utxo_count=len(outside),
        documented_not_in_current_wallet_utxos=outside,
        acquisition_wallet_positions=wallet_positions,
        acquisition_wallet_positions_complete=all(p["state"] != "NEEDS_REVIEW" for p in wallet_positions),
        provenance_linked_sats=linked_sats,
        provenance_linked_utxo_count=len(linked),
        # Independent of the strict four-way largest-remainder percentages.
        # Integer half-up display rounding only; never allocate value to paths.
        provenance_coverage_percentage_bps=(
            (linked_sats * 20000 + total) // (2 * total) if total else None),
        provenance_unresolved_sats=sum(u.amount_sats for u in unresolved),
        provenance_unresolved_utxo_count=len(unresolved),
        unanchored_sats=sum(u.amount_sats for u in unanchored),
        unanchored_utxo_count=len(unanchored),
        fully_documented_percentage_bps=buckets["FULLY_DOCUMENTED"].percentage_bps,
    )
