"""Current wallet set, independent of Acquisitions and of trace creation.

Wallet definitions currently come from the canonical address registry/evidence.
The boundary consumes scripts/addresses, not keys: future descriptor providers
can supply the same facts without a second inventory or provenance engine.
Historical per-outpoint ownership is not current wallet membership.
"""
import asyncio

from backend.discovery.service import WalletDiscovery
from backend.discovery.scan_coordinator import coordinator_for
from backend.persistence.inventory import SCAN_DEFINITIONS, save_refresh
from backend.reporting.read_data import rows


class InventoryRefresh:
    """One bounded background refresh, sharing the existing scan reservation."""
    def __init__(self, db, rpc):
        self.db, self.rpc = db, rpc
        self.task = None
        self.generation = 0
        self.restart_required = False
        self.state = "IDLE"
        self.message = "No refresh running. Showing the last saved snapshot; refresh to check for new receipts."

    def status(self):
        return {"state": self.state, "message": self.message}

    def start(self):
        from backend.discovery.wallet_scan import SessionError
        if self.restart_required:
            raise SessionError("Restart WAMS to activate the saved node settings before refreshing inventory.")
        if (self.task and not self.task.done()) or coordinator_for(self.rpc).busy:
            raise SessionError("A WAMS scan is already running. Wait for it to finish.")
        self.state, self.message = "RUNNING", "Refreshing known wallet scripts."
        self.task = asyncio.create_task(self._run(self.generation))
        return self.status()

    async def _run(self, generation):
        try:
            async with asyncio.timeout(7200), coordinator_for(self.rpc).reserve():
                definitions = await rows(self.db, SCAN_DEFINITIONS)
                info = await self.rpc.get_blockchain_info()
                if info.get("initialblockdownload") is not False:
                    raise ValueError("Node is not synced")
                # Registry import is mainnet-only; discovery also records network.
                networks = {d["network"] for d in await self.db.list_wallet_discoveries()}
                if info.get("chain") != "main" and networks != {"testnet"}:
                    raise ValueError("Wallet/node network mismatch")
                if definitions:
                    scanner = WalletDiscovery(self.rpc, self.db)
                    scan = await scanner.scan_current_scripts([d["address"] for d in definitions])
                    snapshot = await scanner.inventory_snapshot(scan["transactions"],
                        [d["address"] for d in definitions], scan["height"], scan["bestblock"], "scantxoutset")
                    if generation != self.generation:
                        raise ValueError("Scan context changed")
                    await save_refresh(self.db, definitions, snapshot)
                self.state, self.message = "COMPLETE", "Inventory refreshed. New addresses require wallet discovery."
        except asyncio.CancelledError:
            self.state, self.message = "FAILED", "Refresh interrupted. Previous snapshot retained; Core may still be scanning."
        except Exception:
            # RPC exceptions may contain sensitive addresses or connection data.
            self.state, self.message = "FAILED", "Refresh failed or its context changed. Previous snapshot retained; retry with a synced local node."

    async def invalidate(self):
        self.generation += 1
        if self.task and not self.task.done():
            self.task.cancel()
            await self.task

    async def close(self):
        await self.invalidate()
