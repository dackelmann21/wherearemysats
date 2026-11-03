"""Core UTXO lineage tracing engine.

Campo core principle (REQ 4): the engine tracks UTXOs / outpoints
(TXID:VOUT), never just addresses.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from typing import Any, Optional

from backend.models.schemas import (
    ClassificationSource,
    InputInfo,
    OutputInfo,
    OwnershipStatus,
    RelationshipType,
    ScriptType,
    SpendStatus,
    Trace,
    TraceEdge,
    TraceStatus,
    TransactionInfo,
)
from backend.rpc.bitcoin import (
    BitcoinRPCClient,
    BlockNotSyncedError,
    NodeUnreachableError,
    RpcError,
    RpcTimeoutError,
    sats,
)
from backend.persistence.database import Database
from backend.tracing.errors import TraceNotFoundError
from backend.tracing.provenance import (
    batch_signals,
    provenance_for_output,
    tx_topology,
)

logger = logging.getLogger("lineage.tracing")


class TraceEngine:
    def __init__(self, db: Database, rpc: BitcoinRPCClient, max_depth: int = 100):
        self.db = db
        self.rpc = rpc
        self.max_depth = max_depth
        self._mem_tx_cache: dict[str, TransactionInfo] = {}
        # Unconfirmed cached transactions whose last node re-check failed.
        self._recheck_failed: set[str] = set()
        # Boundary outpoints for the trace currently being walked
        # (CUSTODIAL_DEPOSIT). Exact lineage stops at these points.
        self._walk_boundaries: set[tuple[str, int]] = set()

    # ------------------------------------------------------------------
    # Transaction resolution
    # ------------------------------------------------------------------

    async def load_transaction(
        self, txid: str, *, persist_ownership: bool = True
    ) -> TransactionInfo:
        """Load a transaction, preferring the local SQLite cache.

        Returns a fully resolved TransactionInfo with inputs, outputs,
        totals and fee.

        ``persist_ownership=False`` is for ownership-neutral callers
        (Blockchain Analysis walkers, plain GET reads): the effective
        ownership is still derived in memory, but no stale-EXTERNAL
        reconciliation is written and no ownership-contradiction row is
        recorded. Blockchain facts (transaction / outpoint cache) are still
        cached. The default keeps the Forward Provenance behaviour.
        """
        if txid in self._mem_tx_cache:
            info = self._mem_tx_cache[txid]
            # The cache only protects the immutable transaction payload.
            # Ownership, spend status, provenance and annotations are mutable
            # user-managed state, so they must be re-derived from the DB on
            # every return - otherwise a saved classification or annotation
            # would only show up after a trace re-walk cleared the cache.
            await self._enrich_outputs(info, persist_ownership=persist_ownership)
            return info

        cached = await self.db.get_transaction(txid)
        if cached:
            # We can rebuild TransactionInfo from stored raw_json without
            # further RPC calls for immutable data.
            info = await self._from_cache(
                txid, cached, persist_ownership=persist_ownership
            )
            if self._memory_cacheable(info, info.raw or cached.get("raw_json")):
                self._mem_tx_cache[txid] = info
            return info

        info = await self._load_transaction_from_rpc(
            txid, persist_ownership=persist_ownership
        )
        await self._store_transaction(info)
        if self._memory_cacheable(info, info.raw):
            self._mem_tx_cache[txid] = info
        return info

    def unconfirmed_recheck_failed(self, txid: str) -> bool:
        """The last node re-check of this unconfirmed cached transaction failed
        (node unavailable); its cached entry was served instead."""
        return txid in self._recheck_failed

    @staticmethod
    def _memory_cacheable(info: TransactionInfo, raw: Any) -> bool:
        """P2-N8: only a confirmed transaction (raw ``blockhash``) with a
        known block height is held in ``_mem_tx_cache``. Confirmed without
        height: the next load goes through the SQLite cache path and retries
        the header (R3-10). Unconfirmed (owner decision 2026-10-29): the next
        load re-checks the node for the confirmation, so a long-lived engine
        sees it too."""
        confirmed = bool(isinstance(raw, dict) and raw.get("blockhash"))
        return confirmed and info.block_height is not None

    async def _apply_annotations(self, tx: TransactionInfo) -> None:
        """Refresh each output's user annotation from the current DB state.

        Runs as part of ``_enrich_outputs`` (see ``load_transaction``) because
        annotations are editable, unlike the immutable transaction payload.
        """
        for out in tx.outputs:
            annotations = await self.db.list_annotations_for_entity(
                "outpoint", out.outpoint
            )
            if annotations:
                out.user_annotation = annotations[-1].note or annotations[-1].tag
            else:
                out.user_annotation = None

    async def _refresh_unconfirmed(
        self, txid: str, cached: dict[str, Any], *, persist_ownership: bool = True
    ) -> Optional[TransactionInfo]:
        """P2-N8 (owner decision 2026-10-29): a transaction cached while
        unconfirmed (no ``blockhash`` in raw) is re-checked against the node
        on every load. Once the node reports it confirmed, the RPC load path
        rebuilds and persists it (``_store_transaction`` as in P2-6:
        blockchain facts only; ownership, spend status, provenance and
        annotations stay). Returns ``None`` when the cached entry is to be
        served as before: still unconfirmed, or the node is unavailable
        (same outage types as R3-06, all ``RpcError``) - node errors are
        logged, never re-raised."""
        raw = cached.get("raw_json")
        if not isinstance(raw, dict) or raw.get("blockhash"):
            return None
        try:
            fresh = await self.rpc.get_raw_transaction(txid, verbose=True)
        except RpcError as exc:
            logger.warning(
                "could not re-check unconfirmed cached transaction %s: %s", txid, exc
            )
            self._recheck_failed.add(txid)
            return None
        self._recheck_failed.discard(txid)
        if not isinstance(fresh, dict) or not fresh.get("blockhash"):
            return None
        info = await self._load_transaction_from_rpc(
            txid, persist_ownership=persist_ownership, raw=fresh
        )
        await self._store_transaction(info)
        return info

    async def _from_cache(
        self, txid: str, cached: dict[str, Any], *, persist_ownership: bool = True
    ) -> TransactionInfo:
        refreshed = await self._refresh_unconfirmed(
            txid, cached, persist_ownership=persist_ownership
        )
        if refreshed is not None:
            return refreshed
        tx = TransactionInfo(
            txid=txid,
            block_hash=cached.get("block_hash"),
            block_height=cached.get("block_height"),
            block_time=cached.get("block_time"),
            confirmations=cached.get("confirmations") or 0,
            input_total_sats=cached.get("input_total_sats"),
            output_total_sats=cached.get("output_total_sats"),
            fee_sats=cached.get("fee_sats"),
            vsize=cached.get("vsize"),
        )
        if tx.fee_sats and tx.vsize:
            tx.fee_rate = round(tx.fee_sats / tx.vsize, 2)

        raw = cached.get("raw_json")
        # R3-10: a confirmed tx (raw ``blockhash``) cached without height
        # means the header lookup failed on first load - retry it once here.
        retry_header = bool(raw and raw.get("blockhash")) and tx.block_height is None
        if retry_header:
            try:
                header = await self.rpc.get_block_header(raw["blockhash"], verbose=True)
                tx.block_hash = raw["blockhash"]
                tx.block_height = header.get("height")
                tx.block_time = header.get("time")
                tx.confirmations = raw.get("confirmations") or 0
            except RpcError as exc:
                # Same as the RPC load path: metadata only, keep serving the cache.
                logger.warning("failed to resolve block header for %s: %s", txid, exc)
                retry_header = False
        if raw:
            await self._populate_io_from_raw(
                tx, raw, persist_ownership=persist_ownership
            )
            # A row cached by the wallet inventory carries no input total or
            # fee. Derive them as the RPC path does and complete the row.
            if tx.input_total_sats is None or tx.fee_sats is None:
                self._compute_fee(tx)
                if tx.input_total_sats is not None or tx.fee_sats is not None:
                    await self.db.fill_transaction_totals(
                        txid, tx.input_total_sats, tx.output_total_sats, tx.fee_sats
                    )
            if retry_header:
                tx.raw = raw
                await self._store_transaction(tx)
                tx.raw = None
        else:
            # No cached raw data; re-fetch from RPC.
            info = await self._load_transaction_from_rpc(
                txid, persist_ownership=persist_ownership
            )
            await self._store_transaction(info)
            return info
        self._apply_transaction_meta(tx)
        return tx

    async def _load_transaction_from_rpc(
        self,
        txid: str,
        *,
        persist_ownership: bool = True,
        raw: Optional[dict[str, Any]] = None,
    ) -> TransactionInfo:
        if raw is None:
            raw = await self.rpc.get_raw_transaction(txid, verbose=True)
        if raw is None or not isinstance(raw, dict):
            raise RpcError("Transaction could not be found on the configured Bitcoin node.")

        tx = TransactionInfo(txid=txid)

        block_hash = raw.get("blockhash")
        if block_hash:
            try:
                header = await self.rpc.get_block_header(block_hash, verbose=True)
                tx.block_hash = block_hash
                tx.block_height = header.get("height")
                tx.block_time = header.get("time")
                tx.confirmations = raw.get("confirmations") or 0
            except RpcError as exc:
                logger.warning("failed to resolve block header for %s: %s", txid, exc)

        tx.vsize = raw.get("vsize")
        if tx.vsize is None:
            sz = raw.get("size")
            if sz:
                tx.vsize = sz

        await self._populate_io_from_raw(tx, raw, persist_ownership=persist_ownership)
        self._compute_fee(tx)
        self._apply_transaction_meta(tx)

        # raw cache payload
        tx.raw = raw
        return tx

    @staticmethod
    def _apply_transaction_meta(tx: TransactionInfo) -> None:
        """Set the whole-transaction topology + batch heuristics (pure facts)."""
        tx.transaction_topology = tx_topology(tx)
        tx.likely_batch, _ = batch_signals(tx)

    async def _populate_io_from_raw(
        self, tx: TransactionInfo, raw: dict[str, Any], *, persist_ownership: bool = True
    ) -> None:
        # ---- resolve inputs ----
        vin_list = raw.get("vin") or []
        # Pre-fetch previous transactions for input amounts/addresses.
        prev_txs: dict[str, dict[str, Any] | None] = {}
        for vin in vin_list:
            if "coinbase" in vin:
                continue
            pt = vin.get("txid")
            if pt and pt not in prev_txs:
                prev_txs[pt] = None

        # Use DB cache first for the previous transactions.
        for pt in list(prev_txs.keys()):
            prev_cached = await self.db.get_transaction(pt)
            if prev_cached and prev_cached.get("raw_json"):
                prev_txs[pt] = prev_cached.get("raw_json")

        missing = [pt for pt, val in prev_txs.items() if val is None]
        if missing:
            results = await self.rpc.get_raw_transactions_batch(missing)
            for pt in missing:
                prev_txs[pt] = results.get(pt)

        for vin in vin_list:
            if "coinbase" in vin:
                prev_raw = None
                prev_value = None
                prev_address = None
                prev_script = None
                input_info = InputInfo(
                    txid="",
                    vout=-1,
                    prev_outpoint="coinbase",
                    sequence=vin.get("sequence"),
                    value=prev_value,
                    value_sats=None,
                    script_pubkey=None,
                    script_type=None,
                    address=None,
                )
                tx.inputs.append(input_info)
                tx.unknown_input_count += 1
                continue

            ptxid = vin.get("txid")
            pvout = vin.get("vout")
            prev_outpoint_str = f"{ptxid}:{pvout}"
            prev_raw = prev_txs.get(ptxid)
            prev_value: float | None = None
            prev_address: Optional[str] = None
            prev_script: Optional[str] = None
            prev_script_type: Optional[str] = None

            if prev_raw and isinstance(prev_raw, dict):
                pouts = prev_raw.get("vout") or []
                for po in pouts:
                    if int(po.get("n", -1)) == pvout:
                        prev_value = po.get("value")
                        spk = po.get("scriptPubKey") or {}
                        prev_script = spk.get("hex")
                        prev_script_type = spk.get("type")
                        prev_address = self._address_from_script_pubkey(spk)
                        # cache prevout outpoint data
                        await self.db.upsert_outpoint(
                            {
                                "txid": ptxid,
                                "vout": pvout,
                                "address": prev_address,
                                "amount_sats": sats(prev_value),
                                "script_pubkey": prev_script,
                                "script_type": prev_script_type,
                                "block_height": None,
                                "block_time": None,
                                "spend_status": "SPENT",
                            }
                        )
                        break

            input_info = InputInfo(
                txid=ptxid,
                vout=pvout,
                prev_outpoint=prev_outpoint_str,
                sequence=vin.get("sequence"),
                value=prev_value,
                value_sats=sats(prev_value),
                script_pubkey=prev_script,
                script_type=prev_script_type,
                address=prev_address,
            )
            tx.inputs.append(input_info)
            if prev_value is None:
                tx.unknown_input_count += 1

        # ---- resolve outputs ----
        vout_list = raw.get("vout") or []
        for vo in vout_list:
            n = int(vo.get("n", 0))
            spk = vo.get("scriptPubKey") or {}
            address = self._address_from_script_pubkey(spk)
            script_type = spk.get("type") or ScriptType.from_asm(spk.get("asm"))
            value_sats = sats(vo.get("value"))
            out = OutputInfo(
                txid=tx.txid,
                vout=n,
                outpoint=f"{tx.txid}:{n}",
                value=vo.get("value"),
                value_sats=value_sats,
                script_pubkey=spk.get("hex"),
                script_type=script_type,
                address=address,
                block_height=tx.block_height,
                block_time=tx.block_time,
            )
            tx.outputs.append(out)

        # enrich outputs with ownership + spend info from DB
        await self._enrich_outputs(tx, persist_ownership=persist_ownership)

    def _compute_fee(self, tx: TransactionInfo) -> None:
        outs = [o.value_sats for o in tx.outputs if o.value_sats is not None]
        ins = [i.value_sats for i in tx.inputs if i.value_sats is not None]
        if not outs:
            return
        tx.output_total_sats = sum(outs)
        if len(ins) == len(tx.inputs) and ins and not (
            any(i.txid == "" for i in tx.inputs)
        ):
            tx.input_total_sats = sum(ins)
            if tx.input_total_sats is not None and tx.output_total_sats is not None:
                tx.fee_sats = max(0, tx.input_total_sats - tx.output_total_sats)
                if tx.vsize:
                    tx.fee_rate = round(tx.fee_sats / tx.vsize, 2)
        else:
            tx.input_total_sats = sum(ins) if ins else None

    @staticmethod
    def _address_from_script_pubkey(spk: dict[str, Any]) -> Optional[str]:
        if not spk:
            return None
        if spk.get("address"):
            return spk["address"]
        addrs = spk.get("addresses") or []
        if len(addrs) == 1:
            return addrs[0]
        if addrs:
            return addrs[0]
        return None

    # Highest-trust first: an explicit user confirmation beats wallet
    # discovery, which beats a plain manual import. Legacy addresses with no
    # evidence rows keep the ADDRESS_REGISTRY fallback (preserves existing
    # classification_source values for old databases / tests).
    _EVIDENCE_PRIORITY = {
        ClassificationSource.USER_CONFIRMED: 30,
        ClassificationSource.XPUB_DISCOVERY: 20,
        ClassificationSource.MANUAL_IMPORT: 10,
    }

    @classmethod
    def _primary_evidence(
        cls, evidence: list[dict[str, Any]]
    ) -> Optional[dict[str, Any]]:
        """Pick the strongest evidence row for an address, if any."""
        if not evidence:
            return None
        best: Optional[dict[str, Any]] = None
        best_score = -1
        for row in evidence:
            source = (row.get("source") or "").upper()
            score = cls._EVIDENCE_PRIORITY.get(source, 0)
            if score > best_score:
                best_score = score
                best = row
        return best

    # Evidence sources that are DIRECT statements of ownership (as opposed to
    # propagation / heuristics). Direct evidence + a confirmed custodial
    # boundary on the same outpoint is an explicit ownership/custody conflict.
    _DIRECT_EVIDENCE_SOURCES = {
        ClassificationSource.MANUAL_IMPORT,
        ClassificationSource.XPUB_DISCOVERY,
        ClassificationSource.USER_CONFIRMED,
    }

    async def effective_ownership(
        self, output: OutputInfo, ownership_row: Optional[dict[str, Any]], *, data=None
    ) -> dict[str, Any]:
        """Public read-only effective ownership of ONE outpoint (R2-01, P3-10).

        ``output`` carries the outpoint identity and its address;
        ``ownership_row`` is the persisted ``ownership`` row (or ``None``).
        The effective classification is written onto ``output`` and the
        decision returned, exactly as the engine derives it on every read
        path (``_effective_ownership``). Nothing is persisted: no stale
        EXTERNAL reconciliation and no contradiction row. ``data`` may be a
        bulk read model with the same lookup methods as the database.
        """
        return await self._effective_ownership(output, ownership_row, data=data)

    async def _effective_ownership(
        self, out: OutputInfo, ownership: Optional[dict[str, Any]], *, data=None
    ) -> dict[str, Any]:
        """Derive the effective in-memory ownership classification of an output.

        Read-only: this method never persists a stale-EXTERNAL reconciliation
        and never writes an ownership-contradiction / address-conflict row. The
        trace engine applies the reconciliation it decides to persist itself;
        derived read-models (Provenance Documentation Coverage) reuse the same
        effective semantics without any persistence side effect.

        Kept semantics: registry / direct evidence wins over a stale EXTERNAL
        row in memory; a legacy OWNED/TRACE_START row is not ownership
        evidence; UNKNOWN is never silently promoted to OWNED; topology-only
        heuristics never produce ownership.
        """
        # A persisted classification only wins when it is a real decision
        # (USER/explicit, or any non-UNKNOWN status). An UNKNOWN row is
        # a placeholder and may be upgraded by the registry (REQ 43).
        # TRACE_START is a legacy trace-start assumption and is explicitly
        # NOT a "real decision" — it never counts as ownership evidence and
        # is downgraded to UNKNOWN unless direct address evidence appears.
        is_trace_start = (
            ownership is not None
            and ownership.get("classification_source")
            == ClassificationSource.TRACE_START
            and ownership.get("ownership_status") == OwnershipStatus.OWNED
        )
        # Re-check semantics: an outpoint once classified EXTERNAL must be
        # reconsidered when direct ownership evidence appears for its
        # address (wallet discovery / manual import / user confirmation).
        # Stale external state must not override the stronger new evidence.
        # An explicit USER EXTERNAL classification is kept as a visible
        # PENDING contradiction, never silently deleted.
        data = data or self.db
        primary = None
        registry_wallet = None
        known = None
        if out.address:
            evidence = await data.get_address_evidence(out.address)
            primary = self._primary_evidence(evidence)
            known = await data.get_known_address(out.address)
            registry_wallet = known.get("wallet_name") if known else None
        out.address_confirmed = known is not None
        out.confirmation_outranks_external = known is not None and primary is not None
        out.overridden_classification = None

        persisted_wins = ownership and (not is_trace_start) and (
            ownership.get("ownership_status") != OwnershipStatus.UNKNOWN
            or ownership.get("classification_source")
            not in (None, "UNKNOWN", "ADDRESS_REGISTRY")
        )
        # SK-4b-Rest: USER_CONFIRMED is address-level evidence, not a
        # per-outpoint decision. A persisted OWNED row with that source
        # (legacy reconciliation output) only counts while the address is a
        # registry member; otherwise it is treated like UNKNOWN. Explicit
        # USER always wins.
        if (
            persisted_wins
            and ownership.get("ownership_status") == OwnershipStatus.OWNED
            and ownership.get("classification_source")
            == ClassificationSource.USER_CONFIRMED
            and known is None
        ):
            persisted_wins = False

        stale_external = (
            ownership
            and ownership.get("ownership_status") == OwnershipStatus.EXTERNAL
            and primary is not None
            # Only registry membership may reconcile a stale EXTERNAL: address
            # evidence, including retained USER_CONFIRMED, affects ownership
            # only while the address is in the registry (L-09 / SK-4 / SK-4b).
            and known is not None
        )
        if stale_external:
            explicit_user = ownership.get("classification_source") == ClassificationSource.USER
            out.ownership_status = OwnershipStatus.OWNED
            out.classification_source = primary["source"]
            out.wallet_name = registry_wallet or primary.get("wallet_name")
            if explicit_user:
                # Stored, but overridden by the address confirmation (FIX-OWN-1).
                out.overridden_classification = OwnershipStatus.EXTERNAL
            return {
                "stale_external": True,
                "explicit_user": explicit_user,
                "primary": primary,
            }
        elif persisted_wins:
            out.ownership_status = ownership["ownership_status"]
            out.classification_source = ownership.get("classification_source", "UNKNOWN")
            out.wallet_name = ownership.get("wallet_name")
            # An OWNED decision without its own wallet shows the registry
            # wallet, read live so it follows the registry (SK-4b).
            if out.wallet_name is None and ownership["ownership_status"] == OwnershipStatus.OWNED:
                out.wallet_name = registry_wallet
            out.confidence = ownership.get("confidence")
        elif is_trace_start:
            # Legacy OWNED/TRACE_START row persisted by an older release.
            # A trace-start assumption is NOT ownership evidence (TRACE
            # TARGET != OWNERSHIP EVIDENCE), so the stale assumption is
            # downgraded to UNKNOWN unless direct / registry evidence
            # exists, which then wins. Never persisted: each load
            # re-derives the effective status so registry evidence always
            # beats the stale row.
            if out.address and await data.is_known_address(out.address):
                if primary is not None:
                    out.ownership_status = OwnershipStatus.OWNED
                    out.classification_source = primary["source"]
                    out.wallet_name = registry_wallet or primary.get("wallet_name")
                else:
                    out.ownership_status = OwnershipStatus.OWNED
                    out.classification_source = ClassificationSource.ADDRESS_REGISTRY
                    out.wallet_name = registry_wallet
            else:
                out.ownership_status = OwnershipStatus.UNKNOWN
                out.classification_source = ClassificationSource.UNKNOWN
                out.wallet_name = None
                out.confidence = None
        elif out.address and await data.is_known_address(out.address):
            # Auto-OWNED via the Known Address Registry (REQ 34/40/47 +
            # REQ 43). The classification source now reflects WHY the
            # address is owned:
            #   * address found by wallet discovery -> XPUB_DISCOVERY
            #   * address imported into the registry -> MANUAL_IMPORT
            #   * user confirmed ownership          -> USER_CONFIRMED
            # Legacy / evidence-free addresses keep ADDRESS_REGISTRY so
            # existing behaviour is unchanged. Never auto-EXTERNAL, never
            # overrides a USER classification, bare 64-hex never matches.
            if primary is not None:
                out.ownership_status = OwnershipStatus.OWNED
                out.classification_source = primary["source"]
                out.wallet_name = registry_wallet or primary.get("wallet_name")
            else:
                out.ownership_status = OwnershipStatus.OWNED
                out.classification_source = ClassificationSource.ADDRESS_REGISTRY
                out.wallet_name = registry_wallet
        else:
            # Reset every field: ``out`` may be the engine's cached object and
            # still carry a source/wallet from evidence that is gone now.
            out.ownership_status = OwnershipStatus.UNKNOWN
            out.classification_source = ClassificationSource.UNKNOWN
            out.wallet_name = None
            out.confidence = None
        return {"stale_external": False, "explicit_user": False, "primary": None}

    async def _apply_cached_outpoint_meta(self, out: OutputInfo, *, data=None) -> None:
        """Refresh spend/provenance metadata from the cached outpoint row."""
        cached = await (data or self.db).get_outpoint(out.txid, out.vout)
        if not cached:
            return
        out.spend_status = cached.get("spend_status", "UNKNOWN")
        out.spending_txid = cached.get("spending_txid")
        out.provenance_state = cached.get("provenance_state") or "UNKNOWN"
        out.transaction_topology = cached.get("transaction_topology") or "UNKNOWN"
        out.likely_batch = bool(cached.get("likely_batch"))
        reasons = cached.get("provenance_reasons") or "[]"
        if isinstance(reasons, str):
            try:
                reasons = json.loads(reasons)
            except Exception:
                reasons = []
        out.provenance_reasons = list(reasons) if isinstance(reasons, list) else []

    async def enrich_outputs_readonly(self, tx: TransactionInfo, *, data=None) -> None:
        """Read-only enrichment: effective ownership + cached metadata only.

        Non-persisting equivalent of ``_enrich_outputs`` for derived
        read-models such as Provenance Documentation Coverage, whose
        generation must not mutate persistence. Effective in-memory ownership
        still lets direct/registry evidence win, but no stale-EXTERNAL
        reconciliation is written and no ownership-contradiction /
        address-conflict record is created or updated.
        """
        data = data or self.db
        for out in tx.outputs:
            ownership = await data.get_ownership(out.txid, out.vout)
            await self._effective_ownership(out, ownership, data=data)
            await self._apply_cached_outpoint_meta(out, data=data)

    async def outpoint_ownership_state(self, txid: str, vout: int) -> dict[str, Any]:
        """Read-only effective ownership of one cached outpoint (FIX-OWN-1)."""
        cached = await self.db.get_outpoint(txid, vout) or {}
        out = OutputInfo(txid=txid, vout=vout, outpoint=f"{txid}:{vout}", address=cached.get("address"))
        await self.enrich_outputs_readonly(TransactionInfo(txid=txid, outputs=[out]))
        return {
            "ownership_status": out.ownership_status,
            "classification_source": out.classification_source,
            "wallet_name": out.wallet_name,
            "address_confirmed": out.address_confirmed,
            "confirmation_outranks_external": out.confirmation_outranks_external,
            "overridden_classification": out.overridden_classification,
        }

    async def _enrich_outputs(
        self, tx: TransactionInfo, *, persist_ownership: bool = True
    ) -> None:
        for out in tx.outputs:
            ownership = await self.db.get_ownership(out.txid, out.vout)
            decision = await self._effective_ownership(out, ownership)
            if decision["stale_external"] and persist_ownership:
                if decision["explicit_user"]:
                    await self.db.record_ownership_contradiction(
                        out.address,
                        user_wallet=ownership.get("wallet_name") or "",
                        evidence_wallet=decision["primary"].get("wallet_name") or "",
                        detail=(
                            f"address {out.address} was explicitly classified EXTERNAL "
                            f"but now carries {decision['primary'].get('source')} ownership "
                            f"evidence; ownership is shown as OWNED, the explicit "
                            f"decision is preserved as a PENDING contradiction"
                        ),
                    )
                else:
                    # SK-4b-Rest: OWNED derived from address evidence is not
                    # persisted as an ownership row. The stale EXTERNAL row is
                    # reset to UNKNOWN; OWNED is derived at read time through
                    # the registry branch of _effective_ownership (in-memory
                    # result stays OWNED).
                    await self.db.set_ownership(
                        out.txid,
                        out.vout,
                        OwnershipStatus.UNKNOWN,
                        ClassificationSource.UNKNOWN,
                        None,
                        None,
                    )
            await self._apply_cached_outpoint_meta(out)
        await self._apply_annotations(tx)

    async def _store_transaction(self, info: TransactionInfo) -> None:
        await self.db.upsert_transaction(
            {
                "txid": info.txid,
                "block_hash": info.block_hash,
                "block_height": info.block_height,
                "block_time": info.block_time,
                "confirmations": info.confirmations,
                "input_total_sats": info.input_total_sats,
                "output_total_sats": info.output_total_sats,
                "fee_sats": info.fee_sats,
                "vsize": info.vsize,
                "raw_json": info.raw,
            }
        )
        for out in info.outputs:
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
                    "spend_status": out.spend_status,
                }
            )

    # ------------------------------------------------------------------
    # Spending state resolution
    # ------------------------------------------------------------------

    async def resolve_spend_state(self, txid: str, vout: int) -> dict[str, Any]:
        """Determine whether an outpoint is UNSPENT, SPENT or UNKNOWN.

        Returns a dict with keys: spend_status, spending_txid, vin_index.
        """
        # 1. Cached spend relationship
        cached_spend = await self.db.get_spend(txid, vout)
        if cached_spend:
            spending_txid = cached_spend.get("spending_txid")
            if spending_txid:
                return {
                    "spend_status": "SPENT",
                    "spending_txid": spending_txid,
                    "vin_index": cached_spend.get("vin_index"),
                }

        # 2. gettxout: unspent confirmed UTXO
        utxo = await self.rpc.get_txout(txid, vout, include_mempool=False)
        if utxo is not None:
            status = {"spend_status": "UNSPENT", "spending_txid": None, "vin_index": None}
            await self.db.update_outpoint_spend_status(txid, vout, "UNSPENT")
            return status

        # 3. txospenderindex lookup (explicit mempool_only=false so confirmed
        #    spends are resolved, per the DAG trace spec).
        try:
            resp = await self.rpc.get_tx_spending_prevout([(txid, vout)], mempool_only=False)
        except (NodeUnreachableError, RpcTimeoutError, BlockNotSyncedError):
            # Node outage / timeout / IBD is not a missing index: re-raise
            # before the message-text check below (P3-9, R3-06).
            raise
        except RpcError as exc:
            # index not synced or unavailable
            if "txospenderindex" in exc.message.lower() or "index" in exc.message.lower():
                return {
                    "spend_status": "UNKNOWN",
                    "spending_txid": None,
                    "vin_index": None,
                    "error": str(exc),
                }
            raise

        spending_txid: Optional[str] = None
        if resp:
            entry = resp[0]
            spending_txid = entry.get("spendingtxid")

        if spending_txid:
            vin_index = await self._resolve_vin_index(spending_txid, txid, vout)
            await self.db.upsert_spend(txid, vout, spending_txid, vin_index)
            await self.db.update_outpoint_spend_status(txid, vout, "SPENT")
            return {
                "spend_status": "SPENT",
                "spending_txid": spending_txid,
                "vin_index": vin_index,
            }

        # 4. No spender in index but no UTXO: unconfirmed spend or immature coinbase.
        status = {
            "spend_status": "UNKNOWN",
            "spending_txid": None,
            "vin_index": None,
            "hint": "Output is not in the confirmed UTXO set and no confirmed spender "
                    "was found. It may have been spent by an unconfirmed transaction.",
        }
        return status

    async def _resolve_vin_index(
        self, spending_txid: str, source_txid: str, source_vout: int
    ) -> int | None:
        try:
            raw = await self.rpc.get_raw_transaction(spending_txid, verbose=True)
            vin_list = raw.get("vin") or []
            for idx, vin in enumerate(vin_list):
                if vin.get("txid") == source_txid and vin.get("vout") == source_vout:
                    return idx
        except RpcError:
            logger.debug("could not resolve vin index for %s", spending_txid)
        return None

    # ------------------------------------------------------------------
    # Trace orchestration
    # ------------------------------------------------------------------

    async def analyze_start(
        self, txid: str, address: Optional[str] = None, vout: Optional[int] = None
    ) -> dict[str, Any]:
        """Step for the MVP flow: load the start transaction and (optionally)
        match the address to an output."""
        tx = await self.load_transaction(txid)
        result: dict[str, Any] = {"transaction": tx}
        matched: Optional[int] = None

        if vout is not None:
            if not any(o.vout == vout for o in tx.outputs):
                raise RpcError("The supplied VOUT does not exist in this transaction.")
            result["matched_vout"] = vout
            return result

        if address:
            for out in tx.outputs:
                if out.address == address:
                    matched = out.vout
                    break
            if matched is None:
                raise RpcError(
                    "The supplied Bitcoin address does not match any output in this transaction."
                )
            result["matched_vout"] = matched
        return result

    async def create_trace(
        self,
        name: str,
        start_txid: str,
        start_address: Optional[str],
        start_vout: Optional[int],
        trace_mode: str = "FORWARD_PROVENANCE",
    ) -> tuple[Trace, dict[str, Any]]:
        """Create the trace session, resolve the start outpoint(s), and walk."""
        start_tx = await self.load_transaction(start_txid)

        # Determine the starting vouts.
        vouts: list[int] = []
        resolved_start_vout = start_vout
        if start_vout is not None:
            if not any(o.vout == start_vout for o in start_tx.outputs):
                raise RpcError("The supplied VOUT does not exist in this transaction.")
            vouts.append(start_vout)
        elif start_address:
            for out in start_tx.outputs:
                if out.address == start_address:
                    vouts.append(out.vout)
            if not vouts:
                raise RpcError(
                    "The supplied Bitcoin address does not match any output in this transaction."
                )
            if len(vouts) == 1:
                # TXID + address with a single match defines the start outpoint
                # explicitly, so the trace session records it as its start vout
                # (bugfix spec 4.1).
                resolved_start_vout = vouts[0]
        else:
            # User will pick outputs manually from the UI; we create the trace
            # session with no start outpoint yet, or trace all if requested.
            pass

        trace = await self.db.create_trace(
            name=name,
            start_txid=start_txid,
            start_address=start_address,
            start_vout=resolved_start_vout,
            trace_mode=trace_mode,
        )

        # The chosen start outpoints are traversal SEEDS, not ownership
        # evidence (TRACE TARGET != OWNERSHIP EVIDENCE). Starting a trace never
        # implies ownership: the start outpoint is walked and its spending
        # transaction followed regardless of ownership; ownership is derived
        # purely from evidence (registry / xpub / user confirmation) or shown
        # as UNKNOWN. No ownership row is written for the start here.

        # Reset edges and walk the trace.
        await self.db.delete_trace_edges(trace.id)
        self._mem_tx_cache.clear()
        walk = await self._walk_trace(trace.id)

        status = self._decide_status(trace.id, walk)
        trace = await self.db.update_trace(trace.id, status=status)
        assert trace is not None
        logger.info("trace created id=%d txid=%s status=%s", trace.id, start_txid, status)
        return trace, walk

    async def continue_trace(self, trace_id: int) -> dict[str, Any]:
        """Re-walk a trace after ownership classification changed."""
        trace = await self.db.get_trace(trace_id)
        if not trace:
            raise TraceNotFoundError()
        self._mem_tx_cache.clear()
        walk = await self._walk_trace(trace_id)
        status = self._decide_status(trace_id, walk)
        await self.db.update_trace(trace.id, status=status)
        return walk

    async def _walk_trace(self, trace_id: int) -> dict[str, Any]:
        """Walk every owned outpoint that belongs to this trace.

        Owned outpoints are discovered from the ownership table for
        outpoints reachable through start selection or numeric ownership.
        """
        # Remove stale edges beyond decisions -> rebuild all edges.
        await self.db.delete_trace_edges(trace_id)

        trace = await self.db.get_trace(trace_id)
        if not trace:
            raise TraceNotFoundError()

        # Custodial boundaries for this trace: exact lineage stops there.
        self._walk_boundaries = {
            (b["txid"], b["vout"])
            for b in await self.db.get_custodial_boundaries(trace_id)
        }

        owned = await self._collect_owned_start_points(trace)
        # Keep the shortest processed depth, not merely membership: a merge
        # reached again by a shorter path may have more traversal budget.
        visited: dict[tuple[str, int], int] = {}
        depth_limited: set[tuple[str, int]] = set()
        pending_unknown: list[tuple[str, int]] = []
        terminal_utxos: list[tuple[str, int]] = []
        ended_at_custody_boundary: list[str] = []

        for (txid, vout) in owned:
            await self._trace_outpoint(
                trace_id,
                txid,
                vout,
                hop=0,
                visited=visited,
                pending_unknown=pending_unknown,
                terminal_utxos=terminal_utxos,
                ended_at_custody_boundary=ended_at_custody_boundary,
                depth_limited=depth_limited,
            )

        await self._finalize_relationships(trace_id)

        # Surface explicit ownership/custody conflicts from the boundaries (an
        # outpoint marked CUSTODIAL_DEPOSIT whose address carries direct
        # ownership evidence). Runs after the walk so freshly persisted
        # outpoint rows are available. Never auto-deletes either side.
        await self._detect_ownership_custody_conflicts(trace_id)

        active_conflicts = await self.db.list_ownership_custody_conflicts(
            trace_id, status="PENDING"
        )

        return {
            "visited_outpoints": [f"{t}:{v}" for t, v in visited],
            "pending_unknown": [f"{t}:{v}" for t, v in pending_unknown],
            "terminal_utxos": [f"{t}:{v}" for t, v in terminal_utxos],
            "depth_limited_outpoints": [f"{t}:{v}" for t, v in sorted(depth_limited)],
            "ended_at_custody_boundary": list(dict.fromkeys(ended_at_custody_boundary)),
            "pending_ownership_custody_conflicts": [
                f"{r['txid']}:{int(r['vout'])}" for r in active_conflicts
            ],
        }

    async def _detect_ownership_custody_conflicts(self, trace_id: int) -> None:
        """Reconcile confirmed custodial boundaries against direct ownership
        evidence (real-node field case).

        For every confirmed boundary whose address now has direct ownership
        evidence (MANUAL_IMPORT / XPUB_DISCOVERY / USER_CONFIRMED) a PENDING
        OWNERSHIP_CUSTODY_CONFLICT is created. The boundary and the evidence
        are both preserved - nothing is deleted automatically. PENDING
        conflicts whose underlying condition has cleared (boundary removed or
        evidence gone) are marked resolved, never silently dropped.
        """
        boundaries = await self.db.get_custodial_boundaries(trace_id)
        active: set[tuple[str, int]] = set()
        for b in boundaries:
            txid = b["txid"]
            vout = int(b["vout"])
            outpoint = await self.db.get_outpoint(txid, vout)
            address = (outpoint or {}).get("address")
            if not address:
                continue
            evidence = await self.db.get_address_evidence(address)
            primary = self._primary_evidence(evidence) if evidence else None
            source = str((primary or {}).get("source") or "").upper()
            if source not in self._DIRECT_EVIDENCE_SOURCES:
                continue
            active.add((txid, vout))
            existing = await self.db.get_ownership_custody_conflict(
                trace_id, txid, vout
            )
            if existing:
                status = existing.get("status")
                # A pending review or a durable keep-boundary decision on THIS
                # boundary is left untouched; never re-open a user decision on
                # the same boundary and never spam duplicate PENDING rows.
                if status == "PENDING":
                    continue
                if (
                    status == "KEEP_BOUNDARY"
                    and existing.get("boundary_id") == b["id"]
                ):
                    continue
                # Anything else (a cleared/reviewed cycle or a different,
                # newly created boundary) is a fresh review context: upsert
                # below refreshes the row and sets it back to PENDING.
            wallet = primary.get("wallet_name") or ""
            await self.db.upsert_ownership_custody_conflict(
                trace_id=trace_id,
                txid=txid,
                vout=vout,
                address=address,
                wallet_name=wallet,
                evidence_source=source,
                boundary_id=int(b["id"]),
                service_name=b.get("service_name") or "",
                detail=(
                    f"address has {source} ownership evidence (wallet "
                    f"'{wallet}') but is marked CUSTODIAL_DEPOSIT by a "
                    f"confirmed custodial boundary"
                ),
            )

        for row in await self.db.list_ownership_custody_conflicts(
            trace_id, status="PENDING"
        ):
            key = (row["txid"], int(row["vout"]))
            if key not in active:
                await self.db.resolve_ownership_custody_conflict(
                    int(row["id"]), "condition_cleared"
                )

    async def _collect_owned_start_points(self, trace: Trace) -> list[tuple[str, int]]:
        """Gather the starting outpoints of this trace.

        Uses ownership records created when the user selected outputs.
        If none exist yet (TXID only entered), all outputs remain pending.
        """
        # Every outpoint the user classified as OWNED and that belongs to this
        # trace (start tx) becomes a start point. For simplicity and correctness
        # we start from outputs marked OWNED in the DB for the start tx.
        start_outpoints: list[tuple[str, int]] = []
        if trace.start_vout is not None:
            start_outpoints.append((trace.start_txid, trace.start_vout))
        elif trace.start_address:
            tx = await self._ensure_transaction_for_txid(trace.start_txid)
            for out in tx.outputs:
                if out.address == trace.start_address:
                    start_outpoints.append((trace.start_txid, out.vout))
        else:
            # All outputs of the start transaction whose ownership == OWNED.
            start_tx = await self._ensure_transaction_for_txid(trace.start_txid)
            for out in start_tx.outputs:
                if out.ownership_status == OwnershipStatus.OWNED:
                    start_outpoints.append((trace.start_txid, out.vout))

        # User-confirmed custody-bridge destinations resume exact tracing.
        for (dtxid, dvout) in await self.db.custody_bridge_destinations(trace.id):
            if (dtxid, dvout) not in start_outpoints:
                start_outpoints.append((dtxid, dvout))

        return start_outpoints

    async def _trace_outpoint(
        self,
        trace_id: int,
        txid: str,
        vout: int,
        hop: int,
        visited: dict[tuple[str, int], int],
        pending_unknown: list[tuple[str, int]],
        terminal_utxos: list[tuple[str, int]],
        ended_at_custody_boundary: list[str],
        depth_limited: set[tuple[str, int]],
    ) -> None:
        key = (txid, vout)
        if key in self._walk_boundaries:
            ended_at_custody_boundary.append(f"{txid}:{vout}")
            return
        if key in visited and visited[key] <= hop:
            return
        if hop > self.max_depth:
            depth_limited.add(key)
            return
        depth_limited.discard(key)
        visited[key] = hop

        tx = await self.load_transaction(txid)

        spend = await self.resolve_spend_state(txid, vout)

        if spend.get("spend_status") == SpendStatus.UNSPENT:
            terminal_utxos.append(key)
            await self.db.update_outpoint_spend_status(txid, vout, "UNSPENT")
            return

        if spend.get("spend_status") == SpendStatus.UNKNOWN:
            pending_unknown.append(key)
            return

        spending_txid = spend.get("spending_txid")
        if not spending_txid:
            pending_unknown.append(key)
            return

        spending_tx = await self.load_transaction(spending_txid)

        # Determine owned outputs to walk into.
        owned_outputs = [
            o for o in spending_tx.outputs if o.ownership_status == OwnershipStatus.OWNED
        ]
        unknown_outputs = [
            o
            for o in spending_tx.outputs
            if o.ownership_status == OwnershipStatus.UNKNOWN
        ]

        # Persist provenance + topology for every output of the spending
        # transaction. Topology and
        # batch structure are whole-transaction facts; provenance is per output
        # and depends on which outputs of THIS trace are owned. Topology must
        # never feed custody detection - it only explains the shape.
        owned_keys = {
            f"{spending_txid}:{o.vout}" for o in owned_outputs
        }
        tx_top = tx_topology(spending_tx)
        is_batch = 1 if spending_tx.likely_batch else 0
        for out_o in spending_tx.outputs:
            prov_state, prov_reasons = provenance_for_output(
                spending_tx, out_o.vout, owned_keys
            )
            # Update the in-memory objects too (the transaction may already be
            # cached from the walk) so _build_full_trace sees the same values.
            out_o.provenance_state = prov_state
            out_o.provenance_reasons = prov_reasons
            out_o.transaction_topology = tx_top
            out_o.likely_batch = bool(is_batch)
            await self.db.upsert_outpoint(
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
                    "likely_batch": is_batch,
                    "provenance_reasons": prov_reasons,
                }
            )

        # Mark the source outpoint as spent in our DB.
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

        # Show every successor output, but recurse only into owned outputs.
        # UNKNOWN outputs are terminal ownership-provenance nodes even when the
        # same transaction also contains owned change.
        #
        # A confirmed boundary stops its own output, not independently OWNED
        # change. Unknown pooled siblings remain context as before.
        has_boundary = any(
            (spending_txid, o.vout) in self._walk_boundaries
            for o in spending_tx.outputs
        )
        targets = spending_tx.outputs
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

        if has_boundary:
            for o in spending_tx.outputs:
                if (spending_txid, o.vout) in self._walk_boundaries:
                    ended_at_custody_boundary.append(f"{spending_txid}:{o.vout}")

        # UNKNOWN requires a decision only without an established owned
        # continuation or explicit boundary at THIS spend (not graph-wide).
        if unknown_outputs and not owned_outputs and not has_boundary:
            for o in unknown_outputs:
                if (spending_txid, o.vout) not in pending_unknown:
                    pending_unknown.append((spending_txid, o.vout))

        # Recursively continue into owned outputs.
        for o in owned_outputs:
            await self._trace_outpoint(
                trace_id,
                spending_txid,
                o.vout,
                hop + 1,
                visited,
                pending_unknown,
                terminal_utxos,
                ended_at_custody_boundary,
                depth_limited,
            )

    def materialized_forward_state(
        self, edges: list[TraceEdge], outputs: list[OutputInfo],
        boundaries: list[dict[str, Any]], seeds: set[tuple[str, int]],
    ) -> dict[str, list[str]]:
        """Inspect saved topology only; never discover new chain edges here.

        Breadth-first reachability gives shortest depths in a DAG independently
        of the historical edge hop numbers. Only actual seeds and OWNED paths
        qualify, never contextual transaction siblings or descendants beyond a
        confirmed boundary. Classification uses the same per-spend frontier as
        _trace_outpoint: UNKNOWN siblings only block without OWNED successors or
        an explicit boundary. Their spend state/history is irrelevant. The
        existing walker remains the sole chain walker.
        """
        by_key = {(o.txid, o.vout): o for o in outputs}
        stops = {(b["txid"], b["vout"]) for b in boundaries}
        outgoing: dict[tuple[str, int], list[TraceEdge]] = {}
        for edge in edges:
            if edge.relationship_type != RelationshipType.CUSTODY_BRIDGE:
                outgoing.setdefault((edge.source_txid, edge.source_vout), []).append(edge)
        queue = deque((key, 0) for key in seeds)
        seen: set[tuple[str, int]] = set()
        missing, limited, pending, ended = set(), set(), set(), set()
        while queue:
            key, hop = queue.popleft()
            if key in seen:
                continue
            seen.add(key)
            point = f"{key[0]}:{key[1]}"
            if key in stops:
                ended.add(point)
                continue
            if hop > self.max_depth:
                limited.add(point)
                continue
            out = by_key.get(key)
            if out is None:
                pending.add(point)
                continue
            if out.spend_status == SpendStatus.UNSPENT:
                continue
            if out.spend_status != SpendStatus.SPENT or not out.spending_txid:
                # Preserve the walker's unresolved spend/index behavior, also
                # for selected seeds. UNKNOWN seed ownership itself never blocks.
                pending.add(point)
                continue
            links = [e for e in outgoing.get(key, []) if
                e.spending_txid == out.spending_txid
                and e.destination_txid == out.spending_txid
                and (e.destination_txid, e.destination_vout) in by_key
            ]
            if not links:
                if out.ownership_status == OwnershipStatus.OWNED:
                    missing.add(point)
                else:
                    pending.add(point)
                continue
            successors = {(e.destination_txid, e.destination_vout) for e in links}
            owned = {k for k in successors if by_key[k].ownership_status == OwnershipStatus.OWNED}
            boundary = successors & stops
            ended.update(f"{t}:{v}" for t, v in boundary)
            if not owned and not boundary:
                pending.update(
                    f"{t}:{v}" for t, v in successors
                    if by_key[(t, v)].ownership_status == OwnershipStatus.UNKNOWN
                )
            queue.extend((dest, hop + 1) for dest in owned)
        return {
            "missing_continuation": sorted(missing),
            "depth_limited_outpoints": sorted(limited),
            "pending_unknown": sorted(pending),
            "ended_at_custody_boundary": sorted(ended),
        }

    async def _finalize_relationships(self, trace_id: int) -> None:
        """Assign relationship types per spending transaction."""
        edges = await self.db.get_edges(trace_id)
        if not edges:
            return

        # Display columns follow the longest materialized DAG path, whereas
        # traversal budgets use the shortest path. A shorter-path retry must
        # not collapse a merge and its successor into one timeline column.
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
        for edge in edges:
            edge.hop_number = levels.get((edge.source_txid, edge.source_vout), edge.hop_number)

        # Group edges by spending transaction.
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
                stx = self._mem_tx_cache.get(spending_txid)
                if stx is None:
                    stx = await self.load_transaction(spending_txid)
                total_inputs = len(
                    [i for i in stx.inputs if i.txid or i.prev_outpoint != "coinbase"]
                )
                external_inputs = max(0, total_inputs - n_in)
            except (NodeUnreachableError, RpcTimeoutError, BlockNotSyncedError):
                # Node outage / auth / timeout / IBD is not missing data: fail
                # instead of reporting a MIXED spend as DIRECT (P1-3e).
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

    def _decide_status(self, trace_id: int, walk: dict[str, Any]) -> str:
        if walk.get("depth_limited_outpoints"):
            return TraceStatus.FORWARD_LIMIT_REACHED
        if walk.get("pending_ownership_custody_conflicts"):
            return TraceStatus.NEEDS_REVIEW
        if walk.get("pending_unknown"):
            return TraceStatus.NEEDS_CLASSIFICATION
        if walk.get("ended_at_custody_boundary"):
            return TraceStatus.COMPLETE_AT_CUSTODY_BOUNDARY
        return TraceStatus.COMPLETE

    async def _ensure_transaction_for_txid(self, txid: str) -> TransactionInfo:
        return await self.load_transaction(txid)

    # ------------------------------------------------------------------
    # Classification
    # ------------------------------------------------------------------

    async def classify_output(
        self,
        trace_id: Optional[int],
        txid: str,
        vout: int,
        ownership_status: str,
        classification_source: str = ClassificationSource.USER,
        wallet_name: Optional[str] = None,
        confidence: Optional[float] = None,
        announce: bool = True,
    ) -> dict[str, Any]:
        if ownership_status not in (
            OwnershipStatus.OWNED,
            OwnershipStatus.LIKELY_OWNED,
            OwnershipStatus.EXTERNAL,
            OwnershipStatus.CUSTODIAL_DEPOSIT,
            OwnershipStatus.UNKNOWN,
        ):
            raise RpcError(f"Invalid ownership status: {ownership_status}")

        await self.db.set_ownership(
            txid,
            vout,
            ownership_status,
            classification_source,
            wallet_name,
            confidence,
        )
        logger.info(
            "classification %s:%d -> %s (source=%s, trace=%s)",
            txid,
            vout,
            ownership_status,
            classification_source,
            trace_id,
        )

        # Re-walk after an OWNED classification (new propagation) and after a
        # genuine "reset to automatic" (UNKNOWN + UNKNOWN source: the outpoint
        # is re-evaluated from the current registry/evidence and the spend
        # chain re-derived). An explicit UNKNOWN/USER "Unknown" button on the
        # Classify tab keeps the outpoint UNKNOWN and does NOT re-walk (parity
        # with the existing Classify behaviour); EXTERNAL terminates the walk
        # and is not re-walked either.
        reset_to_automatic = (
            ownership_status == OwnershipStatus.UNKNOWN
            and classification_source in (None, "", ClassificationSource.UNKNOWN)
        )
        if announce and trace_id is not None and (
            ownership_status == OwnershipStatus.OWNED or reset_to_automatic
        ):
            walk = await self.continue_trace(trace_id)
            return {"walk": walk, "trace_id": trace_id}
        return {"trace_id": trace_id}

    async def repair_downstream_ownership(
        self, trace_id: int, boundary_txid: str, boundary_vout: int
    ) -> list[str]:
        """Re-evaluate downstream exact-lineage ownership after accepting a
        custodial boundary at ``(boundary_txid, boundary_vout)``.

        Exact lineage STOPPED at the boundary, so any downstream outpoint that
        is OWNED/LIKELY_OWNED only because ownership had been propagated
        through the boundary must be reset to UNKNOWN and re-evaluated by the
        next walk. Explicit classifications that are independently supported
        by the known-address registry are kept (their OWNED status does not
        depend on propagation through the boundary).

        Must run BEFORE ``continue_trace`` (which deletes the edges).

        Returns the list of reset outpoints (``"TXID:VOUT"``).
        """
        boundary = (boundary_txid, boundary_vout)

        edges = await self.db.get_edges(trace_id)
        adjacency: dict[tuple[str, int], set[tuple[str, int]]] = {}
        for e in edges:
            if e.relationship_type == RelationshipType.CUSTODY_BRIDGE:
                # Logical / user-confirmed links are not on-chain lineage and
                # are never repaired here.
                continue
            adjacency.setdefault((e.source_txid, e.source_vout), set()).add(
                (e.destination_txid, e.destination_vout)
            )

        protected: set[tuple[str, int]] = {
            (b["txid"], b["vout"])
            for b in await self.db.get_custodial_boundaries(trace_id)
        }
        protected.update(await self.db.custody_bridge_destinations(trace_id))
        protected.add(boundary)

        reached: set[tuple[str, int]] = set()
        stack = [boundary]
        while stack:
            key = stack.pop()
            if key in reached:
                continue
            reached.add(key)
            stack.extend(adjacency.get(key, ()))

        reset: list[str] = []
        for txid, vout in sorted(reached):
            if (txid, vout) in protected:
                continue
            ownership = await self.db.get_ownership(txid, vout)
            if not ownership:
                continue
            status = ownership.get("ownership_status")
            if status not in (OwnershipStatus.OWNED, OwnershipStatus.LIKELY_OWNED):
                continue
            op = await self.db.get_outpoint(txid, vout)
            address = (op or {}).get("address")
            if address and await self.db.is_known_address(address):
                # Independently supported by the known-address registry:
                # this ownership does NOT originate from propagation through
                # the boundary, so it survives the boundary acceptance.
                continue
            await self.db.set_ownership(
                txid, vout, OwnershipStatus.UNKNOWN, ClassificationSource.UNKNOWN
            )
            reset.append(f"{txid}:{vout}")

        if reset:
            logger.info(
                "custodial acceptance %s:%d reset downstream ownership: %s",
                boundary_txid,
                boundary_vout,
                ", ".join(reset),
            )
        return reset

    async def remove_custodial_classification(
        self, trace_id: int, txid: str, vout: int
    ) -> dict[str, Any]:
        """Remove an explicit CUSTODIAL_DEPOSIT classification (the inverse of
        accepting a custodial boundary).

        - the confirmed boundary record on this outpoint (if any) is deleted,
          so exact lineage no longer stops there;
        - the outpoint ownership is reset to UNKNOWN/UNKNOWN - only the
          custody classification is dropped, the UTXO, tx graph and any
          wallet ownership evidence are preserved;
        - the trace is re-walked: the former stopping outpoint is re-entered,
          ownership is recomputed from the current evidence (OWNED with the
          strongest evidence source when present, otherwise UNKNOWN), the
          downstream spend chain is followed again and the trace status is
          recomputed.

        Any PENDING ownership/custody conflict on this outpoint is resolved to
        ``condition_cleared`` by the next walk because its boundary is gone;
        nothing is silently deleted.
        """
        boundary = await self.db.get_custodial_boundary(trace_id, txid, vout)
        if boundary:
            await self.db.delete_custodial_boundary(int(boundary["id"]))
        await self.db.set_ownership(
            txid, vout, OwnershipStatus.UNKNOWN, ClassificationSource.UNKNOWN
        )
        walk = await self.continue_trace(trace_id)
        return {"boundary_removed": bool(boundary), "walk": walk}

    # ------------------------------------------------------------------
    # Status / health
    # ------------------------------------------------------------------

    async def node_status(self) -> dict[str, Any]:
        status: dict[str, Any] = {
            "reachable": False,
            "core_version": None,
            "network": None,
            "network_match": None,
            "blockchain_synced": False,
            "txindex_synced": False,
            "txospenderindex_synced": False,
            "block_height": None,
            "indexes": [],
            "rpc_error": None,
        }
        try:
            info = await self.rpc.get_network_info()
            status["core_version"] = info.get("version")
            status["subversion"] = info.get("subversion")
        except RpcError as exc:
            status["rpc_error"] = exc.message
            return status

        status["reachable"] = True
        chain_info = await self.rpc.get_blockchain_info()
        chain = chain_info.get("chain")
        status["network"] = chain
        status["expected_network"] = "main" if self.rpc.settings.btc_network in ("mainnet", "main") else chain
        status["network_match"] = chain == status["expected_network"]
        status["blockchain_synced"] = bool(
            chain_info.get("initialblockdownload") is False
            and (chain_info.get("verificationprogress") or 0) > 0.9999
        )
        status["block_height"] = chain_info.get("blocks")

        indexes = await self.rpc.get_index_info()
        status["indexes"] = [
            {
                "name": name,
                "synced": bool(idx.get("synced")),
                "best_block_height": idx.get("best_block_height"),
            }
            for name, idx in indexes.items()
        ]
        for idx in status["indexes"]:
            if idx["name"] == "txindex":
                status["txindex_synced"] = idx["synced"]
            if idx["name"] == "txospenderindex":
                status["txospenderindex_synced"] = idx["synced"]

        # Index sync progress (0.0..1.0) for the status UI.
        block_height = status.get("block_height")
        for name in ("txindex", "txospenderindex"):
            idx = next((i for i in status["indexes"] if i["name"] == name), None)
            if idx is None or not block_height:
                status[f"{name}_progress"] = 1.0 if idx and idx.get("synced") else None
                status[f"{name}_indexed_height"] = idx.get("best_block_height") if idx else None
                status[f"{name}_target_height"] = block_height or idx.get("best_block_height")
                continue
            best = idx.get("best_block_height")
            status[f"{name}_indexed_height"] = best
            status[f"{name}_target_height"] = block_height
            if idx.get("synced"):
                status[f"{name}_progress"] = 1.0
            elif best is not None and block_height:
                status[f"{name}_progress"] = max(0.0, min(1.0, best / float(block_height)))
            else:
                status[f"{name}_progress"] = None

        return status
