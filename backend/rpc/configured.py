"""Application RPC boundary: configured credentials and matching, ready chain."""

import asyncio
import time

from backend.node_settings import CHAINS, NodeConfiguration, NodeSettingsError
from backend.rpc.bitcoin import BitcoinRPCClient, BlockNotSyncedError, NodeUnreachableError


class ConfiguredBitcoinRPCClient(BitcoinRPCClient):
    def __init__(self, settings):
        super().__init__(settings)
        self._ready_until = 0.0
        self._ready_lock = asyncio.Lock()

    def configuration_error(self):
        try:
            NodeConfiguration.from_settings(self.settings)
        except NodeSettingsError:
            return "Bitcoin Core is not configured. Open Node Settings."
        return None

    async def _call(self, method, params=None, timeout=None):
        error = self.configuration_error()
        if error:
            raise NodeUnreachableError(error)
        # Status calls remain possible during IBD. All data operations fail
        # closed on mismatch/IBD; a failed check is never cached as readiness.
        if method not in {"getblockchaininfo", "getnetworkinfo", "getindexinfo"}:
            async with self._ready_lock:
                if time.monotonic() >= self._ready_until:
                    info = await super()._call("getblockchaininfo")
                    expected = CHAINS[NodeConfiguration.from_settings(self.settings).network]
                    if not isinstance(info, dict) or info.get("chain") != expected:
                        raise NodeUnreachableError("Bitcoin Core network does not match Node Settings.")
                    if info.get("initialblockdownload") is not False:
                        raise BlockNotSyncedError("Bitcoin Core is still in initial block download.")
                    self._ready_until = time.monotonic() + 5
        try:
            return await super()._call(method, params, timeout)
        except Exception:
            self._ready_until = 0.0
            raise
