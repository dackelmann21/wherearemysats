"""Bitcoin Core JSON-RPC client.

The application is strictly read-only (REQ 37): only query RPCs are used.
Credentials come from server-side environment/secret configuration (REQ 35).
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import httpx

from backend.config import Settings

logger = logging.getLogger("lineage.rpc")

#: One bitcoin expressed in satoshis, as an exact decimal value.
_ONE_BTC_SATS = Decimal(100_000_000)


def sats(value: float | int | str | Decimal | None) -> int | None:
    """Convert a Bitcoin amount to integer satoshis exactly.

    Bitcoin Core reports amounts as JSON decimal numbers with at most 8 places
    (satoshis are the smallest unit). The value is interpreted as an exact
    decimal, scaled by 100,000,000, and required to be an integer number of
    satoshis. Authoritative amounts are never routed through binary
    floating-point multiplication or rounding.

    ``None`` maps to ``None``. Raises ``ValueError`` for values that describe
    fractional satoshis (more than 8 decimal places) or are not valid amounts.
    """
    if value is None:
        return None
    try:
        if isinstance(value, Decimal):
            amount = value
        elif isinstance(value, (int, str)):
            amount = Decimal(value)
        else:
            # float -> exact decimal string. repr() yields the shortest
            # decimal that round-trips to the same binary value, which for a
            # Core-emitted ≤8-decimal amount reproduces the amount exactly.
            amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid Bitcoin amount: {value!r}") from exc
    if not amount.is_finite():
        raise ValueError(f"invalid Bitcoin amount: {value!r} (not a finite decimal)")
    scaled = amount * _ONE_BTC_SATS
    if scaled != scaled.to_integral_value():
        raise ValueError(
            f"Bitcoin amount {value!r} describes fractional satoshis; "
            "a valid amount has at most 8 decimal places."
        )
    return int(scaled)


class RpcError(Exception):
    """Bitcoin Core RPC returned an application-level error."""

    def __init__(self, message: str, code: int | None = None, data: Any = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data


class NodeUnreachableError(RpcError):
    """Bitcoin Core could not be reached / auth failed."""


# FIX-OWN-1b: the one user-facing text for NodeUnreachableError.
NODE_UNREACHABLE_MESSAGE = "Bitcoin node is not reachable."


class RpcAuthenticationError(NodeUnreachableError):
    """HTTP 401: invalid RPC credentials."""


class RpcForbiddenError(NodeUnreachableError):
    """HTTP 403: RPC access denied."""


class RpcTimeoutError(RpcError):
    """A Bitcoin Core RPC request exceeded its allowed duration.

    Distinct from :class:`NodeUnreachableError`: the node may be perfectly
    healthy, the individual query is simply too slow (e.g. a historical
    wallet-discovery scan). Carries the configured timeout (in seconds) that
    was exceeded so callers can produce messages like
    ``"Historical wallet scan timed out after 600 seconds."``.
    """

    def __init__(self, message: str, timeout: float | None = None):
        super().__init__(message)
        self.timeout = timeout


class BlockNotSyncedError(RpcError):
    """Blockchain / indexes not fully synced."""


class BitcoinRPCClient:
    """Async JSON-RPC client for a Bitcoin Core node."""

    def __init__(self, settings: Settings):
        self.settings = settings
        scheme = "https" if settings.btc_rpc_tls else "http"
        host = settings.btc_rpc_host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        self.url = (
            f"{scheme}://{host}:{settings.btc_rpc_port}/wallet/"
        )
        # Use the node endpoint (no wallet) unless wallet RPCs are needed.
        self.node_url = f"{scheme}://{host}:{settings.btc_rpc_port}/"
        self._client: httpx.AsyncClient | None = None
        self._request_id = 0
        # The lock only guards request-id allocation; it is NEVER held across
        # the network call (two independent Core RPCs may run concurrently).
        self._id_lock = asyncio.Lock()

    async def _next_request_id(self) -> int:
        async with self._id_lock:
            self._request_id += 1
            return self._request_id

    async def _call(
        self,
        method: str,
        params: list[Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        if self._client is None:
            self._client = httpx.AsyncClient(
                auth=(self.settings.btc_rpc_user, self.settings.btc_rpc_password),
                timeout=self.settings.rpc_timeout,
                trust_env=False,
                follow_redirects=False,
            )
        # The per-request timeout overrides the client default only for THIS
        # call; all ordinary RPC calls keep the normal 30s default.
        effective_timeout = (
            timeout if timeout is not None else self.settings.rpc_timeout
        )
        payload = {
            "jsonrpc": "1.0",
            "id": await self._next_request_id(),
            "method": method,
            "params": params or [],
        }
        try:
            resp = await self._client.post(
                self.node_url, json=payload, timeout=effective_timeout
            )
        except httpx.TimeoutException:
            # The node did not answer in time. This is NOT a connectivity
            # problem: connectivity failures stay NodeUnreachableError below.
            raise RpcTimeoutError(
                f"Bitcoin Core RPC '{method}' timed out after "
                f"{effective_timeout:g} seconds.",
                timeout=effective_timeout,
            ) from None
        except httpx.HTTPError:
            raise NodeUnreachableError(NODE_UNREACHABLE_MESSAGE) from None

        status = getattr(resp, "status_code", 200)
        if status == 401:
            raise RpcAuthenticationError("Bitcoin Core authentication failed.")
        if status == 403:
            raise RpcForbiddenError("Bitcoin Core RPC access is forbidden.")
        if status not in (200, 500):
            raise RpcError("Bitcoin Core returned an unexpected HTTP response.")

        try:
            data = resp.json()
        except (ValueError, RecursionError):
            # RecursionError: deeply nested JSON from a hostile node (SEC-09).
            raise RpcError("Invalid response from Bitcoin Core.") from None

        if not isinstance(data, dict):
            raise RpcError("Invalid response from Bitcoin Core.")
        if "error" in data and data["error"] is not None:
            err = data["error"]
            if not isinstance(err, dict):
                raise RpcError("Invalid response from Bitcoin Core.")
            code = err.get("code")
            # Never pass untrusted RPC text/data into logs, APIs or persisted
            # job errors. Preserve the index hints used by capability detection.
            raw = str(err.get("message", "")).lower()
            message = "Bitcoin Core RPC request failed."
            for index in ("blockfilterindex", "txospenderindex", "txindex"):
                if index in raw:
                    message = f"Bitcoin Core {index} unavailable or not synced."
                    break
            else:
                if "filter index" in raw or "filterindex" in raw:
                    message = "Bitcoin Core blockfilterindex unavailable or not synced."
                elif "in progress" in raw or "already" in raw and "scan" in raw:
                    message = "Bitcoin Core scan already in progress."
                elif "index" in raw:
                    message = "Bitcoin Core index unavailable or not synced."
            raise RpcError(message, code=code if type(code) is int else None)

        if status != 200 or "result" not in data:
            raise RpcError("Invalid response from Bitcoin Core.")

        return data.get("result")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Node / network
    # ------------------------------------------------------------------

    async def get_network_info(self) -> dict[str, Any]:
        return await self._call("getnetworkinfo")

    async def get_blockchain_info(self) -> dict[str, Any]:
        return await self._call("getblockchaininfo")

    async def get_index_info(self) -> dict[str, Any]:
        """Returns {index_name: {synced, best_block_height, ...}}."""
        result = await self._call("getindexinfo")
        if isinstance(result, dict):
            return result
        return {}

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    async def get_raw_transaction(
        self, txid: str, verbose: bool = True, blockhash: str | None = None
    ) -> Any:
        params: list[Any] = [txid, verbose]
        if blockhash:
            params.append(blockhash)
        return await self._call("getrawtransaction", params)

    async def get_raw_transactions_batch(
        self, txids: list[str], verbose: bool = True
    ) -> dict[str, Any]:
        """Fetch multiple raw transactions with bounded concurrency."""
        results: dict[str, Any] = {}
        unique = list(dict.fromkeys(txids))
        for txid in unique:
            try:
                results[txid] = await self.get_raw_transaction(txid, verbose=verbose)
            except RpcError as exc:
                # If txindex is unavailable, prevouts of old txs cannot load.
                logger.warning("could not load prevout transaction %s: %s", txid, exc)
                results[txid] = None
        return results

    async def get_txout(
        self, txid: str, vout: int, include_mempool: bool = False
    ) -> dict[str, Any] | None:
        return await self._call(
            "gettxout", [txid, vout, include_mempool]
        )

    async def get_tx_spending_prevout(
        self, outpoints: list[tuple[str, int]], mempool_only: bool = False
    ) -> list[dict[str, Any]]:
        """Returns a list of {txid, vout, spendingtxid?} for the given outpoints.

        ``mempool_only`` is passed explicitly (default ``false``) so the
        txospenderindex resolves confirmed spends as well as mempool spends.
        """
        if not outpoints:
            return []
        params = [
            [{"txid": t, "vout": v} for t, v in outpoints],
            # Bitcoin Core 31 (and later) requires the second positional
            # parameter to be an *options object*; a bare boolean was rejected
            # ("JSON value of type bool is not of expected type object").
            {"mempool_only": mempool_only},
        ]
        return await self._call("gettxspendingprevout", params)

    async def scantxoutset(
        self,
        action: str = "start",
        descriptors: list[str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Scan the node's UTXO set for a set of output descriptors.

        Only used to discover *candidate* continuation outputs for registered
        addresses (custody-bridge candidates) and as the wallet-discovery
        fallback. Always a read-only query.

        ``timeout`` is the per-request timeout in seconds; ``None`` keeps the
        normal ``settings.rpc_timeout`` (30s). The wallet-discovery fallback
        passes the long ``settings.discovery_rpc_timeout`` explicitly because
        a historical derivation scan may legitimately take minutes.
        """
        return await self._call(
            "scantxoutset", [action, descriptors or []], timeout=timeout
        )

    async def scanblocks(
        self,
        descriptors: list[str],
        start_height: int,
        stop_height: int,
        blockfilter_type: str = "basic",
    ) -> dict[str, Any]:
        """Scan basic block filters for matching output descriptors.

        Bitcoin Core RPC wire format (action first, then scan objects, then
        heights, then filter type):

            scanblocks "start" ["addr(...)", ...] <start_height> <stop_height> "basic"

        ``options`` is only appended when a caller needs it; by default no
        options object is sent. Returns the Core response shape
        ``{from_height, to_height, relevant_blocks, completed}`` (NOTE:
        ``relevant_blocks``, never ``blockhashes``). Used by one-time wallet
        discovery to find which blocks contain transactions touching
        *derived* addresses. Privacy: the extended key is never sent to the
        node, only concrete ``addr()`` descriptors for already-derived
        addresses.
        """
        # Historical wallet discovery is the only consumer of scanblocks.
        # A bounded chain scan can take minutes, so the long per-request
        # timeout is applied here (not the normal 30s RPC default).
        return await self._call(
            "scanblocks",
            [
                "start",
                descriptors,
                start_height,
                stop_height,
                blockfilter_type,
            ],
            timeout=self.settings.discovery_rpc_timeout,
        )

    async def scanblocks_start(
        self,
        descriptors: list[str],
        start_height: int,
        stop_height: int,
        blockfilter_type: str = "basic",
    ) -> dict[str, Any]:
        """Run a ``scanblocks`` scan against Bitcoin Core.

        Wire format matches the native ``scanblocks start`` action:
        ``["start", <scanobjects>, <start_height>, <stop_height>, <filtertype>]``
        with Bitcoin Core's scan "start" semantics:

        IMPORTANT: on Bitcoin Core as observed ``scanblocks start`` is
        SYNCHRONOUS - it does NOT acknowledge immediately. The call only
        returns its final result after the scan completes or is aborted
        (``{from_height, to_height, relevant_blocks, completed}``, or
        ``completed=false`` after an abort). It MUST therefore run in a
        dedicated background task so the HTTP/browser request is never blocked
        (REVERSE_ADDRESS_DISCOVERY_LIFECYCLE_FIX_REQUIREMENTS.md).

        Uses the long discovery timeout like the pre-lifecycle address-discovery
        call: a bounded chain scan may legitimately take minutes, and this call
        blocks until Core answers.
        """
        return await self._call(
            "scanblocks",
            [
                "start",
                descriptors,
                start_height,
                stop_height,
                blockfilter_type,
            ],
            timeout=self.settings.discovery_rpc_timeout,
        )

    async def scanblocks_status(self) -> Optional[dict[str, Any]]:
        """Query the progress/state of the node's currently active scanblocks.

        Native active shape: ``{progress: <percent>, current_height: <height>}``.
        Only the original start response contains the result. Status has no
        scan identity and must never be used to adopt a scan after restart.

        IMPORTANT: when Core has NO active blockfilter scan it returns ``null``,
        so this method yields ``None`` (observed on Bitcoin Core). Callers must treat
        ``None`` as ``{"running": False}`` and MUST never call ``.get()`` on a
        ``None`` result. AddressScanManager centralizes this via
        ``_core_scan_status()``.
        """
        return await self._call("scanblocks", ["status"])

    async def scanblocks_abort(self) -> bool:
        """True means abort requested, not necessarily already stopped."""
        return await self._call("scanblocks", ["abort"])

    async def get_block_count(self) -> int:
        return await self._call("getblockcount")

    # ------------------------------------------------------------------
    # Blocks
    # ------------------------------------------------------------------

    async def get_block_hash(self, height: int) -> str:
        return await self._call("getblockhash", [height])

    async def get_block_header(self, blockhash: str, verbose: bool = False) -> Any:
        return await self._call("getblockheader", [blockhash, verbose])

    async def get_block(self, blockhash: str, verbosity: int = 1) -> Any:
        return await self._call("getblock", [blockhash, verbosity])
