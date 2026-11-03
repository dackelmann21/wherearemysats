"""Reverse-mode target address discovery (address -> candidate outpoints).

REVERSE_PROVENANCE and BLOCKCHAIN_ANALYSIS_REVERSE allow starting a trace from
a *Bitcoin address* in addition to a TXID. An address is only ever discovery
input: no ownership evidence, no Known Address Registry row, no trace targets
are written here (``TRACE TARGET != OWNERSHIP EVIDENCE``). This module is
read-only against the *local* Bitcoin infrastructure only:

  * primary: Bitcoin Core ``scanblocks`` over the basic blockfilter index
    (the existing internal address-history capability already built for wallet
    discovery) + ``getblock(verbosity=2)`` + ``gettxspendingprevout``, so
    outputs that were long since spent are still discovered;
  * fallback (only when configured): the on-box Electrs over the Electrum
    protocol, isolated in ``backend.electrs``. Fallback triggers ONLY for a
    genuine Core capability problem (method/index missing) — never for
    transport errors, timeouts or protocol errors, mirroring wallet-discovery
    policy.

Privacy: every source is local (Bitcoin Core RPC on the local network, or
a local Electrs server). No mempool.space / Blockstream / blockchain.com
/ public Electrum / other explorer is ever contacted.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

from backend.electrs import ElectrsClient, ElectrsUnreachableError
from backend.models.schemas import SpendStatus
from backend.registry.address_validation import (
    address_to_script,
    is_valid_mainnet_address,
)
from backend.rpc.bitcoin import (
    BitcoinRPCClient,
    BlockNotSyncedError,
    NodeUnreachableError,
    RpcError,
    RpcTimeoutError,
    sats,
)

# discovery_source values surfaced to the API/UI.
SOURCE_BITCOIND_SCANBLOCKS = "bitcoin_core_scanblocks"
SOURCE_ELECTRS = "electrs"

# Sort within the same spend class: newest block first, unconfirmed first of all.
_UNCONFIRMED = 1 << 62


class InvalidAddressError(Exception):
    """The string is not a valid mainnet Bitcoin address (already safe to echo)."""


class AddressDiscoveryUnavailableError(Exception):
    """Neither local source could resolve address history (user-facing)."""


class _CoreDiscoveryUnavailable(Exception):
    """Internal: Bitcoin Core is reachable but cannot serve historical scans.

    Only this class may trigger the Electrs fallback, mirroring the wallet
    discovery rule that genuine capability problems are the sole fallback
    trigger (transport errors/timeouts/protocol errors must not be silently
    hidden behind another index source).
    """

    def __init__(self, cause_message: str = ""):
        super().__init__(cause_message)
        self.cause_message = cause_message


def is_core_capability_error(exc: RpcError) -> bool:
    """True when the node answered but cannot serve block-filter scans.

    Same test the wallet-discovery service already applies: ``-32601`` = method
    not found; a missing/unsynced blockfilter index is surfaced by Core as an
    RPC error whose message names the filter index. Any other RPC error is a
    protocol/implementation error and is never treated as a capability problem.
    """
    if exc.code == -32601:
        return True
    message = (exc.message or "").lower()
    return (
        "blockfilterindex" in message
        or "filter index" in message
        or "filterindex" in message
    )


class AddressDiscovery:
    """Resolves every locally-discoverable output paying to one address.

    Pure read: nothing is written to the database, registry or node. The
    returned candidates are plain dicts with the normalized shape the API
    exposes (txid, vout, address, value_sats, spend_status, spending_txid,
    block_height, block_time), sorted UNSPENT first then newest.
    """

    def __init__(self, rpc: BitcoinRPCClient, electrs: Optional[ElectrsClient] = None):
        self.rpc = rpc
        self.electrs = electrs

    async def resolve_address_outputs(self, address: str) -> tuple[list[dict[str, Any]], str]:
        """Return (sorted candidate outputs, discovery_source)."""
        candidate = (address or "").strip()
        if not candidate or not is_valid_mainnet_address(candidate):
            raise InvalidAddressError("not a valid mainnet Bitcoin address")
        try:
            candidates = await self._resolve_with_scanblocks(candidate)
            return candidates, SOURCE_BITCOIND_SCANBLOCKS
        except _CoreDiscoveryUnavailable:
            if self.electrs is None or not self.electrs.enabled:
                raise AddressDiscoveryUnavailableError(
                    "Bitcoin Core cannot serve address history (no blockfilter "
                    "index) and no local Electrs fallback is configured"
                )
            try:
                candidates = await self._resolve_with_electrs(candidate)
            except ElectrsUnreachableError as exc:
                raise AddressDiscoveryUnavailableError(str(exc)) from exc
            return candidates, SOURCE_ELECTRS

    # ------------------------------------------------------------------
    # Bitcoin Core scanblocks path (primary)
    # ------------------------------------------------------------------

    async def _resolve_with_scanblocks(
        self, address: str
    ) -> list[dict[str, Any]]:
        try:
            tip = await self.rpc.get_block_count()
        except (NodeUnreachableError, RpcTimeoutError, RpcError):
            raise
        try:
            from backend.discovery.scan_coordinator import coordinator_for
            result = await coordinator_for(self.rpc).run(lambda: self.rpc.scanblocks(
                ["addr(" + address + ")"], 0, tip, "basic"
            ))
        except (NodeUnreachableError, RpcTimeoutError, BlockNotSyncedError):
            # SK-2: timeouts, transport failures and IBD (node not synced) are
            # reported as errors and never trigger the Electrs fallback (Core
            # may still be scanning or syncing).
            raise
        except RpcError as exc:
            if is_core_capability_error(exc):
                raise _CoreDiscoveryUnavailable(
                    exc.message or "scanblocks unavailable"
                ) from exc
            raise AddressDiscoveryUnavailableError(
                "Bitcoin Core address scan failed: " + (exc.message or "unknown RPC error")
            ) from exc

        if not isinstance(result, dict) or result.get("completed") is not True:
            raise AddressDiscoveryUnavailableError("Address scan did not complete.")
        return await self.resolve_from_blocks(
            address, (result.get("relevant_blocks", []) or [])
        )

    async def resolve_from_blocks(
        self, address: str, relevant_blocks: list[str]
    ) -> list[dict[str, Any]]:
        """Resolve candidate outputs paying to ``address`` from scan ``relevant_blocks``.

        Shared by the synchronous ``_resolve_with_scanblocks`` path and the async
        address-discovery job lifecycle: when Core finishes an asynchronous
        scan, the completion poll hands the returned relevant block hashes here
        instead of issuing another scan. Pure read, identical normalization.
        """
        outpoints: dict[tuple[str, int], dict[str, Any]] = {}
        for blockhash in relevant_blocks or []:
            block = await self.rpc.get_block(blockhash, verbosity=2)
            block_height = block.get("height")
            block_time = block.get("time")
            for tx in block.get("tx", []):
                txid = tx.get("txid")
                for vout in tx.get("vout", []):
                    spk = vout.get("scriptPubKey") or {}
                    out_addr = spk.get("address") or (spk.get("addresses") or [None])[0]
                    if not out_addr or out_addr.lower() != address.lower():
                        continue
                    value_sats = sats(vout.get("value"))
                    if value_sats is None or value_sats <= 0:
                        continue
                    outpoints[(txid, vout.get("n"))] = {
                        "txid": txid,
                        "vout": vout.get("n"),
                        "address": out_addr,
                        "value_sats": value_sats,
                        "block_height": block_height,
                        "block_time": block_time,
                    }
        if not outpoints:
            return []

        spend_rows = await self.rpc.get_tx_spending_prevout(
            [(t, v) for (t, v) in outpoints], mempool_only=False
        )
        spender: dict[tuple[str, int], str] = {}
        for row in spend_rows:
            if row.get("spendingtxid"):
                spender[(row["txid"], row["vout"])] = row["spendingtxid"]
        return self._sorted([
            {
                **candidate,
                "spend_status": (
                    SpendStatus.SPENT if (txid, vout) in spender else SpendStatus.UNSPENT
                ),
                "spending_txid": spender.get((txid, vout)),
            }
            for (txid, vout), candidate in outpoints.items()
        ])

    # ------------------------------------------------------------------
    # Local Electrs fallback path
    # ------------------------------------------------------------------

    async def _resolve_with_electrs(self, address: str) -> list[dict[str, Any]]:
        script = address_to_script(address)
        if not script:
            raise InvalidAddressError("cannot derive an output script for this address")
        scripthash = hashlib.sha256(bytes.fromhex(script)).digest()[::-1].hex()
        history = await self.electrs.scripthash_history(scripthash)
        unspent = await self.electrs.listunspent(scripthash)
        unspent_keys = {
            (entry["tx_hash"], entry["tx_pos"]) for entry in unspent if entry.get("tx_hash")
        }

        outpoints: dict[tuple[str, int], dict[str, Any]] = {}
        for entry in history:
            txid = entry.get("tx_hash")
            height = entry.get("height")
            if not txid:
                continue
            vtx = await self.electrs.verbose_transaction(txid)
            block_time = vtx.get("blocktime") or vtx.get("time")
            for vout in vtx.get("vout", []):
                spk = vout.get("scriptPubKey") or {}
                out_addr = spk.get("address") or (spk.get("addresses") or [None])[0]
                if not out_addr or out_addr.lower() != address.lower():
                    continue
                value_sats = sats(vout.get("value"))
                if value_sats is None or value_sats <= 0:
                    continue
                vout_n = vout.get("n")
                is_unspent = (txid, vout_n) in unspent_keys
                outpoints[(txid, vout_n)] = {
                    "txid": txid,
                    "vout": vout_n,
                    "address": out_addr,
                    "value_sats": value_sats,
                    "spend_status": (
                        SpendStatus.UNSPENT if is_unspent else SpendStatus.SPENT
                    ),
                    "spending_txid": None if is_unspent else None,
                    "block_height": height if height is not None and height >= 0 else None,
                    "block_time": block_time,
                }
        return self._sorted(list(outpoints.values()))

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _sorted(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            candidates,
            key=lambda c: (
                0 if c["spend_status"] == SpendStatus.UNSPENT else 1,
                -(c["block_height"] if c.get("block_height") is not None else _UNCONFIRMED),
            ),
        )
