"""SQLite persistence layer.

Blockchain facts (transactions, outpoints, spends) are cached here.
User classifications, annotations, trace sessions and edges are owned here.
The database lives on a persistent volume (REQ 25 / REQ 41).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

import aiosqlite
from aiosqlite.context import contextmanager as _aiosqlite_result

from backend.models.schemas import (
    Annotation,
    Trace,
    TraceEdge,
)
from backend.persistence.migrations import acquisition_migration as _migration_v19
from backend.persistence.migrations import inventory as _migration_v20
from backend.persistence.migrations import no_acquisition as _migration_v21
from backend.persistence.migrations import origin_resolutions as _migration_v22
from backend.persistence.migrations import unresolved_count as _migration_v23
from backend.persistence.migrations import key_access as _migration_v24
from backend.persistence.migrations import exit_txid_unique as _migration_v25

logger = logging.getLogger("lineage.db")

_SCHEMA_VERSION = 25

# Name of the forward-provenance trace an Acquisition create/re-anchor produces:
#   f"Acquisition {acquisition_date} {origin_txid[:8]}:{origin_vout}"
# (backend/acquisitions/service.py::_create_and_link_trace). It is the only
# producer of that name, which makes it a positive provenance signature for
# "this trace exists because of an Acquisition" - used by
# ``_acquisition_owned_trace_ids`` to find Acquisition traces that lost their
# ``acquisitions.trace_id`` link. It is matched together with
# ``trace_mode = 'FORWARD_PROVENANCE'``, which excludes every independent
# analysis mode. A unit test pins this pattern against the real producer so
# the two cannot drift apart.
_ACQUISITION_TRACE_NAME_RE = re.compile(
    r"^Acquisition (?:\d{4}-\d{2}-\d{2}|undated) [0-9a-f]{8}:\d+$"
)


def _acquisition_trace_name(
    acquisition_date: Any, origin_txid: str, origin_vout: Any
) -> str:
    """The exact name the Acquisition create path gives its forward trace.

    The single-Acquisition delete needs to recognise the trace belonging to
    ONE Acquisition, which is stricter than the pattern above: it must be able
    to tell that name from another Acquisition's. That requires reproducing the
    name rather than recognising its shape.
    """
    return f"Acquisition {acquisition_date or 'undated'} {origin_txid[:8]}:{origin_vout}"

# Columns added in schema version 5 (MIXED_PROVENANCE_BATCH_REQUIREMENTS.md).
# They live on the *outpoints* table so provenance/topology survive an
# immediate re-walk and are queryable at the DB layer. The defaults are safe
# for older code paths that still write rows without these fields.
_PROVENANCE_COLUMNS = {
    "provenance_state": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
    "transaction_topology": "TEXT NOT NULL DEFAULT 'UNKNOWN'",
    "likely_batch": "INTEGER NOT NULL DEFAULT 0",
    "provenance_reasons": "TEXT NOT NULL DEFAULT '[]'",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    start_txid TEXT NOT NULL,
    start_address TEXT,
    start_vout INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'IN_PROGRESS',
    trace_mode TEXT NOT NULL DEFAULT 'FORWARD_PROVENANCE'
);

CREATE TABLE IF NOT EXISTS transactions (
    txid TEXT PRIMARY KEY,
    block_hash TEXT,
    block_height INTEGER,
    block_time INTEGER,
    confirmations INTEGER DEFAULT 0,
    input_total_sats INTEGER,
    output_total_sats INTEGER,
    fee_sats INTEGER,
    vsize INTEGER,
    raw_json TEXT,
    fetched_at INTEGER
);

CREATE TABLE IF NOT EXISTS outpoints (
    txid TEXT NOT NULL,
    vout INTEGER NOT NULL,
    address TEXT,
    amount_sats INTEGER,
    script_pubkey TEXT,
    script_type TEXT,
    block_height INTEGER,
    block_time INTEGER,
    spend_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    provenance_state TEXT NOT NULL DEFAULT 'UNKNOWN',
    transaction_topology TEXT NOT NULL DEFAULT 'UNKNOWN',
    likely_batch INTEGER NOT NULL DEFAULT 0,
    provenance_reasons TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (txid, vout)
);

CREATE TABLE IF NOT EXISTS spends (
    source_txid TEXT NOT NULL,
    source_vout INTEGER NOT NULL,
    spending_txid TEXT NOT NULL,
    vin_index INTEGER,
    PRIMARY KEY (source_txid, source_vout)
);

CREATE TABLE IF NOT EXISTS ownership (
    txid TEXT NOT NULL,
    vout INTEGER NOT NULL,
    ownership_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    classification_source TEXT NOT NULL DEFAULT 'UNKNOWN',
    wallet_name TEXT,
    confidence REAL,
    PRIMARY KEY (txid, vout)
);

CREATE TABLE IF NOT EXISTS trace_edges (
    trace_id INTEGER NOT NULL,
    source_txid TEXT NOT NULL,
    source_vout INTEGER NOT NULL,
    spending_txid TEXT NOT NULL,
    destination_txid TEXT NOT NULL,
    destination_vout INTEGER NOT NULL,
    hop_number INTEGER NOT NULL DEFAULT 0,
    relationship_type TEXT NOT NULL DEFAULT 'UNKNOWN',
    PRIMARY KEY (
        trace_id, source_txid, source_vout, destination_txid, destination_vout
    )
);

CREATE TABLE IF NOT EXISTS annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    tag TEXT NOT NULL DEFAULT 'UNKNOWN',
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS known_address_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    wallet_name TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'PASTE',
    address_count INTEGER NOT NULL DEFAULT 0,
    invalid_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    imported_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS known_addresses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL UNIQUE,
    wallet_name TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    batch_id INTEGER,
    address_type TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- Custodial / pooled-funds boundaries (CUSTODIAL_BOUNDARY_REQUIREMENTS.md).
-- A boundary marks an exact-lineage end point where user funds entered a
-- custodial/pooled service. The walk stops (exactly) at these outpoints and
-- never follows the service's internal consolidation transactions.
CREATE TABLE IF NOT EXISTS custodial_boundaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id INTEGER NOT NULL,
    txid TEXT NOT NULL,
    vout INTEGER NOT NULL,
    service_name TEXT NOT NULL DEFAULT '',
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (trace_id, txid, vout)
);

-- User-confirmed custody bridges. These are LOGICAL/ECONOMIC links, never
-- on-chain provenance. provenance is stored explicitly so a bridge can never
-- be confused with an exact spend edge (EXACT vs USER_CONFIRMED).
-- Many-to-many is supported on the deposit side via custody_bridge_sources
-- (multiple deposits -> one withdrawal), while each bridge has one
-- destination outpoint (one deposit -> multiple withdrawals = several rows).
CREATE TABLE IF NOT EXISTS custody_bridges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id INTEGER NOT NULL,
    destination_txid TEXT NOT NULL,
    destination_vout INTEGER NOT NULL,
    provenance TEXT NOT NULL DEFAULT 'USER_CONFIRMED',
    confidence_score REAL,
    reasons TEXT NOT NULL DEFAULT '[]',
    service_name TEXT NOT NULL DEFAULT '',
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (trace_id, destination_txid, destination_vout)
);

CREATE TABLE IF NOT EXISTS custody_bridge_sources (
    bridge_id INTEGER NOT NULL,
    boundary_id INTEGER NOT NULL,
    PRIMARY KEY (bridge_id, boundary_id)
);

-- User-declined heuristic suggestions. Persisting the "decline" decision
-- prevents the same POSSIBLE_CUSTODIAL_BOUNDARY candidate from being offered
-- again on every re-check. Isolated in its own table so it can be reset
-- (undeclined) at any time without touching on-chain trace data or confirmed
-- boundaries. Unique per (trace, txid, vout).
CREATE TABLE IF NOT EXISTS custodial_dismissals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id INTEGER NOT NULL,
    txid TEXT NOT NULL,
    vout INTEGER NOT NULL,
    reasons TEXT NOT NULL DEFAULT '[]',
    confidence TEXT NOT NULL DEFAULT 'likely',
    service_name TEXT NOT NULL DEFAULT '',
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (trace_id, txid, vout)
);

-- One-time wallet discovery (WALLET_XPUB_DISCOVERY_REQUIREMENTS.md).
--
-- The canonical known_addresses table stays the single ownership source.
-- address_evidence records WHY an address is considered owned ("source must
-- describe why"). Multiple evidence rows may reference the same address:
-- deduplication is resolved by attaching evidence, never by duplicating the
-- address (section 1 design principle). The extended public key itself is
-- NEVER stored anywhere (section 7/8: the key exists in memory only and is
-- never sent to the node or written to disk/logs).
CREATE TABLE IF NOT EXISTS address_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL,
    wallet_name TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'MANUAL_IMPORT',
    derivation_path TEXT NOT NULL DEFAULT '',
    branch_index INTEGER,
    address_index INTEGER,
    key_type TEXT NOT NULL DEFAULT '',
    discovery_id INTEGER,
    batch_id INTEGER,
    first_seen_at TEXT,
    last_seen_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (address, wallet_name, source)
);

CREATE TABLE IF NOT EXISTS wallet_discoveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_name TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    key_type TEXT NOT NULL DEFAULT '',
    network TEXT NOT NULL DEFAULT '',
    script_type TEXT NOT NULL DEFAULT '',
    account_path TEXT NOT NULL DEFAULT '',
    gap_limit INTEGER NOT NULL DEFAULT 20,
    start_height INTEGER,
    end_height INTEGER,
    scan_mode TEXT NOT NULL DEFAULT 'scanblocks',
    status TEXT NOT NULL DEFAULT 'COMPLETED',
    trace_count INTEGER NOT NULL DEFAULT 0,
    branch0_found INTEGER NOT NULL DEFAULT 0,
    branch1_found INTEGER NOT NULL DEFAULT 0,
    branch0_last_index INTEGER NOT NULL DEFAULT -1,
    branch1_last_index INTEGER NOT NULL DEFAULT -1,
    found_vout_count INTEGER NOT NULL DEFAULT 0,
    unexpected_vout_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS wallet_scan_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    discovery_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    scan_mode TEXT NOT NULL DEFAULT 'scanblocks',
    start_height INTEGER,
    end_height INTEGER,
    scanned_through_height INTEGER NOT NULL DEFAULT 0,
    chunk_size INTEGER NOT NULL DEFAULT 500,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- An address found by discovery that already belongs to a different
-- wallet_name (or has contradictory ownership evidence) is recorded here
-- instead of being silently re-assigned (section on conflicts).
CREATE TABLE IF NOT EXISTS address_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL,
    wallet_name TEXT NOT NULL,
    evidence_wallet_name TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING',
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

-- Explicit ownership / custody conflict
-- (UTXO_TRACE_OWNERSHIP_CUSTODY_REQUIREMENTS.md). When an outpoint is marked
-- as a confirmed CUSTODIAL_DEPOSIT boundary AND the same address carries
-- direct ownership evidence (MANUAL_IMPORT / XPUB_DISCOVERY /
-- USER_CONFIRMED), the two statements contradict each other. Instead of
-- silently keeping either side, an explicit conflict is recorded so the user
-- can review (keep the boundary, or remove it and continue exact tracing).
-- The conflict never auto-deletes either the boundary or the evidence.
CREATE TABLE IF NOT EXISTS ownership_custody_conflicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id INTEGER NOT NULL,
    txid TEXT NOT NULL,
    vout INTEGER NOT NULL,
    address TEXT NOT NULL,
    wallet_name TEXT NOT NULL DEFAULT '',
    evidence_source TEXT NOT NULL DEFAULT '',
    boundary_id INTEGER,
    service_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING',
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE (trace_id, txid, vout)
);

-- Background analysis job tracking (BLOCKCHAIN_ANALYSIS_ASYNC_UX_PERFORMANCE_FIX_REQUIREMENTS.md).
CREATE TABLE IF NOT EXISTS analysis_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id INTEGER NOT NULL,
    operation TEXT NOT NULL DEFAULT 'CREATE',
    state TEXT NOT NULL DEFAULT 'QUEUED',
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    progress_tx INTEGER NOT NULL DEFAULT 0,
    progress_outpoints INTEGER NOT NULL DEFAULT 0,
    progress_hop INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT,
    FOREIGN KEY (trace_id) REFERENCES traces(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS trace_reverse_seeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id INTEGER NOT NULL,
    txid TEXT NOT NULL,
    vout INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (trace_id, txid, vout),
    FOREIGN KEY (trace_id) REFERENCES traces(id) ON DELETE CASCADE
);

-- Persistent reverse-mode address-discovery scan lifecycle
-- (REVERSE_ADDRESS_DISCOVERY_LIFECYCLE_FIX_REQUIREMENTS.md). An address->outputs
-- discovery is a durable job (QUEUED/RUNNING/COMPLETE/CANCELLED/FAILED) so a
-- long scan survives a browser close/reconnect and is never tied to one HTTP
-- request. progress_json mirrors what Bitcoin Core's scanblocks status reported
-- (never fabricated); result_json caches the resolved candidate outputs +
-- discovery_source on COMPLETE so the UI can restore the candidate list. The
-- table is deliberately isolated from analysis_jobs / AnalysisWorker.
CREATE TABLE IF NOT EXISTS address_discovery_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'QUEUED',
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    error_summary TEXT,
    progress_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT NOT NULL DEFAULT '{}'
);

-- Manually recorded historical Bitcoin purchases
-- (ACQUISITION_TRACKING_V1_REQUIREMENTS.md, schema version 12).
--
-- Every acquisition is an ECONOMIC record anchored to an exact blockchain
-- outpoint (origin_txid:origin_vout). Economic input (btc_amount,
-- purchase_value_eur, fees_eur) is stored as TEXT in exact decimal form so it
-- round-trips without binary float error. On-chain truth lives in
-- origin_amount_sats / origin_block_* and is never overwritten by the
-- economic side; a mismatch between the user-entered btc_amount and the
-- on-chain amount is persisted as amount_mismatch (warning only).
--
-- Creating an acquisition also creates+runs a normal FORWARD_PROVENANCE trace
-- from the exact outpoint; trace_id links the acquisition to that trace for
-- View Trace / descendant summary. There is deliberately NO foreign key to
-- traces: deleting a trace (e.g. DELETE /api/traces) never deletes an
-- acquisition. It only sets acquisitions.trace_id to NULL (done in code), because
-- trace ids are reused after the "Delete all traces" sequence reset; a re-run
-- then recreates a replacement trace.
CREATE TABLE IF NOT EXISTS acquisitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    acquisition_date TEXT NOT NULL,
    btc_amount TEXT NOT NULL,
    purchase_value_eur TEXT NOT NULL,
    fees_eur TEXT,
    fee_percent TEXT,
    broker TEXT,
    origin_type TEXT NOT NULL DEFAULT 'ONCHAIN',
    origin_txid TEXT,
    origin_vout INTEGER,
    origin_address TEXT,
    origin_amount_sats INTEGER,
    origin_block_height INTEGER,
    origin_block_time INTEGER,
    amount_mismatch INTEGER NOT NULL DEFAULT 0,
    reference TEXT,
    notes TEXT,
    transfer_destination_address TEXT,
    trace_id INTEGER,
    trace_status TEXT,
    status TEXT NOT NULL DEFAULT 'RESOLVING',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (origin_txid, origin_vout)
);

-- Economic disposals (ACQUISITION_REVIEW_AND_DISPOSALS_V1, schema version 16).
--
-- A Disposal is an ECONOMIC event recording that part (or all) of an
-- acquisition was sold / otherwise economically disposed of. It is a SEPARATE
-- domain object from an Acquisition (an economic origin): future sales may
-- dispose only a part of one acquisition, combine several acquisitions, or
-- happen off-chain after a transfer to an exchange. There is deliberately NO
-- `sold = true` flag on acquisitions; the disposed quantity is derived at read
-- time from the allocation links below.
--
-- SPENT != SOLD: an on-chain output being spent is a blockchain fact and never
-- automatically creates a Disposal - only explicit user action may.
--
-- disposal_type defaults to SALE for v1 (GIFT / SPEND / OTHER are reserved for
-- future releases and are not implemented as workflows).
-- Economic fields are stored as TEXT in exact decimal form (no binary float):
-- btc_amount, gross_proceeds_eur, sale_price_eur_per_btc (optional manual
-- EUR/BTC price), fee_percent (optional; authority for fees_eur),
-- fees_eur (derived = Gross * Fee% / 100, ROUND_HALF_UP), and
-- net_proceeds_eur (derived = Gross - fees_eur, ROUND_HALF_UP).
CREATE TABLE IF NOT EXISTS disposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    disposal_type TEXT NOT NULL DEFAULT 'SALE',
    disposal_date TEXT NOT NULL,
    disposal_time TEXT,
    btc_amount TEXT NOT NULL,
    gross_proceeds_eur TEXT NOT NULL,
    sale_price_eur_per_btc TEXT,
    fee_percent TEXT,
    fees_eur TEXT,
    net_proceeds_eur TEXT NOT NULL,
    broker TEXT,
    reference TEXT,
    notes TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Disposal <-> Acquisition allocation links (also the basis of the derived
-- position state). allocated_sats is an INTEGER sat amount; multiple links per
-- disposal (several acquisitions in one sale) and multiple disposals per
-- acquisition (partial successive sales) are both supported. There is no FK on
-- acquisitions: deleting an acquisition removes its links only and never
-- touches the disposal record (mirrors the trace-delete decoupling). Disposals
-- are deleted via their own endpoint which removes their links + evidence.
CREATE TABLE IF NOT EXISTS disposal_acquisition_links (
    disposal_id INTEGER NOT NULL,
    acquisition_id INTEGER NOT NULL,
    allocated_sats INTEGER NOT NULL,
    PRIMARY KEY (disposal_id, acquisition_id)
);

-- Optional blockchain evidence for a disposal. Deliberately a child table so
-- multiple inputs/outputs can be recorded later. For v1 a source outpoint +
-- spending transaction are sufficient; destination data stays nullable. The
-- blockchain transfer is EVIDENCE only - it is never called "the sale
-- transaction" (an economic sale may happen off-chain after a transfer).
CREATE TABLE IF NOT EXISTS disposal_blockchain_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    disposal_id INTEGER NOT NULL,
    source_txid TEXT,
    source_vout INTEGER,
    spending_txid TEXT,
    destination_vout INTEGER,
    destination_address TEXT,
    amount_sats INTEGER,
    created_at TEXT NOT NULL
);

-- Exchange Exit (EXCHANGE_EXIT_REQUIREMENTS.md v0.11.0).
--
-- An Exchange Exit is a user-confirmed boundary where BTC leaves self-custody
-- to an exchange. It is a first-class domain object that persists the scope-
-- boundary decision; the linked Disposal record (via exchange_exit_id) carries
-- the economic/sale data using the existing disposal model.
--
-- SPENT != SOLD: an Exchange Exit is NEVER auto-created. Only explicit user
-- action may create one, and only when the user has identified a candidate
-- transaction that spends owned outpoints to an external party.
CREATE TABLE IF NOT EXISTS exchange_exits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    txid TEXT NOT NULL,
    exit_date TEXT NOT NULL,
    exit_time TEXT,
    broker TEXT,
    reference TEXT,
    notes TEXT,
    block_height INTEGER,
    block_time INTEGER,
    input_total_sats INTEGER,
    output_total_sats INTEGER,
    fee_sats INTEGER,
    disposal_id INTEGER,
    verified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Source outpoints feeding the exit tx, per acquisition. Each row is a
-- (txid:vout) that was an OWNED input of the exit transaction. The source
-- amount is split into disposed_sats (to the exchange) and network_fee_sats
-- (miner fee attribution). Remaining source value not in either column is
-- retained as owned change and stays CURRENT.
-- ownership_status = the source output's ownership classification at exit time
-- (OWNED/LIKELY_OWNED established vs UNKNOWN). ownership_confirmed = 1 ONLY
-- when the ownership was NOT independently established (the narrow UNKNOWN
-- acquisition-origin fallback) and the user confirmed self-custody for this
-- exit. Established OWNED sources carry ownership_confirmed = 0, so read-back
-- never conflates blockchain-established ownership with a user confirmation.
CREATE TABLE IF NOT EXISTS exchange_exit_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange_exit_id INTEGER NOT NULL,
    acquisition_id INTEGER NOT NULL,
    source_txid TEXT NOT NULL,
    source_vout INTEGER NOT NULL,
    source_amount_sats INTEGER NOT NULL,
    disposed_sats INTEGER NOT NULL,
    network_fee_sats INTEGER NOT NULL DEFAULT 0,
    ownership_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    ownership_confirmed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (exchange_exit_id, acquisition_id, source_txid, source_vout)
);

-- Outputs of the exit transaction. Records the on-chain output layout for
-- the exit and marks which outputs are the exchange deposit vs owned change.
-- is_exchange_deposit = 1 for outputs the user confirmed as exchange deposits.
CREATE TABLE IF NOT EXISTS exchange_exit_outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exchange_exit_id INTEGER NOT NULL,
    vout INTEGER NOT NULL,
    address TEXT,
    amount_sats INTEGER,
    ownership_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    is_exchange_deposit INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (exchange_exit_id, vout)
);
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_trace_edges_trace ON trace_edges(trace_id);
CREATE INDEX IF NOT EXISTS idx_outpoints_txid ON outpoints(txid);
CREATE INDEX IF NOT EXISTS idx_outpoints_address ON outpoints(address);
CREATE INDEX IF NOT EXISTS idx_outpoints_spend ON outpoints(spend_status);
CREATE INDEX IF NOT EXISTS idx_traces_status ON traces(status);
CREATE INDEX IF NOT EXISTS idx_known_addresses_wallet ON known_addresses(wallet_name);
CREATE INDEX IF NOT EXISTS idx_known_addresses_batch ON known_addresses(batch_id);
CREATE INDEX IF NOT EXISTS idx_custodial_boundaries_trace ON custodial_boundaries(trace_id);
CREATE INDEX IF NOT EXISTS idx_custody_bridges_trace ON custody_bridges(trace_id);
CREATE INDEX IF NOT EXISTS idx_custody_bridge_src_bridge ON custody_bridge_sources(bridge_id);
CREATE INDEX IF NOT EXISTS idx_custodial_dismissals_trace ON custodial_dismissals(trace_id);
CREATE INDEX IF NOT EXISTS idx_evidence_address ON address_evidence(address);
CREATE INDEX IF NOT EXISTS idx_evidence_wallet ON address_evidence(wallet_name);
CREATE INDEX IF NOT EXISTS idx_evidence_discovery ON address_evidence(discovery_id);
CREATE INDEX IF NOT EXISTS idx_evidence_batch ON address_evidence(batch_id);
CREATE INDEX IF NOT EXISTS idx_scan_job_active ON wallet_scan_jobs(discovery_id) WHERE status IN ('PENDING', 'RUNNING');
CREATE INDEX IF NOT EXISTS idx_conflicts_address ON address_conflicts(address);
CREATE INDEX IF NOT EXISTS idx_conflicts_status ON address_conflicts(status);
CREATE INDEX IF NOT EXISTS idx_occ_trace ON ownership_custody_conflicts(trace_id);
CREATE INDEX IF NOT EXISTS idx_occ_status ON ownership_custody_conflicts(status);
CREATE INDEX IF NOT EXISTS idx_analysis_jobs_trace ON analysis_jobs(trace_id);
CREATE INDEX IF NOT EXISTS idx_analysis_jobs_state ON analysis_jobs(state);
CREATE INDEX IF NOT EXISTS idx_reverse_seeds_trace ON trace_reverse_seeds(trace_id);
CREATE INDEX IF NOT EXISTS idx_addr_discovery_jobs_state ON address_discovery_jobs(state);
CREATE INDEX IF NOT EXISTS idx_addr_discovery_jobs_address ON address_discovery_jobs(address);
CREATE INDEX IF NOT EXISTS idx_acquisitions_status ON acquisitions(status);
CREATE INDEX IF NOT EXISTS idx_disposal_links_disposal ON disposal_acquisition_links(disposal_id);
CREATE INDEX IF NOT EXISTS idx_disposal_links_acq ON disposal_acquisition_links(acquisition_id);
CREATE INDEX IF NOT EXISTS idx_disposal_evidence_disposal ON disposal_blockchain_evidence(disposal_id);
CREATE INDEX IF NOT EXISTS idx_disposals_date ON disposals(disposal_date DESC);
CREATE INDEX IF NOT EXISTS idx_exits_txid ON exchange_exits(txid);
CREATE INDEX IF NOT EXISTS idx_exits_disposal ON exchange_exits(disposal_id);
CREATE INDEX IF NOT EXISTS idx_exit_sources_exit ON exchange_exit_sources(exchange_exit_id);
CREATE INDEX IF NOT EXISTS idx_exit_sources_acq ON exchange_exit_sources(acquisition_id);
CREATE INDEX IF NOT EXISTS idx_exit_outputs_exit ON exchange_exit_outputs(exchange_exit_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _analysis_job_from_row(row: aiosqlite.Row) -> dict[str, Any]:
    """Convert an analysis_jobs row into a plain dict (worker-owned data)."""
    return {
        "id": row["id"],
        "trace_id": row["trace_id"],
        "operation": row["operation"],
        "state": row["state"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
        "progress_tx": row["progress_tx"],
        "progress_outpoints": row["progress_outpoints"],
        "progress_hop": row["progress_hop"],
        "error_summary": row["error_summary"],
        "unresolved_count": row["unresolved_count"],
    }


def _address_discovery_job_from_row(row: aiosqlite.Row) -> dict[str, Any]:
    """Convert an address_discovery_jobs row into a plain dict (manager-owned)."""
    return {
        "id": row["id"],
        "address": row["address"],
        "state": row["state"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
        "error_summary": row["error_summary"],
        "progress": json.loads(row["progress_json"] or "{}"),
        "result": json.loads(row["result_json"] or "{}"),
    }


class _SharedConnection(aiosqlite.Connection):
    """The shared request connection: resets itself after SQLITE_BUSY (R3-01).

    sqlite3 opens a deferred transaction with an implicit ``BEGIN`` before a
    write. If that write then loses the lock race (``database is locked``
    after the busy timeout, or SQLITE_BUSY_SNAPSHOT), nothing rolls the
    transaction back: the next read pins a stale snapshot and every later
    write on this connection fails at once until restart.

    The rollback here is safe for every request sharing this connection: a
    transaction that hits SQLITE_BUSY holds no writes. In WAL mode a
    successful write takes the single write lock, and a connection holding it
    never gets SQLITE_BUSY again before its commit or rollback. Only BUSY
    triggers the reset; every other error leaves the transaction as it is.

    The writer connection of ``write_transaction`` is a
    ``_WriterConnection``: it rolls back on any exception itself.
    """

    @_aiosqlite_result
    async def execute(self, sql, parameters=None):
        try:
            return await super().execute(sql, parameters)
        except sqlite3.OperationalError as exc:
            await self._rollback_if_busy(exc)
            raise

    @_aiosqlite_result
    async def executemany(self, sql, parameters):
        try:
            return await super().executemany(sql, parameters)
        except sqlite3.OperationalError as exc:
            await self._rollback_if_busy(exc)
            raise

    async def _rollback_if_busy(self, exc: sqlite3.OperationalError) -> None:
        busy = (getattr(exc, "sqlite_errorcode", 0) & 0xFF) == sqlite3.SQLITE_BUSY
        if busy and self.in_transaction:
            logger.warning("shared connection: rolled back a transaction after SQLITE_BUSY")
            await self.rollback()


class _WriterConnection(aiosqlite.Connection):
    """The writer connection of ``write_transaction``: guards its transaction (R3-02).

    Most ``Database`` methods commit themselves. Called on the yielded writer
    inside the block, such a commit would silently end the transaction:
    everything before it persists even if the block fails later, and the
    rollback of the context manager has nothing left to undo. While
    ``guarded`` is set, ``commit()`` and ``rollback()`` therefore fail loudly;
    ``write_transaction`` clears the flag right before its own final
    commit or rollback.
    """

    guarded: bool = False

    def _check_guard(self, name: str) -> None:
        if self.guarded:
            raise RuntimeError(
                f"{name}() inside Database.write_transaction(): call the method "
                "with commit=False or move the commit outside the block"
            )

    async def commit(self) -> None:
        self._check_guard("commit")
        await super().commit()

    async def rollback(self) -> None:
        self._check_guard("rollback")
        await super().rollback()


class DiscoveryRegistryChanged(Exception):
    """The reviewed address membership is no longer the current registry state."""


class Database:
    def __init__(self, path: str):
        self.path = path
        self.db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        path = self.path
        self.db = await _SharedConnection(lambda: sqlite3.connect(path), 64)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL;")
        await self.db.execute("PRAGMA foreign_keys=ON;")
        await self.db.execute("PRAGMA busy_timeout=5000;")
        try:
            await self._migrate()
        except BaseException:
            # Fail closed: a database whose migrations did not complete (or
            # did not land on _SCHEMA_VERSION) is not left open for use.
            await self.close()
            raise
        logger.info("database ready at %s (schema version %d)", self.path, _SCHEMA_VERSION)

    async def close(self) -> None:
        if self.db is not None:
            await self.db.close()
            self.db = None

    @asynccontextmanager
    async def write_transaction(self) -> AsyncIterator["Database"]:
        """One multi-step mutation as ONE transaction on an isolated connection.

        Every request shares ``self.db``, and SQLite transactions belong to the
        connection, not to the coroutine: at any ``await`` another request can
        ``commit()`` (persisting a partial state) or ``rollback()`` (dropping
        foreign writes). The yielded ``Database`` is bound to its own writer
        connection instead, so every existing method works unchanged inside the
        transaction - as long as it does not commit itself.

        ``foreign_keys`` and ``busy_timeout`` per connection (WAL is a
        persistent database mode set by ``connect``), ``BEGIN IMMEDIATE`` so
        reads inside observe the state this transaction updates, commit on
        success, rollback on any exception, connection always closed.
        ``database is locked`` after the busy timeout propagates like any
        other error.

        Inside the block, use only the yielded instance: a write on the shared
        connection would wait for this transaction's lock.

        A ``commit()`` or ``rollback()`` issued inside the block (a method
        called without ``commit=False``, or directly on the writer connection)
        raises ``RuntimeError`` and the transaction is rolled back (R3-02).
        """
        path = self.path
        writer = Database(path)
        conn = await _WriterConnection(lambda: sqlite3.connect(path), 64)
        writer.db = conn
        conn.row_factory = aiosqlite.Row
        try:
            await conn.execute("PRAGMA busy_timeout=5000")
            await conn.execute("PRAGMA foreign_keys=ON")
            await conn.execute("BEGIN IMMEDIATE")
            conn.guarded = True
            yield writer
            conn.guarded = False
            await conn.commit()
        except BaseException:
            conn.guarded = False
            await conn.rollback()
            raise
        finally:
            await writer.close()

    async def _migrate(self) -> None:
        async with self.db.executescript(_SCHEMA):
            pass
        await self.db.executescript(_INDEXES)
        # Simple user_version based migrations for the future.
        cursor = await self.db.execute("PRAGMA user_version;")
        row = await cursor.fetchone()
        current = row[0] if row else 0
        if current < 18:
            # v4 -> v5: add provenance/topology columns to outpoints without
            # touching existing rows (non-destructive: safe defaults).
            await self._add_outpoint_columns()
            # v5 -> v6: the address_evidence / wallet_discoveries /
            # wallet_scan_jobs / address_conflicts tables are created by the
            # idempotent _SCHEMA block on every connect, so older databases
            # pick them up automatically here. No data migration is needed.
            # v6 -> v7: ownership_custody_conflicts joins them the same way
            # (pure additive schema, no data migration). The bump is a marker
            # so the migrations block stays honest about the current version.
            # v7 -> v8: add trace_mode to traces table (BLOCKCHAIN_ANALYSIS
            # MODE_REQUIREMENTS.md). Existing rows default to FORWARD_PROVENANCE.
            await self._add_trace_mode_column()
            # v8 -> v9: analysis_jobs table (BLOCKCHAIN_ANALYSIS_ASYNC_UX_
            # PERFORMANCE_FIX_REQUIREMENTS.md). Pure additive table created by
            # the idempotent _SCHEMA block on every connect; no data migration.
            # v9 -> v10: trace_reverse_seeds table (v0.6.0 reverse modes).
            # Pure additive table created by the idempotent _SCHEMA block on
            # every connect; no data migration.
            # v10 -> v11: address_discovery_jobs table
            # (REVERSE_ADDRESS_DISCOVERY_LIFECYCLE_FIX_REQUIREMENTS.md). Pure
            # additive table created by the idempotent _SCHEMA block on every
            # connect; no data migration.
            # v11 -> v12: acquisitions table
            # (ACQUISITION_TRACKING_V1_REQUIREMENTS.md). Pure additive table
            # created by the idempotent _SCHEMA block on every connect; no
            # data migration and no existing rows are modified.
            # v12 -> v13: nullable acquisitions.fee_percent
            # (ACQUISITION_UX_IMPROVEMENTS_V1_REQUIREMENTS.md). Existing rows
            # keep their fee_percent NULL and their absolute fees_eur untouched.
            await self._add_acquisition_fee_percent_column()
            # v13 -> v14: nullable acquisitions.reference + notes
            # (ACQUISITION_TRACKING_V1_REQUIREMENTS.md CSV import). Existing
            # rows get NULL for both; no existing data is modified.
            await self._add_acquisition_reference_notes_columns()
            # v14 -> v15: origin_type + transfer_destination_address +
            # nullable origin columns (CUSTODIAL_ACQUISITIONS_V1_REQUIREMENTS.md).
            # Every existing row becomes origin_type 'ONCHAIN' with its origin
            # columns preserved verbatim; the UNIQUE(origin_txid, origin_vout)
            # constraint stays intact (SQLite treats NULLs as distinct, so
            # multiple custodial rows may carry NULL origins).
            await self._add_acquisition_custodial_support()
            # v15 -> v16: disposals + disposal_acquisition_links +
            # disposal_blockchain_evidence tables
            # (ACQUISITION_REVIEW_AND_DISPOSALS_V1_REQUIREMENTS.md). Pure
            # additive tables created by the idempotent _SCHEMA block on every
            # connect; no data migration and no existing rows are modified.
            # v16 -> v17: exchange_exits / exchange_exit_sources /
            # exchange_exit_outputs tables (EXCHANGE_EXIT_REQUIREMENTS.md).
            # Pure additive tables created by _SCHEMA on every connect. Also
            # adds exchange_exit_id on disposals and disposed_sats /
            # network_fee_sats on disposal_acquisition_links (nullable, backward
            # compatible: existing rows keep allocated_sats as-is, disposed_sats
            # defaults to allocated_sats at read time).
            await self._add_exchange_exit_columns()
            # v17 -> v18: per-source ownership confirmation on an Exchange Exit
            # (EXCHANGE_EXIT_UNKNOWN_ORIGIN_V1). Pure additive nullable columns
            # on the EXISTING exchange_exit_sources table: ownership_status
            # (the source output's classification at exit time, UNKNOWN-safe)
            # and ownership_confirmed (1 only when the user confirmed an
            # otherwise-not-established UNKNOWN acquisition-origin outpoint).
            # Existing rows keep UNKNOWN / 0; never writes to the Known
            # Address Registry.
            await self._add_exchange_exit_source_ownership_columns()
            await self.db.execute("PRAGMA user_version = 18;")
            await self.db.commit()
        if current < 19:
            await _migration_v19.migrate_acquisition_anchors(self.db)
        if current < 20:
            await _migration_v20.migrate(self.db)
        if current < 21:
            await _migration_v21.migrate(self.db)
        if current < 22:
            await _migration_v22.migrate(self.db)
        if current < 23:
            await _migration_v23.migrate(self.db)
        if current < 24:
            await _migration_v24.migrate(self.db)
        if current < 25:
            await _migration_v25.migrate(self.db)
        # P2-N3-Rest: ensured on EVERY connect, not only on the 24 -> 25 bump,
        # so the index appears on the first start after legacy duplicates are
        # deleted (with duplicates the app starts without it and warns).
        await _migration_v25.ensure_unique_index(self.db)
        # Fail closed (P3-6, D-14): every step sets its own user_version, so
        # the chain must have landed exactly on _SCHEMA_VERSION. A database
        # at any other version is not used.
        cursor = await self.db.execute("PRAGMA user_version;")
        row = await cursor.fetchone()
        final = int(row[0]) if row else 0
        if final != _SCHEMA_VERSION:
            raise RuntimeError(
                f"schema version mismatch: user_version={final}, expected {_SCHEMA_VERSION}"
            )

    async def _add_outpoint_columns(self) -> None:
        """Idempotently add the schema-v5 outpoint columns to an old table."""
        cursor = await self.db.execute("PRAGMA table_info(outpoints);")
        rows = await cursor.fetchall()
        existing = {str(r["name"]) for r in rows}
        for name, ddl in _PROVENANCE_COLUMNS.items():
            if name not in existing:
                await self.db.execute(
                    "ALTER TABLE outpoints ADD COLUMN " + name + " " + ddl
                )
                logger.info("schema migration: added outpoints.%s", name)

    async def _add_trace_mode_column(self) -> None:
        """Idempotently add the schema-v8 trace_mode column to the traces table."""
        cursor = await self.db.execute("PRAGMA table_info(traces);")
        rows = await cursor.fetchall()
        existing = {str(r["name"]) for r in rows}
        if "trace_mode" not in existing:
            await self.db.execute(
                "ALTER TABLE traces ADD COLUMN trace_mode TEXT NOT NULL DEFAULT 'FORWARD_PROVENANCE'"
            )
            logger.info("schema migration: added traces.trace_mode")

    async def _add_acquisition_fee_percent_column(self) -> None:
        """Idempotently add the schema-v13 fee_percent column to acquisitions."""
        cursor = await self.db.execute("PRAGMA table_info(acquisitions);")
        rows = await cursor.fetchall()
        existing = {str(r["name"]) for r in rows}
        if "fee_percent" not in existing:
            await self.db.execute(
                "ALTER TABLE acquisitions ADD COLUMN fee_percent TEXT"
            )
            logger.info("schema migration: added acquisitions.fee_percent")

    async def _add_acquisition_reference_notes_columns(self) -> None:
        """Idempotently add the schema-v14 reference/notes columns to
        acquisitions (CSV import: official supported columns, persisted as
        acquisition metadata). Existing rows keep NULL (no data modified)."""
        cursor = await self.db.execute("PRAGMA table_info(acquisitions);")
        rows = await cursor.fetchall()
        existing = {str(r["name"]) for r in rows}
        for name in ("reference", "notes"):
            if name not in existing:
                await self.db.execute(
                    f"ALTER TABLE acquisitions ADD COLUMN {name} TEXT"
                )
                logger.info("schema migration: added acquisitions.%s", name)

    async def _add_acquisition_custodial_support(self) -> None:
        """Schema-v15 migration for custodial acquisitions.

        Adds acquisition.origin_type (default 'ONCHAIN') and the nullable
        transfer_destination_address column, and makes the four on-chain origin
        columns (origin_txid, origin_vout, origin_address, origin_amount_sats)
        nullable so custodial rows can exist without an on-chain origin.

        SQLite cannot relax a NOT NULL constraint in place, so when an existing
        table still declares NOT NULL origins (or lacks the new columns) the
        table is rebuilt: every existing row keeps its data and becomes
        origin_type 'ONCHAIN' (the column default), with origin values carried
        over verbatim. The UNIQUE (origin_txid, origin_vout) constraint is
        preserved; SQLite treats NULL values as distinct so multiple custodial
        rows may share NULL origins without colliding.
        """
        cursor = await self.db.execute("PRAGMA table_info(acquisitions);")
        rows = await cursor.fetchall()
        existing = {str(r["name"]) for r in rows}
        origin_cols = ("origin_txid", "origin_vout", "origin_address", "origin_amount_sats")
        target_cols = (
            "id", "acquisition_date", "btc_amount", "purchase_value_eur", "fees_eur",
            "fee_percent", "broker", "origin_type", "origin_txid", "origin_vout",
            "origin_address", "origin_amount_sats", "origin_block_height",
            "origin_block_time", "amount_mismatch", "reference", "notes",
            "transfer_destination_address", "trace_id", "trace_status", "status",
            "created_at", "updated_at",
        )
        needs_rebuild = "origin_type" not in existing or "transfer_destination_address" not in existing
        if not needs_rebuild:
            for r in rows:
                if str(r["name"]) in origin_cols and r["notnull"]:
                    needs_rebuild = True
                    break
        if not needs_rebuild:
            return
        common = [c for c in target_cols if c in existing and c not in ("origin_type", "transfer_destination_address")]
        collist = ", ".join(common)
        await self.db.execute(
            "CREATE TABLE acquisitions_new ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " acquisition_date TEXT NOT NULL,"
            " btc_amount TEXT NOT NULL,"
            " purchase_value_eur TEXT NOT NULL,"
            " fees_eur TEXT,"
            " fee_percent TEXT,"
            " broker TEXT,"
            " origin_type TEXT NOT NULL DEFAULT 'ONCHAIN',"
            " origin_txid TEXT,"
            " origin_vout INTEGER,"
            " origin_address TEXT,"
            " origin_amount_sats INTEGER,"
            " origin_block_height INTEGER,"
            " origin_block_time INTEGER,"
            " amount_mismatch INTEGER NOT NULL DEFAULT 0,"
            " reference TEXT,"
            " notes TEXT,"
            " transfer_destination_address TEXT,"
            " trace_id INTEGER,"
            " trace_status TEXT,"
            " status TEXT NOT NULL DEFAULT 'RESOLVING',"
            " created_at TEXT NOT NULL,"
            " updated_at TEXT NOT NULL,"
            " UNIQUE (origin_txid, origin_vout)"
            ")"
        )
        await self.db.execute(
            f"INSERT INTO acquisitions_new ({collist}) SELECT {collist} FROM acquisitions"
        )
        await self.db.execute("DROP TABLE acquisitions")
        await self.db.execute("ALTER TABLE acquisitions_new RENAME TO acquisitions")
        await self.db.execute("CREATE INDEX idx_acquisitions_status ON acquisitions(status)")
        logger.info("schema migration: acquisitions rebuilt for origin_type (v15)")

    async def _add_exchange_exit_columns(self) -> None:
        """Schema-v17 migration: Exchange Exit columns (v0.11.0).

        Adds disposals.exchange_exit_id (nullable) and the per-allocation
        disposed_sats / network_fee_sats columns on
        disposal_acquisition_links. All are NULL-safe for existing rows:
        a legacy allocation has no exchange-exit link (NULL) and reads back as
        disposed_sats = allocated_sats / network_fee_sats = 0.
        """
        t = await self.db.execute("PRAGMA table_info(disposals);")
        rows = await t.fetchall()
        existing = {str(r["name"]) for r in rows}
        if "exchange_exit_id" not in existing:
            await self.db.execute(
                "ALTER TABLE disposals ADD COLUMN exchange_exit_id INTEGER"
            )
            logger.info("schema migration: added disposals.exchange_exit_id")
        t = await self.db.execute("PRAGMA table_info(disposal_acquisition_links);")
        rows = await t.fetchall()
        existing = {str(r["name"]) for r in rows}
        for name, ddl in (
            ("disposed_sats", "INTEGER"),
            ("network_fee_sats", "INTEGER"),
        ):
            if name not in existing:
                await self.db.execute(
                    f"ALTER TABLE disposal_acquisition_links ADD COLUMN {name} {ddl}"
                )
                logger.info("schema migration: added disposal_acquisition_links.%s", name)

    async def _add_exchange_exit_source_ownership_columns(self) -> None:
        """Schema-v18 migration: per-source Exchange Exit ownership confirmation.

        Adds two pure additive NULL-safe columns on the EXISTING
        exchange_exit_sources table:
        * ownership_status    -> the source output's classification at exit time
        * ownership_confirmed -> 1 ONLY for the narrow UNKNOWN acquisition-origin
          fallback where the user confirmed self-custody for this exit.
        Existing rows keep UNKNOWN / 0; nothing is ever written to the Known
        Address Registry.
        """
        t = await self.db.execute("PRAGMA table_info(exchange_exit_sources);")
        rows = await t.fetchall()
        existing = {str(r["name"]) for r in rows}
        for name, ddl in (
            ("ownership_status", "TEXT NOT NULL DEFAULT 'UNKNOWN'"),
            ("ownership_confirmed", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in existing:
                await self.db.execute(
                    f"ALTER TABLE exchange_exit_sources ADD COLUMN {name} {ddl}"
                )
                logger.info("schema migration: added exchange_exit_sources.%s", name)

    # ------------------------------------------------------------------
    # Traces
    # ------------------------------------------------------------------

    async def create_trace(
        self,
        name: str,
        start_txid: str,
        start_address: Optional[str],
        start_vout: Optional[int],
        trace_mode: str = "FORWARD_PROVENANCE",
    ) -> Trace:
        now = _now()
        cursor = await self.db.execute(
            "INSERT INTO traces (name, start_txid, start_address, start_vout, created_at, updated_at, status, trace_mode)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, start_txid, start_address, start_vout, now, now, "IN_PROGRESS", trace_mode),
        )
        await self.db.commit()
        trace_id = int(cursor.lastrowid)
        return await self.get_trace(trace_id)  # type: ignore[return-value]

    async def get_trace(self, trace_id: int) -> Trace | None:
        cursor = await self.db.execute("SELECT * FROM traces WHERE id = ?", (trace_id,))
        row = await cursor.fetchone()
        return self._row_to_trace(row) if row else None

    async def list_traces(self) -> list[Trace]:
        cursor = await self.db.execute("SELECT * FROM traces ORDER BY id DESC")
        rows = await cursor.fetchall()
        return [self._row_to_trace(r) for r in rows]

    async def update_trace(
        self, trace_id: int, name: str | None = None,
        status: str | None = None,
    ) -> Trace | None:
        updates: list[str] = []
        values: list[Any] = []
        if name is not None:
            updates.append("name = ?")
            values.append(name)
        if status is not None:
            updates.append("status = ?")
            values.append(status)
        updates.append("updated_at = ?")
        values.append(_now())
        values.append(trace_id)
        if updates:
            sql = f"UPDATE traces SET {', '.join(updates)} WHERE id = ?"
            await self.db.execute(sql, values)
            await self.db.commit()
        return await self.get_trace(trace_id)

    async def _delete_trace_rows(self, trace_id: int) -> None:
        """Remove every row keyed by ``trace_id`` WITHOUT committing.

        Callers that need several traces (or traces plus other records) removed
        as ONE transaction use this; ``delete_trace`` is the committing
        single-trace wrapper. The two are kept in sync deliberately: a bulk
        operation must never reuse ``delete_trace`` directly, because its
        commit would end the surrounding transaction and destroy atomicity.
        """
        cursor = await self.db.execute(
            "SELECT id FROM custody_bridges WHERE trace_id = ?", (trace_id,)
        )
        bridge_ids = [int(row["id"]) for row in await cursor.fetchall()]
        for bid in bridge_ids:
            await self.db.execute(
                "DELETE FROM custody_bridge_sources WHERE bridge_id = ?", (bid,)
            )
        await self.db.execute(
            "DELETE FROM custody_bridges WHERE trace_id = ?", (trace_id,)
        )
        await self.db.execute(
            "DELETE FROM custodial_boundaries WHERE trace_id = ?", (trace_id,)
        )
        await self.db.execute(
            "DELETE FROM custodial_dismissals WHERE trace_id = ?", (trace_id,)
        )
        await self.db.execute(
            "DELETE FROM ownership_custody_conflicts WHERE trace_id = ?", (trace_id,)
        )
        await self.db.execute(
            "DELETE FROM annotations WHERE entity_type = 'trace' AND entity_id = ?",
            (str(trace_id),),
        )
        await self.db.execute("DELETE FROM trace_edges WHERE trace_id = ?", (trace_id,))
        # acquisitions.trace_id has no FK. Trace ids are reused after a
        # "Delete all traces" sequence reset, so a dangling link would bind a
        # new, unrelated trace to this Acquisition (and delete it with it).
        # The Acquisition itself is kept; only the link is cleared.
        await self.db.execute(
            "UPDATE acquisitions SET trace_id = NULL WHERE trace_id = ?", (trace_id,)
        )
        await self.db.execute("DELETE FROM traces WHERE id = ?", (trace_id,))

    async def delete_trace(self, trace_id: int) -> None:
        """Delete a trace AND all TRACE-SPECIFIC state.

        Every row keyed by ``trace_id`` is removed (boundaries, bridges and
        their deposit sources, dismissals, ownership/custody conflicts,
        trace-scoped annotations, edges, the trace record itself;
        ``analysis_jobs`` and ``trace_reverse_seeds`` follow via FK cascade).
        An Acquisition linked to the trace is kept, but its
        ``acquisitions.trace_id`` is set to NULL so a reused trace id can never
        bind a new trace to it. Shared /
        global data is deliberately kept so a recreated trace from the same
        start outpoint rebuilds quickly from the cached blockchain data:
        transactions, outpoints, spends, global outpoint ownership, the known
        address registry + address_evidence and wallet discovery rows are NOT
        touched.

        One isolated transaction (P1-9).
        """
        async with self.write_transaction() as tx:
            await tx._delete_trace_rows(trace_id)

    async def delete_all_traces(self) -> int:
        """Delete EVERY trace and all TRACE-SPECIFIC state.

        Each trace is removed with the same semantics as ``delete_trace``:
        custody bridges + their deposit sources, boundaries, dismissals,
        ownership/custody conflicts, trace-scoped annotations, edges and the
        trace record itself.  Shared / global data is deliberately preserved:
        the blockchain cache (transactions, outpoints, spends), global outpoint
        ownership, the Known Address Registry + address_evidence and wallet
        discovery rows are NOT touched.

        The ``traces`` id sequence is reset so the next trace created is
        ``Trace #1`` again. Trace ids are therefore not stable across this
        reset, which is why every ``acquisitions.trace_id`` link to a deleted
        trace is set to NULL (see ``_delete_trace_rows``); the Acquisitions
        themselves are kept.  Returns the number of traces deleted.

        One isolated transaction (P1-9) for every trace, the link clearing and
        the sequence reset: a failure leaves all traces in place.
        """
        async with self.write_transaction() as tx:
            cursor = await tx.db.execute("SELECT id FROM traces ORDER BY id")
            ids = [int(row["id"]) for row in await cursor.fetchall()]
            for tid in ids:
                await tx._delete_trace_rows(tid)
            await tx.db.execute("DELETE FROM sqlite_sequence WHERE name = 'traces'")
        return len(ids)

    @staticmethod
    def _row_to_trace(row: aiosqlite.Row) -> Trace:
        return Trace(
            id=row["id"],
            name=row["name"] or "",
            start_txid=row["start_txid"],
            start_address=row["start_address"],
            start_vout=row["start_vout"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            status=row["status"],
            trace_mode=(
                row["trace_mode"]
                if "trace_mode" in row.keys()
                else "FORWARD_PROVENANCE"
            ),
        )

    # ------------------------------------------------------------------
    # Transactions (cache)
    # ------------------------------------------------------------------

    async def upsert_transaction(self, info: dict[str, Any], *, commit=True) -> None:
        raw_json = json.dumps(info.get("raw_json")) if info.get("raw_json") else None
        await self.db.execute(
            """
            INSERT INTO transactions (
                txid, block_hash, block_height, block_time, confirmations,
                input_total_sats, output_total_sats, fee_sats, vsize,
                raw_json, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(txid) DO UPDATE SET
                block_hash = excluded.block_hash,
                block_height = excluded.block_height,
                block_time = excluded.block_time,
                confirmations = excluded.confirmations,
                input_total_sats = excluded.input_total_sats,
                output_total_sats = excluded.output_total_sats,
                fee_sats = excluded.fee_sats,
                vsize = excluded.vsize,
                raw_json = excluded.raw_json,
                fetched_at = excluded.fetched_at
            """,
            (
                info.get("txid"),
                info.get("block_hash"),
                info.get("block_height"),
                info.get("block_time"),
                info.get("confirmations"),
                info.get("input_total_sats"),
                info.get("output_total_sats"),
                info.get("fee_sats"),
                info.get("vsize"),
                raw_json,
                int(time.time()),
            ),
        )
        if commit:
            await self.db.commit()

    async def fill_transaction_totals(
        self,
        txid: str,
        input_total_sats: int | None,
        output_total_sats: int | None,
        fee_sats: int | None,
    ) -> None:
        """Complete the derived totals of a cached transaction row in place.

        Only the three derived columns change, and only when they differ; a
        row cached by the wallet inventory carries no input total or fee.
        """
        cursor = await self.db.execute(
            """
            UPDATE transactions
               SET input_total_sats = ?, output_total_sats = ?, fee_sats = ?
             WHERE txid = ?
               AND (input_total_sats IS NOT ? OR output_total_sats IS NOT ?
                    OR fee_sats IS NOT ?)
            """,
            (input_total_sats, output_total_sats, fee_sats, txid,
             input_total_sats, output_total_sats, fee_sats),
        )
        if cursor.rowcount:
            await self.db.commit()

    async def get_transaction(self, txid: str) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM transactions WHERE txid = ?", (txid,)
        )
        row = await cursor.fetchone()
        if not row:
            return None
        info = dict(row)
        if info.get("raw_json"):
            info["raw_json"] = json.loads(info["raw_json"])
        return info

    async def count_transactions(self) -> int:
        cursor = await self.db.execute("SELECT COUNT(*) AS n FROM transactions")
        row = await cursor.fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------
    # Outpoints
    # ------------------------------------------------------------------

    async def upsert_outpoint(self, o: dict[str, Any], *, commit=True) -> None:
        """Upsert one outpoint row.

        The schema-v5 provenance/topology fields are only written when the
        caller supplies them; a partial re-upsert (e.g. a spend-status refresh
        that does not know provenance) therefore never clobbers previously
        computed provenance.
        """
        base = {
            "address": o.get("address"),
            "amount_sats": o.get("amount_sats"),
            "script_pubkey": o.get("script_pubkey"),
            "script_type": o.get("script_type"),
            "block_height": o.get("block_height"),
            "block_time": o.get("block_time"),
            "spend_status": o.get("spend_status", "UNKNOWN"),
        }
        extras: dict[str, Any] = {}
        if o.get("provenance_state") is not None:
            extras["provenance_state"] = o["provenance_state"]
        if o.get("transaction_topology") is not None:
            extras["transaction_topology"] = o["transaction_topology"]
        if o.get("likely_batch") is not None:
            extras["likely_batch"] = 1 if o["likely_batch"] else 0
        if o.get("provenance_reasons") is not None:
            val = o["provenance_reasons"]
            extras["provenance_reasons"] = (
                json.dumps(val) if isinstance(val, list) else val
            )

        fields = {**base, **extras}
        current = await self.get_outpoint(o.get("txid"), o.get("vout"))
        if current is not None and all(current.get(k) == v for k, v in fields.items()):
            # B5-01: identical content is no write (and no commit), so a read
            # path re-caching known facts leaves the evidence revision alone.
            return
        cols = ["txid", "vout", *fields.keys()]
        values = [o.get("txid"), o.get("vout"), *fields.values()]
        sets = [f"{k} = excluded.{k}" for k in fields]
        sql = (
            "INSERT INTO outpoints (" + ", ".join(cols) + ")"
            " VALUES (" + ", ".join("?" for _ in cols) + ")"
            " ON CONFLICT(txid, vout) DO UPDATE SET " + ", ".join(sets)
        )
        await self.db.execute(sql, values)
        if commit:
            await self.db.commit()

    async def get_outpoint(self, txid: str, vout: int) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM outpoints WHERE txid = ? AND vout = ?", (txid, vout)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_outpoint_spend_status(
        self, txid: str, vout: int, spend_status: str
    ) -> None:
        current = await self.get_outpoint(txid, vout)
        if current is None or current.get("spend_status") == spend_status:
            # B5-01: nothing to change (the UPDATE would match no row or keep
            # the value), so no write and no commit.
            return
        await self.db.execute(
            "UPDATE outpoints SET spend_status = ? WHERE txid = ? AND vout = ?",
            (spend_status, txid, vout),
        )
        await self.db.commit()

    # ------------------------------------------------------------------
    # Spends
    # ------------------------------------------------------------------

    async def upsert_spend(
        self, source_txid: str, source_vout: int,
        spending_txid: str, vin_index: int | None,
    ) -> None:
        await self.db.execute(
            """
            INSERT OR REPLACE INTO spends (source_txid, source_vout, spending_txid, vin_index)
            VALUES (?, ?, ?, ?)
            """,
            (source_txid, source_vout, spending_txid, vin_index),
        )
        await self.db.commit()

    async def get_spend(self, source_txid: str, source_vout: int) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM spends WHERE source_txid = ? AND source_vout = ?",
            (source_txid, source_vout),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Ownership
    # ------------------------------------------------------------------

    async def set_ownership(
        self,
        txid: str,
        vout: int,
        ownership_status: str,
        classification_source: str = "USER",
        wallet_name: Optional[str] = None,
        confidence: Optional[float] = None,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO ownership (
                txid, vout, ownership_status, classification_source, wallet_name, confidence
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(txid, vout) DO UPDATE SET
                ownership_status = excluded.ownership_status,
                classification_source = excluded.classification_source,
                wallet_name = excluded.wallet_name,
                confidence = excluded.confidence
            """,
            (
                txid, vout, ownership_status, classification_source, wallet_name, confidence,
            ),
        )
        await self.db.commit()

    async def get_ownership(
        self, txid: str, vout: int
    ) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM ownership WHERE txid = ? AND vout = ?", (txid, vout)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_ownership(self) -> list[dict[str, Any]]:
        cursor = await self.db.execute("SELECT * FROM ownership")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Known address registry
    # ------------------------------------------------------------------

    async def create_known_address_batch(
        self,
        name: str,
        wallet_name: str,
        source: str,
        address_count: int = 0,
        invalid_count: int = 0,
        duplicate_count: int = 0,
        imported_count: int = 0,
        commit: bool = True,
    ) -> dict[str, Any] | None:
        now = _now()
        cursor = await self.db.execute(
            """
            INSERT INTO known_address_batches (
                name, wallet_name, source, address_count, invalid_count,
                duplicate_count, imported_count, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name, wallet_name, source, address_count, invalid_count,
                duplicate_count, imported_count, now,
            ),
        )
        if commit:
            await self.db.commit()
        batch_id = cursor.lastrowid
        if batch_id is None:
            return None
        return await self.get_known_address_batch(batch_id)

    async def get_known_address_batch(self, batch_id: int) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM known_address_batches WHERE id = ?", (batch_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_known_address_batches(self) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM known_address_batches ORDER BY id DESC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def delete_known_address_batch(self, batch_id: int) -> int:
        """Remove a batch and all addresses imported by it. Returns count removed."""
        # Import/discovery evidence goes with its registry address (SK-4);
        # USER_CONFIRMED evidence is an explicit user decision and survives.
        # Registry-derived ownership and address conflicts are reset like
        # "Delete all addresses" (§4.1, P1-8b). ONE isolated transaction
        # (P1-9b): a rollback on the shared connection would drop foreign
        # writes, a foreign commit would persist a partial delete. The
        # addresses are read inside it, so they match what is deleted.
        async with self.write_transaction() as tx:
            cursor = await tx.db.execute(
                "SELECT address FROM known_addresses WHERE batch_id = ?", (batch_id,)
            )
            addresses = [str(r["address"]) for r in await cursor.fetchall()]
            removed = len(addresses)
            await tx.db.execute(
                "DELETE FROM address_evidence "
                "WHERE source IN ('MANUAL_IMPORT', 'XPUB_DISCOVERY') "
                "AND address IN (SELECT address FROM known_addresses WHERE batch_id = ?)",
                (batch_id,),
            )
            await tx.db.execute(
                "DELETE FROM known_addresses WHERE batch_id = ?", (batch_id,)
            )
            await tx.db.execute(
                "DELETE FROM known_address_batches WHERE id = ?", (batch_id,)
            )
            await tx._reset_registry_derived_state(addresses)
        return removed

    async def add_known_address(
        self,
        address: str,
        wallet_name: str = "default",
        label: str = "",
        batch_id: int | None = None,
        address_type: str = "",
        key_access: bool = True,
    ) -> str:
        """Insert one known address. Returns 'imported' or 'duplicate'.

        ``key_access=False`` keeps the address as ownership evidence but out of
        the Inventory scan and holdings (REG-1).
        """
        existing = await self.get_known_address(address)
        if existing is not None:
            return "duplicate"
        now = _now()
        await self.db.execute(
            """
            INSERT INTO known_addresses (
                address, wallet_name, label, batch_id, address_type, created_at, key_access
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (address, wallet_name, label, batch_id, address_type, now, int(bool(key_access))),
        )
        await self.db.commit()
        return "imported"

    async def set_known_address_key_access(self, address: str, key_access: bool) -> bool:
        """Set the Registry key access flag (REG-1). False if the address is unknown.

        Ownership evidence and scan rows are left as they are; reporting reads
        the flag, so turning key access on again restores scanned membership.
        """
        cursor = await self.db.execute(
            "UPDATE known_addresses SET key_access = ? WHERE address = ?",
            (int(bool(key_access)), address),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def bulk_insert_known_addresses(
        self, rows: list[dict[str, Any]]
    ) -> dict[str, int]:
        """Bulk insert. Returns stats: imported, duplicates, conflicts, total."""
        stats = {"imported": 0, "duplicates": 0, "conflicts": 0, "total": len(rows)}
        now = _now()
        for row in rows:
            address = row["address"]
            wallet_name = str(row.get("wallet_name") or "default")
            label = str(row.get("label") or "")
            batch_id = row.get("batch_id")
            address_type = str(row.get("address_type") or "")
            key_access = int(bool(row.get("key_access", True)))
            try:
                await self.db.execute(
                    """
                    INSERT INTO known_addresses (
                        address, wallet_name, label, batch_id, address_type, created_at, key_access
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (address, wallet_name, label, batch_id, address_type, now, key_access),
                )
                await self.db.commit()
                stats["imported"] += 1
            except Exception:
                cursor = await self.db.execute(
                    "SELECT COUNT(*) AS c FROM known_addresses WHERE address = ?",
                    (address,),
                )
                existing = await cursor.fetchone()
                if existing and int(existing["c"]) > 0:
                    stats["duplicates"] += 1
                else:
                    stats["conflicts"] += 1
        return stats

    async def is_known_address(self, address: str) -> bool:
        cursor = await self.db.execute(
            "SELECT COUNT(*) AS c FROM known_addresses WHERE address = ?", (address,)
        )
        row = await cursor.fetchone()
        return bool(row and int(row["c"]) > 0)

    async def get_known_address(self, address: str) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM known_addresses WHERE address = ?", (address,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def search_known_addresses(self, query: str = "") -> list[dict[str, Any]]:
        if query:
            like = f"%{query}%"
            cursor = await self.db.execute(
                """
                SELECT * FROM known_addresses
                WHERE address LIKE ? OR wallet_name LIKE ? OR label LIKE ?
                ORDER BY id LIMIT 1000
                """,
                (like, like, like),
            )
        else:
            cursor = await self.db.execute(
                "SELECT * FROM known_addresses ORDER BY id LIMIT 1000"
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def list_wallet_groups(self) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            """
            SELECT wallet_name,
                   COUNT(*) AS address_count,
                   COUNT(DISTINCT batch_id) AS batch_count
            FROM known_addresses
            GROUP BY wallet_name
            ORDER BY wallet_name
            """
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def delete_known_address(self, address: str) -> bool:
        # Import/discovery evidence goes with its registry address (SK-4);
        # USER_CONFIRMED evidence is an explicit user decision and survives.
        # Registry-derived ownership and address conflicts are reset like
        # "Delete all addresses" (§4.1, P1-8b). ONE isolated transaction
        # (P1-9b), see delete_known_address_batch.
        async with self.write_transaction() as tx:
            await tx.db.execute(
                "DELETE FROM address_evidence "
                "WHERE address = ? AND source IN ('MANUAL_IMPORT', 'XPUB_DISCOVERY')",
                (address,),
            )
            cursor = await tx.db.execute(
                "DELETE FROM known_addresses WHERE address = ?", (address,)
            )
            removed = cursor.rowcount > 0
            if removed:
                await tx._reset_registry_derived_state([address])
        return removed

    async def count_known_addresses(self) -> int:
        cursor = await self.db.execute("SELECT COUNT(*) AS c FROM known_addresses")
        row = await cursor.fetchone()
        return int(row["c"]) if row else 0

    async def set_batch_import_counts(
        self, batch_id: int, imported: int, duplicates: int
    ) -> None:
        await self.db.execute(
            "UPDATE known_address_batches SET duplicate_count = ?, imported_count = ? WHERE id = ?",
            (duplicates, imported, batch_id),
        )
        await self.db.commit()

    # ------------------------------------------------------------------
    # Custodial boundaries & custody bridges
    # ------------------------------------------------------------------

    async def create_custodial_boundary(
        self,
        trace_id: int,
        txid: str,
        vout: int,
        service_name: str = "",
        note: Optional[str] = None,
    ) -> dict[str, Any] | None:
        now = _now()
        await self.db.execute(
            """
            INSERT INTO custodial_boundaries (trace_id, txid, vout, service_name, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(trace_id, txid, vout) DO UPDATE SET
                service_name = excluded.service_name,
                note = excluded.note
            """,
            (trace_id, txid, vout, service_name or "", note, now),
        )
        # A confirmed/accepted boundary supersedes any earlier decline decision
        # for the same outpoint, so it is no longer surfaced as a suggestion.
        await self.db.execute(
            "DELETE FROM custodial_dismissals WHERE trace_id = ? AND txid = ? AND vout = ?",
            (trace_id, txid, vout),
        )
        await self.db.commit()
        cursor = await self.db.execute(
            "SELECT id FROM custodial_boundaries WHERE trace_id = ? AND txid = ? AND vout = ?",
            (trace_id, txid, vout),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return await self.get_custodial_boundary_by_id(int(row["id"]))

    async def get_custodial_boundary(
        self, trace_id: int, txid: str, vout: int
    ) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM custodial_boundaries WHERE trace_id = ? AND txid = ? AND vout = ?",
            (trace_id, txid, vout),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_custodial_boundary_by_id(
        self, boundary_id: int
    ) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM custodial_boundaries WHERE id = ?", (boundary_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_custodial_boundaries(self, trace_id: int) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM custodial_boundaries WHERE trace_id = ? ORDER BY id", (trace_id,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def delete_custodial_boundary(self, boundary_id: int) -> bool:
        """Delete a boundary. Any custody bridges whose only source is this
        boundary are removed as well (they would be dangling). The underlying
        Bitcoin trace data is never touched."""
        cursor = await self.db.execute(
            "SELECT bridge_id FROM custody_bridge_sources WHERE boundary_id = ?",
            (boundary_id,),
        )
        bridge_rows = await cursor.fetchall()
        bridge_ids = [int(r["bridge_id"]) for r in bridge_rows]
        for bridge_id in bridge_ids:
            await self.db.execute(
                "DELETE FROM custody_bridge_sources WHERE bridge_id = ?", (bridge_id,)
            )
            await self.db.execute(
                "DELETE FROM custody_bridges WHERE id = ?", (bridge_id,)
            )
        await self.db.execute(
            "DELETE FROM custody_bridge_sources WHERE boundary_id = ?", (boundary_id,)
        )
        cursor = await self.db.execute(
            "DELETE FROM custodial_boundaries WHERE id = ?", (boundary_id,)
        )
        removed = cursor.rowcount > 0
        await self.db.commit()
        return removed

    async def create_custody_bridge(
        self,
        trace_id: int,
        boundary_ids: list[int],
        destination_txid: str,
        destination_vout: int,
        provenance: str = "USER_CONFIRMED",
        confidence_score: Optional[float] = None,
        reasons: Optional[list[str]] = None,
        service_name: str = "",
        note: Optional[str] = None,
    ) -> dict[str, Any] | None:
        now = _now()
        reasons_json = json.dumps(reasons or [])
        cursor = await self.db.execute(
            """
            INSERT INTO custody_bridges (
                trace_id, destination_txid, destination_vout, provenance,
                confidence_score, reasons, service_name, note, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trace_id, destination_txid, destination_vout) DO UPDATE SET
                provenance = excluded.provenance,
                confidence_score = excluded.confidence_score,
                reasons = excluded.reasons,
                service_name = excluded.service_name,
                note = excluded.note
            RETURNING id
            """,
            (
                trace_id, destination_txid, destination_vout, provenance,
                confidence_score, reasons_json, service_name or "", note, now,
            ),
        )
        # RETURNING yields the bridge id for both the insert and the
        # DO UPDATE branch; lastrowid would be stale on the update path.
        row = await cursor.fetchone()
        await self.db.commit()
        if row is None:
            return None
        bridge_id = int(row[0])
        for boundary_id in boundary_ids:
            try:
                await self.db.execute(
                    "INSERT INTO custody_bridge_sources (bridge_id, boundary_id) VALUES (?, ?)",
                    (bridge_id, boundary_id),
                )
            except Exception:
                pass  # already linked
        await self.db.commit()
        bridges = await self.get_custody_bridges(trace_id)
        return next((b for b in bridges if b["id"] == bridge_id), None)

    async def get_custody_bridges(self, trace_id: int) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            """
            SELECT b.*, bs.boundary_id AS boundary_id, cb.txid AS boundary_txid, cb.vout AS boundary_vout
            FROM custody_bridges b
            LEFT JOIN custody_bridge_sources bs ON bs.bridge_id = b.id
            LEFT JOIN custodial_boundaries cb ON cb.id = bs.boundary_id
            WHERE b.trace_id = ?
            ORDER BY b.id
            """,
            (trace_id,),
        )
        rows = await cursor.fetchall()
        grouped: dict[int, dict[str, Any]] = {}
        for r in rows:
            d = dict(r)
            bid = int(d["id"])
            entry = grouped.setdefault(bid, {
                "id": bid,
                "trace_id": d["trace_id"],
                "destination_txid": d["destination_txid"],
                "destination_vout": d["destination_vout"],
                "provenance": d["provenance"],
                "confidence_score": d["confidence_score"],
                "reasons": json.loads(d["reasons"] or "[]"),
                "service_name": d["service_name"] or "",
                "note": d["note"],
                "created_at": d["created_at"],
                "sources": [],
            })
            if d.get("boundary_txid"):
                entry["sources"].append({
                    "boundary_id": d.get("boundary_id"),
                    "txid": d["boundary_txid"],
                    "vout": d["boundary_vout"],
                })
        out = list(grouped.values())
        # Stable primary source = boundary with the smallest id.
        for entry in out:
            if entry["sources"]:
                entry["sources"].sort(key=lambda s: s["boundary_id"] or 0)
                primary = entry["sources"][0]
                entry["boundary_id"] = primary["boundary_id"]
                entry["boundary_txid"] = primary["txid"]
                entry["boundary_vout"] = primary["vout"]
            else:
                entry.update({"boundary_id": None, "boundary_txid": None, "boundary_vout": None})
        return out

    async def delete_custody_bridge(self, bridge_id: int) -> bool:
        """Remove a custody bridge. The Bitcoin trace data remains intact."""
        await self.db.execute(
            "DELETE FROM custody_bridge_sources WHERE bridge_id = ?", (bridge_id,)
        )
        cursor = await self.db.execute(
            "DELETE FROM custody_bridges WHERE id = ?", (bridge_id,)
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def custody_bridge_destinations(self, trace_id: int) -> list[tuple[str, int]]:
        bridges = await self.get_custody_bridges(trace_id)
        out: list[tuple[str, int]] = []
        for b in bridges:
            if b.get("destination_txid"):
                out.append((b["destination_txid"], b["destination_vout"]))
        return out

    # ------------------------------------------------------------------
    # Custodial dismissals (declined heuristic suggestions)
    # ------------------------------------------------------------------

    async def create_custodial_dismissal(
        self,
        trace_id: int,
        txid: str,
        vout: int,
        reasons: list[str] | None = None,
        confidence: str = "likely",
        service_name: str = "",
        note: Optional[str] = None,
    ) -> dict[str, Any]:
        now = _now()
        await self.db.execute(
            """
            INSERT INTO custodial_dismissals
                (trace_id, txid, vout, reasons, confidence, service_name, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trace_id, txid, vout) DO UPDATE SET
                reasons = excluded.reasons,
                confidence = excluded.confidence,
                service_name = excluded.service_name,
                note = excluded.note,
                created_at = excluded.created_at
            """,
            (
                trace_id,
                txid,
                vout,
                json.dumps(reasons or []),
                confidence,
                service_name or "",
                note,
                now,
            ),
        )
        await self.db.commit()
        return await self.get_custodial_dismissal(trace_id, txid, vout)  # type: ignore[return-value]

    async def get_custodial_dismissals(self, trace_id: int) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM custodial_dismissals WHERE trace_id = ? ORDER BY id",
            (trace_id,),
        )
        rows = await cursor.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["reasons"] = json.loads(d.get("reasons") or "[]")
            out.append(d)
        return out

    async def get_custodial_dismissal(
        self, trace_id: int, txid: str, vout: int
    ) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM custodial_dismissals WHERE trace_id = ? AND txid = ? AND vout = ?",
            (trace_id, txid, vout),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        d["reasons"] = json.loads(d.get("reasons") or "[]")
        return d

    async def delete_custodial_dismissal(
        self, trace_id: int, txid: str, vout: int
    ) -> bool:
        cursor = await self.db.execute(
            "DELETE FROM custodial_dismissals WHERE trace_id = ? AND txid = ? AND vout = ?",
            (trace_id, txid, vout),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def custodial_dismissal_outpoints(
        self, trace_id: int
    ) -> set[tuple[str, int]]:
        cursor = await self.db.execute(
            "SELECT txid, vout FROM custodial_dismissals WHERE trace_id = ?",
            (trace_id,),
        )
        rows = await cursor.fetchall()
        return {(str(r["txid"]), int(r["vout"])) for r in rows}

    async def find_registered_outpoints_after(
        self, block_time: int | None
    ) -> list[dict[str, Any]]:
        """Candidate continuation outputs: every outpoint whose address is in
        the known-address registry, created strictly AFTER ``block_time``.

        This is one of the sources for custody-bridge candidate search (the
        other is a scantxoutset scan of the live chain). No xpub required.
        """
        if block_time is None:
            cursor = await self.db.execute(
                """
                SELECT o.*, k.wallet_name AS registry_wallet, k.label AS registry_label,
                       k.address_type AS registry_address_type
                FROM outpoints o
                JOIN known_addresses k ON k.address = o.address
                ORDER BY o.block_time DESC
                """
            )
        else:
            cursor = await self.db.execute(
                """
                SELECT o.*, k.wallet_name AS registry_wallet, k.label AS registry_label,
                       k.address_type AS registry_address_type
                FROM outpoints o
                JOIN known_addresses k ON k.address = o.address
                WHERE o.block_time > ?
                ORDER BY o.block_time DESC
                """,
                (block_time,),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Wallet discovery & address evidence (schema v6)
    # ------------------------------------------------------------------

    async def create_wallet_discovery(
        self,
        wallet_name: str,
        label: str,
        key_type: str,
        network: str,
        script_type: str,
        account_path: str,
        gap_limit: int,
        scan_mode: str = "scanblocks",
        start_height: int | None = None,
        end_height: int | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        now = _now()
        cursor = await self.db.execute(
            """
            INSERT INTO wallet_discoveries (
                wallet_name, label, key_type, network, script_type, account_path,
                gap_limit, scan_mode, start_height, end_height, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'RUNNING', ?)
            """,
            (
                wallet_name, label, key_type, network, script_type, account_path,
                gap_limit, scan_mode, start_height, end_height, now,
            ),
        )
        if commit:
            await self.db.commit()
        discovery_id = int(cursor.lastrowid)
        return await self.get_wallet_discovery(discovery_id)  # type: ignore[return-value]

    async def get_wallet_discovery(self, discovery_id: int) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM wallet_discoveries WHERE id = ?", (discovery_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def find_completed_wallet_discovery(
        self,
        wallet_name: str,
        label: str,
        key_type: str,
        script_type: str,
        account_path: str,
        network: str,
    ) -> dict[str, Any] | None:
        """Return the most recent COMPLETED discovery for the same wallet.

        Repeated runs of the same wallet reuse its discovery slot instead of
        littering the database with a new row per run (dedup topic 3)."""
        cursor = await self.db.execute(
            """
            SELECT * FROM wallet_discoveries
            WHERE status = 'COMPLETED' AND wallet_name = ?
              AND label = ? AND key_type = ? AND script_type = ?
              AND account_path = ? AND network = ?
            ORDER BY id LIMIT 1
            """,
            (wallet_name, label, key_type, script_type, account_path, network),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_wallet_discoveries(self) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM wallet_discoveries ORDER BY id DESC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def finish_wallet_discovery(
        self,
        discovery_id: int,
        *,
        trace_count: int,
        branch0_found: int,
        branch1_found: int,
        branch0_last_index: int,
        branch1_last_index: int,
        found_vout_count: int,
        unexpected_vout_count: int,
        commit: bool = True,
    ) -> None:
        await self.db.execute(
            """
            UPDATE wallet_discoveries SET
                status = 'COMPLETED', finished_at = ?,
                trace_count = ?, branch0_found = ?, branch1_found = ?,
                branch0_last_index = ?, branch1_last_index = ?,
                found_vout_count = ?, unexpected_vout_count = ?
            WHERE id = ?
            """,
            (
                _now(), trace_count, branch0_found, branch1_found,
                branch0_last_index, branch1_last_index, found_vout_count,
                unexpected_vout_count, discovery_id,
            ),
        )
        if commit:
            await self.db.commit()

    async def delete_wallet_discovery(self, discovery_id: int) -> int:
        """Delete a discovery and the addresses/evidence it alone introduced.

        Addresses are only removed when (a) their only evidence row belongs to
        this discovery, and (b) none of their outpoints carries a per-outpoint
        USER ownership decision (P2-N2; placeholder/derived rows do not count).
        On-chain trace data (outpoints/transactions) is never touched.
        Runs as ONE isolated write transaction (P1-9b): a failure mid-flow
        leaves no partial DML open on the shared connection.
        """
        async with self.write_transaction() as tx:
            cursor = await tx.db.execute(
                "SELECT address FROM address_evidence WHERE discovery_id = ?",
                (discovery_id,),
            )
            evidence_addresses = [str(r["address"]) for r in await cursor.fetchall()]

            removed = 0
            for address in evidence_addresses:
                other = await tx.db.execute(
                    "SELECT COUNT(*) AS c FROM address_evidence "
                    "WHERE address = ? AND (discovery_id IS NULL OR discovery_id != ?)",
                    (address, discovery_id),
                )
                row = await other.fetchone()
                has_other = row and int(row["c"]) > 0
                if not has_other and await tx._address_not_owned_anywhere(address):
                    await tx.db.execute(
                        "DELETE FROM known_addresses WHERE address = ?", (address,)
                    )
                    removed += 1

            # The import batches must be read before this discovery's
            # evidence rows are deleted: selected afterwards, the batch delete
            # never matched (P1-9b). A batch that still holds a retained
            # address stays, so that address remains removable by batch.
            await tx.db.execute(
                "DELETE FROM known_address_batches WHERE id IN "
                "(SELECT DISTINCT batch_id FROM address_evidence WHERE discovery_id = ?) "
                "AND NOT EXISTS (SELECT 1 FROM known_addresses k "
                "WHERE k.batch_id = known_address_batches.id)",
                (discovery_id,),
            )
            await tx.db.execute(
                "DELETE FROM address_evidence WHERE discovery_id = ?", (discovery_id,)
            )
            await tx.db.execute(
                "DELETE FROM wallet_scan_jobs WHERE discovery_id = ?", (discovery_id,)
            )
            await tx.db.execute(
                "UPDATE address_conflicts SET status = 'SHARED', resolved_at = ? "
                "WHERE evidence_wallet_name IN "
                "(SELECT wallet_name FROM wallet_discoveries WHERE id = ?)",
                (_now(), discovery_id),
            )
            cursor = await tx.db.execute(
                "DELETE FROM wallet_discoveries WHERE id = ?", (discovery_id,)
            )
            return removed

    async def _reset_registry_derived_state(self, addresses: list[str]) -> int:
        """Drop registry-derived state for deleted registry addresses (§4.1).

        Deletes ``address_conflicts`` rows for the addresses and resets
        ownership rows of outpoints paying them to UNKNOWN/UNKNOWN unless the
        row is an explicit per-outpoint decision (USER only; USER_CONFIRMED is
        address-level evidence, SK-4b-Rest).
        Never sets EXTERNAL. Does not commit; the caller owns the transaction.
        Returns the number of ownership rows reset.
        """
        reset_outpoints = 0
        if addresses:
            # Address-conflict records only make sense while the registry exists.
            placeholders = ",".join("?" for _ in addresses)
            await self.db.execute(
                f"DELETE FROM address_conflicts WHERE address IN ({placeholders})",
                addresses,
            )

            # Reset ownership that derived from registry/import/discovery.
            # Explicit per-outpoint decisions (USER) survive. USER_CONFIRMED
            # is address-level evidence, not a per-outpoint decision
            # (SK-4b-Rest), so such rows are reset like other derived state.
            # Outpoints are found through idx_outpoints_address; a correlated
            # EXISTS scanned all ownership rows per batch and held the write
            # lock for seconds on a large registry (R3-03, same rows).
            preserve_sources = ("USER",)
            batch_size = 500
            for i in range(0, len(addresses), batch_size):
                batch = addresses[i : i + batch_size]
                placeholders = ",".join("?" for _ in batch)
                cursor = await self.db.execute(
                    f"""
                    UPDATE ownership
                    SET ownership_status = 'UNKNOWN',
                        classification_source = 'UNKNOWN',
                        wallet_name = NULL,
                        confidence = NULL
                    WHERE (txid, vout) IN (
                        SELECT txid, vout FROM outpoints
                        WHERE address IN ({placeholders})
                    )
                    AND classification_source NOT IN ({','.join('?' for _ in preserve_sources)})
                    """,
                    (*batch, *preserve_sources),
                )
                reset_outpoints += cursor.rowcount
        return reset_outpoints

    async def delete_all_bitcoin_addresses(self) -> dict[str, int]:
        """Reset the Known Address Registry and import/discovery evidence.

        Deletes every registered/imported/discovered address, import batches,
        wallet-discovery metadata and scan jobs, and address-evidence rows
        whose source is MANUAL_IMPORT or XPUB_DISCOVERY.  USER_CONFIRMED
        evidence is preserved.  Ownership rows whose classification was based
        solely on registry/import/discovery state are reset to UNKNOWN.

        Explicit per-outpoint classifications (classification_source USER)
        are preserved; USER_CONFIRMED rows are address-level evidence and are
        reset (SK-4b-Rest).  No outpoint is converted to EXTERNAL
        solely because registry evidence was removed.

        The operation is ONE isolated transaction (P1-9): an exception before
        the final commit leaves all persisted state unchanged, and no other
        request can commit a partial state.
        """
        async with self.write_transaction() as tx:
            cursor = await tx.db.execute("SELECT address FROM known_addresses")
            addresses = [str(row["address"]) for row in await cursor.fetchall()]

            cursor = await tx.db.execute(
                "SELECT COUNT(*) AS c FROM known_address_batches"
            )
            deleted_batches = int((await cursor.fetchone())["c"])

            cursor = await tx.db.execute(
                "SELECT COUNT(*) AS c FROM address_evidence "
                "WHERE source IN ('MANUAL_IMPORT', 'XPUB_DISCOVERY')"
            )
            deleted_evidence = int((await cursor.fetchone())["c"])

            cursor = await tx.db.execute("SELECT COUNT(*) AS c FROM wallet_discoveries")
            deleted_discoveries = int((await cursor.fetchone())["c"])

            cursor = await tx.db.execute("SELECT COUNT(*) AS c FROM wallet_scan_jobs")
            deleted_scan_jobs = int((await cursor.fetchone())["c"])

            # Remove import/discovery evidence first.
            await tx.db.execute(
                "DELETE FROM address_evidence WHERE source IN ('MANUAL_IMPORT', 'XPUB_DISCOVERY')"
            )
            # Remove the registry and its batches.
            await tx.db.execute("DELETE FROM known_addresses")
            await tx.db.execute("DELETE FROM known_address_batches")
            # Remove discovery metadata (no XPUB is stored here).
            await tx.db.execute("DELETE FROM wallet_discoveries")
            await tx.db.execute("DELETE FROM wallet_scan_jobs")

            reset_outpoints = await tx._reset_registry_derived_state(addresses)
        return {
            "deleted_addresses": len(addresses),
            "deleted_batches": deleted_batches,
            "deleted_evidence": deleted_evidence,
            "deleted_discoveries": deleted_discoveries,
            "deleted_scan_jobs": deleted_scan_jobs,
            "reset_outpoints": reset_outpoints,
        }

    # ------------------------------------------------------------------
    # Acquisition dataset reset ("Delete all Acquisitions")
    # ------------------------------------------------------------------

    async def _acquisition_owned_trace_ids(
        self, acq_rows: Optional[list[aiosqlite.Row]] = None
    ) -> list[int]:
        """Ids of the traces that exist BECAUSE of an Acquisition.

        This is the only place where "Acquisition-owned trace" is defined, and
        it is deliberately a positive signature rather than an inference from
        "no Acquisition points at me" (which would also match every trace the
        user created by hand).

        A trace is Acquisition-owned when it is a FORWARD_PROVENANCE trace that
        EITHER

        * is referenced by ``acquisitions.trace_id`` - the architectural link
          the schema defines for exactly this purpose - OR
        * still carries the canonical acquisition-trace name that
          ``_create_and_link_trace`` writes
          (``Acquisition <acquisition_date> <txid[:8]>:<vout>``). The second
          signal catches an Acquisition trace that lost its link: an origin
          re-anchor creates a replacement trace, and an independently deleted
          trace is re-created, so the previous one lingers unreferenced. The
          name is only ever produced by the Acquisition create path.

        ``trace_mode`` is the discriminator that keeps independent features
        out: BLOCKCHAIN_ANALYSIS, BLOCKCHAIN_ANALYSIS_REVERSE and
        REVERSE_PROVENANCE are ownership-neutral or ownership-driven analyses
        of the user's own coins that exist entirely independently of any
        Acquisition, and none of them is ever linked to one.

        Tests pin this pattern against the real producer so it cannot drift.

        ``acq_rows`` scopes the answer. ``None`` means "every Acquisition",
        which is the whole-dataset reset: with no Acquisition left, ANY
        signature-shaped trace is unreachable garbage and is collected. Passing
        specific rows asks the narrower question "what exists because of THESE
        Acquisitions", used by the single-Acquisition delete, where a bare
        signature match would be too coarse and could remove a DIFFERENT
        surviving Acquisition's trace. It then requires the name to be exactly
        the one this Acquisition's create path would have written.
        """
        if acq_rows is None:
            cursor = await self.db.execute(
                "SELECT id, trace_id, acquisition_date, origin_txid, origin_vout"
                " FROM acquisitions ORDER BY id"
            )
            acq_rows = await cursor.fetchall()
            whole_dataset = True
        else:
            whole_dataset = False

        linked: set[int] = set()
        expected_names: set[str] = set()
        for row in acq_rows:
            if row["trace_id"] is not None:
                linked.add(int(row["trace_id"]))
            if row["origin_txid"]:
                expected_names.add(
                    _acquisition_trace_name(
                        row["acquisition_date"],
                        row["origin_txid"],
                        row["origin_vout"],
                    )
                )

        cursor = await self.db.execute(
            "SELECT id, name, trace_mode FROM traces ORDER BY id"
        )

        owned: list[int] = []
        for row in await cursor.fetchall():
            trace_id = int(row["id"])
            if (row["trace_mode"] or "FORWARD_PROVENANCE") != "FORWARD_PROVENANCE":
                # Ownership-neutral / independent analysis modes survive.
                continue
            if trace_id in linked:
                owned.append(trace_id)
                continue
            name = row["name"] or ""
            if name in expected_names:
                owned.append(trace_id)
            elif whole_dataset and _ACQUISITION_TRACE_NAME_RE.match(name):
                owned.append(trace_id)
        return owned

    async def _delete_acquisition_rows(self, acq_ids: list[int]) -> dict[str, int]:
        """The single canonical DML that removes Acquisitions and everything
        keyed to them by ``acquisition_id``.

        Both ``delete_acquisition`` (one id) and ``delete_all_acquisitions``
        (every id) go through here, so the dependency semantics cannot drift
        between the single and the bulk path. Allocation links go first: they
        are the only ``acquisition_id`` references in the schema and neither
        table carries an FK to ``acquisitions``, so removing the allocations is
        precisely what keeps the referential integrity intact.

        Commits nothing. The caller owns the transaction so the whole delete
        stays one atomic unit.
        """
        if not acq_ids:
            return {
                "deleted_acquisitions": 0,
                "removed_exchange_exit_links": 0,
                "removed_disposal_links": 0,
            }
        placeholders = ",".join("?" for _ in acq_ids)
        exit_cursor = await self.db.execute(
            f"DELETE FROM exchange_exit_sources WHERE acquisition_id IN ({placeholders})",
            acq_ids,
        )
        disposal_cursor = await self.db.execute(
            f"DELETE FROM disposal_acquisition_links WHERE acquisition_id IN ({placeholders})",
            acq_ids,
        )
        acq_cursor = await self.db.execute(
            f"DELETE FROM acquisitions WHERE id IN ({placeholders})", acq_ids
        )
        return {
            "deleted_acquisitions": acq_cursor.rowcount,
            "removed_exchange_exit_links": exit_cursor.rowcount,
            "removed_disposal_links": disposal_cursor.rowcount,
        }

    async def delete_all_acquisitions(self) -> dict[str, int]:
        """Remove the whole Acquisition dataset so it can be imported again.

        This is a dataset reset, NOT a database reset. It removes the
        Acquisition records and the data that exists specifically because of
        them, and deliberately leaves independent application data alone.

        Deleted (Acquisition-owned / Acquisition-derived):

        * every active ``acquisitions`` anchor row;
        * the ``disposal_acquisition_links`` and ``exchange_exit_sources``
          allocation rows pointing at them, so no ``acquisition_id`` reference
          is left dangling. Both are plain columns with no FK, so removing the
          allocations here is what keeps the referential integrity that the
          single-Acquisition delete currently leaves open on the Exit side;
        * the Acquisition-owned forward traces (see
          ``_acquisition_owned_trace_ids``) with exactly the same trace-scoped
          semantics as ``delete_trace``.

        Preserved (independent objects - only the Acquisition relationship is
        removed, never the object itself):

        * ``exchange_exits`` + ``exchange_exit_outputs``. An Exchange Exit is a
          first-class user-confirmed CUSTODY BOUNDARY; its sources are the
          per-Acquisition allocation. The boundary, its date and its exchange
          deposit layout stay meaningful - and re-classifiable - with no
          allocation, exactly as the Exit survives the Acquisitions in the
          opposite delete direction. Exchange Exit != Disposal, and an Exit is
          never auto-created or auto-deleted from a side effect.
        * ``disposals`` + ``disposal_blockchain_evidence``. A Disposal is an
          independent ECONOMIC object (gross, fees, net, broker, date) that
          also supports custodial/off-chain/manual cases, so destroying it here
          would destroy unrelated user data. This matches the single-Acquisition
          delete, which removes only the allocation links.
        * traces in BLOCKCHAIN_ANALYSIS / BLOCKCHAIN_ANALYSIS_REVERSE /
          REVERSE_PROVENANCE mode, and any hand-created forward trace without
          the Acquisition signature.
        * the blockchain cache (transactions, outpoints, spends) - which is what
          lets a re-imported CSV re-trace without re-fetching - global outpoint
          ownership, the Known Address Registry, address evidence, wallet
          discovery, address discovery jobs and settings.

        Nothing in the ownership/registry domain is touched. Creating an
        Acquisition never writes ``known_addresses``, ``address_evidence`` or
        ``ownership`` rows, so there is no Acquisition-derived evidence to
        remove and no classification is re-derived as a side effect: no
        UNKNOWN/EXTERNAL/OWNED value changes here.

        Duplicate detection needs no separate reset. Canonical duplicate identity
        is ``UNIQUE (origin_txid, origin_vout)`` + ``get_acquisition_by_origin``
        on the active table, which is emptied here. The inert v19 recovery
        snapshot is preserved like a backup and never participates in identity.

        Position state is derived per read, never stored, so it is trivially
        valid at zero Acquisitions and needs no invalidation.

        The whole operation is ONE isolated transaction (P1-9): an exception
        before the final commit rolls back every change, so a failure can never
        leave half the dataset deleted, dangling allocations or orphaned
        traces, and no other request can commit a partial state.
        """
        async with self.write_transaction() as tx:
            cursor = await tx.db.execute(
                "SELECT id FROM acquisitions ORDER BY id"
            )
            acq_rows = await cursor.fetchall()
            acq_ids = [int(row["id"]) for row in acq_rows]

            trace_ids = await tx._acquisition_owned_trace_ids()

            cursor = await tx.db.execute("SELECT COUNT(*) AS c FROM exchange_exits")
            exits_before = int((await cursor.fetchone())["c"])
            cursor = await tx.db.execute("SELECT COUNT(*) AS c FROM disposals")
            disposals_before = int((await cursor.fetchone())["c"])
            cursor = await tx.db.execute("SELECT COUNT(*) AS c FROM traces")
            traces_before = int((await cursor.fetchone())["c"])

            # Traces first: they hold no FK to acquisitions, but removing the
            # trace-scoped state while the whole set is still known keeps the
            # single transaction's intent obvious.
            for trace_id in trace_ids:
                await tx._delete_trace_rows(trace_id)

            counts = await tx._delete_acquisition_rows(acq_ids)

        deleted_acquisitions = counts["deleted_acquisitions"]
        return {
            "deleted_acquisitions": deleted_acquisitions,
            "deleted_acquisition_traces": len(trace_ids),
            "removed_exchange_exit_links": counts["removed_exchange_exit_links"],
            "removed_disposal_links": counts["removed_disposal_links"],
            "preserved_exchange_exits": exits_before,
            "preserved_disposals": disposals_before,
            "preserved_traces": traces_before - len(trace_ids),
        }

    async def _address_not_owned_anywhere(self, address: str) -> bool:
        # P2-N2 (consolidation decision 2026-10-11): only a per-outpoint USER
        # decision blocks removing the address. UNKNOWN/UNKNOWN placeholders
        # (resets) and derived rows (legacy ADDRESS_REGISTRY / USER_CONFIRMED
        # reconciliation output) are not decisions; registry-derived OWNED is
        # not persisted at all since SK-4b-Rest.
        cursor = await self.db.execute(
            "SELECT COUNT(*) AS c FROM ownership o "
            "JOIN outpoints op ON op.txid = o.txid AND op.vout = o.vout "
            "WHERE op.address = ? AND o.classification_source = 'USER'",
            (address,),
        )
        row = await cursor.fetchone()
        if row and int(row["c"]) > 0:
            return False
        # Cached blockchain facts (including inventory observations) are not
        # ownership evidence. Keep those facts, but allow removing their wallet.
        return True

    async def create_scan_job(
        self,
        discovery_id: int,
        scan_mode: str,
        start_height: int | None,
        end_height: int | None,
        chunk_size: int = 500,
    ) -> dict[str, Any]:
        now = _now()
        cursor = await self.db.execute(
            """
            INSERT INTO wallet_scan_jobs (
                discovery_id, status, scan_mode, start_height, end_height,
                chunk_size, created_at, updated_at
            )
            VALUES (?, 'PENDING', ?, ?, ?, ?, ?, ?)
            """,
            (discovery_id, scan_mode, start_height, end_height, chunk_size, now, now),
        )
        await self.db.commit()
        job_id = int(cursor.lastrowid)
        cursor = await self.db.execute(
            "SELECT * FROM wallet_scan_jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        return dict(row)

    async def list_scan_jobs(self, discovery_id: int | None = None) -> list[dict[str, Any]]:
        if discovery_id is not None:
            cursor = await self.db.execute(
                "SELECT * FROM wallet_scan_jobs WHERE discovery_id = ? ORDER BY id",
                (discovery_id,),
            )
        else:
            cursor = await self.db.execute(
                "SELECT * FROM wallet_scan_jobs ORDER BY id DESC"
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def commit_discovery_results(
        self,
        discovery_id: int,
        wallet_name: str,
        batch_id: int,
        found: list[dict[str, Any]],
        commit: bool = True,
    ) -> dict[str, int]:
        """Atomically persist discovered addresses + evidence + conflicts.

        ``found`` is a list of dicts with keys: address, branch_index,
        address_index, derivation_path, key_type, first_seen_at, label.
        Returns stats: imported / duplicates / conflicts. Deduplication is by
        construction: the canonical known_addresses table stays unique per
        address and repeated evidence attaches to the same row (dedup topics).
        """
        stats = {"imported": 0, "duplicates": 0, "conflicts": 0}
        now = _now()
        try:
            for item in found:
                address = item["address"]
                existing = await self.get_known_address(address)
                if existing is None:
                    await self.db.execute(
                        """
                        INSERT INTO known_addresses (
                            address, wallet_name, label, batch_id,
                            address_type, created_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            address, wallet_name, item.get("label") or "",
                            batch_id, item.get("key_type") or "", now,
                        ),
                    )
                    stats["imported"] += 1
                else:
                    stats["duplicates"] += 1
                    if str(existing.get("wallet_name") or "") != wallet_name:
                        await self._record_address_conflict(
                            address, existing, wallet_name, item
                        )
                        stats["conflicts"] += 1

                await self.db.execute(
                    """
                    INSERT INTO address_evidence (
                        address, wallet_name, source, derivation_path,
                        branch_index, address_index, key_type, discovery_id,
                        batch_id, first_seen_at, last_seen_at, created_at
                    )
                    VALUES (?, ?, 'XPUB_DISCOVERY', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(address, wallet_name, source) DO UPDATE SET
                        derivation_path = excluded.derivation_path,
                        branch_index = excluded.branch_index,
                        address_index = excluded.address_index,
                        key_type = excluded.key_type,
                        discovery_id = excluded.discovery_id,
                        batch_id = excluded.batch_id,
                        first_seen_at = excluded.first_seen_at,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        address, wallet_name, item.get("derivation_path") or "",
                        item.get("branch_index"), item.get("address_index"),
                        item.get("key_type") or "", discovery_id, batch_id,
                        item.get("first_seen_at"), now, now,
                    ),
                )
            if commit:
                await self.db.commit()
        except Exception:
            # With commit=False the caller owns the transaction and its
            # rollback (write_transaction rejects one issued here, P4-N2).
            if commit:
                await self.db.rollback()
            raise
        return stats

    async def import_wallet_result(self, plan, found, meta):
        """Confirmed key-free import, on an isolated writer connection.

        No schema change and no intermediate commits. Runs in
        ``write_transaction``: BEGIN IMMEDIATE makes duplicate/conflict checks
        observe the state this transaction updates, other tasks sharing
        self.db cannot accidentally commit this transaction, and a commit or
        rollback issued inside it raises ``RuntimeError`` (R3-02, P4-5).
        Rolled back on any exception, including ``DiscoveryRegistryChanged``.
        """
        async with self.write_transaction() as writer:
            for item in found:
                current = await writer.get_known_address(item["address"])
                if (current.get("wallet_name") if current else None) != item.get("existing_wallet"):
                    raise DiscoveryRegistryChanged()
            discovery = await writer.find_completed_wallet_discovery(
                **{k: getattr(plan, k) for k in
                   ("wallet_name", "label", "key_type", "script_type", "account_path", "network")})
            if discovery is None:
                discovery = await writer.create_wallet_discovery(
                    **{k: getattr(plan, k) for k in
                       ("wallet_name", "label", "key_type", "network", "script_type",
                        "account_path", "gap_limit", "scan_mode", "start_height", "end_height")},
                    commit=False)
            did = discovery["id"]
            batch = await writer.create_known_address_batch(
                name=f"discovery-{did}", wallet_name=plan.wallet_name,
                source="XPUB_DISCOVERY", address_count=len(found), commit=False)
            stats = await writer.commit_discovery_results(
                did, plan.wallet_name, batch["id"], found, commit=False)
            summary = {}
            for branch in (0, 1):
                items = [m for m in found if m["branch_index"] == branch]
                summary[f"branch{branch}_found"] = len(items)
                summary[f"branch{branch}_last_index"] = max(
                    (m["address_index"] for m in items), default=-1)
            await writer.finish_wallet_discovery(
                did, **summary, **{k: meta[k] for k in
                    ("trace_count", "found_vout_count", "unexpected_vout_count")}, commit=False)
            await writer.db.execute(
                "UPDATE wallet_discoveries SET start_height=?, end_height=?, scan_mode=?, gap_limit=? WHERE id=?",
                (plan.start_height, plan.end_height, plan.scan_mode, plan.gap_limit, did))
            await writer.db.execute(
                "UPDATE known_address_batches SET imported_count=?, duplicate_count=? WHERE id=?",
                (stats["imported"], stats["duplicates"], batch["id"]))
            if "inventory" in meta:
                from backend.persistence.inventory import store_snapshot
                await store_snapshot(writer, [m["address"] for m in found], meta["inventory"])
            await writer.db.execute(
                "INSERT OR REPLACE INTO wallet_inventory_policy(discovery_id, policy_json) VALUES (?, ?)",
                (did, json.dumps({**{k: v for k, v in meta.items() if k != "inventory"},
                                 "scanned_at": meta.get("inventory", {}).get("observed_at")})))
            return {"discovery_id": did, **stats, **summary, "status": "COMPLETED", "committed": True}

    async def _record_address_conflict(
        self, address: str, existing: dict[str, Any],
        wallet_name: str, item: dict[str, Any],
    ) -> None:
        detail = (
            f"registry wallet '{existing.get('wallet_name')}' vs discovery wallet "
            f"'{wallet_name}' at {item.get('derivation_path') or '?'}"
        )
        now = _now()
        cursor = await self.db.execute(
            "SELECT id, status FROM address_conflicts WHERE address = ? AND status = 'PENDING'",
            (address,),
        )
        row = await cursor.fetchone()
        if row:
            await self.db.execute(
                """
                UPDATE address_conflicts SET reason = 'ownership_mismatch',
                    detail = ? WHERE id = ?
                """,
                (detail, int(row["id"])),
            )
        else:
            await self.db.execute(
                """
                INSERT INTO address_conflicts (
                    address, wallet_name, evidence_wallet_name, reason,
                    detail, status, created_at
                )
                VALUES (?, ?, ?, 'ownership_mismatch', ?, 'PENDING', ?)
                """,
                (address, str(existing.get("wallet_name") or ""), wallet_name, detail, now),
            )

    async def list_address_conflicts(
        self, status: str | None = None
    ) -> list[dict[str, Any]]:
        if status:
            cursor = await self.db.execute(
                "SELECT * FROM address_conflicts WHERE status = ? ORDER BY id",
                (status,),
            )
        else:
            cursor = await self.db.execute(
                "SELECT * FROM address_conflicts ORDER BY id DESC"
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def resolve_address_conflict(self, conflict_id: int, resolution: str) -> bool:
        """Resolve an address conflict.

        ``KEEP``  - the registry wallet stays; the conflicting evidence is
                    recorded but marked SHARED (no silent move).
        ``SHARED``- both wallets legitimately use the address.
        ``MOVED`` - the address effectively belongs to the discovery wallet;
                    the registry wallet_name is updated but the address is
                    never duplicated.
        """
        if resolution not in ("KEEP", "SHARED", "MOVED"):
            raise ValueError(f"invalid resolution: {resolution}")
        cursor = await self.db.execute(
            "SELECT * FROM address_conflicts WHERE id = ?", (conflict_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return False
        address = str(row["address"])
        if resolution == "MOVED":
            await self.db.execute(
                "UPDATE known_addresses SET wallet_name = ? WHERE address = ?",
                (str(row["evidence_wallet_name"]), address),
            )
        await self.db.execute(
            "UPDATE address_conflicts SET status = ?, resolved_at = ? WHERE id = ?",
            (resolution, _now(), conflict_id),
        )
        await self.db.commit()
        return True

    async def record_ownership_contradiction(
        self,
        address: str,
        user_wallet: str,
        evidence_wallet: str,
        detail: str,
    ) -> None:
        """Record an explicit USER EXTERNAL classification that contradicts
        newer direct ownership evidence for the same address.

        The explicit user decision is never deleted. The stronger new evidence
        wins for ownership because stale/mistaken EXTERNAL state must not hide
        freshly discovered owned addresses (re-check / re-classification). The
        contradiction is surfaced as a PENDING address conflict so nothing is
        silently discarded.
        """
        now = _now()
        cursor = await self.db.execute(
            "SELECT id, status, detail FROM address_conflicts "
            "WHERE address = ? AND reason = 'ownership_contradiction' AND status = 'PENDING'",
            (address,),
        )
        row = await cursor.fetchone()
        if row and row["detail"] == detail:
            # Trace reads call this on every load. An unchanged record is no
            # write, so concurrent provenance reads keep a stable evidence
            # revision instead of answering 503 (FIX-OWN-1).
            return
        if row:
            await self.db.execute(
                "UPDATE address_conflicts SET detail = ? WHERE id = ?",
                (detail, int(row["id"])),
            )
        else:
            await self.db.execute(
                """
                INSERT INTO address_conflicts (
                    address, wallet_name, evidence_wallet_name, reason,
                    detail, status, created_at
                )
                VALUES (?, ?, ?, 'ownership_contradiction', ?, 'PENDING', ?)
                """,
                (address, user_wallet or "", evidence_wallet or "", detail, now),
            )
        await self.db.commit()

    async def upsert_ownership_custody_conflict(
        self,
        trace_id: int,
        txid: str,
        vout: int,
        address: str,
        wallet_name: str,
        evidence_source: str,
        boundary_id: int | None,
        service_name: str,
        detail: str,
    ) -> None:
        """Create (or refresh) a PENDING ownership-custody conflict.

        PENDING rows are never overwritten into a resolved state here; the
        resolution is exclusively produced by the review endpoint or by the
        walk when the underlying condition has cleared.
        """
        now = _now()
        await self.db.execute(
            """
            INSERT INTO ownership_custody_conflicts (
                trace_id, txid, vout, address, wallet_name, evidence_source,
                boundary_id, service_name, status, detail, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
            ON CONFLICT(trace_id, txid, vout) DO UPDATE SET
                address = excluded.address,
                wallet_name = excluded.wallet_name,
                evidence_source = excluded.evidence_source,
                boundary_id = excluded.boundary_id,
                service_name = excluded.service_name,
                detail = excluded.detail,
                status = 'PENDING',
                resolved_at = NULL
            """,
            (
                trace_id, txid, vout, address, wallet_name, evidence_source,
                boundary_id, service_name, detail, now,
            ),
        )
        await self.db.commit()

    async def get_ownership_custody_conflict_by_id(
        self, conflict_id: int
    ) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM ownership_custody_conflicts WHERE id = ?", (conflict_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_ownership_custody_conflict(
        self, trace_id: int, txid: str, vout: int
    ) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM ownership_custody_conflicts "
            "WHERE trace_id = ? AND txid = ? AND vout = ?",
            (trace_id, txid, vout),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_ownership_custody_conflicts(
        self, trace_id: int, status: str | None = None
    ) -> list[dict[str, Any]]:
        if status:
            cursor = await self.db.execute(
                "SELECT * FROM ownership_custody_conflicts "
                "WHERE trace_id = ? AND status = ? ORDER BY id",
                (trace_id, status),
            )
        else:
            cursor = await self.db.execute(
                "SELECT * FROM ownership_custody_conflicts "
                "WHERE trace_id = ? ORDER BY id",
                (trace_id,),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def resolve_ownership_custody_conflict(
        self, conflict_id: int, resolution: str
    ) -> bool:
        """Mark a conflict resolved. Reports only; never touches the boundary
        or the evidence (the endpoint performing the review owns that)."""
        cursor = await self.db.execute(
            "UPDATE ownership_custody_conflicts SET status = ?, resolved_at = ? "
            "WHERE id = ?",
            (resolution, _now(), conflict_id),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def add_address_evidence(
        self,
        address: str,
        wallet_name: str,
        source: str,
        derivation_path: str = "",
        brand: int | None = None,
        address_index: int | None = None,
        key_type: str = "",
        batch_id: int | None = None,
        first_seen_at: str | None = None,
    ) -> str:
        """Attach an evidence row to an address. Returns 'inserted'/'updated'.

        source is one of MANUAL_IMPORT / XPUB_DISCOVERY / USER_CONFIRMED.
        Used by the registry import and confirm workflows so that every owned
        address ends up with an auditable reason.
        """
        if source not in ("MANUAL_IMPORT", "XPUB_DISCOVERY", "USER_CONFIRMED"):
            raise ValueError(f"unknown evidence source: {source}")
        now = _now()
        await self.db.execute(
            """
            INSERT INTO address_evidence (
                address, wallet_name, source, derivation_path, branch_index,
                address_index, key_type, batch_id, first_seen_at, last_seen_at,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(address, wallet_name, source) DO UPDATE SET
                derivation_path = excluded.derivation_path,
                branch_index = excluded.branch_index,
                address_index = excluded.address_index,
                key_type = excluded.key_type,
                batch_id = excluded.batch_id,
                last_seen_at = excluded.last_seen_at
            """,
            (
                address, wallet_name, source, derivation_path, brand,
                address_index, key_type, batch_id, first_seen_at, now, now,
            ),
        )
        await self.db.commit()
        return "updated"

    async def get_address_evidence(self, address: str) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM address_evidence WHERE address = ? ORDER BY id",
            (address,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def list_address_evidence(
        self, wallet_name: str | None = None, source: str | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM address_evidence"
        clauses: list[str] = []
        values: list[Any] = []
        if wallet_name:
            clauses.append("wallet_name = ?")
            values.append(wallet_name)
        if source:
            clauses.append("source = ?")
            values.append(source)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT 2000"
        cursor = await self.db.execute(sql, values)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def delete_address_evidence(
        self, address: str, wallet_name: str, source: str
    ) -> bool:
        cursor = await self.db.execute(
            "DELETE FROM address_evidence WHERE address = ? AND wallet_name = ? AND source = ?",
            (address, wallet_name, source),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def count_address_evidence(self) -> int:
        cursor = await self.db.execute("SELECT COUNT(*) AS c FROM address_evidence")
        row = await cursor.fetchone()
        return int(row["c"]) if row else 0

    # ------------------------------------------------------------------
    # Trace edges
    # ------------------------------------------------------------------

    async def upsert_edge(self, edge: TraceEdge) -> None:
        await self.db.execute(
            """
            INSERT OR REPLACE INTO trace_edges (
                trace_id, source_txid, source_vout, spending_txid,
                destination_txid, destination_vout, hop_number, relationship_type
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                edge.trace_id,
                edge.source_txid,
                edge.source_vout,
                edge.spending_txid,
                edge.destination_txid,
                edge.destination_vout,
                edge.hop_number,
                edge.relationship_type,
            ),
        )
        await self.db.commit()

    async def get_edges(self, trace_id: int) -> list[TraceEdge]:
        cursor = await self.db.execute(
            "SELECT * FROM trace_edges WHERE trace_id = ? ORDER BY hop_number", (trace_id,)
        )
        rows = await cursor.fetchall()
        edges = []
        for r in rows:
            edges.append(
                TraceEdge(
                    trace_id=r["trace_id"],
                    source_txid=r["source_txid"],
                    source_vout=r["source_vout"],
                    spending_txid=r["spending_txid"],
                    destination_txid=r["destination_txid"],
                    destination_vout=r["destination_vout"],
                    hop_number=r["hop_number"],
                    relationship_type=r["relationship_type"],
                )
            )
        return edges

    async def get_outpoint_trace_index(self) -> dict[str, list[int]]:
        """Map ``"txid:vout"`` -> sorted trace ids that contain the outpoint.

        An outpoint "belongs" to a stored trace when it is that trace's start
        outpoint or appears as the source or destination of one of its edges.
        Two read-only queries, used to classify merge co-inputs as
        KNOWN_TRACE_INPUT and to build ``related_trace_ids``
        (MERGE_INPUT_CONTEXT_AND_GRAPH_RENDERER_FIX_REQUIREMENTS.md).
        """
        index: dict[str, set[int]] = {}
        cursor = await self.db.execute(
            "SELECT id, start_txid, start_vout FROM traces"
        )
        for row in await cursor.fetchall():
            if row["start_vout"] is not None:
                index.setdefault(
                    f"{row['start_txid']}:{row['start_vout']}", set()
                ).add(int(row["id"]))
        cursor = await self.db.execute(
            "SELECT trace_id, source_txid, source_vout, "
            "destination_txid, destination_vout FROM trace_edges"
        )
        for row in await cursor.fetchall():
            tid = int(row["trace_id"])
            index.setdefault(
                f"{row['source_txid']}:{row['source_vout']}", set()
            ).add(tid)
            index.setdefault(
                f"{row['destination_txid']}:{row['destination_vout']}", set()
            ).add(tid)
        return {key: sorted(ids) for key, ids in index.items()}

    async def delete_trace_edges(self, trace_id: int) -> None:
        await self.db.execute(
            "DELETE FROM trace_edges WHERE trace_id = ?", (trace_id,)
        )
        await self.db.commit()

    # ------------------------------------------------------------------
    # Annotations
    # ------------------------------------------------------------------

    async def add_annotation(
        self, entity_type: str, entity_id: str, tag: str, note: Optional[str]
    ) -> Annotation:
        # One current annotation per entity: an annotation is descriptive
        # metadata, and "Save annotation" edits the same row instead of
        # appending duplicates. Persisting the latest tag/note is what the
        # UI/API contract needs (a later GET must return the update).
        existing = await self.db.execute(
            "SELECT id FROM annotations WHERE entity_type = ? AND entity_id = ?",
            (entity_type, entity_id),
        )
        row = await existing.fetchone()
        created = _now()
        if row:
            annotation_id = int(row["id"])
            await self.db.execute(
                "UPDATE annotations SET tag = ?, note = ?, created_at = ?"
                " WHERE id = ?",
                (tag, note, created, annotation_id),
            )
        else:
            cursor = await self.db.execute(
                "INSERT INTO annotations (entity_type, entity_id, tag, note,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (entity_type, entity_id, tag, note, created),
            )
            annotation_id = int(cursor.lastrowid)
        await self.db.commit()
        return Annotation(
            id=annotation_id,
            entity_type=entity_type,
            entity_id=entity_id,
            tag=tag,
            note=note,
            created_at=created,
        )

    async def list_annotations_for_entity(
        self, entity_type: str, entity_id: str
    ) -> list[Annotation]:
        cursor = await self.db.execute(
            "SELECT * FROM annotations WHERE entity_type = ? AND entity_id = ?"
            " ORDER BY id",
            (entity_type, entity_id),
        )
        rows = await cursor.fetchall()
        return [
            Annotation(
                id=r["id"],
                entity_type=r["entity_type"],
                entity_id=r["entity_id"],
                tag=r["tag"],
                note=r["note"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    async def list_all_annotations(self) -> list[Annotation]:
        cursor = await self.db.execute(
            "SELECT * FROM annotations ORDER BY id"
        )
        rows = await cursor.fetchall()
        return [
            Annotation(
                id=r["id"],
                entity_type=r["entity_type"],
                entity_id=r["entity_id"],
                tag=r["tag"],
                note=r["note"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    async def set_setting(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        await self.db.commit()

    async def get_setting(self, key: str) -> str | None:
        cursor = await self.db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row["value"] if row else None

    async def get_all_settings(self) -> dict[str, str]:
        cursor = await self.db.execute("SELECT key, value FROM settings")
        rows = await cursor.fetchall()
        return {r["key"]: r["value"] for r in rows}

    # ------------------------------------------------------------------
    # Analysis jobs (BLOCKCHAIN_ANALYSIS_ASYNC_UX_PERFORMANCE_FIX_REQUIREMENTS.md)
    # ------------------------------------------------------------------

    async def create_analysis_job(
        self,
        trace_id: int,
        operation: str = "CREATE",
    ) -> dict[str, Any]:
        """Create a persisted background analysis job in QUEUED state."""
        now = _now()
        cursor = await self.db.execute(
            """
            INSERT INTO analysis_jobs (
                trace_id, operation, state, created_at, updated_at
            )
            VALUES (?, ?, 'QUEUED', ?, ?)
            """,
            (trace_id, operation, now, now),
        )
        await self.db.commit()
        job_id = int(cursor.lastrowid)
        cursor = await self.db.execute(
            "SELECT * FROM analysis_jobs WHERE id = ?", (job_id,)
        )
        job_row = await cursor.fetchone()
        return _analysis_job_from_row(job_row)

    async def get_analysis_job(self, job_id: int) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM analysis_jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        return _analysis_job_from_row(row) if row else None

    async def get_latest_analysis_job(self, trace_id: int) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM analysis_jobs WHERE trace_id = ? ORDER BY id DESC LIMIT 1",
            (trace_id,),
        )
        row = await cursor.fetchone()
        return _analysis_job_from_row(row) if row else None

    async def list_analysis_jobs_for_trace(self, trace_id: int) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM analysis_jobs WHERE trace_id = ? ORDER BY id DESC",
            (trace_id,),
        )
        rows = await cursor.fetchall()
        return [_analysis_job_from_row(r) for r in rows]

    async def has_active_analysis_job(self, trace_id: int) -> bool:
        """True when the trace currently has a QUEUED or RUNNING analysis job."""
        cursor = await self.db.execute(
            "SELECT id FROM analysis_jobs WHERE trace_id = ? AND state IN ('QUEUED', 'RUNNING') LIMIT 1",
            (trace_id,),
        )
        row = await cursor.fetchone()
        return row is not None

    async def update_analysis_job_state(
        self,
        job_id: int,
        state: str,
        error_summary: str | None = None,
    ) -> None:
        """Transition a job to RUNNING / COMPLETE / LIMIT_REACHED / FAILED.

        ``error_summary`` must be a sanitized, user-facing short message (never
        a stack trace). Uses safe, small writes per existing persistence
        granularity (no long-lived transactions during the walk itself).
        """
        now = _now()
        started_at: str | None = None
        completed_at: str | None = None
        if state == "RUNNING":
            started_at = now
        elif state in ("COMPLETE", "LIMIT_REACHED", "FAILED"):
            completed_at = now
        await self.db.execute(
            """
            UPDATE analysis_jobs
            SET state = ?, updated_at = ?,
                error_summary = CASE WHEN ? IS NULL THEN error_summary ELSE ? END,
                started_at = CASE WHEN ? IS NULL THEN started_at ELSE ? END,
                completed_at = CASE WHEN ? IS NULL THEN completed_at ELSE ? END
            WHERE id = ?
            """,
            (
                state, now,
                error_summary, error_summary,
                started_at, started_at,
                completed_at, completed_at,
                job_id,
            ),
        )
        await self.db.commit()

    async def set_analysis_job_unresolved_count(self, job_id: int, count: int) -> None:
        """Record the walk's distinct unresolved predecessors (P1-3c, SK-3)."""
        await self.db.execute(
            "UPDATE analysis_jobs SET unresolved_count = ? WHERE id = ?",
            (count, job_id),
        )
        await self.db.commit()

    async def get_latest_finished_unresolved_count(self, trace_id: int) -> int | None:
        """``unresolved_count`` of the trace's latest COMPLETE / LIMIT_REACHED job."""
        cursor = await self.db.execute(
            """
            SELECT unresolved_count FROM analysis_jobs
            WHERE trace_id = ? AND state IN ('COMPLETE', 'LIMIT_REACHED')
            ORDER BY id DESC LIMIT 1
            """,
            (trace_id,),
        )
        row = await cursor.fetchone()
        return row["unresolved_count"] if row else None

    async def update_analysis_job_progress(
        self,
        job_id: int,
        transactions: int,
        outpoints: int,
        hop: int,
    ) -> None:
        """Persist lightweight progress counters for a RUNNING job."""
        await self.db.execute(
            """
            UPDATE analysis_jobs
            SET progress_tx = ?, progress_outpoints = ?, progress_hop = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (transactions, outpoints, hop, _now(), job_id),
        )
        await self.db.commit()

    async def list_stale_analysis_jobs(self) -> list[dict[str, Any]]:
        """Jobs left QUEUED/RUNNING by a previous process (restart cleanup)."""
        cursor = await self.db.execute(
            "SELECT * FROM analysis_jobs WHERE state IN ('QUEUED', 'RUNNING')"
        )
        rows = await cursor.fetchall()
        return [_analysis_job_from_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Reverse-mode address discovery jobs
    # (REVERSE_ADDRESS_DISCOVERY_LIFECYCLE_FIX_REQUIREMENTS.md).
    #
    # Durable lifecycle for an address -> candidate-outputs scan. Owned by the
    # dedicated AddressScanManager (backend/discovery/address_scan.py); the
    # analysis_jobs / AnalysisWorker system is deliberately untouched.
    # ------------------------------------------------------------------

    async def create_address_discovery_job(self, address: str) -> dict[str, Any]:
        """Create a persisted address-discovery scan job in QUEUED state."""
        now = _now()
        cursor = await self.db.execute(
            """
            INSERT INTO address_discovery_jobs (
                address, state, created_at, updated_at
            )
            VALUES (?, 'QUEUED', ?, ?)
            """,
            (address, now, now),
        )
        await self.db.commit()
        job_id = int(cursor.lastrowid)
        return await self.get_address_discovery_job(job_id)

    async def get_address_discovery_job(self, job_id: int) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM address_discovery_jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        return _address_discovery_job_from_row(row) if row else None

    async def get_active_address_discovery_job(self) -> dict[str, Any] | None:
        """The currently QUEUED/RUNNING discovery job, if any.

        Used for duplicate-scan protection: while an active job exists the app
        must NOT issue another scanblocks start (Bitcoin Core allows exactly one
        background scan and would answer ``Scan already in progress``).
        """
        cursor = await self.db.execute(
            """
            SELECT * FROM address_discovery_jobs
            WHERE state IN ('QUEUED', 'RUNNING')
            ORDER BY id DESC LIMIT 1
            """
        )
        row = await cursor.fetchone()
        return _address_discovery_job_from_row(row) if row else None

    async def list_stale_address_discovery_jobs(self) -> list[dict[str, Any]]:
        """Discovery jobs left QUEUED/RUNNING by a previous process (restart)."""
        cursor = await self.db.execute(
            "SELECT * FROM address_discovery_jobs WHERE state IN ('QUEUED', 'RUNNING')"
        )
        rows = await cursor.fetchall()
        return [_address_discovery_job_from_row(r) for r in rows]

    async def update_address_discovery_job_state(
        self,
        job_id: int,
        state: str,
        error_summary: str | None = None,
        progress: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Transition a discovery job to RUNNING / COMPLETE / CANCELLED / FAILED.

        ``error_summary`` must be a sanitized, user-facing short message (never
        a stack trace). ``progress`` mirrors what Bitcoin Core's scanblocks
        status reported; ``result`` caches the resolved candidates on COMPLETE.
        """
        now = _now()
        started_at: str | None = None
        completed_at: str | None = None
        if state == "RUNNING":
            started_at = now
        elif state in ("COMPLETE", "CANCELLED", "FAILED"):
            completed_at = now
        await self.db.execute(
            """
            UPDATE address_discovery_jobs
            SET state = ?, updated_at = ?,
                error_summary = CASE WHEN ? IS NULL THEN error_summary ELSE ? END,
                started_at = CASE WHEN ? IS NULL THEN started_at ELSE ? END,
                completed_at = CASE WHEN ? IS NULL THEN completed_at ELSE ? END,
                progress_json = CASE WHEN ? IS NULL THEN progress_json ELSE ? END,
                result_json = CASE WHEN ? IS NULL THEN result_json ELSE ? END
            WHERE id = ?
            """,
            (
                state, now,
                error_summary, error_summary,
                started_at, started_at,
                completed_at, completed_at,
                None if progress is None else json.dumps(progress),
                None if progress is None else json.dumps(progress),
                None if result is None else json.dumps(result),
                None if result is None else json.dumps(result),
                job_id,
            ),
        )
        await self.db.commit()
        return await self.get_address_discovery_job(job_id)

    async def update_address_discovery_job_progress(
        self, job_id: int, progress: dict[str, Any]
    ) -> None:
        """Persist the latest Core-reported scan progress for a RUNNING job."""
        await self.db.execute(
            """
            UPDATE address_discovery_jobs
            SET progress_json = ?, updated_at = ?
            WHERE id = ?
            """,
            (json.dumps(progress), _now(), job_id),
        )
        await self.db.commit()

    # ------------------------------------------------------------------
    # Reverse traversal seeds (REVERSE_PROVENANCE_REQUIREMENTS_v0.6.0.md /
    # REVERSE_BLOCKCHAIN_ANALYSIS_REQUIREMENTS_v0.6.0.md)
    #
    # Deliberate reverse continuation anchors: each row is an outpoint the user
    # explicitly asked the reverse walk to treat as a traversal seed (in
    # addition to the trace target). Rewalks are seeded from {target} U {rows},
    # so continuation never loses the already-discovered graph and never
    # re-anchors destructively.
    # ------------------------------------------------------------------

    async def add_reverse_seed(self, trace_id: int, txid: str, vout: int) -> None:
        await self.db.execute(
            """
            INSERT OR IGNORE INTO trace_reverse_seeds (trace_id, txid, vout, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (trace_id, txid, vout, _now()),
        )
        await self.db.commit()

    async def list_reverse_seeds(self, trace_id: int) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT txid, vout FROM trace_reverse_seeds WHERE trace_id = ? ORDER BY id",
            (trace_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def delete_reverse_seeds(self, trace_id: int) -> None:
        await self.db.execute(
            "DELETE FROM trace_reverse_seeds WHERE trace_id = ?", (trace_id,)
        )
        await self.db.commit()

    # ------------------------------------------------------------------
    # Acquisitions (ACQUISITION_TRACKING_V1_REQUIREMENTS.md)
    #
    # User metadata anchored to an exact on-chain outpoint.
    # On-chain values are integer sats. The acquisitions
    # row is deliberately decoupled from the linked trace (no FK): trace
    # deletion never cascades here, and trace_id is best-effort.
    # ------------------------------------------------------------------

    @staticmethod
    def _acquisition_from_row(row: aiosqlite.Row) -> dict[str, Any]:
        result = dict(row)
        amount = format(Decimal(row["origin_amount_sats"]) / Decimal(100_000_000), "f")
        result["btc_amount"] = amount.rstrip("0").rstrip(".") if "." in amount else amount
        return result

    async def create_acquisition(self, **fields: Any) -> dict[str, Any]:
        """Insert one acquisition row (origin must already be exact)."""
        cursor = await self.db.execute(
            """
            INSERT INTO acquisitions (
                acquisition_date, source, reference, notes, origin_txid,
                origin_vout, origin_address, origin_amount_sats, origin_block_height,
                origin_block_time,
                trace_id, trace_status, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fields.get("acquisition_date"),
                fields.get("source"),
                fields.get("reference"),
                fields.get("notes"),
                fields.get("origin_txid"),
                fields.get("origin_vout"),
                fields.get("origin_address"),
                fields.get("origin_amount_sats"),
                fields.get("origin_block_height"),
                fields.get("origin_block_time"),
                fields.get("trace_id"),
                fields.get("trace_status"),
                fields.get("status", "RESOLVING"),
                _now(),
                _now(),
            ),
        )
        await self.db.commit()
        return await self.get_acquisition(cursor.lastrowid)

    async def get_acquisition(self, acq_id: int) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM acquisitions WHERE id = ?", (acq_id,)
        )
        row = await cursor.fetchone()
        return self._acquisition_from_row(row) if row else None

    async def get_acquisition_by_origin(self, txid: str, vout: int) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM acquisitions WHERE origin_txid = ? AND origin_vout = ?",
            (txid, vout),
        )
        row = await cursor.fetchone()
        return self._acquisition_from_row(row) if row else None

    async def list_acquisitions(self) -> list[dict[str, Any]]:
        # Unified ordering for manual + CSV-imported acquisitions: newest date
        # first, stable secondary id DESC so same-date rows keep a consistent,
        # import-order-independent order.
        cursor = await self.db.execute(
            "SELECT * FROM acquisitions ORDER BY acquisition_date DESC, id DESC"
        )
        rows = await cursor.fetchall()
        return [self._acquisition_from_row(r) for r in rows]

    async def update_acquisition(self, acq_id: int, **changes: Any) -> dict[str, Any] | None:
        setters: list[str] = []
        values: list[Any] = []
        field_map = {
            "acquisition_date": "acquisition_date",
            "source": "source",
            "reference": "reference",
            "notes": "notes",
            "origin_txid": "origin_txid",
            "origin_vout": "origin_vout",
            "origin_address": "origin_address",
            "origin_amount_sats": "origin_amount_sats",
            "origin_block_height": "origin_block_height",
            "origin_block_time": "origin_block_time",
            "trace_id": "trace_id",
            "trace_status": "trace_status",
            "status": "status",
        }
        for key, column in field_map.items():
            if key in changes:
                setters.append(f"{column} = ?")
                values.append(changes[key])
        if not setters:
            return await self.get_acquisition(acq_id)
        setters.append("updated_at = ?")
        values.append(_now())
        values.append(acq_id)
        await self.db.execute(
            f"UPDATE acquisitions SET {', '.join(setters)} WHERE id = ?", tuple(values)
        )
        await self.db.commit()
        return await self.get_acquisition(acq_id)

    async def delete_acquisition(self, acq_id: int) -> bool:
        """Remove one Acquisition and everything keyed to it.

        The single-Acquisition path deliberately follows EXACTLY the same
        dependency semantics as ``delete_all_acquisitions`` - they are the
        same operation at different scopes, and they share the canonical
        helpers so the two cannot drift apart:

        * the Exchange Exit / Disposal allocation links pointing at this
          Acquisition are removed, so no ``acquisition_id`` reference is left
          dangling. Neither table has an FK to ``acquisitions``, so this is what
          keeps the referential integrity intact;
        * the Acquisition-owned FORWARD_PROVENANCE trace and its trace-scoped
          state go with it (see ``_acquisition_owned_trace_ids``).

        Preserved, exactly as in the bulk reset: the Exchange Exit itself and
        its deposit layout, the Disposal itself and its other allocations, the
        independent analysis/reverse traces, the blockchain cache, global
        ownership, the Known Address Registry and its evidence, wallet
        discovery and settings.

        One isolated transaction (P1-9): any failure rolls back the trace
        removal, the link removals and the row itself together, and no other
        request can commit a partial state.
        """
        async with self.write_transaction() as tx:
            cursor = await tx.db.execute(
                "SELECT id, trace_id, acquisition_date, origin_txid, origin_vout"
                " FROM acquisitions WHERE id = ?",
                (acq_id,),
            )
            acq_row = await cursor.fetchone()
            if acq_row is None:
                return False

            # Scoped to THIS Acquisition, so a name-signature match can never
            # reach another surviving Acquisition's trace.
            trace_ids = await tx._acquisition_owned_trace_ids([acq_row])
            for trace_id in trace_ids:
                await tx._delete_trace_rows(trace_id)
            counts = await tx._delete_acquisition_rows([acq_id])
        return counts["deleted_acquisitions"] > 0

    # ------------------------------------------------------------------
    # Disposals (ACQUISITION_REVIEW_AND_DISPOSALS_V1, schema version 16)
    #
    # Economic disposal records + their Acquisition allocation links and
    # optional blockchain evidence. The disposed quantity is derived from the
    # allocation links at read time; there is deliberately no `sold` flag on
    # acquisitions and SPENT outputs never auto-create disposals. Deleting an
    # acquisition removes only its own allocation links (never the disposal or
    # evidence); deleting a disposal removes its links + evidence only.
    # ------------------------------------------------------------------

    @staticmethod
    def _disposal_from_row(row: aiosqlite.Row) -> dict[str, Any]:
        out = {
            "id": row["id"],
            "disposal_type": row["disposal_type"],
            "disposal_date": row["disposal_date"],
            "disposal_time": row["disposal_time"],
            "btc_amount": row["btc_amount"],
            "gross_proceeds_eur": row["gross_proceeds_eur"],
            "sale_price_eur_per_btc": row["sale_price_eur_per_btc"],
            "fee_percent": row["fee_percent"],
            "fees_eur": row["fees_eur"],
            "net_proceeds_eur": row["net_proceeds_eur"],
            "broker": row["broker"],
            "reference": row["reference"],
            "notes": row["notes"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        try:
            out["exchange_exit_id"] = row["exchange_exit_id"]
        except (KeyError, IndexError):
            out["exchange_exit_id"] = None
        return out

    async def get_disposal(self, disposal_id: int) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM disposals WHERE id = ?", (disposal_id,)
        )
        row = await cursor.fetchone()
        return self._disposal_from_row(row) if row else None

    async def list_disposals(self) -> list[dict[str, Any]]:
        # Newest sale first, stable secondary id DESC (mirrors acquisitions).
        cursor = await self.db.execute(
            "SELECT * FROM disposals ORDER BY disposal_date DESC, id DESC"
        )
        rows = await cursor.fetchall()
        return [self._disposal_from_row(r) for r in rows]

    async def create_disposal(self, *, commit: bool = True, **fields: Any) -> dict[str, Any]:
        now = _now()
        cursor = await self.db.execute(
            """
            INSERT INTO disposals (
                disposal_type, disposal_date, disposal_time, btc_amount,
                gross_proceeds_eur, sale_price_eur_per_btc, fee_percent, fees_eur,
                net_proceeds_eur, broker, reference, notes,
                exchange_exit_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fields.get("disposal_type", "SALE"),
                fields["disposal_date"],
                fields.get("disposal_time"),
                fields["btc_amount"],
                fields["gross_proceeds_eur"],
                fields.get("sale_price_eur_per_btc"),
                fields.get("fee_percent"),
                fields.get("fees_eur"),
                fields["net_proceeds_eur"],
                fields.get("broker"),
                fields.get("reference"),
                fields.get("notes"),
                fields.get("exchange_exit_id"),
                now,
                now,
            ),
        )
        if commit:
            await self.db.commit()
        return await self.get_disposal(int(cursor.lastrowid))

    async def update_disposal(
        self, disposal_id: int, *, commit: bool = True, **changes: Any
    ) -> dict[str, Any] | None:
        setters: list[str] = []
        values: list[Any] = []
        for column in (
            "disposal_type", "disposal_date", "disposal_time", "btc_amount",
            "gross_proceeds_eur", "sale_price_eur_per_btc", "fee_percent",
            "fees_eur", "net_proceeds_eur", "broker", "reference", "notes",
            "exchange_exit_id",
        ):
            if column in changes:
                setters.append(f"{column} = ?")
                values.append(changes[column])
        if not setters:
            return await self.get_disposal(disposal_id)
        setters.append("updated_at = ?")
        values.append(_now())
        values.append(disposal_id)
        await self.db.execute(
            f"UPDATE disposals SET {', '.join(setters)} WHERE id = ?", tuple(values)
        )
        if commit:
            await self.db.commit()
        return await self.get_disposal(disposal_id)

    async def replace_disposal_links(
        self, disposal_id: int, allocations: list[Any], *, commit: bool = True
    ) -> None:
        """Replace the disposal's acquisition allocation links (delete + insert).

        allocations entries may be (a) a 2-tuple (acquisition_id, allocated_sats)
        for legacy allocations, (b) a 4-tuple (acquisition_id, allocated_sats,
        disposed_sats, network_fee_sats), or (c) a dict with those keys. Called
        inside the create/update service flow together with the disposal row for
        atomicity.
        """
        await self.db.execute(
            "DELETE FROM disposal_acquisition_links WHERE disposal_id = ?",
            (disposal_id,),
        )
        for alloc in allocations:
            if isinstance(alloc, dict):
                acquisition_id = alloc["acquisition_id"]
                allocated_sats = alloc["allocated_sats"]
                disposed_sats = alloc.get("disposed_sats")
                network_fee_sats = alloc.get("network_fee_sats")
            else:
                acquisition_id, allocated_sats = alloc[0], alloc[1]
                disposed_sats = alloc[2] if len(alloc) > 2 else None
                network_fee_sats = alloc[3] if len(alloc) > 3 else None
            await self.db.execute(
                "INSERT INTO disposal_acquisition_links "
                " (disposal_id, acquisition_id, allocated_sats, disposed_sats,"
                "  network_fee_sats) VALUES (?, ?, ?, ?, ?)",
                (disposal_id, acquisition_id, allocated_sats, disposed_sats, network_fee_sats),
            )
        if commit:
            await self.db.commit()

    async def get_disposal_allocations(self, disposal_id: int) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT acquisition_id, allocated_sats, disposed_sats, network_fee_sats "
            "FROM disposal_acquisition_links "
            "WHERE disposal_id = ? ORDER BY acquisition_id",
            (disposal_id,),
        )
        rows = await cursor.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            disposed = r["disposed_sats"]
            out.append({
                "acquisition_id": r["acquisition_id"],
                "allocated_sats": r["allocated_sats"],
                "disposed_sats": disposed if disposed is not None else r["allocated_sats"],
                "network_fee_sats": r["network_fee_sats"] or 0,
            })
        return out

    async def get_acquisition_disposals(self, acq_id: int) -> list[dict[str, Any]]:
        """Disposals linked to one acquisition with their allocated_sats."""
        cursor = await self.db.execute(
            """
            SELECT d.*, l.allocated_sats, l.disposed_sats, l.network_fee_sats
            FROM disposal_acquisition_links l
            JOIN disposals d ON d.id = l.disposal_id
            WHERE l.acquisition_id = ?
            ORDER BY d.disposal_date DESC, d.id DESC
            """,
            (acq_id,),
        )
        rows = await cursor.fetchall()
        result: list[dict[str, Any]] = []
        for r in rows:
            item = self._disposal_from_row(r)
            item["allocated_sats"] = r["allocated_sats"]
            disposed = r["disposed_sats"]
            item["disposed_sats"] = disposed if disposed is not None else r["allocated_sats"]
            item["network_fee_sats"] = r["network_fee_sats"] or 0
            result.append(item)
        return result

    async def get_acquisition_exchange_exits(self, acq_id: int) -> list[dict[str, Any]]:
        """Exchange Exits linked to one acquisition, with per-exit totals.

        unattributed_inputs counts FX-2 rows (no amount): exit inputs this
        Acquisition is an anchor of but which are attributed to none."""
        cursor = await self.db.execute(
            """
            SELECT e.id, e.txid, e.exit_date, e.exit_time, e.broker, e.reference,
                   e.notes, e.block_height, e.block_time, e.input_total_sats,
                   e.output_total_sats, e.fee_sats, e.verified, e.disposal_id,
                   e.created_at, e.updated_at,
                   COALESCE(SUM(s.disposed_sats), 0) AS disposed_sats,
                   COALESCE(SUM(s.network_fee_sats), 0) AS network_fee_sats,
                   COALESCE(SUM(s.disposed_sats + s.network_fee_sats), 0) AS allocated_sats,
                   SUM(s.disposed_sats + s.network_fee_sats = 0) AS unattributed_inputs
            FROM exchange_exit_sources s
            JOIN exchange_exits e ON e.id = s.exchange_exit_id
            WHERE s.acquisition_id = ?
            GROUP BY e.id
            ORDER BY e.exit_date DESC, e.id DESC
            """,
            (acq_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_allocation_survey(self) -> dict[int, int]:
        """acquisition_id -> total allocated sats across Disposals AND Exchange Exits.

        Exchange Exit source rows allocate disposed_sats + network_fee_sats per
        acquisition (the ownership boundary) without requiring an economic
        Disposal. Lightweight single-query aggregate used by the position-counts
        / filter paths (no per-row graph work; only persisted links are read)."""
        cursor = await self.db.execute(
            "SELECT acquisition_id, COALESCE(SUM(allocated_sats), 0) AS total FROM ("
            "  SELECT acquisition_id, allocated_sats FROM disposal_acquisition_links"
            "  UNION ALL"
            "  SELECT acquisition_id, disposed_sats + network_fee_sats AS allocated_sats"
            "  FROM exchange_exit_sources"
            ") GROUP BY acquisition_id"
        )
        rows = await cursor.fetchall()
        return {int(r["acquisition_id"]): int(r["total"] or 0) for r in rows}

    async def get_acquisition_allocated_sats(self, acq_id: int) -> int:
        """Total allocated sats for one acquisition (Disposals + Exchange Exits)."""
        cursor = await self.db.execute(
            "SELECT COALESCE(SUM(allocated_sats), 0) AS total FROM ("
            "  SELECT allocated_sats FROM disposal_acquisition_links WHERE acquisition_id = ?"
            "  UNION ALL"
            "  SELECT disposed_sats + network_fee_sats AS allocated_sats"
            "  FROM exchange_exit_sources WHERE acquisition_id = ?"
            ")",
            (acq_id, acq_id),
        )
        row = await cursor.fetchone()
        return int(row["total"] if row else 0)

    async def replace_disposal_evidence(
        self, disposal_id: int, evidences: list[dict[str, Any]], *, commit: bool = True
    ) -> None:
        """Replace the disposal's blockchain-evidence rows (delete + insert)."""
        await self.db.execute(
            "DELETE FROM disposal_blockchain_evidence WHERE disposal_id = ?",
            (disposal_id,),
        )
        for ev in evidences:
            await self.db.execute(
                "INSERT INTO disposal_blockchain_evidence ("
                " disposal_id, source_txid, source_vout, spending_txid,"
                " destination_vout, destination_address, amount_sats, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    disposal_id,
                    ev.get("source_txid"),
                    ev.get("source_vout"),
                    ev.get("spending_txid"),
                    ev.get("destination_vout"),
                    ev.get("destination_address"),
                    ev.get("amount_sats"),
                    _now(),
                ),
            )
        if commit:
            await self.db.commit()

    async def get_disposal_evidence(self, disposal_id: int) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM disposal_blockchain_evidence WHERE disposal_id = ? "
            "ORDER BY id",
            (disposal_id,),
        )
        rows = await cursor.fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append({
                "id": r["id"],
                "disposal_id": r["disposal_id"],
                "source_txid": r["source_txid"],
                "source_vout": r["source_vout"],
                "spending_txid": r["spending_txid"],
                "destination_vout": r["destination_vout"],
                "destination_address": r["destination_address"],
                "amount_sats": r["amount_sats"],
                "created_at": r["created_at"],
            })
        return out

    async def delete_disposal(self, disposal_id: int) -> bool:
        """Remove a disposal + its allocation links + blockchain evidence. The
        linked Acquisitions and their Traces are NOT touched (SPENT is never
        disposal and deleting an economic record never deletes holdings)."""
        await self.db.execute(
            "DELETE FROM disposal_acquisition_links WHERE disposal_id = ?",
            (disposal_id,),
        )
        await self.db.execute(
            "DELETE FROM disposal_blockchain_evidence WHERE disposal_id = ?",
            (disposal_id,),
        )
        cursor = await self.db.execute("DELETE FROM disposals WHERE id = ?", (disposal_id,))
        await self.db.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Exchange Exit (EXCHANGE_EXIT_REQUIREMENTS.md v0.11.0). A user-confirmed
    # blockchain boundary where BTC leaves self-custody to an exchange. The
    # on-chain fact (sources + outputs) is owned here; the economic Disposal
    # record (created by the service) links back via disposals.exchange_exit_id.
    # ------------------------------------------------------------------

    async def create_exchange_exit(self, *, commit: bool = True, **fields: Any) -> dict[str, Any]:
        now = _now()
        cursor = await self.db.execute(
            """
            INSERT INTO exchange_exits (
                txid, exit_date, exit_time, broker, reference, notes,
                block_height, block_time, input_total_sats, output_total_sats,
                fee_sats, disposal_id, verified, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fields["txid"],
                fields["exit_date"],
                fields.get("exit_time"),
                fields.get("broker"),
                fields.get("reference"),
                fields.get("notes"),
                fields.get("block_height"),
                fields.get("block_time"),
                fields.get("input_total_sats"),
                fields.get("output_total_sats"),
                fields.get("fee_sats"),
                fields.get("disposal_id"),
                1 if fields.get("verified") else 0,
                now,
                now,
            ),
        )
        if commit:
            await self.db.commit()
        return await self.get_exchange_exit(int(cursor.lastrowid))

    async def get_exchange_exit(self, exit_id: int) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM exchange_exits WHERE id = ?", (exit_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_exchange_exit_by_txid(self, txid: str) -> dict[str, Any] | None:
        cursor = await self.db.execute(
            "SELECT * FROM exchange_exits WHERE txid = ? ORDER BY id DESC LIMIT 1",
            (txid,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_other_exchange_exit_by_txid(
        self, txid: str, exclude_exit_id: int | None = None
    ) -> dict[str, Any] | None:
        """Lowest-id Exchange Exit with this exit TXID, ignoring ``exclude_exit_id``
        (P2-5b). Used by the duplicate check on create/update."""
        cursor = await self.db.execute(
            "SELECT * FROM exchange_exits WHERE txid = ? AND id != ? "
            "ORDER BY id ASC LIMIT 1",
            (txid, exclude_exit_id if exclude_exit_id is not None else -1),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_exit_allocated_sats(self, acq_id: int) -> int:
        """Sum of Exchange Exit source allocations (disposed + network fee) of
        one Acquisition over all Exits (P2-5b).
        Disposals are deliberately not counted (Exchange Exit != Disposal)."""
        cursor = await self.db.execute(
            "SELECT COALESCE(SUM(disposed_sats + network_fee_sats), 0) AS total "
            "FROM exchange_exit_sources WHERE acquisition_id = ?",
            (acq_id,),
        )
        row = await cursor.fetchone()
        return int(row["total"] or 0) if row else 0

    async def list_exchange_exits(self) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM exchange_exits ORDER BY exit_date DESC, id DESC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def update_exchange_exit(
        self, exit_id: int, *, commit: bool = True, **changes: Any
    ) -> dict[str, Any] | None:
        setters: list[str] = []
        values: list[Any] = []
        for column in (
            "txid", "exit_date", "exit_time", "broker", "reference", "notes",
            "block_height", "block_time", "input_total_sats", "output_total_sats",
            "fee_sats", "disposal_id", "verified",
        ):
            if column in changes:
                setters.append(f"{column} = ?")
                values.append(changes[column])
        if not setters:
            return await self.get_exchange_exit(exit_id)
        setters.append("updated_at = ?")
        values.append(_now())
        values.append(exit_id)
        await self.db.execute(
            f"UPDATE exchange_exits SET {', '.join(setters)} WHERE id = ?", tuple(values)
        )
        if commit:
            await self.db.commit()
        return await self.get_exchange_exit(exit_id)

    async def replace_exchange_exit_sources(
        self, exit_id: int, sources: list[dict[str, Any]], *, commit: bool = True
    ) -> None:
        """Replace the exit's source outpoint rows (delete + insert)."""
        await self.db.execute(
            "DELETE FROM exchange_exit_sources WHERE exchange_exit_id = ?", (exit_id,)
        )
        for s in sources:
            await self.db.execute(
                "INSERT INTO exchange_exit_sources ("
                " exchange_exit_id, acquisition_id, source_txid, source_vout,"
                " source_amount_sats, disposed_sats, network_fee_sats,"
                " ownership_status, ownership_confirmed, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    exit_id,
                    s["acquisition_id"],
                    s["source_txid"],
                    s["source_vout"],
                    s["source_amount_sats"],
                    s["disposed_sats"],
                    s["network_fee_sats"],
                    s.get("ownership_status") or "UNKNOWN",
                    1 if s.get("ownership_confirmed") else 0,
                    _now(),
                ),
            )
        if commit:
            await self.db.commit()

    async def get_exchange_exit_sources(self, exit_id: int) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM exchange_exit_sources WHERE exchange_exit_id = ? "
            "ORDER BY acquisition_id, source_txid, source_vout",
            (exit_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_exchange_exit_sources_for_acquisition(
        self, acq_id: int
    ) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM exchange_exit_sources WHERE acquisition_id = ? "
            "ORDER BY id",
            (acq_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def replace_exchange_exit_outputs(
        self, exit_id: int, outputs: list[dict[str, Any]], *, commit: bool = True
    ) -> None:
        """Replace the exit's output layout rows (delete + insert)."""
        await self.db.execute(
            "DELETE FROM exchange_exit_outputs WHERE exchange_exit_id = ?", (exit_id,)
        )
        for o in outputs:
            await self.db.execute(
                "INSERT INTO exchange_exit_outputs ("
                " exchange_exit_id, vout, address, amount_sats,"
                " ownership_status, is_exchange_deposit, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    exit_id,
                    o["vout"],
                    o.get("address"),
                    o.get("amount_sats"),
                    o.get("ownership_status", "UNKNOWN"),
                    1 if o.get("is_exchange_deposit") else 0,
                    _now(),
                ),
            )
        if commit:
            await self.db.commit()

    async def get_exchange_exit_outputs(self, exit_id: int) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            "SELECT * FROM exchange_exit_outputs WHERE exchange_exit_id = ? ORDER BY vout",
            (exit_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def delete_exchange_exit(self, exit_id: int) -> bool:
        """Delete an Exchange Exit and ONLY the rows the Exit owns.

        Scope is deliberately, structurally narrow - this method is the single
        canonical DML for Exchange Exit deletion and it must never be widened
        into the Acquisition cleanup path (``_delete_acquisition_rows`` /
        ``delete_acquisition`` / ``delete_all_acquisitions``). Removing an Exit
        classification is not removing an Acquisition, an Acquisition-owned
        trace, an origin outpoint or Acquisition provenance metadata.

        Removed (Exit-owned):

        * ``exchange_exit_sources`` - the per-source allocation of this Exit;
        * ``exchange_exit_outputs`` - the deposit layout of this Exit;
        * the ``exchange_exits`` row itself;
        * the allocation links and blockchain evidence of a Disposal that is
          this Exit's OWN economic record (``disposals.exchange_exit_id``,
          the link direction the schema defines). Those links are the Exit's
          per-source allocation mirrored into the Disposal domain, and they
          feed the position-state derivation through ``get_allocation_survey``.
          Leaving them behind would keep every Acquisition allocated through
          this Exit pinned to DISPOSED / PARTIALLY_DISPOSED after the Exit
          classification is gone, so the state could never be re-derived from
          the surviving Acquisition trace. The Disposal's own economic row is
          PRESERVED (Exchange Exit != Disposal, and an Exit never destroys an
          economic record).

        The Acquisitions, their FORWARD_PROVENANCE traces, the origin outpoints,
        the blockchain cache, the global ownership rows, the Known Address
        Registry, unrelated Disposals and unrelated Exchange Exits are all left
        untouched. Position state is derived per read and never stored, so an
        Acquisition re-derives on the next read without any stored-state reset.

        A legacy ``exchange_exits.disposal_id`` link (schema v17/v18, never
        written by the current create path) is treated exactly like
        ``disposals.exchange_exit_id`` (SK-1): the linked Disposal's allocation
        links and blockchain evidence are released, its economic row is
        preserved. A dangling link is ignored.

        One isolated transaction (P1-9): an exception before the final commit
        leaves the Exit, its sources, its outputs and the linked allocations
        all in place, and no other request can commit a partial state.
        """
        async with self.write_transaction() as tx:
            return await tx._delete_exchange_exit_rows(exit_id)

    async def _delete_exchange_exit_rows(self, exit_id: int) -> bool:
        """DML of ``delete_exchange_exit``. Commits nothing."""
        await self.db.execute(
            "DELETE FROM exchange_exit_sources WHERE exchange_exit_id = ?",
            (exit_id,),
        )
        await self.db.execute(
            "DELETE FROM exchange_exit_outputs WHERE exchange_exit_id = ?",
            (exit_id,),
        )
        # Exit-owned allocation/evidence mirrored into the Disposal domain.
        # Only Disposals linked to THIS exit are touched - through
        # disposals.exchange_exit_id or the legacy exchange_exits.disposal_id
        # (SK-1) - so an unrelated sale of the same acquisition keeps its
        # allocation. The Disposal rows themselves are never deleted here.
        linked_disposals = (
            "SELECT id FROM disposals WHERE exchange_exit_id = ?"
            " UNION SELECT disposal_id FROM exchange_exits"
            " WHERE id = ? AND disposal_id IS NOT NULL"
        )
        await self.db.execute(
            f"DELETE FROM disposal_acquisition_links WHERE disposal_id IN ({linked_disposals})",
            (exit_id, exit_id),
        )
        await self.db.execute(
            f"DELETE FROM disposal_blockchain_evidence WHERE disposal_id IN ({linked_disposals})",
            (exit_id, exit_id),
        )
        cursor = await self.db.execute(
            "DELETE FROM exchange_exits WHERE id = ?", (exit_id,)
        )
        return cursor.rowcount > 0
