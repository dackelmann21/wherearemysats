"""One-time wallet discovery (WALLET_XPUB_DISCOVERY_REQUIREMENTS.md).

The service turns a user-supplied extended *public* key (xpub / ypub / zpub)
into a set of owned addresses without ever sending the key to the Bitcoin
Core node and without storing it. The scan talks to the node only with
per-address ``addr()`` descriptors:

  * primary: ``scanblocks`` over the basic block filter index
    (wire format ``["start", descriptors, start_height, stop_height, "basic"]``,
    response ``relevant_blocks``), each relevant block then inspected with
    ``getblock(<hash>, 2)`` so outputs that were *already spent* are still
    discovered;
  * fallback: ``scantxoutset`` (UTXO-set only). The fallback runs ONLY for a
    genuine capability problem (method/index unavailable) - never for
    protocol/parameter errors or transport failures. Fallback results are
    flagged ``scan_mode="scantxoutset"`` + ``utxo_only`` and warn that spent
    outputs cannot be seen.

Discovered addresses feed the *same* canonical known-address registry — there
is no parallel ownership universe (section 1 design principle). Each
discovered address gets an ``address_evidence`` row (source ``XPUB_DISCOVERY``,
derivation path, first-seen) so the classification source describes *why* the
address is owned.

Privacy guarantees enforced here (sections 7/8):

  * the extended key lives only in ``DiscoveryConfig`` for the duration of a
    request and is never written to the database, files or logs;
  * the API responses contain a redacted ``key_hint`` only;
  * invalid input is never echoed back by the service.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from backend.discovery.bip32 import (
    ExtendedKeyError,
    ExtendedPublicKey,
    encode_pubkey,
    parse_extended_public_key,
)
from backend.models.schemas import DiscoveryPlan
from backend.reporting.read_data import bounded_map
from backend.discovery.scan_coordinator import coordinator_for
from backend.rpc.bitcoin import (
    BitcoinRPCClient,
    NodeUnreachableError,
    RpcError,
    RpcTimeoutError,
    sats,
)

logger = logging.getLogger("lineage.discovery")

# Kept out of the API surface: the operational knobs below are deliberately
# conservative so a mistake cannot cause an unbounded scan.
_DEFAULT_GAP_LIMIT = 20
_MAX_DERIVED_ADDRESSES = 2000
_MAX_SCAN_PASSES = 12

KEY_TYPE_TO_SCRIPT_TYPE = {
    "zpub": "p2wpkh",
    "ypub": "p2sh-p2wpkh",
    "xpub": "p2wpkh",
}

_SCRIPT_TYPES = {"p2wpkh", "p2sh-p2wpkh", "p2pkh", "p2tr"}


class DiscoveryConfig:
    """Carries the in-memory extended public key for one discovery run.

    This is the only object that ever holds the raw key; never store it.
    """

    def __init__(
        self,
        key: str,
        wallet_name: str = "discovery",
        label: str = "",
        key_type: str = "auto",
        network: str = "auto",
        script_type: str = "auto",
        gap_limit: int = _DEFAULT_GAP_LIMIT,
        start_height: Optional[int] = None,
        end_height: Optional[int] = None,
        scan_mode: str = "auto",
    ):
        self.key = (key or "").strip()
        self.wallet_name = (wallet_name or "discovery").strip()
        self.label = (label or "").strip()
        self.key_type = (key_type or "auto").lower()
        self.network = (network or "auto").lower()
        self.script_type = (script_type or "auto").lower()
        self.gap_limit = max(1, int(gap_limit or _DEFAULT_GAP_LIMIT))
        self.start_height = start_height
        self.end_height = end_height
        self.scan_mode = (scan_mode or "auto").lower()


def redact_key(raw: str, key_type: str = "") -> str:
    """Return a safe, redacted hint for a key. Never the full key."""
    stripped = (raw or "").strip()
    if not stripped:
        return ""
    prefix = key_type or stripped[:4]
    return f"{prefix}…{stripped[-4:]}"


class DiscoveryError(Exception):
    """User-facing discovery failure (already safe to display)."""


class DiscoveryCapabilityError(DiscoveryError):
    """The node is reachable but does not offer historical block-filter
    scanning.

    This is the ONLY error class that may trigger the scantxoutset fallback.
    Protocol/programmer errors (invalid RPC parameters, descriptors, ...) and
    transport errors must never be silently hidden behind the UTXO-only
    fallback (WALLET_XPUB_DISCOVERY_REQUIREMENTS.md).
    """

    def __init__(self, message: str, cause_message: str = ""):
        super().__init__(message)
        self.cause_message = cause_message


class DiscoveryScanTimeoutError(DiscoveryError):
    """The historical/fallback scan exceeded the configured RPC timeout.

    Deliberately distinct from "Bitcoin Core could not be reached": the node
    may be healthy — the scan itself is too slow. Carries the timeout (in
    seconds) that was exceeded so callers can display the precise duration.
    """

    def __init__(self, timeout: float | None):
        seconds = f"{timeout:g}" if timeout is not None else "?"
        super().__init__(f"Historical wallet scan timed out after {seconds} seconds.")
        self.timeout = timeout


def _is_scanblocks_capability_error(exc: RpcError) -> bool:
    """True when the node answered but cannot serve historical scans.

    ``-32601`` = method not found; missing/unsynced block-filter indexes are
    surfaced by Core as an RPC error whose message names the filter index. Any
    other RPC error (bad parameters, bad descriptors, ...) is a protocol or
    implementation error and is NEVER treated as a capability problem.
    """
    if exc.code == -32601:
        return True
    message = (exc.message or "").lower()
    return (
        "blockfilterindex" in message
        or "filter index" in message
        or "filterindex" in message
    )


def discovery_rpc_timeout(rpc: Any) -> float | None:
    """Long per-request timeout configured for wallet-discovery scans.

    ``None`` when the RPC object carries no settings (e.g. test doubles) —
    the caller then falls back to the RPC client's own default.
    """
    s = getattr(rpc, "settings", None)
    if s is None:
        return None
    return getattr(s, "discovery_rpc_timeout", None)


class WalletDiscovery:
    """Executes plans, previews and committed runs against one node."""

    def __init__(self, rpc: BitcoinRPCClient, db: Any, progress=None):
        self.rpc = rpc
        self.db = db
        self.progress = progress or (lambda **fields: None)

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    def build_plan(self, config: DiscoveryConfig) -> DiscoveryPlan:
        try:
            xk = parse_extended_public_key(config.key)
        except ExtendedKeyError as exc:
            raise DiscoveryError(str(exc)) from exc

        warnings: list[str] = []

        if config.key_type != "auto":
            if config.key_type != xk.key_type and config.key_type not in (
                xk.key_type,
                "xpub",
            ):
                warnings.append(
                    f"declared key type '{config.key_type}' does not match the "
                    f"extended key prefix (parsed as {xk.key_type})"
                )
        if config.network != "auto" and config.network not in (xk.network, "main", "mainnet"):
            if xk.network != "mainnet" or config.network not in ("main", "mainnet"):
                warnings.append(
                    f"declared network '{config.network}' conflicts with the "
                    f"extended key prefix ({xk.network})"
                )

        if config.script_type != "auto":
            script_type = config.script_type
            if script_type not in _SCRIPT_TYPES:
                raise DiscoveryError(f"unsupported script type: {config.script_type}")
        else:
            script_type = KEY_TYPE_TO_SCRIPT_TYPE.get(xk.key_type, "p2wpkh")
            if xk.key_type == "xpub":
                warnings.append(
                    "generic xpub: script type not encoded in the key, "
                    f"assuming {script_type} — verify before running"
                )
        if script_type == "p2tr":
            warnings.append(
                "taproot: non-hardened BIP32 derivation assumed "
                "(BIP-86 style), confirm with your wallet"
            )

        network = "mainnet" if xk.network == "mainnet" else "testnet"
        preview: list[dict[str, Any]] = []
        for branch, count in ((0, 3), (1, 3)):
            for i in range(count):
                try:
                    address = xk.derive_address(branch, i, script_type, network)
                except ExtendedKeyError as exc:
                    raise DiscoveryError(str(exc)) from exc
                preview.append({"branch": branch, "index": i, "address": address})

        hint = redact_key(config.key, xk.key_type)
        return DiscoveryPlan(
            key_type=xk.key_type,
            network=network,
            script_type=script_type,
            account_path=xk.derived_path,
            fingerprint=xk.fingerprint.hex(),
            key_hint=hint,
            wallet_name=config.wallet_name,
            label=config.label,
            gap_limit=config.gap_limit,
            scan_mode=config.scan_mode,
            start_height=config.start_height,
            end_height=config.end_height,
            scan_supported=True,
            warnings=warnings,
            preview_addresses=preview,
        )

    # ------------------------------------------------------------------
    # Scanning
    # ------------------------------------------------------------------

    def _derive(self, xk: ExtendedPublicKey, plan: DiscoveryPlan, count: int) -> list[str]:
        addr: list[str] = []
        for branch in (0, 1):
            for i in range(count):
                addr.append(xk.derive_address(branch, i, plan.script_type, plan.network))
        return addr

    def _derive_index(
        self, xk: ExtendedPublicKey, plan: DiscoveryPlan, count: int
    ) -> dict[str, tuple[int, int]]:
        """Map of derived address -> (branch_index, address_index)."""
        mapping: dict[str, tuple[int, int]] = {}
        for branch in (0, 1):
            for i in range(count):
                mapping[xk.derive_address(branch, i, plan.script_type, plan.network)] = (branch, i)
        return mapping

    @staticmethod
    def _needed_count(last_used: int, gap: int) -> int:
        """Window size covering ``gap`` unused addresses past ``last_used``."""
        return max(gap, last_used + gap + 1)

    async def _previous_indexes(self, xk, plan):
        """Reuse only cursors whose stored address actually matches this key.

        A wallet label is not a key identity. Checking derivation avoids applying
        a different account's cursor when the same label was reused.
        """
        last = [-1, -1]
        if self.db is not None:
            cursor = await self.db.db.execute("""SELECT address, branch_index, address_index
                FROM address_evidence WHERE wallet_name=? AND source='XPUB_DISCOVERY'""",
                (plan.wallet_name,))
            for row in await cursor.fetchall():
                branch, index = row["branch_index"], row["address_index"]
                if (branch in (0, 1) and isinstance(index, int) and 0 <= index < _MAX_DERIVED_ADDRESSES
                        and xk.derive_address(branch, index, plan.script_type, plan.network) == row["address"]):
                    last[branch] = max(last[branch], index)
        return last

    async def scan_current_scripts(self, addresses):
        """Shared Core UTXO query for derived or manually configured scripts.

        Core returns scriptPubKey as HEX, not a decoded address object. Resolve
        the producing transactions through the same local RPC, also providing
        the existing provenance evaluator with canonical input facts.
        """
        addresses = set(addresses)
        async with coordinator_for(self.rpc).reserve():
            result = await self.rpc.scantxoutset("start", ["addr(" + a + ")" for a in sorted(addresses)],
                                               timeout=discovery_rpc_timeout(self.rpc))
            if (not isinstance(result, dict) or result.get("success") is not True
                    or not isinstance(result.get("unspents"), list)
                    or not isinstance(result.get("height"), int) or not result.get("bestblock")):
                raise DiscoveryError("UTXO scan did not complete; previous inventory is preserved.")
            if len(result["unspents"]) > 100_000:
                raise DiscoveryError("Inventory scan exceeds the output limit.")
            async def load(txid):
                raw = await self.rpc.get_raw_transaction(txid, verbose=True)
                header = (await self.rpc.get_block_header(raw["blockhash"], verbose=True)
                          if raw.get("blockhash") else {})
                return self._transaction_fact(raw, header.get("height"), header.get("time"))
            facts = await bounded_map([u["txid"] for u in result["unspents"]], load)
            normalized = []
            for entry in result["unspents"]:
                raw = facts[entry["txid"]]["raw_json"]
                output = next((v for v in raw["vout"] if v["n"] == entry["vout"]), None)
                if output is None or sats(output["value"]) != sats(entry["amount"]):
                    raise DiscoveryError("UTXO scan returned inconsistent transaction evidence.")
                if self._address_from_vout(output) not in addresses:
                    raise DiscoveryError("UTXO scan returned an output outside the requested wallet scripts.")
                script = entry.get("scriptPubKey")
                if isinstance(script, str) and script != output["scriptPubKey"].get("hex"):
                    raise DiscoveryError("UTXO scan returned inconsistent script evidence.")
                normalized.append({**entry, "scriptPubKey": output["scriptPubKey"]})
            result = {**result, "unspents": normalized, "transactions": list(facts.values())}
            if await self.rpc.get_block_hash(result["height"]) != result["bestblock"]:
                raise DiscoveryError("The scanned chain changed. Refresh again.")
            return result

    @staticmethod
    def _transaction_fact(raw, height, block_time):
        return {"txid": raw["txid"], "raw_json": raw, "block_hash": raw.get("blockhash"),
                "block_height": height, "block_time": block_time,
                "confirmations": raw.get("confirmations"), "vsize": raw.get("vsize"),
                "output_total_sats": sum(sats(o["value"]) for o in raw["vout"])}

    async def inventory_snapshot(self, facts, addresses, height, bestblock, mode):
        addresses = set(addresses)
        outputs = {}
        for tx in facts:
            for out in tx["raw_json"]["vout"]:
                address = self._address_from_vout(out)
                if address not in addresses:
                    continue
                spk = out["scriptPubKey"]
                outputs[(tx["txid"], out["n"])] = {
                    "txid": tx["txid"], "vout": out["n"], "address": address,
                    "amount_sats": sats(out["value"]), "script_pubkey": spk.get("hex"),
                    "script_type": spk.get("type"), "block_height": tx["block_height"],
                    "block_time": tx["block_time"],
                }
        current = await bounded_map(outputs, lambda key: self.rpc.get_txout(*key, include_mempool=True))
        for key, out in outputs.items():
            live = current[key]
            if live is not None and sats(live["value"]) != out["amount_sats"]:
                raise DiscoveryError("Inventory output value disagrees with the local node.")
            # Absence is not proof of a sale, or even a confirmed spend (reorg).
            out["spend_status"] = "UNSPENT" if live is not None else "UNKNOWN"
        if await self.rpc.get_block_hash(height) != bestblock:
            raise DiscoveryError("The scanned chain changed. Refresh again.")
        return {"transactions": facts, "outputs": list(outputs.values()),
                "height": height, "bestblock": bestblock, "mode": mode,
                "observed_at": datetime.now(timezone.utc).isoformat()}

    async def _scan_with_scanblocks(
        self,
        xk: ExtendedPublicKey,
        plan: DiscoveryPlan,
        start: int,
        stop: int,
    ) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
        matches: dict[str, dict[str, Any]] = {}
        found_outpoints = set()
        unexpected_outpoints = set()
        matched_txids = set()
        last_used = await self._previous_indexes(xk, plan)
        facts = {}
        passes = 0
        warnings: list[str] = []
        meta = {
            "trace_count": 0,
            "found_vout_count": 0,
            "unexpected_vout_count": 0,
        }

        while True:
            passes += 1
            if passes > _MAX_SCAN_PASSES:
                meta["limited"] = True
                warnings.append(
                    "scan stopped: spending continued past the derivation cap; "
                    "increase gap_limit or restrict start/end height"
                )
                break
            window = max(
                self._needed_count(last_used[0], plan.gap_limit),
                self._needed_count(last_used[1], plan.gap_limit),
            )
            if window > _MAX_DERIVED_ADDRESSES:
                meta["limited"] = True
                warnings.append(
                    f"scan stopped: more than {_MAX_DERIVED_ADDRESSES} addresses would be derived"
                )
                window = _MAX_DERIVED_ADDRESSES
            derived = self._derive_index(xk, plan, window)
            descriptors = ["addr(" + a + ")" for a in derived.keys()]
            self.progress(stage="SCANNING", scan_mode="scanblocks", scan_pass=passes,
                          start_height=start, stop_height=stop, progress=None, current_height=None)
            try:
                result = await coordinator_for(self.rpc).run(
                    lambda: self.rpc.scanblocks(descriptors, start, stop))
            except RpcTimeoutError as exc:
                # Slow scan, not a connectivity problem. Never degraded to
                # "unreachable" and never silently hidden behind the UTXO-only
                # fallback (a stalled scan is not a capability problem).
                raise DiscoveryScanTimeoutError(exc.timeout) from exc
            except NodeUnreachableError:
                # Transport/connectivity: raise untouched so the API maps it to
                # its transport error, never to "the node lacks scanblocks".
                raise
            except RpcError as exc:
                if _is_scanblocks_capability_error(exc):
                    raise DiscoveryCapabilityError(
                        "Bitcoin Core is connected, but historical wallet "
                        "discovery is unavailable.",
                        cause_message=exc.message or "scanblocks unavailable",
                    ) from exc
                # Valid-parameter / protocol error: an implementation or RPC
                # bug. Do NOT fall back silently; the error must reach the UI.
                raise DiscoveryError(
                    "Wallet discovery RPC failed: "
                    + (exc.message or "unknown RPC error")
                ) from exc
            # Core (>= 28) returns relevant_blocks (never blockhashes), plus
            # from_height / to_height / completed.
            if (not isinstance(result, dict) or result.get("completed") is not True
                    or result.get("from_height") != start or result.get("to_height") != stop
                    or not isinstance(result.get("relevant_blocks"), list)):
                raise DiscoveryError("Wallet scan did not complete the requested range; nothing was imported.")
            relevant_blocks = result["relevant_blocks"]
            self.progress(stage="PROCESSING", progress=None, current_height=None)
            meta["branch0_scanned_through"] = window - 1
            meta["branch1_scanned_through"] = window - 1
            meta["passes"] = passes

            first_at: dict[str, int | None] = {}
            for blockhash in relevant_blocks:
                block = await self.rpc.get_block(blockhash, verbosity=2)
                block_time = block.get("time")
                for tx in block.get("tx", []):
                    txid = tx.get("txid")
                    for vout in tx.get("vout", []):
                        address = self._address_from_vout(vout)
                        if not address or address not in derived:
                            continue
                        value = vout.get("value") or 0
                        outpoint = (txid, vout["n"])
                        found_outpoints.add(outpoint)
                        facts[txid] = self._transaction_fact(tx, block.get("height"), block_time)
                        if len(found_outpoints) > 100_000:
                            raise DiscoveryError("Wallet scan exceeds the output limit; narrow the scan range.")
                        matched_txids.add(txid)
                        if value <= 0:
                            unexpected_outpoints.add(outpoint)
                        if (
                            address not in first_at
                            or (block_time and block_time < (first_at[address] or 0))
                        ):
                            first_at[address] = block_time
            meta["trace_count"] = len(matched_txids)
            meta["found_vout_count"] = len(found_outpoints)
            meta["unexpected_vout_count"] = len(unexpected_outpoints)

            new_last_used = list(last_used)
            for address, first_block_time in first_at.items():
                branch_index, address_index = derived[address]
                matches[address] = {
                    "address": address,
                    "branch_index": branch_index,
                    "address_index": address_index,
                    "derivation_path": f"{plan.account_path}/{branch_index}/{address_index}",
                    "key_type": plan.key_type,
                    "first_seen_at": self._iso_time(first_block_time),
                    "label": plan.label,
                }
                new_last_used[branch_index] = max(
                    new_last_used[branch_index], address_index
                )

            need = max(
                self._needed_count(new_last_used[0], plan.gap_limit),
                self._needed_count(new_last_used[1], plan.gap_limit),
            )
            grew = new_last_used[0] > last_used[0] or new_last_used[1] > last_used[1]
            last_used = new_last_used
            if window == _MAX_DERIVED_ADDRESSES and need > window:
                meta["limited"] = True
                warnings.append("scan stopped: derivation cap reached before the full trailing gap")
                break
            if grew and need > window:
                continue  # rescan the whole range with a wider window
            break

        ordered = sorted(
            matches.values(),
            key=lambda r: (r["branch_index"], r["address_index"]),
        )
        meta["transactions"] = list(facts.values())
        return ordered, meta, warnings

    @staticmethod
    def _address_from_vout(vout: dict[str, Any]) -> Optional[str]:
        spk = vout.get("scriptPubKey") or {}
        if spk.get("address"):
            return spk["address"]
        addrs = spk.get("addresses") or []
        return addrs[0] if addrs else None

    @staticmethod
    def _iso_time(epoch: Optional[int]) -> Optional[str]:
        if not epoch:
            return None
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()

    def _scan_mode(self, config: DiscoveryConfig, plan: DiscoveryPlan) -> str:
        if config.scan_mode == "scantxoutset":
            return "scantxoutset"
        return "scanblocks"

    async def _resolve_range(
        self, config: DiscoveryConfig
    ) -> tuple[int, int, int]:
        tip = await self.rpc.get_block_count()
        start = config.start_height if config.start_height is not None else 0
        start = max(0, int(start))
        stop = config.end_height if config.end_height is not None else tip
        stop = min(int(stop), tip)
        if stop < start:
            raise DiscoveryError(
                f"invalid scan range: start {start} > stop {stop}"
            )
        return start, stop, tip

    async def _annotate_existing(self, matched: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Attach the registry wallet name for addresses already known."""
        out: list[dict[str, Any]] = []
        for item in matched:
            existing_wallet = None
            if self.db is not None:
                known = await self.db.get_known_address(item["address"])
                existing_wallet = known.get("wallet_name") if known else None
            out.append({**item, "existing_wallet": existing_wallet})
        return out

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def scan(
        self, config: DiscoveryConfig
    ) -> tuple[DiscoveryPlan, list[dict[str, Any]], dict[str, int], list[str]]:
        """Derive + scan the chain. Returns (plan, matched, meta, warnings)."""
        try:
            async with coordinator_for(self.rpc).reserve():
                return await self._scan(config)
        finally:
            config.key = ""

    async def _scan(self, config):
        plan = self.build_plan(config)
        xk = parse_extended_public_key(config.key)
        start, stop, _ = await self._resolve_range(config)
        anchor = await self.rpc.get_block_hash(stop)
        plan.start_height, plan.end_height = start, stop
        mode = self._scan_mode(config, plan)
        plan.scan_mode = mode

        if mode == "scantxoutset":
            matched, meta, warnings = await self._scan_with_utxo(xk, plan)
            meta["utxo_only"] = True
        else:
            try:
                matched, meta, warnings = await self._scan_with_scanblocks(
                    xk, plan, start, stop
                )
            except DiscoveryCapabilityError as scanblocks_exc:
                # Only a capability problem may fall back to the UTXO set.
                plan.scan_mode = "scantxoutset"
                plan.warnings.append(
                    "scanblocks unavailable — fell back to scantxoutset (UTXO-only)"
                )
                try:
                    matched, meta, warnings = await self._scan_with_utxo(xk, plan)
                except NodeUnreachableError:
                    raise
                except RpcTimeoutError as utxo_exc:
                    # The UTXO fallback itself stalled — still a scan timeout,
                    # never "unreachable".
                    raise DiscoveryScanTimeoutError(utxo_exc.timeout) from utxo_exc
                except RpcError as utxo_exc:
                    # Degraded fallback failure: report BOTH stages. The RPC
                    # error messages never contain the extended key.
                    raise DiscoveryError(
                        "Historical wallet discovery is unavailable ("
                        + (scanblocks_exc.cause_message or "scanblocks unavailable")
                        + "); the UTXO-set fallback also failed: "
                        + (utxo_exc.message or "unknown RPC error")
                    ) from utxo_exc
                meta["utxo_only"] = True

        plan.warnings = list(dict.fromkeys([*plan.warnings, *warnings]))
        if await self.rpc.get_block_hash(stop) != anchor:
            raise DiscoveryError("The scanned chain changed. Scan again.")
        matched = await self._annotate_existing(matched)
        height = meta.pop("inventory_height", stop)
        bestblock = meta.pop("inventory_bestblock", None) or anchor
        meta["inventory"] = await self.inventory_snapshot(
            meta.pop("transactions", []), [m["address"] for m in matched],
            height, bestblock, plan.scan_mode)
        for item in matched:
            item.pop("_first_at", None)
        return plan, matched, meta, warnings

    async def _scan_with_utxo(
        self, xk: ExtendedPublicKey, plan: DiscoveryPlan
    ) -> tuple[list[dict[str, Any]], dict[str, int], list[str]]:
        """scantxoutset fallback: UTXO-set only, no spend/block history."""
        matches: dict[str, dict[str, Any]] = {}
        last_used = await self._previous_indexes(xk, plan)
        passes = 0
        warnings: list[str] = [
            "scantxoutset scan is UTXO-only and cannot see spent outputs"
        ]
        meta = {
            "trace_count": 0,
            "found_vout_count": 0,
            "unexpected_vout_count": 0,
        }
        unspents_all: list[dict[str, Any]] = []
        while True:
            passes += 1
            if passes > _MAX_SCAN_PASSES:
                warnings.append("scan stopped: derivation pass cap reached")
                break
            window = max(
                self._needed_count(last_used[0], plan.gap_limit),
                self._needed_count(last_used[1], plan.gap_limit),
            )
            if window > _MAX_DERIVED_ADDRESSES:
                warnings.append("scan stopped: derivation cap reached")
                window = _MAX_DERIVED_ADDRESSES
            derived = self._derive_index(xk, plan, window)
            self.progress(stage="SCANNING", scan_mode="scantxoutset", scan_pass=passes,
                          progress=None, current_height=None)
            # Wallet-discovery scantxoutset runs get the long per-request
            # timeout (a historical derivation pass may take minutes); the
            # custody-bridge candidate scan keeps the normal 30s default.
            result = await self.scan_current_scripts(derived)
            if not isinstance(result, dict) or result.get("success") is not True:
                raise DiscoveryError("UTXO scan did not complete; nothing was imported.")
            self.progress(stage="PROCESSING")
            unspents = result.get("unspents", []) or []
            unspents_all = unspents
            new_last_used = list(last_used)
            for utxo in unspents:
                address = self._address_from_vout({"scriptPubKey": utxo.get("scriptPubKey")})
                if not address or address not in derived:
                    continue
                branch_index, address_index = derived[address]
                matches[address] = {
                    "address": address,
                    "branch_index": branch_index,
                    "address_index": address_index,
                    "derivation_path": f"{plan.account_path}/{branch_index}/{address_index}",
                    "key_type": plan.key_type,
                    "first_seen_at": None,
                    "label": plan.label,
                }
                new_last_used[branch_index] = max(new_last_used[branch_index], address_index)
            need = max(
                self._needed_count(new_last_used[0], plan.gap_limit),
                self._needed_count(new_last_used[1], plan.gap_limit),
            )
            grew = new_last_used[0] > last_used[0] or new_last_used[1] > last_used[1]
            last_used = new_last_used
            if window == _MAX_DERIVED_ADDRESSES and need > window:
                warnings.append("scan stopped: derivation cap reached")
                break
            if grew and need > window:
                continue
            break

        ordered = sorted(
            matches.values(),
            key=lambda r: (r["branch_index"], r["address_index"]),
        )
        meta = {
            "trace_count": len(matched_txs_for_utxo := {u.get("txid") for u in unspents_all}),
            "found_vout_count": len(unspents_all),
            "unexpected_vout_count": 0,
            "branch0_scanned_through": window - 1,
            "branch1_scanned_through": window - 1,
            "passes": min(passes, _MAX_SCAN_PASSES),
            "transactions": result["transactions"],
            "inventory_height": result["height"],
            "inventory_bestblock": result["bestblock"],
            "limited": any("cap reached" in w for w in warnings),
        }
        return ordered, meta, warnings

    async def preview(
        self, config: DiscoveryConfig
    ) -> dict[str, Any]:
        """Full dry-run: derive + scan, persist nothing."""
        plan, matched, meta, warnings = await self.scan(config)
        plan.warnings = list(dict.fromkeys([*plan.warnings, *warnings]))
        branch0 = [m for m in matched if m["branch_index"] == 0]
        branch1 = [m for m in matched if m["branch_index"] == 1]
        return {
            "plan": plan,
            "matched": matched,
            "meta": meta,
            "branch0_found": len(branch0),
            "branch1_found": len(branch1),
            "branch0_last_index": max([m["address_index"] for m in branch0], default=-1),
            "branch1_last_index": max([m["address_index"] for m in branch1], default=-1),
            "status": "PREVIEW",
            "committed": False,
        }

    async def commit(self, result) -> dict[str, Any]:
        """Persist a trusted key-free result. Never derive or start a scan.

        HTTP callers must go through WalletScanSessions for confirmation,
        revision, context and chain-anchor validation; client results are never
        passed here. The complete persistence operation is one transaction.
        """
        plan, matched, meta, _ = result
        stats = await self.db.import_wallet_result(plan, matched, meta)
        return {**stats, "matched": matched, "meta": meta, "scan_mode": plan.scan_mode}
