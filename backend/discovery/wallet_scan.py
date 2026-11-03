"""Bounded RAM-only scan → review → confirmation. No durable pending payloads.

The background coroutine alone owns the input key. Session records contain
only key-free values, and tasks are removed on completion (including failure).
"""

import asyncio
import copy
import re
import secrets
import time
from dataclasses import dataclass, field

from backend.discovery.scan_coordinator import coordinator_for
from backend.discovery.service import DiscoveryError, DiscoveryScanTimeoutError, WalletDiscovery
from backend.persistence.database import DiscoveryRegistryChanged
from backend.rpc.bitcoin import RpcError, RpcTimeoutError
from backend.validation import detect_sensitive_input


class SessionError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


@dataclass
class Session:
    handle: str
    context: object
    generation: int
    expires: float
    state: str = "PREPARING"
    revision: int = 1
    progress: dict = field(default_factory=dict)
    result: object = None
    outcome: object = None
    message: str = ""
    poll_at: float = 0


class WalletScanSessions:
    def __init__(self, rpc, db, *, ttl=900, max_sessions=8, scan_timeout=7200, clock=time.monotonic):
        self.rpc, self.db = rpc, db
        self.ttl, self.max_sessions, self.scan_timeout = ttl, max_sessions, scan_timeout
        self.clock = clock
        self.sessions, self.tasks = {}, {}
        self.lock = asyncio.Lock()
        self.generation = 0
        self.restart_required = False
        self.coordinator = coordinator_for(rpc)
        self._janitor = None

    def context(self):
        # Settings activation replaces the immutable object. Never copy secrets.
        return id(getattr(self.rpc, "settings", self.rpc))

    def _drop(self, handle):
        self.sessions.pop(handle, None)
        task = self.tasks.get(handle)
        if task and not task.done():
            task.cancel()  # no Core abort; its work may still be running

    def _prune(self):
        for handle, session in list(self.sessions.items()):
            if session.expires <= self.clock() or session.context != self.context():
                self._drop(handle)

    async def _sweep(self):
        while True:
            await asyncio.sleep(min(30, self.ttl))
            async with self.lock:
                self._prune()

    def _get(self, handle):
        self._prune()
        session = self.sessions.get(handle)
        if session is None:
            raise SessionError("Discovery session expired, was discarded, or was lost on restart. Scan again.", 410)
        return session

    async def start(self, config):
        async with self.lock:
            if self.restart_required:
                config.key = ""
                raise SessionError("Restart WAMS to activate the saved node settings before scanning wallets.")
            self._prune()
            if len(self.sessions) >= self.max_sessions:
                config.key = ""
                raise SessionError("Too many pending discoveries. Discard a result or wait for expiry.", 429)
            if self.tasks or self.coordinator.busy:
                config.key = ""
                raise SessionError("A WAMS scan is already running. Wait for it to finish.")
            # Validate metadata before it can reach a durable evidence row.
            for value in (config.wallet_name, config.label):
                if (len(value) > 200 or detect_sensitive_input(value)
                        or re.search(r"(?:xpub|ypub|zpub|tpub|upub|vpub)[1-9A-HJ-NP-Za-km-z]{20,}", value)):
                    config.key = ""
                    raise SessionError("Wallet name or label contains invalid or sensitive material.", 400)
            if detect_sensitive_input(config.key):
                config.key = ""
                raise SessionError("Only an extended public key is accepted; never enter private keys or seeds.", 400)
            handle = secrets.token_urlsafe(32)
            session = Session(handle, self.context(), self.generation, self.clock() + self.scan_timeout)
            self.sessions[handle] = session
            task = asyncio.create_task(self._scan(session, config))
            self.tasks[handle] = task
            task.add_done_callback(lambda _: self.tasks.pop(handle, None))
            if self._janitor is None:
                self._janitor = asyncio.create_task(self._sweep())
            return self._view(session)

    def _progress(self, session, **fields):
        session.state = fields.pop("stage", session.state)
        session.progress.update(fields)

    async def _scan(self, session, config):
        try:
            async with asyncio.timeout(self.scan_timeout):
                service = WalletDiscovery(self.rpc, self.db, lambda **f: self._progress(session, **f))
                plan = service.build_plan(config)
                if plan.key_type == "xpub" and config.script_type == "auto":
                    raise DiscoveryError("Select the script type used by this wallet before scanning a generic xpub.")
                if plan.script_type == "p2tr":
                    raise DiscoveryError("Taproot discovery is not supported by the current derivation implementation.")
                info = await self.rpc.get_blockchain_info()
                expected = "main" if plan.network == "mainnet" else "test"
                if info.get("chain") != expected or info.get("initialblockdownload") is not False:
                    raise DiscoveryError("Wallet key network must match a fully synced node.")
                # Freeze range and anchor before scanning; new blocks are fine.
                start, stop, _ = await service._resolve_range(config)
                config.start_height, config.end_height = start, stop
                anchor = await self.rpc.get_block_hash(stop)
                plan, matched, meta, warnings = await service.scan(config)
                if await self.rpc.get_block_hash(stop) != anchor:
                    raise DiscoveryError("The scanned chain changed. Scan again before importing.")
                plan.key_hint, plan.fingerprint, plan.preview_addresses = "", "", []
                if session.context != self.context() or session.generation != self.generation:
                    return
                session.result = (plan, matched, meta, anchor)
                session.state = "READY"
                session.expires = self.clock() + self.ttl
                session.revision += 1
        except asyncio.CancelledError:
            # Discard/shutdown never claims that Core was aborted.
            session.result = None
        except Exception as exc:
            session.state = "FAILED"
            session.result = None
            session.expires = self.clock() + self.ttl
            if isinstance(exc, (RpcTimeoutError, DiscoveryScanTimeoutError, TimeoutError)):
                session.message = "The scan request timed out. Bitcoin Core may still be scanning; nothing was imported."
            elif isinstance(exc, DiscoveryError):
                text = str(exc)
                session.message = ("Wallet discovery failed. Nothing was imported."
                    if re.search(r"[xtyzuv]pub", text) and "generic xpub" not in text else text)
            elif isinstance(exc, RpcError):
                session.message = "Bitcoin Core could not complete this request (it may be busy or unavailable). Nothing was imported."
            else:
                session.message = "Wallet discovery failed. Nothing was imported."
            # Never store/log the exception or traceback: it can hold key locals.
        finally:
            config.key = ""

    def _view(self, session):
        view = {"handle": session.handle, "state": session.state, "revision": session.revision,
                "expires_in": max(0, int(session.expires - self.clock())),
                "progress": dict(session.progress), "message": session.message}
        if session.result is not None:
            plan, matches, meta, _ = session.result
            view["review"] = {"wallet_name": plan.wallet_name, "label": plan.label,
                "network": plan.network, "script_type": plan.script_type, "scan_mode": plan.scan_mode,
                "start_height": plan.start_height, "stop_height": plan.end_height,
                "gap_limit": plan.gap_limit, "warnings": list(plan.warnings),
                "matched": copy.deepcopy(matches), **{k: v for k, v in meta.items() if k != "inventory"}}
            for branch in (0, 1):
                items = [m for m in matches if m["branch_index"] == branch]
                view["review"][f"branch{branch}_found"] = len(items)
                view["review"][f"branch{branch}_last_index"] = max(
                    (m["address_index"] for m in items), default=-1)
        if session.outcome is not None:
            view["outcome"] = dict(session.outcome)
        return view

    async def status(self, handle):
        async with self.lock:
            session = self._get(handle)
            task = self.tasks.get(handle)
            if (session.state == "SCANNING" and session.progress.get("scan_mode") == "scanblocks"
                    and self.coordinator.owner is task and self.coordinator.in_start
                    and self.clock() >= session.poll_at):
                session.poll_at = self.clock() + 1
                try:
                    core = await self.coordinator.status()
                    if core["running"] and session.state == "SCANNING":
                        session.progress.update(progress=core["progress"], current_height=core["current_height"])
                except RpcError:
                    session.progress.update(progress=None, current_height=None)
            return self._view(session)

    async def commit(self, handle, revision):
        async with self.lock:
            session = self._get(handle)
            if session.state == "COMMITTED" and revision == session.revision:
                return self._view(session)
            if session.state != "READY" or session.revision != revision:
                raise SessionError("This discovery is not ready or its review revision changed.")
            plan, matched, meta, anchor = session.result
            try:
                info = await self.rpc.get_blockchain_info()
                expected = "main" if plan.network == "mainnet" else "test"
                if (info.get("chain") != expected or info.get("initialblockdownload") is not False
                        or await self.rpc.get_block_hash(plan.end_height) != anchor):
                    self._drop(handle)
                    raise SessionError("The scanned chain/node context changed. Scan again.")
                self._get(handle)  # expiry/context may have changed during RPC
                session.state = "COMMITTING"
                # The task survives an HTTP disconnect: publish outcome exactly
                # once after the isolated transaction commits.
                task = asyncio.create_task(self._persist(session, plan, matched, meta))
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
            except DiscoveryRegistryChanged:
                session.result = (plan, await WalletDiscovery(self.rpc, self.db)._annotate_existing(matched), meta, anchor)
                session.revision += 1
                session.state = "READY"
                raise SessionError("Registry membership changed. Review the updated results and confirm again.") from None
            except SessionError:
                raise
            except Exception:
                session.state = "READY"
                raise SessionError("Import could not complete. Nothing was imported; retry this review.", 503) from None
            return self._view(session)

    async def _persist(self, session, plan, matched, meta):
        session.outcome = await self.db.import_wallet_result(plan, matched, meta)
        session.result = None
        session.state = "COMMITTED"
        session.expires = self.clock() + self.ttl

    async def discard(self, handle):
        async with self.lock:
            self._drop(handle)

    def invalidate_locked(self):
        self.generation += 1
        for handle in list(self.sessions):
            self._drop(handle)

    async def invalidate(self):
        async with self.lock:
            self.invalidate_locked()

    async def close(self):
        await self.invalidate()
        tasks = list(self.tasks.values())
        if self._janitor:
            self._janitor.cancel()
            tasks.append(self._janitor)
        await asyncio.gather(*tasks, return_exceptions=True)
