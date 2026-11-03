"""Minimal, isolated client for a *local* Electrs (Electrum protocol).

Reverse-mode address discovery (REVERSE_PROVENANCE /
BLOCKCHAIN_ANALYSIS_REVERSE) must never call external services (REQ privacy:
no mempool.space / Blockstream / public Electrum / other explorers). This
module talks only to an on-box Electrs that the operator runs locally
(Umbrel ships Bitcoin Core + electrs). Bitcoin Core's own ``scanblocks``
against the blockfilter index is preferred; this client is an optional
fallback and is dead code unless ``ELECTRS_URL`` is configured.

Protocol: JSON-RPC over newline-delimited TCP (Electrum x1.x). Only the three
methods address discovery needs are implemented.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional
from urllib.parse import urlparse


# Upper bound for one newline-delimited reply. asyncio's default (64 KiB)
# is too small for the history of a busy address; beyond this bound the reply
# is a protocol error (SEC-10).
LINE_LIMIT = 16 * 1024 * 1024


class ElectrsUnreachableError(Exception):
    """The local Electrs could not be reached or produced a protocol error.

    Messages are fixed texts: they reach the 503 detail and the operator log,
    so they never contain Electrs-supplied text or the configured host:port.
    """


class ElectrsClient:
    """Thin async Electrum-protocol client for a configured *local* Electrs.

    ``url`` accepts ``tcp://host:port``, ``ssl://host:port`` or ``host:port``.
    An empty url disables the client entirely: every method raises
    ``ElectrsUnreachableError`` and no connection is ever attempted.
    """

    def __init__(self, url: str = "", timeout: float = 10.0):
        self.url = (url or "").strip()
        self.timeout = timeout
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_id = 1
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def _host_port(self) -> tuple[str, int, bool]:
        raw = self.url
        use_tls = False
        if "://" in raw:
            parsed = urlparse(raw)
            host = parsed.hostname or "127.0.0.1"
            port = parsed.port or 50001
            use_tls = parsed.scheme == "ssl"
        else:
            host_part, _, port_part = raw.rpartition(":")
            host = host_part or "127.0.0.1"
            try:
                port = int(port_part or 50001)
            except ValueError:
                raise ElectrsUnreachableError("invalid ELECTRS_URL")
        return host, port, use_tls

    async def _connect(self) -> None:
        if not self.enabled:
            raise ElectrsUnreachableError("ELECTRS_URL is not configured")
        host, port, use_tls = self._host_port()
        try:
            if use_tls:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port, ssl=True, limit=LINE_LIMIT), self.timeout
                )
            else:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port, limit=LINE_LIMIT), self.timeout
                )
        except (OSError, asyncio.TimeoutError) as exc:
            raise ElectrsUnreachableError("cannot reach the local Electrs") from exc
        self._reader, self._writer = reader, writer

    async def _request(self, method: str, params: list[Any]) -> Any:
        async with self._lock:
            if self._writer is None or self._writer.is_closing():
                await self._connect()
            req_id = self._next_id
            self._next_id += 1
            payload = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            self._writer.write((payload + "\n").encode())
            await self._writer.drain()
            try:
                line = await asyncio.wait_for(self._reader.readline(), self.timeout)
            except (asyncio.TimeoutError, ConnectionError) as exc:
                await self.close()
                raise ElectrsUnreachableError(f"Electrs {method} timed out/disconnected") from exc
            except ValueError as exc:  # line longer than LINE_LIMIT
                await self.close()
                raise ElectrsUnreachableError(f"Electrs reply to {method} exceeds the size limit") from exc
            if not line:
                await self.close()
                raise ElectrsUnreachableError(f"Electrs closed connection on {method}")
            try:
                resp = json.loads(line.decode())
            except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                await self.close()
                raise ElectrsUnreachableError(f"Electrs sent non-JSON reply to {method}") from exc
            if not isinstance(resp, dict):
                await self.close()
                raise ElectrsUnreachableError(f"Electrs sent an invalid reply to {method}")
            if resp.get("id") != req_id:
                await self.close()
                raise ElectrsUnreachableError(f"Electrs response id mismatch on {method}")
            if resp.get("error") is not None:
                raise ElectrsUnreachableError(f"Electrs {method} returned an error")
            return resp.get("result")

    async def scripthash_history(self, scripthash: str) -> list[dict[str, Any]]:
        """Return [{tx_hash, height}] for all known transactions touching an
        output script (history + mempool are local to the on-box node)."""
        return await self._request("blockchain.scripthash.get_history", [scripthash]) or []

    async def listunspent(self, scripthash: str) -> list[dict[str, Any]]:
        """Return [{tx_hash, tx_pos, height, value}] for UTXOs paying the
        script. Note: history above is authoritative for the spent side; this
        marks unspent state without touching a block scan."""
        return await self._request("blockchain.scripthash.listunspent", [scripthash]) or []

    async def verbose_transaction(self, txid: str) -> dict[str, Any]:
        """Return the verbose transaction as returned by ``gettransaction``
        with verbose=true, including ``block_height``/``blocktime`` when
        available from the local index."""
        return await self._request("blockchain.transaction.get", [txid, True]) or {}

    async def close(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        self._reader, self._writer = None, None