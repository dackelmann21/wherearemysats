"""Reverse-address jobs share Core scan ownership with wallet discovery.

Only the live blocking start task can produce completion. Native status is
percentage/current_height or null; it neither contains results nor proves who
owns a scan. Restart fails interrupted jobs, never adopts or restarts Core work.
"""

import asyncio

from backend.address_discovery import AddressDiscovery, InvalidAddressError
from backend.discovery.scan_coordinator import coordinator_for
from backend.models.schemas import (
    AddressDiscoveryResult, AddressScanJob, AddressScanJobState,
    AddressScanProgress, AddressScanStatus,
)
from backend.registry.address_validation import is_valid_mainnet_address
from backend.rpc.bitcoin import NODE_UNREACHABLE_MESSAGE, NodeUnreachableError, RpcError, RpcTimeoutError

FOREIGN_SCAN_MESSAGE = "Bitcoin Core already has a block scan in progress."
_INTERRUPTED_BEFORE_START = "The address scan was interrupted before it started. Please try again."
_UNREACHABLE_AFTER_RESTART = "The address scan execution was lost on restart. Start a new scan once Core is idle."


def _is_scan_in_progress_error(exc):
    text = exc.message.lower()
    return "in progress" in text or "already" in text and "scan" in text


class AddressScanManager:
    def __init__(self, db, rpc, discovery=None):
        self.db, self.rpc = db, rpc
        self.discovery = discovery or AddressDiscovery(rpc)
        self.coordinator = coordinator_for(rpc)
        self._background_tasks = {}
        self._start_lock = asyncio.Lock()

    async def _core_scan_status(self):
        return await self.coordinator.status()

    async def start_scan(self, address):
        candidate = (address or "").strip()
        if not candidate or not is_valid_mainnet_address(candidate):
            raise InvalidAddressError("not a valid mainnet Bitcoin address")
        async with self._start_lock:
            active = await self.db.get_active_address_discovery_job()
            if active:
                return self._status_for(active)
            if self.coordinator.busy:
                return self._foreign_status({})
            try:
                core = await self._core_scan_status()
            except RpcError as exc:
                if exc.code != -32601 and "blockfilterindex" not in exc.message:
                    raise
                core = {"running": False}
            if core["running"]:
                return self._foreign_status(core)
            tip = await self.rpc.get_block_count()
            job = await self.db.create_address_discovery_job(candidate)
            job = await self.db.update_address_discovery_job_state(
                job["id"], AddressScanJobState.RUNNING,
                progress={"from_height": 0, "to_height": tip})
            task = asyncio.create_task(self._run_scanblocks_start(job["id"], candidate, tip))
            self._background_tasks[job["id"]] = task
            task.add_done_callback(lambda _: self._background_tasks.pop(job["id"], None))
            return self._status_for(await self.db.get_address_discovery_job(job["id"]))

    async def active_status(self):
        active = await self.db.get_active_address_discovery_job()
        if active:
            job = await self.poll_job(active["id"])
            return AddressScanStatus(active=True, job=job)
        try:
            core = await self._core_scan_status()
        except RpcError:
            return AddressScanStatus()
        if core["running"] or self.coordinator.busy:
            return self._foreign_status(core)
        return AddressScanStatus()

    async def poll_job(self, job_id):
        job = await self.db.get_address_discovery_job(job_id)
        if not job:
            return None
        task = self._background_tasks.get(job_id)
        if (job["state"] == AddressScanJobState.RUNNING and task is not None
                and self.coordinator.owner is task and self.coordinator.in_start):
            try:
                core = await self._core_scan_status()
            except NodeUnreachableError:
                raise
            except RpcError:
                core = {"running": False}
            # Idle never implies success. Retain the fixed range from start.
            if core["running"]:
                progress = {**(job.get("progress") or {}),
                            "progress": core["progress"], "current_height": core["current_height"]}
                await self.db.update_address_discovery_job_progress(job_id, progress)
        return self._job_model(await self.db.get_address_discovery_job(job_id))

    async def abort_job(self, job_id):
        job = await self.db.get_address_discovery_job(job_id)
        if not job:
            return None
        if job["state"] == AddressScanJobState.RUNNING:
            await self.coordinator.abort_owned(self._background_tasks.get(job_id))
            # True means requested; only completed=false from start finalizes.
            await asyncio.sleep(0)
        return self._job_model(await self.db.get_address_discovery_job(job_id))

    async def reconcile_on_startup(self):
        stale = await self.db.list_stale_address_discovery_jobs()
        for job in stale:
            await self.db.update_address_discovery_job_state(
                job["id"], AddressScanJobState.FAILED,
                error_summary=(_INTERRUPTED_BEFORE_START if job["state"] == AddressScanJobState.QUEUED
                               else _UNREACHABLE_AFTER_RESTART))
        return len(stale)

    async def close(self):
        tasks = list(self._background_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_scanblocks_start(self, job_id, address, tip):
        try:
            try:
                result = await self.coordinator.run(lambda: self.rpc.scanblocks_start(
                    ["addr(" + address + ")"], 0, tip, "basic"))
            except RpcError as exc:
                from backend.address_discovery import is_core_capability_error
                if not is_core_capability_error(exc):
                    raise
                outputs, source = await self.discovery.resolve_address_outputs(address)
            else:
                if not isinstance(result, dict) or result.get("completed") is not True:
                    await self.db.update_address_discovery_job_state(job_id, AddressScanJobState.CANCELLED)
                    return
                outputs = await self.discovery.resolve_from_blocks(address, result.get("relevant_blocks") or [])
                source = "bitcoin_core_scanblocks"
            await self.db.update_address_discovery_job_state(
                job_id, AddressScanJobState.COMPLETE,
                progress={"from_height": 0, "to_height": tip, "progress": 100, "current_height": tip},
                result={"address": address, "discovery_source": source, "outputs": self._output_models(outputs)})
        except asyncio.CancelledError:
            # Shutdown/lost task does not issue a global Core abort.
            raise
        except Exception as exc:
            if isinstance(exc, NodeUnreachableError):
                message = NODE_UNREACHABLE_MESSAGE + " Its scan may still be running."
            elif isinstance(exc, RpcTimeoutError):
                message = "The scan request timed out. Bitcoin Core may still be scanning."
            elif isinstance(exc, RpcError) and _is_scan_in_progress_error(exc):
                message = FOREIGN_SCAN_MESSAGE
            else:
                message = "Address discovery failed. No completed result is available."
            await self.db.update_address_discovery_job_state(job_id, AddressScanJobState.FAILED, error_summary=message)

    @staticmethod
    def _output_models(outputs):
        return [{**o, "outpoint": f"{o['txid']}:{o['vout']}"} for o in outputs]

    def _job_model(self, job):
        result = None
        if job.get("result") and "outputs" in job["result"]:
            result = AddressDiscoveryResult(**job["result"])
        return AddressScanJob(
            id=job["id"], address=job["address"], state=job["state"],
            created_at=job["created_at"], started_at=job.get("started_at"),
            updated_at=job["updated_at"], completed_at=job.get("completed_at"),
            error_summary=job.get("error_summary"),
            progress=AddressScanProgress(**(job.get("progress") or {})), result=result)

    def _status_for(self, job):
        return AddressScanStatus(active=job["state"] in ("QUEUED", "RUNNING"), job=self._job_model(job))

    def _foreign_status(self, core):
        return AddressScanStatus(foreign_scan=True, foreign_scan_message=FOREIGN_SCAN_MESSAGE,
                                 core_scan=AddressScanProgress(**core))
