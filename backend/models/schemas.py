"""Pydantic schemas / models used by the API and the trace engine."""

from __future__ import annotations

from typing import Any, List, Optional, Literal

from pydantic import BaseModel, Field, StrictBool

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TraceStatus(str):
    COMPLETE = "COMPLETE"
    IN_PROGRESS = "IN_PROGRESS"
    NEEDS_CLASSIFICATION = "NEEDS_CLASSIFICATION"
    # Needs review: at least one outpoint carries BOTH direct ownership evidence
    # and a confirmed custodial boundary (ownership/custody conflict).
    NEEDS_REVIEW = "NEEDS_REVIEW"
    # Complete because exact lineage ended at a confirmed custodial boundary.
    # The line stops there by user confirmation, not because of a gap.
    COMPLETE_AT_CUSTODY_BOUNDARY = "COMPLETE_AT_CUSTODY_BOUNDARY"
    # Blockchain Analysis walking stopped at an analysis safety limit
    # (BLOCKCHAIN_ANALYSIS_MODE_REQUIREMENTS.md). The graph collected so far is
    # intentionally partial and can be extended with the follow action.
    ANALYSIS_LIMIT_REACHED = "ANALYSIS_LIMIT_REACHED"
    # Reverse Provenance walking stopped at a reverse safety limit
    # (REVERSE_PROVENANCE_REQUIREMENTS_v0.6.0.md). Never misreported as
    # NEEDS_CLASSIFICATION; the partial graph is preserved and can be extended
    # with Re-check / continue.
    REVERSE_LIMIT_REACHED = "REVERSE_LIMIT_REACHED"
    FORWARD_LIMIT_REACHED = "FORWARD_LIMIT_REACHED"
    ERROR = "ERROR"


class TraceMode(str):
    """Why/how a trace record was produced.

    FORWARD_PROVENANCE   - Forward Provenance mode: ownership-driven traversal
                           of the user's own coins (engine + registries apply).
    BLOCKCHAIN_ANALYSIS  - Blockchain Analysis mode (forward): ownership-
                           independent, spend-state-driven walk of arbitrary
                           transactions. Only the walk performs no ownership
                           classification. Persisted value stays backward
                           compatible (legacy rows mean forward analysis).
    REVERSE_PROVENANCE   - Ownership-aware Reverse Provenance mode
                           (REVERSE_PROVENANCE_REQUIREMENTS_v0.6.0.md): from a
                           concrete TXID:VOUT target, follows OWNED predecessor
                           ancestry backward while showing all inputs as
                           on-chain context.
    BLOCKCHAIN_ANALYSIS_REVERSE - Ownership-neutral reverse ancestry walk
                           (REVERSE_BLOCKCHAIN_ANALYSIS_REQUIREMENTS_v0.6.0.md):
                           every resolvable predecessor input is followed
                           regardless of ownership. Ownership is display
                           metadata only.
    Existing traces default to FORWARD_PROVENANCE (backward compatible).
    """

    FORWARD_PROVENANCE = "FORWARD_PROVENANCE"
    BLOCKCHAIN_ANALYSIS = "BLOCKCHAIN_ANALYSIS"
    REVERSE_PROVENANCE = "REVERSE_PROVENANCE"
    BLOCKCHAIN_ANALYSIS_REVERSE = "BLOCKCHAIN_ANALYSIS_REVERSE"


class SpendStatus(str):
    UNSPENT = "UNSPENT"
    SPENT = "SPENT"
    UNKNOWN = "UNKNOWN"


class OwnershipStatus(str):
    OWNED = "OWNED"
    LIKELY_OWNED = "LIKELY_OWNED"
    EXTERNAL = "EXTERNAL"
    # Exact UTXO lineage ends here: funds entered a custodial/pooled service.
    CUSTODIAL_DEPOSIT = "CUSTODIAL_DEPOSIT"
    UNKNOWN = "UNKNOWN"


class ClassificationSource(str):
    USER = "USER"
    KNOWN_ADDRESS = "KNOWN_ADDRESS"
    ADDRESS_REGISTRY = "ADDRESS_REGISTRY"
    DESCRIPTOR = "DESCRIPTOR"
    XPUB = "XPUB"
    HEURISTIC = "HEURISTIC"
    # Wallet discovery / evidence sources (WALLET_XPUB_DISCOVERY_REQUIREMENTS.md).
    # XPUB_DISCOVERY - address found by scanning the chain against a derived
    #                  chain from a user-supplied extended public key.
    # MANUAL_IMPORT  - address imported by hand / CSV paste into the registry.
    #                  The import endpoint records why the address is owned.
    # USER_CONFIRMED - user explicitly confirmed ownership of an address.
    XPUB_DISCOVERY = "XPUB_DISCOVERY"
    MANUAL_IMPORT = "MANUAL_IMPORT"
    USER_CONFIRMED = "USER_CONFIRMED"
    # Ownership conveyed by tracing itself (a successor of an owned outpoint).
    # Never a registry / registry authority: propagation alone is not custody
    # evidence (MIXED_PROVENANCE_BATCH_REQUIREMENTS.md).
    PROPAGATED = "PROPAGATED"
    # Legacy trace-start assumption: older releases marked the chosen start
    # outpoint OWNED/TRACE_START at trace creation. Trace starts are traversal
    # targets, NOT ownership evidence (TRACE TARGET != OWNERSHIP EVIDENCE), so
    # new traces never write this source and legacy rows are downgraded to
    # UNKNOWN unless direct address evidence exists. Kept only so old rows
    # still parse.
    TRACE_START = "TRACE_START"
    UNKNOWN = "UNKNOWN"


class ProvenanceState(str):
    """Attribution confidence for a *single* output of a transaction.

    Ownership concerns WHO controls an address; provenance concerns HOW
    confident we are about the input->output attribution of a UTXO.

    EXACT     - exactly one input and exactly one meaningful output: the
                provenance of that output is mathematically exact.
    MIXED     - multi-input and/or multi-output transaction that carries at
                least one owned output: per-input attribution is blocked, but
                the flow is reachable from owned lineage.
    AMBIGUOUS - no owned output in the transaction, OR several owned outputs:
                the continuation cannot be pinned down.
    UNKNOWN   - not computed / output is not a meaningful value-bearing script.
    """

    EXACT = "EXACT"
    MIXED = "MIXED"
    AMBIGUOUS = "AMBIGUOUS"
    UNKNOWN = "UNKNOWN"


class TransactionTopology(str):
    """Whole-transaction topology (independent of which inputs were traced)."""

    DIRECT = "DIRECT"
    SPLIT = "SPLIT"
    MERGE = "MERGE"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class RelationshipType(str):
    DIRECT = "DIRECT"
    SPLIT = "SPLIT"
    MERGED = "MERGED"
    MIXED = "MIXED"
    # User-confirmed LOGICAL/ECONOMIC link across a custodial service.
    # Never an on-chain provenance claim.
    CUSTODY_BRIDGE = "CUSTODY_BRIDGE"
    UNKNOWN = "UNKNOWN"


class TraceInputRole(str):
    """Role of a transaction input within the trace being viewed.

    Merge / consolidation input context (MERGE_INPUT_CONTEXT_AND_GRAPH
    _RENDERER_FIX_REQUIREMENTS.md). Display-only classification - the fact
    recorded is that this input UTXO was consumed together with the traced
    lineage input by the same spending transaction. It NEVER makes a co-input
    an ancestor of the Acquisition trace or claims any sat-level attribution.

    LINEAGE_INPUT      - the tracked outpoint through which the trace reached
                         this spending transaction.
    CO_INPUT           - another input of the same transaction that is
                         transaction context but not an ancestor edge of this
                         trace path.
    KNOWN_TRACE_INPUT  - a co-input whose exact outpoint is already present in
                         another stored trace (linkable via related_trace_ids).
    """

    LINEAGE_INPUT = "LINEAGE_INPUT"
    CO_INPUT = "CO_INPUT"
    KNOWN_TRACE_INPUT = "KNOWN_TRACE_INPUT"


class Provenance(str):
    """Trust / provenance level of a relationship.

    EXACT          - mathematically proven by the Bitcoin transaction graph
    INFERRED       - candidate suggested by heuristics (never persisted as fact)
    USER_CONFIRMED - user explicitly confirmed a custody bridge
    VERIFIED       - reserved for future exchange/account-history verification
    """

    EXACT = "EXACT"
    INFERRED = "INFERRED"
    USER_CONFIRMED = "USER_CONFIRMED"
    VERIFIED = "VERIFIED"


class ScriptType:
    P2PKH = "p2pkh"
    P2SH = "p2sh"
    P2WPKH = "p2wpkh"
    P2WSH = "p2wsh"
    P2TR = "p2tr"
    P2PK = "p2pk"
    NONSTANDARD = "nonstandard"

    @classmethod
    def from_asm(cls, asm: str | None) -> str:
        asm = asm or ""
        if asm.startswith("OP_PUSHBYTES_72 OP_PUSHBYTES_33") or (
            asm.startswith("OP_PUSHBYTES_65") or asm.startswith("OP_PUSHBYTES_72")
        ):
            if "OP_CHECKSIG" in asm:
                return cls.P2PK
        if asm == "OP_DUP OP_HASH160 OP_PUSHBYTES_20 OP_EQUALVERIFY OP_CHECKSIG":
            return cls.P2PKH
        if asm.startswith("OP_HASH160 OP_PUSHBYTES_20 OP_EQUAL"):
            return cls.P2SH
        if asm.startswith("OP_0 OP_PUSHBYTES_20"):
            return cls.P2WPKH
        if asm.startswith("OP_0 OP_PUSHBYTES_32"):
            return cls.P2WSH
        if asm.startswith("OP_1 OP_PUSHBYTES_32"):
            return cls.P2TR
        return cls.NONSTANDARD


# ---------------------------------------------------------------------------
# Core objects
# ---------------------------------------------------------------------------


class Outpoint(BaseModel):
    txid: str
    vout: int

    @property
    def as_str(self) -> str:
        return f"{self.txid}:{self.vout}"


class AddressOutput(BaseModel):
    """A single candidate outpoint returned by the reverse-mode address
    discovery endpoint. ``outpoint`` is included for convenience (txid:vout).
    ``block_height`` / ``block_time`` are ``None`` for unconfirmed outputs.
    """

    txid: str
    vout: int
    outpoint: str
    address: str
    value_sats: int
    spend_status: str = SpendStatus.UNKNOWN
    spending_txid: Optional[str] = None
    block_height: Optional[int] = None
    block_time: Optional[int] = None


class AddressDiscoveryResult(BaseModel):
    """Container for all discovered outpoints paying to one Bitcoin address.

    ``discovery_source`` tells the UI which local index produced the results
    (``bitcoin_core_scanblocks`` or ``electrs``) so the information provenance
    is always clear.
    """

    address: str
    discovery_source: str
    outputs: list[AddressOutput] = Field(default_factory=list)


class AddressScanJobState(str):
    """Operational state of a persistent reverse-mode address-discovery scan.

    QUEUED    - created, scanblocks start not yet acknowledged.
    RUNNING   - Bitcoin Core is running the (asynchronous) blockfilter scan.
    COMPLETE  - the scan finished and the candidate outputs were resolved.
    CANCELLED - the user stopped the app-owned scan (nothing is deleted).
    FAILED    - a real scan/RPC failure; ``error_summary`` holds a sanitized,
                user-facing message (full tracebacks stay in the logs only).
    """

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class AddressScanProgress(BaseModel):
    """Live progress of the node's async scanblocks scan.

    Progress is native Core percentage points. Heights delimiting the range
    come from WAMS's start request, current_height comes from Core status.
    """

    from_height: Optional[int] = None
    to_height: Optional[int] = None
    progress: Optional[float] = None
    current_height: Optional[int] = None
    currently_scanning: Optional[str] = None


class AddressScanJob(BaseModel):
    """Persisted operational state of an address-discovery scan."""

    id: int
    address: str
    state: str = AddressScanJobState.QUEUED
    created_at: str
    started_at: Optional[str] = None
    updated_at: str
    completed_at: Optional[str] = None
    error_summary: Optional[str] = None
    progress: "AddressScanProgress" = Field(default_factory=lambda: AddressScanProgress())
    result: Optional["AddressDiscoveryResult"] = None


class AddressScanStart(BaseModel):
    """Request body for starting an address-discovery scan."""

    address: str = ""


class AddressScanStatus(BaseModel):
    """Response of a scan start / reconnect query.

    Either the app owns an active or recently finished discovery scan
    (``job`` set), or Bitcoin Core is running a scan the app did NOT start
    (``foreign_scan`` set) - the two situations are mutually exclusive:
    ``active`` is True only for a QUEUED/RUNNING app-owned job.
    """

    active: bool = False
    job: Optional["AddressScanJob"] = None
    foreign_scan: bool = False
    foreign_scan_message: Optional[str] = None
    core_scan: Optional["AddressScanProgress"] = None


class ChainInfo(BaseModel):
    chain: str
    blocks: int
    headers: int
    bestblockhash: str
    verificationprogress: float
    initialblockdownload: bool


class IndexInfo(BaseModel):
    name: str
    synced: bool
    best_block_height: int = 0


class NodeStatus(BaseModel):
    reachable: bool
    core_version: Optional[str] = None
    network: Optional[str] = None
    network_match: Optional[bool] = None
    blockchain_synced: bool = False
    txindex_synced: bool = False
    txospenderindex_synced: bool = False
    block_height: Optional[int] = None
    indexes: list[IndexInfo] = Field(default_factory=list)
    rpc_error: Optional[str] = None


class InputInfo(BaseModel):
    txid: str
    vout: int
    prev_outpoint: str
    sequence: Optional[int] = None
    # Resolved values (from the referenced output)
    value: Optional[float] = None
    value_sats: Optional[int] = None
    script_pubkey: Optional[str] = None
    script_type: Optional[str] = None
    address: Optional[str] = None
    # Spending relationship
    spending_txid: Optional[str] = None
    our_trace_spend: bool = False
    # Merge / consolidation input context
    # (MERGE_INPUT_CONTEXT_AND_GRAPH_RENDERER_FIX_REQUIREMENTS.md). Enriched on
    # the trace/detail payload only; ownership is the state of the REFERENCED
    # previous output, and the role/related traces are display context that
    # never alters edges/provenance.
    ownership_status: str = OwnershipStatus.UNKNOWN
    classification_source: str = ClassificationSource.UNKNOWN
    wallet_name: Optional[str] = None
    trace_role: str = TraceInputRole.CO_INPUT
    related_trace_ids: list[int] = Field(default_factory=list)


class OutputInfo(BaseModel):
    txid: str
    vout: int
    outpoint: str
    value: Optional[float] = None
    value_sats: Optional[int] = None
    script_pubkey: Optional[str] = None
    script_type: Optional[str] = None
    address: Optional[str] = None
    # Blockchain state
    spend_status: str = SpendStatus.UNKNOWN
    spending_txid: Optional[str] = None
    spending_vin: Optional[int] = None
    # B5-04: why a SPENT output's spender could not be looked up (node down,
    # timeout); null when found or simply unknown.
    spending_lookup_error: Optional[str] = None
    block_height: Optional[int] = None
    block_time: Optional[int] = None
    # Ownership
    ownership_status: str = OwnershipStatus.UNKNOWN
    classification_source: str = ClassificationSource.UNKNOWN
    wallet_name: Optional[str] = None
    confidence: Optional[float] = None
    user_annotation: Optional[str] = None
    # FIX-OWN-1: the address is in Known Addresses, and a stored explicit
    # per-output classification that this confirmation currently overrides.
    address_confirmed: bool = False
    overridden_classification: Optional[str] = None
    # FIX-OWN-1b: the confirmation carries address evidence, so it outranks a
    # stored EXTERNAL. A legacy registry entry without evidence does not.
    confirmation_outranks_external: bool = False
    # Provenance / topology (MIXED_PROVENANCE_BATCH_REQUIREMENTS.md)
    provenance_state: str = ProvenanceState.UNKNOWN
    provenance_reasons: list[str] = Field(default_factory=list)
    transaction_topology: str = TransactionTopology.UNKNOWN
    likely_batch: bool = False
    # Exact outpoint relationship only; never inherited from an ancestor.
    origin_resolution: Optional[dict] = None


class TransactionInfo(BaseModel):
    txid: str
    block_hash: Optional[str] = None
    block_height: Optional[int] = None
    block_time: Optional[int] = None
    confirmations: int = 0
    input_total_sats: Optional[int] = None
    output_total_sats: Optional[int] = None
    fee_sats: Optional[int] = None
    vsize: Optional[int] = None
    fee_rate: Optional[float] = None
    inputs: list[InputInfo] = Field(default_factory=list)
    outputs: list[OutputInfo] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    note: Optional[str] = None
    unknown_input_count: int = 0
    raw: Optional[Any] = None
    # Whole-transaction topology (MIXED_PROVENANCE_BATCH_REQUIREMENTS.md).
    transaction_topology: str = TransactionTopology.UNKNOWN
    likely_batch: bool = False


# ---------------------------------------------------------------------------
# Trace objects
# ---------------------------------------------------------------------------


class TraceCreate(BaseModel):
    name: str = ""
    start_txid: str
    start_address: Optional[str] = None
    start_vout: Optional[int] = None
    trace_mode: str = TraceMode.FORWARD_PROVENANCE


class AnalysisFollowRequest(BaseModel):
    """Follow the spending transaction of a spent output in an analysis trace.

    BLOCKCHAIN_ANALYSIS_MODE_REQUIREMENTS.md: re-walks the analysis trace with
    the followed output as an additional seed so a limit-truncated graph can be
    deliberately extended. Never changes ownership state.
    """

    txid: str
    vout: int


class AnalysisJobState(str):
    """Operational state of a background Blockchain Analysis job.

    Distinct from the trace domain state: a job describes whether the
    background computation is queued / running / done. A job reaching a safety
    limit is LIMIT_REACHED while the trace itself stays ANALYSIS_LIMIT_REACHED
    (BLOCKCHAIN_ANALYSIS_ASYNC_UX_PERFORMANCE_FIX_REQUIREMENTS.md section 6).
    """

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    LIMIT_REACHED = "LIMIT_REACHED"
    FAILED = "FAILED"


class AnalysisOperation(str):
    """The long-running Blockchain Analysis operation a job executes."""

    CREATE = "CREATE"
    FOLLOW = "FOLLOW"
    CONTINUE = "CONTINUE"


class AnalysisJob(BaseModel):
    """Persisted operational state for a background analysis execution."""

    id: int
    trace_id: int
    operation: str = AnalysisOperation.CREATE
    state: str = AnalysisJobState.QUEUED
    created_at: str
    started_at: Optional[str] = None
    updated_at: str
    completed_at: Optional[str] = None
    progress_tx: int = 0
    progress_outpoints: int = 0
    progress_hop: int = 0
    error_summary: Optional[str] = None


class AnalysisJobResponse(BaseModel):
    """Payload the frontend polls to observe analysis progress.

    Merges the persisted job state with the configured safety limits so the UI
    can render counters like ``Transactions: 74 / 200`` without an extra call.
    """

    trace_id: int
    job_id: int
    state: str
    operation: str
    error_summary: Optional[str] = None
    progress: "AnalysisJobProgress" = Field(default_factory=lambda: AnalysisJobProgress())
    limits: "AnalysisJobLimits" = Field(default_factory=lambda: AnalysisJobLimits())


class AnalysisJobProgress(BaseModel):
    transactions: int = 0
    outpoints: int = 0
    hop: int = 0


class AnalysisJobLimits(BaseModel):
    transactions: int = 0
    outpoints: int = 0
    hops: int = 0


class TraceCreateResponse(BaseModel):
    """Response for a Blockchain Analysis trace creation request (202).

    The traversal runs in the background worker; the client navigates using
    ``id`` / ``trace_id`` and polls ``GET /api/traces/{trace_id}/analysis-job``
    to observe the QUEUED → RUNNING → terminal progression.
    """

    id: int
    trace_id: int
    trace_mode: str
    status: str
    analysis_job: "AnalysisJob"


class AnalysisJobEnqueued(BaseModel):
    """Confirmation that a follow / re-check job was scheduled (202)."""

    message: str
    analysis_job: "AnalysisJob"


class Trace(BaseModel):
    id: int
    name: str = ""
    start_txid: str
    start_address: Optional[str] = None
    start_vout: Optional[int] = None
    created_at: str
    updated_at: str
    status: str = TraceStatus.IN_PROGRESS
    trace_mode: str = TraceMode.FORWARD_PROVENANCE


class TraceEdge(BaseModel):
    trace_id: int
    source_txid: str
    source_vout: int
    spending_txid: str
    destination_txid: str
    destination_vout: int
    hop_number: int
    relationship_type: str = RelationshipType.UNKNOWN
    provenance: str = Provenance.EXACT


class ClassificationUpdate(BaseModel):
    ownership_status: str = OwnershipStatus.UNKNOWN
    classification_source: str = ClassificationSource.USER
    wallet_name: Optional[str] = None
    confidence: Optional[float] = None
    register_owned_address: bool = False
    # REG-1: only with register_owned_address. False marks the address as
    # "no key access"; None/True never clears a flag already set.
    key_access: Optional[bool] = None
    # FIX-OWN-1: only with EXTERNAL or a reset. Removes the address from
    # Known Addresses first (same operation as deleting it there).
    remove_address_confirmation: bool = False


class AnnotationCreate(BaseModel):
    entity_type: str  # "transaction" | "outpoint" | "trace"
    entity_id: str
    tag: str = "UNKNOWN"
    note: Optional[str] = None


class Annotation(BaseModel):
    id: int
    entity_type: str
    entity_id: str
    tag: str
    note: Optional[str] = None
    created_at: str


class TraceSummary(BaseModel):
    trace_id: int
    status: str
    start_txid: str
    start_amount_sats: Optional[int] = None
    transactions_followed: int = 0
    tracked_branches: int = 0
    current_utxos: int = 0
    unknown_outputs: int = 0
    merges: int = 0
    splits: int = 0
    # Reverse modes (REVERSE_PROVENANCE_REQUIREMENTS_v0.6.0.md /
    # REVERSE_BLOCKCHAIN_ANALYSIS_REQUIREMENTS_v0.6.0.md). Always None/0 for
    # forward modes so forward summaries never change shape.
    target_outpoint: Optional[str] = None
    owned_ancestors: int = 0
    unknown_predecessors: int = 0
    external_predecessors: int = 0
    oldest_block_height: Optional[int] = None
    oldest_block_time: Optional[int] = None


class FullTrace(BaseModel):
    trace: Trace
    transactions: list[TransactionInfo] = Field(default_factory=list)
    outpoints: list[OutputInfo] = Field(default_factory=list)
    edges: list[TraceEdge] = Field(default_factory=list)
    annotations: list[Annotation] = Field(default_factory=list)
    summary: Optional[TraceSummary] = None
    needs_classification: bool = False
    needs_review: bool = False
    # Derived from reachable materialized endpoints; the limit status itself
    # is durable in traces.status (no schema migration required).
    forward_stop_reason: Optional[str] = None
    forward_stopped_outpoints: list[str] = Field(default_factory=list)
    forward_max_depth: Optional[int] = None
    # Distinct unresolved predecessors of the latest COMPLETE / LIMIT_REACHED
    # reverse walk job (P1-3c, SK-3); None when not recorded.
    unresolved_count: Optional[int] = None
    custodial_boundaries: list["CustodialBoundary"] = Field(default_factory=list)
    custody_bridges: list["CustodyBridge"] = Field(default_factory=list)
    ownership_custody_conflicts: list["OwnershipCustodyConflict"] = Field(default_factory=list)
    possible_boundaries: list[dict[str, Any]] = Field(default_factory=list)
    dismissed_candidates: list["CustodialDismissal"] = Field(default_factory=list)


class TraceExport(BaseModel):
    schema_version: str = "0.3"
    trace: Trace
    transactions: list[TransactionInfo] = Field(default_factory=list)
    outpoints: list[OutputInfo] = Field(default_factory=list)
    edges: list[TraceEdge] = Field(default_factory=list)
    annotations: list[Annotation] = Field(default_factory=list)
    summary: Optional[TraceSummary] = None
    forward_stop_reason: Optional[str] = None
    forward_stopped_outpoints: list[str] = Field(default_factory=list)
    forward_max_depth: Optional[int] = None
    custodial_boundaries: list["CustodialBoundary"] = Field(default_factory=list)
    custody_bridges: list["CustodyBridge"] = Field(default_factory=list)
    ownership_custody_conflicts: list["OwnershipCustodyConflict"] = Field(default_factory=list)
    dismissed_candidates: list["CustodialDismissal"] = Field(default_factory=list)


# Input payload for batch fetch (frontend asks the backend to load a tx)
class LoadTxRequest(BaseModel):
    txid: str


class OutpointsRequest(BaseModel):
    txid: str
    vouts: list[int]


class AnalyzeResponse(BaseModel):
    trace_id: int | None = None
    transaction: Optional[TransactionInfo] = None
    matched_vout: Optional[int] = None
    error: Optional[str] = None
    needs_classification: bool = False


class AddressMatchResponse(BaseModel):
    matched_vout: Optional[int] = None
    message: Optional[str] = None


# Settings table (application settings)
class AppSetting(BaseModel):
    key: str
    value: str


# ---------------------------------------------------------------------------
# Known address registry
# ---------------------------------------------------------------------------


class KnownAddressImportRequest(BaseModel):
    """CSV/paste import. Accepts a raw pasted block of text OR an explicit
    list of rows (already parsed/annotated by the client)."""

    content: Optional[str] = None
    rows: Optional[list[dict[str, Any]]] = None
    wallet_name: Optional[str] = None
    source: Optional[str] = "PASTE"
    name: Optional[str] = None
    # REG-1: applies to newly imported entries; duplicates keep their flag.
    key_access: bool = True


class KnownAddress(BaseModel):
    id: int
    address: str
    wallet_name: str
    label: str
    batch_id: Optional[int] = None
    address_type: str = ""
    created_at: str
    # REG-1: False = ownership evidence only, never scanned or held.
    key_access: bool = True


class KnownAddressUpdate(BaseModel):
    key_access: StrictBool


class KnownAddressQuery(BaseModel):
    query: Optional[str] = None
    wallet_name: Optional[str] = None


class KnownAddressImportResult(BaseModel):
    imported: int = 0
    duplicates: int = 0
    conflicts: int = 0
    total: int


class BatchRecord(BaseModel):
    id: int
    name: str
    wallet_name: str
    source: str
    address_count: int
    invalid_count: int
    duplicate_count: int
    imported_count: int
    created_at: str


# ---------------------------------------------------------------------------
# Custodial boundaries & custody bridges
# ---------------------------------------------------------------------------


class CustodialBoundaryCreate(BaseModel):
    txid: str
    vout: int
    service_name: str = ""
    note: Optional[str] = None


class CustodialBoundary(BaseModel):
    id: int
    trace_id: int
    txid: str
    vout: int
    service_name: str = ""
    note: Optional[str] = None
    created_at: str


class CustodyBridgeConfirm(BaseModel):
    boundary_id: int
    destination_txid: str
    destination_vout: int
    confidence: Optional[float] = None


class CustodyBridgeSource(BaseModel):
    boundary_id: Optional[int] = None
    txid: Optional[str] = None
    vout: Optional[int] = None


class CustodyBridge(BaseModel):
    id: int
    trace_id: int
    destination_txid: str
    destination_vout: int
    provenance: str = Provenance.USER_CONFIRMED
    confidence_score: Optional[float] = None
    reasons: list[str] = Field(default_factory=list)
    service_name: str = ""
    note: Optional[str] = None
    created_at: str
    boundary_id: Optional[int] = None
    boundary_txid: Optional[str] = None
    boundary_vout: Optional[int] = None
    sources: list[CustodyBridgeSource] = Field(default_factory=list)


class CustodyCandidate(BaseModel):
    boundary_id: Optional[int] = None
    txid: str
    vout: int
    outpoint: str
    address: Optional[str] = None
    amount_sats: Optional[int] = None
    block_time: Optional[int] = None
    block_height: Optional[int] = None
    spend_status: str = SpendStatus.UNKNOWN
    wallet_name: Optional[str] = None
    provenance: str = Provenance.INFERRED
    confidence_score: int = 0
    reasons: list[str] = Field(default_factory=list)


class CustodialDismissalCreate(BaseModel):
    """Body for declining a heuristic suggestion at an outpoint."""
    reasons: list[str] = Field(default_factory=list)
    confidence: str = "likely"
    service_name: str = ""
    note: Optional[str] = None


class CustodialDismissal(BaseModel):
    id: int
    trace_id: int
    txid: str
    vout: int
    reasons: list[str] = Field(default_factory=list)
    confidence: str = "likely"
    service_name: str = ""
    note: Optional[str] = None
    created_at: str


class OwnershipCustodyConflict(BaseModel):
    """An outpoint carrying BOTH direct ownership evidence and a confirmed
    custodial boundary.

    ``status``:
      PENDING           - requires user review (trace status NEEDS_REVIEW)
      KEEP_BOUNDARY     - reviewed; boundary kept, exact lineage ends there
      BOUNDARY_REMOVED  - reviewed; custody classification removed, tracing
                          continued from the registry-backed ownership
      condition_cleared - the underlying boundary/evidence combination no
                          longer exists (never used to hide a live conflict)
    """

    id: int
    trace_id: int
    txid: str
    vout: int
    address: str
    wallet_name: str = ""
    evidence_source: str = ""
    boundary_id: Optional[int] = None
    service_name: str = ""
    status: str = "PENDING"
    detail: str = ""
    created_at: str
    resolved_at: Optional[str] = None


# ---------------------------------------------------------------------------
# One-time wallet discovery (WALLET_XPUB_DISCOVERY_REQUIREMENTS.md)
#
# PRIVACY: no request/response model in this section ever carries the raw
# extended public key. The key is accepted once by the parse endpoint, kept in
# memory only for the duration of the scan and is never stored, logged or
# rendered back by the API.
# ---------------------------------------------------------------------------


class DiscoveryRequest(BaseModel):
    """Body for parse / preview / run of a one-time wallet discovery.

    ``key`` is the extended *public* key (xpub / ypub / zpub). It is never
    stored: the discovery service works from memory only.
    """

    key: str = ""
    wallet_name: Optional[str] = None
    label: Optional[str] = None
    key_type: Optional[str] = None          # xpub | ypub | zpub | auto
    network: Optional[str] = None           # mainnet | testnet | auto
    script_type: Optional[str] = None       # auto | p2wpkh | p2sh-p2wpkh | p2pkh | p2tr
    gap_limit: int = 20
    start_height: Optional[int] = None
    end_height: Optional[int] = None
    dry_run: bool = False
    scan_mode: Optional[str] = None         # scanblocks | scantxoutset | auto


class WalletScanRequest(BaseModel):
    model_config = {"extra": "forbid"}
    key: str = Field(min_length=1, max_length=256, repr=False)
    wallet_name: str = Field(default="discovery", max_length=200)
    label: str = Field(default="", max_length=200)
    script_type: Literal["auto", "p2wpkh", "p2sh-p2wpkh", "p2pkh", "p2tr"] = "auto"
    gap_limit: int = Field(default=20, ge=1, le=2000)
    start_height: Optional[int] = Field(default=None, ge=0)
    end_height: Optional[int] = Field(default=None, ge=0)
    scan_mode: Literal["auto", "scanblocks", "scantxoutset"] = "auto"


class WalletScanConfirmation(BaseModel):
    model_config = {"extra": "forbid"}
    revision: int = Field(ge=1, strict=True)


class DiscoveryAddressPreview(BaseModel):
    branch: int          # 0 = receive, 1 = change
    index: int
    address: str


class DiscoveryPlan(BaseModel):
    key_type: str
    network: str
    script_type: str
    account_path: str
    fingerprint: str = ""
    key_hint: str = ""    # redacted tail only, e.g. "zpub…4f7a" - never the full key
    wallet_name: str
    label: str = ""
    gap_limit: int
    scan_mode: str = ""
    start_height: Optional[int] = None
    end_height: Optional[int] = None
    scan_supported: bool = True
    warnings: list[str] = Field(default_factory=list)
    preview_addresses: list[DiscoveryAddressPreview] = Field(default_factory=list)


class MatchedAddress(BaseModel):
    address: str
    branch: int
    index: int
    derivation_path: str
    first_seen_at: Optional[str] = None
    existing_wallet: Optional[str] = None    # None = freshly imported
    evidence: Optional[list[str]] = None


class DiscoveryRunResult(BaseModel):
    discovery_id: int = 0
    imported: int = 0
    duplicates: int = 0
    conflicts: int = 0
    matched: list[MatchedAddress] = Field(default_factory=list)
    trace_count: int = 0
    found_vout_count: int = 0
    unexpected_vout_count: int = 0
    branch0_found: int = 0
    branch1_found: int = 0
    branch0_last_index: int = -1
    branch1_last_index: int = -1
    scan_mode: str = ""
    status: str = "PREVIEW"
    committed: bool = False
    utxo_only: bool = False
    error: Optional[str] = None


class EvidenceItem(BaseModel):
    id: int
    address: str
    wallet_name: str
    source: str
    derivation_path: str = ""
    branch_index: Optional[int] = None
    address_index: Optional[int] = None
    key_type: str = ""
    discovery_id: Optional[int] = None
    batch_id: Optional[int] = None
    first_seen_at: Optional[str] = None
    last_seen_at: Optional[str] = None
    created_at: str


class DiscoveryInfo(BaseModel):
    id: int
    wallet_name: str
    label: str = ""
    key_type: str = ""
    network: str = ""
    script_type: str = ""
    account_path: str = ""
    gap_limit: int = 20
    start_height: Optional[int] = None
    end_height: Optional[int] = None
    scan_mode: str = "scanblocks"
    status: str = "COMPLETED"
    trace_count: int = 0
    branch0_found: int = 0
    branch1_found: int = 0
    branch0_last_index: int = -1
    branch1_last_index: int = -1
    found_vout_count: int = 0
    unexpected_vout_count: int = 0
    created_at: str
    finished_at: Optional[str] = None
    jobs: list[dict[str, Any]] = Field(default_factory=list)


class AddressConflict(BaseModel):
    id: int
    address: str
    wallet_name: str
    evidence_wallet_name: str
    reason: str = ""
    detail: str = ""
    status: str = "PENDING"
    created_at: str
    resolved_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Acquisitions (ACQUISITION_TRACKING_V1_REQUIREMENTS.md)
# ---------------------------------------------------------------------------


class AcquisitionStatus(str):
    """Lifecycle of the acquisition record + its linked forward trace."""

    RESOLVING = "RESOLVING"          # origin TXID:VOUT being resolved
    TRACING = "TRACING"              # forward provenance trace being created
    COMPLETE = "COMPLETE"            # record saved and trace finished
    REVIEW_REQUIRED = "REVIEW_REQUIRED"  # linked trace needs classification/review
    TRACE_FAILED = "TRACE_FAILED"    # record saved, but trace creation failed


class AcquisitionCreate(BaseModel):
    model_config = {"extra": "forbid"}
    acquisition_date: Optional[str] = None
    source: Optional[str] = None
    reference: Optional[str] = None
    notes: Optional[str] = None
    txid: str
    vout: Optional[int] = Field(default=None, ge=0, strict=True)
    # Transitional address resolver; canonical storage always uses TXID:VOUT.
    address: Optional[str] = None


class AcquisitionUpdate(BaseModel):
    """Metadata only: the node-resolved anchor is immutable."""
    model_config = {"extra": "forbid"}
    acquisition_date: Optional[str] = None
    source: Optional[str] = None
    reference: Optional[str] = None
    notes: Optional[str] = None


class AcquisitionOriginCandidate(BaseModel):
    """Node-resolved transaction output; never user-entered value or address."""

    vout: int
    amount_sats: int
    address: Optional[str] = None
    spend_status: str = SpendStatus.UNKNOWN
    block_height: Optional[int] = None
    block_time: Optional[int] = None


class AcquisitionDescendant(BaseModel):
    """Lightweight current-state summary for an acquisition's linked trace.

    Deliberately NOT a full provenance graph and never claims exact-sat
    identity after merges/mixing (see "MIXED" handling).
    """

    terminal_count: int = 0
    unspent_terminal_count: int = 0
    # Known on-chain positions, not wallet-control assertions. Includes a live
    # unspent exact origin even when its ownership is UNKNOWN; successors still
    # follow the existing owned/custody-scoped descendant rules.
    verified_unspent_outpoints: list[dict[str, Any]] = Field(default_factory=list)
    primary_terminal_txid: Optional[str] = None
    primary_terminal_vout: Optional[int] = None
    primary_terminal_address: Optional[str] = None
    primary_terminal_amount_sats: Optional[int] = None
    mixed: bool = False
    spend_unknown: bool = False
    # Position/classification review, NOT Documentation Coverage NEEDS_REVIEW.
    needs_review: bool = False
    unresolved_spends: list[dict[str, Any]] = Field(default_factory=list)
    # FIX-UI-7: unexplained spends of an own custody-path outpoint that keep
    # own change but also send value to a non-own, non-boundary output (the
    # FX-3b condition that prevents fully_exited). Presentation only: position
    # state, ranking and the Exit candidates API are unaffected.
    # foreign_amount_sats sums the known non-own output values.
    partial_unexplained_spends: list[dict[str, Any]] = Field(default_factory=list)
    # On-chain only: no unspent successor, no unresolved/unknown spend, and at
    # least one spent source explained by an Exit (assigned or 0/0) or disposal
    # evidence. Carries no amount (FIX-EXIT-3).
    fully_exited: bool = False
    # Terminals known to be spent, with optional spending_txid. Used to suggest
    # blockchain evidence for a disposal without recomputing the trace graph.
    spent_terminals: list[dict[str, Any]] = Field(default_factory=list)
    # Every unexplained spent source of the custody path (owned, or the live
    # origin) with its spending_txid, leaf or not. Source of Exit candidates
    # only (FIX-EXIT-4); no ownership or disposal claim.
    owned_spends: list[dict[str, Any]] = Field(default_factory=list)
    note: str = ""


class Acquisition(BaseModel):
    id: int
    acquisition_date: Optional[str] = None
    # Read-only decimal presentation of origin_amount_sats.
    btc_amount: str
    source: Optional[str] = None
    reference: Optional[str] = None
    notes: Optional[str] = None
    origin_txid: str
    origin_vout: int
    origin_address: Optional[str] = None
    origin_amount_sats: int
    origin_block_height: Optional[int] = None
    origin_block_time: Optional[int] = None
    trace_id: Optional[int] = None
    trace_status: Optional[str] = None
    status: str = AcquisitionStatus.RESOLVING
    created_at: str
    updated_at: str
    descendant: Optional[AcquisitionDescendant] = None
    warning: Optional[str] = None
    # Derived position state + disposal summary (v0.10.0
    # ACQUISITION_REVIEW_AND_DISPOSALS_V1): never persisted on the row.
    position_state: str = "NEEDS_REVIEW"
    allocated_sats: int = 0
    disposal_count: int = 0
    disposals: list["AcquisitionDisposalSummary"] = Field(default_factory=list)
    exchange_exit_count: int = 0
    exchange_exits: list["AcquisitionExchangeExitSummary"] = Field(default_factory=list)


class DisposalType(str):
    """Economic disposal kind. SALE is the only implemented workflow in v1;
    GIFT / SPEND / OTHER are reserved for future releases."""

    SALE = "SALE"
    GIFT = "GIFT"
    SPEND = "SPEND"
    OTHER = "OTHER"


class PositionState(str):
    """DERIVED position state of an Acquisition (v0.10.0). Read at render time
    from disposal allocations + trace/current-descendant summary + trace
    processing state - never persisted as a manually maintained flag, so it
    cannot go stale when a future xpub import resolves more addresses.

    Precedence: DISPOSED > PARTIALLY_DISPOSED (explicit allocations) >
    TRACING > CURRENT > NEEDS_REVIEW.
    """

    CURRENT = "CURRENT"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    PARTIALLY_DISPOSED = "PARTIALLY_DISPOSED"
    DISPOSED = "DISPOSED"
    TRACING = "TRACING"


class DisposalEvidenceCreate(BaseModel):
    """Optional blockchain evidence for a disposal. For v1 a source outpoint
    + its verified spending transaction are sufficient; destination data stays
    nullable. source_txid/source_vout and spending_txid travel together."""

    source_txid: Optional[str] = None
    source_vout: Optional[int] = None
    spending_txid: Optional[str] = None
    destination_vout: Optional[int] = None
    destination_address: Optional[str] = None
    amount_sats: Optional[int] = None


class DisposalEvidence(BaseModel):
    id: int
    disposal_id: int
    source_txid: Optional[str] = None
    source_vout: Optional[int] = None
    spending_txid: Optional[str] = None
    destination_vout: Optional[int] = None
    destination_address: Optional[str] = None
    amount_sats: Optional[int] = None
    created_at: str


class DisposalAllocationCreate(BaseModel):
    acquisition_id: int
    allocated_sats: int
    # Exchange-Exit allocation detail (v0.11.0). When present, allocated_sats
    # MUST equal disposed_sats + network_fee_sats (disposed = BTC credited to
    # the exchange; network_fee = miner fee attribution). Legacy allocations
    # omit both and read back as disposed_sats = allocated_sats, fee = 0.
    disposed_sats: Optional[int] = None
    network_fee_sats: Optional[int] = None


class DisposalCreate(BaseModel):
    disposal_type: str = DisposalType.SALE
    disposal_date: str
    disposal_time: Optional[str] = None
    btc_amount: str
    gross_proceeds_eur: str
    # Optional user-supplied EUR/BTC sale price. When omitted, the effective
    # sale price is derived from gross proceeds / btc amount at read time.
    sale_price_eur_per_btc: Optional[str] = None
    fee_percent: Optional[str] = None
    broker: Optional[str] = None
    reference: Optional[str] = None
    notes: Optional[str] = None
    # Links the disposal to one or more acquisitions. Sum must equal
    # btc_amount (exact sat conversion); per-acquisition cumulative allocation
    # must never exceed the acquisition's economic btc amount.
    allocations: list[DisposalAllocationCreate] = Field(default_factory=list)
    blockchain_evidence: list[DisposalEvidenceCreate] = Field(default_factory=list)


class DisposalUpdate(BaseModel):
    disposal_date: Optional[str] = None
    disposal_time: Optional[str] = None
    btc_amount: Optional[str] = None
    gross_proceeds_eur: Optional[str] = None
    sale_price_eur_per_btc: Optional[str] = None
    fee_percent: Optional[str] = None
    broker: Optional[str] = None
    reference: Optional[str] = None
    notes: Optional[str] = None
    allocations: Optional[list[DisposalAllocationCreate]] = None
    blockchain_evidence: Optional[list[DisposalEvidenceCreate]] = None


class Disposal(BaseModel):
    id: int
    disposal_type: str = DisposalType.SALE
    disposal_date: str
    disposal_time: Optional[str] = None
    btc_amount: str
    gross_proceeds_eur: str
    sale_price_eur_per_btc: Optional[str] = None
    fee_percent: Optional[str] = None
    fees_eur: Optional[str] = None
    net_proceeds_eur: str
    broker: Optional[str] = None
    reference: Optional[str] = None
    notes: Optional[str] = None
    # Derived only when gross proceeds + btc amount allow it (gross / btc).
    effective_sale_price_eur_per_btc: Optional[str] = None
    allocations: list[DisposalAllocationCreate] = Field(default_factory=list)
    blockchain_evidence: list[DisposalEvidence] = Field(default_factory=list)
    warning: Optional[str] = None
    # Set when this disposal is the economic record of an Exchange Exit
    # (v0.11.0). The exit's on-chain sources/outputs live in exchange_exit_*.
    exchange_exit_id: Optional[int] = None
    created_at: str
    updated_at: str


class AcquisitionDisposalSummary(BaseModel):
    """What the Acquisition row needs to show for its disposals: one per
    linked disposal, carrying the allocated_sats for THIS acquisition."""

    disposal_id: int
    disposal_date: str
    disposal_type: str = DisposalType.SALE
    allocated_sats: int
    disposed_sats: Optional[int] = None
    network_fee_sats: Optional[int] = None
    gross_proceeds_eur: str
    fees_eur: Optional[str] = None
    net_proceeds_eur: str
    broker: Optional[str] = None


class AcquisitionExchangeExitSummary(BaseModel):
    """What the Acquisition row needs to show for its Exchange Exits: one per
    linked exit, carrying the allocated/disposed/fee sats for THIS acquisition
    plus the objective full transaction miner fee of the exit (chain data).

    unattributed_inputs (FX-2) counts exit inputs this Acquisition is an
    anchor of that are attributed to no single Acquisition; they carry no
    amount and reduce no remaining amount."""

    exit_id: int
    txid: str
    exit_date: str
    exit_time: Optional[str] = None
    allocated_sats: int
    disposed_sats: int
    network_fee_sats: int
    fee_sats: int = 0
    broker: Optional[str] = None
    reference: Optional[str] = None
    notes: Optional[str] = None
    verified: bool = False
    disposal_id: Optional[int] = None
    unattributed_inputs: int = 0


class PositionCounts(BaseModel):
    total: int
    CURRENT: int
    NEEDS_REVIEW: int
    PARTIALLY_DISPOSED: int
    DISPOSED: int
    TRACING: int


class DisposalCandidate(BaseModel):
    """A suggested blockchain-evidence candidate for an ONCHAIN acquisition's
    trace: the last known spent terminal outpoint and its spending tx."""

    source_txid: str
    source_vout: int
    spending_txid: str
    address: Optional[str] = None
    amount_sats: Optional[int] = None


class ExchangeExitCandidateOutput(BaseModel):
    """One output of a candidate exchange-exit transaction. possible_deposit is
    True when the output is owned by no known wallet and carries value - i.e.
    the natural default selection for "the exchange received these sats"."""

    vout: int
    address: Optional[str] = None
    amount_sats: Optional[int] = None
    ownership_status: str = OwnershipStatus.UNKNOWN
    possible_deposit: bool = False


class ExchangeExitCandidateInput(BaseModel):
    """One input of a candidate exchange-exit transaction that is a known OWNED
    source outpoint. acquisition_id links it to the Acquisition it belongs to.

    ownership_confirmed is True ONLY for the narrow origin fallback: the
    candidate is the Acquisition's own origin outpoint (directly tied to
    tracked provenance) whose ownership is not independently established, so
    the user must confirm self-custody before recording the exit. Established
    OWNED / LIKELY_OWNED sources carry ownership_confirmed False (already
    settled, no confirmation required).

    FX-1: a candidate lists EVERY input of the exit transaction. Class and
    anchors come from the provenance evaluation behind exit_provenance_rows.
    acquisition_id is set only when that evaluation names exactly one
    Acquisition and nothing else; MIXED, multi-Acquisition, unresolved or
    EXTERNAL/UNKNOWN inputs keep it None and are not an own source.

    FX-2: unattributed marks an OWNED/LIKELY_OWNED input with Acquisition
    anchors that is not attributed to one Acquisition (MIXED, several
    Acquisitions, unresolved origin). It is saved with its anchors but no
    amount and reduces no remaining amount."""

    txid: str
    vout: int
    amount_sats: Optional[int] = None
    address: Optional[str] = None
    acquisition_id: Optional[int] = None
    acquisition_date: Optional[str] = None
    source: Optional[str] = None
    ownership_status: str = OwnershipStatus.UNKNOWN
    ownership_confirmed: bool = False
    provenance_class: Optional[str] = None
    anchor_acquisition_ids: list[int] = Field(default_factory=list)
    no_acquisition_anchor_outpoints: list[str] = Field(default_factory=list)
    unattributed: bool = False


class ExchangeExitCandidateAcquisition(BaseModel):
    """An Acquisition whose owned outpoints feed the candidate exit tx."""

    acquisition_id: int
    acquisition_date: Optional[str] = None
    source: Optional[str] = None
    involved_sats: int = 0


class ExchangeExitCandidate(BaseModel):
    """A possible Exchange Exit transaction for an acquisition: the spend of the
    last known OWNED outpoint(s) that sends sats to a non-owned receiver.
    Deliberately one hop only - the external output's later spends (which are
    not owned) are NEVER suggested (the one-hop-too-far regression)."""

    txid: str
    block_height: Optional[int] = None
    block_time: Optional[int] = None
    # Proposed (editable) Exit Date: the calendar date of the CONFIRMED block
    # that contains this spending transaction - the custody boundary itself.
    # None when the spending transaction has no confirmed block timestamp yet;
    # the acquisition/purchase date is never substituted for it.
    suggested_exit_date: Optional[str] = None
    inputs: list[ExchangeExitCandidateInput] = Field(default_factory=list)
    outputs: list[ExchangeExitCandidateOutput] = Field(default_factory=list)
    fee_sats: Optional[int] = None
    acquisitions: list[ExchangeExitCandidateAcquisition] = Field(default_factory=list)
    mixed: bool = False
    isolation_ok: bool = False
    # Merged class over all inputs, as exit_provenance_rows reads it (FX-1).
    provenance_class: Optional[str] = None


class ExchangeExitCandidateLoadError(BaseModel):
    """A candidate (or its source) transaction the node could not deliver
    (FIX-UI-6). Reported so the editor can say WHY no candidate appears;
    successfully loaded candidates are unaffected and the response never
    fails as a whole because of one unloadable transaction."""

    txid: str
    reason: str


class ExchangeExitCandidateList(BaseModel):
    """Candidates API response: candidates plus load errors (FIX-UI-6)."""

    candidates: list[ExchangeExitCandidate] = Field(default_factory=list)
    load_errors: list[ExchangeExitCandidateLoadError] = Field(default_factory=list)


class ExchangeExitSourceCreate(BaseModel):
    """Per-acquisition source outpoint that left self-custody via the exit.

    disposed_sats = sats in the user-selected exchange deposit outputs;
    network_fee_sats = that acquisition's accounting share of the miner fee,
    not a per-input blockchain fact. Both are
    whole satoshis; disposed + network_fee must not exceed the source amount.

    ownership_confirmed = the user confirms self-custody of this (possibly
    not-independently-established) source outpoint solely for the purpose of
    this Exchange Exit. It is REQUIRED for the narrow UNKNOWN-origin fallback
    (the source is the acquisition's own origin outpoint) and never writes
    anything to the Known Address Registry."""

    acquisition_id: int
    source_txid: str
    source_vout: int
    disposed_sats: int
    network_fee_sats: int = 0
    ownership_confirmed: bool = False


class ExchangeExitOutputSelection(BaseModel):
    """Which output of the exit tx the user confirmed as an exchange deposit."""

    vout: int
    is_exchange_deposit: bool = True


class ExchangeExitCreate(BaseModel):
    txid: str
    exit_date: str
    exit_time: Optional[str] = None
    broker: Optional[str] = None
    reference: Optional[str] = None
    notes: Optional[str] = None
    # One entry per owned source outpoint that fed the exit, grouped per
    # acquisition. Every input of the exit must be covered by the sum of these.
    sources: list[ExchangeExitSourceCreate] = Field(default_factory=list)
    # The exit's outputs, with the confirmed exchange deposits marked.
    output_selection: list[ExchangeExitOutputSelection] = Field(default_factory=list)
    # Miner fee (exact full transaction fee = sum(input values) - sum(output
    # values)). When omitted the derived fee is used and must be >= 0. When
    # supplied it is enforced to equal the derived blockchain fee exactly.
    fee_sats: Optional[int] = None
    # A Wallet Exit is an OWNERSHIP-boundary record (EXIT != SALE, exit !=
    # disposal): it NEVER carries economic facts (gross proceeds, sale price,
    # fee percent) on itself. Those live exclusively in the separate Disposal
    # domain record that the user creates explicitly.
    # Optional user metadata (editable; NEVER part of blockchain evidence).
    tag: Optional[str] = None
    comment: Optional[str] = None


class ExchangeExitUpdate(BaseModel):
    exit_date: Optional[str] = None
    exit_time: Optional[str] = None
    broker: Optional[str] = None
    reference: Optional[str] = None
    notes: Optional[str] = None
    tag: Optional[str] = None
    comment: Optional[str] = None
    fee_sats: Optional[int] = None


class ExchangeExitSource(BaseModel):
    acquisition_id: int
    source_txid: str
    source_vout: int
    source_amount_sats: int
    disposed_sats: int
    network_fee_sats: int
    ownership_status: str = OwnershipStatus.UNKNOWN
    ownership_confirmed: bool = False


class ExchangeExitOutput(BaseModel):
    vout: int
    address: Optional[str] = None
    amount_sats: Optional[int] = None
    ownership_status: str = OwnershipStatus.UNKNOWN
    is_exchange_deposit: bool = False
    ownership_conflict: bool = False


class ExchangeExit(BaseModel):
    id: int
    txid: str
    exit_date: str
    exit_time: Optional[str] = None
    broker: Optional[str] = None
    reference: Optional[str] = None
    notes: Optional[str] = None
    block_height: Optional[int] = None
    block_time: Optional[int] = None
    input_total_sats: Optional[int] = None
    output_total_sats: Optional[int] = None
    fee_sats: Optional[int] = None
    verified: bool = False
    disposal_id: Optional[int] = None
    sources: list[ExchangeExitSource] = Field(default_factory=list)
    outputs: list[ExchangeExitOutput] = Field(default_factory=list)
    created_at: str
    updated_at: str
    # FX-2, read-only: does the stored attribution match the current on-chain
    # evaluation? None = not checked (list views), MATCHES, DIFFERS or
    # UNAVAILABLE. Nothing is ever rewritten from this check.
    allocation_check: Optional[str] = None
    allocation_differences: list[str] = Field(default_factory=list)


class AcquisitionImportRowStatus(str):
    """Per-row status of an Acquisitions CSV import.

    Preview phase (nothing is ever written):
      READY            - validated + resolved to an exact TXID:VOUT, no
                         duplicate exists; can be imported.
      DUPLICATE        - an acquisition already exists for the exact origin.
      INVALID          - the row failed validation (economics / TXID / address
                         / bad VOUT). ``error`` explains why.
      REVIEW_REQUIRED  - TXID + address resolves to MORE than one output;
                         ``candidates`` lists the exact VOUTs to choose from.

    After the user presses "Import Ready Rows":
      IMPORTED         - the READY row was created (acquisition_id + trace_id
                         set). Behaves exactly like a manual Acquisition.
      IMPORT_FAILED    - a READY row could not be created (RPC/edge case);
                         ``error`` explains why (other rows still imported).
    """

    READY = "READY"
    DUPLICATE = "DUPLICATE"
    INVALID = "INVALID"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    IMPORTED = "IMPORTED"
    IMPORT_FAILED = "IMPORT_FAILED"


class AcquisitionImportRequest(BaseModel):
    """Body for the actual CSV import (POST /api/acquisitions/import).

    ``content`` is the same CSV text the preview phase parsed; ``lines`` lists
    the preview rows the user selected (only those are created). ``selections``
    carries the VOUT the user picked for REVIEW_REQUIRED rows: line_number ->
    chosen vout. The server treats a line as authoritative only if it is still
    READY at create time (e.g. a same-batch sibling may turn it into a DUPLICATE).
    """

    content: str = ""
    lines: List[int] = Field(default_factory=list)
    selections: dict[str, int] = Field(default_factory=dict)
    # Optional "Source for all rows" from the import dialog; a non-empty
    # per-row ``source`` cell takes precedence (FIX-UI-4).
    default_source: str = ""


class AcquisitionImportPreviewRequest(BaseModel):
    """Body for POST /api/acquisitions/import/preview.

    Parses + normalizes + validates + resolves every row WITHOUT a single
    database write (no Acquisition, no Trace, no ownership evidence, and the
    engine cache is never touched). ``selections`` lets the user resolve a
    REVIEW_REQUIRED row by choosing a candidate VOUT in the preview itself -
    the row is then revalidated and becomes READY without editing the CSV."""

    content: str = ""
    selections: dict[str, int] = Field(default_factory=dict)
    default_source: str = ""


class AcquisitionImportRowResult(BaseModel):
    line: int
    status: str = AcquisitionImportRowStatus.READY
    txid: str = ""
    acquisition_date: Optional[str] = None
    btc_amount: str = ""
    source: str = ""
    reference: str = ""
    notes: str = ""
    # Resolved exact origin (READY / IMPORTED / DUPLICATE rows).
    origin_vout: Optional[int] = None
    # INVALID / IMPORT_FAILED explanation.
    error: Optional[str] = None
    # REVIEW_REQUIRED: the exact candidate VOUTs the user can pick from.
    candidates: List[AcquisitionOriginCandidate] = Field(default_factory=list)
    # DUPLICATE: the existing record that already owns this origin.
    existing_acquisition_id: Optional[int] = None
    existing_acquisition_date: Optional[str] = None
    # IMPORTED: the created record + its linked forward trace.
    acquisition_id: Optional[int] = None
    trace_id: Optional[int] = None


class AcquisitionImportResult(BaseModel):
    total: int
    ready: int = 0
    imported: int
    duplicates: int
    invalid: int
    review_required: int
    failed: int = 0
    delimiter: str = ""
    header_columns: List[str] = Field(default_factory=list)
    # CSV header columns that are NOT part of the official CSV format. They are
    # reported (never silently dropped) but not persisted. reference/notes are
    # official supported columns and never appear here.
    ignored_columns: List[str] = Field(default_factory=list)
    rows: List[AcquisitionImportRowResult] = Field(default_factory=list)
