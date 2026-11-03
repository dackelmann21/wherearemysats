"""One process owns WAMS scanning; Core remains the arbiter across applications.

No addresses or watch keys are stored here. A reservation covers all passes of
a logical wallet scan. Losing a request never implies that Core stopped: every
new start checks native status, and busy/errors fail closed. No restart adoption.
"""

import asyncio
from contextlib import asynccontextmanager

from backend.rpc.bitcoin import RpcError


class ScanBusy(RpcError):
    def __init__(self):
        super().__init__("Bitcoin Core scan already in progress. Wait for it to finish.")


def normalize_status(raw):
    if raw is None:
        return {"running": False}
    if (not isinstance(raw, dict) or type(raw.get("progress")) not in (int, float)
            or not 0 <= raw["progress"] <= 100
            or type(raw.get("current_height")) is not int):
        raise RpcError("Invalid Bitcoin Core scan status.")
    return {"running": True, "progress": raw["progress"],
            "current_height": raw["current_height"]}


class ScanCoordinator:
    def __init__(self, rpc):
        self.rpc = rpc
        self.owner = None
        self.in_start = False

    @property
    def busy(self):
        return self.owner is not None

    @asynccontextmanager
    async def reserve(self):
        task = asyncio.current_task()
        if self.owner is task:
            yield
            return
        if self.busy:
            raise ScanBusy()
        self.owner = task
        try:
            yield
        finally:
            self.in_start = False
            self.owner = None

    async def status(self):
        return normalize_status(await self.rpc.scanblocks_status())

    async def run(self, operation):
        async with self.reserve():
            try:
                status = await self.status()
            except RpcError as exc:
                # Let start report capability failures through the existing
                # fallback policy. All transport/protocol failures stay errors.
                if exc.code != -32601 and "blockfilterindex" not in exc.message:
                    raise
            else:
                if status["running"]:
                    raise ScanBusy()
            self.in_start = True
            try:
                return await operation()
            finally:
                self.in_start = False

    async def abort_owned(self, task):
        # Only an explicit Stop for the live start task can request abort.
        # A persisted job or status sample alone never establishes ownership.
        if task is None or task.done() or self.owner is not task or not self.in_start:
            return False
        result = await self.rpc.scanblocks_abort()
        if type(result) is not bool:
            raise RpcError("Invalid Bitcoin Core scan abort response.")
        return result


def coordinator_for(rpc):
    coordinator = getattr(rpc, "_scan_coordinator", None)
    if coordinator is None:
        coordinator = rpc._scan_coordinator = ScanCoordinator(rpc)
    return coordinator
