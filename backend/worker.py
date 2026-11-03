"""Bounded background executor for Blockchain Analysis walk jobs.

BLOCKCHAIN_ANALYSIS_ASYNC_UX_PERFORMANCE_FIX_REQUIREMENTS.md: the expensive
spend-state walk (RPC-bound, can fan out to hundreds of outpoints) is moved
off the synchronous HTTP request path into this worker. Each job runs in a
dedicated OS thread owning its own asyncio event loop and thread-bound
Database / ConfiguredBitcoinRPCClient / TraceEngine / BlockchainAnalysisWalker, so the
walk can never block the FastAPI request handlers. No Redis / Celery /
external infra: the job ledger is the SQLite ``analysis_jobs`` table and the
bounded worker pool is a ``ThreadPoolExecutor`` sized from
``analysis_max_concurrent_jobs`` (default 1).

The walker's traversal semantics are frozen: the worker calls the same
``rewalk`` / ``follow`` methods the synchronous path used, only with an
optional progress callback that persists lightweight counters.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from backend.config import Settings
from backend.models.schemas import TraceMode
from backend.persistence.database import Database
from backend.rpc.configured import ConfiguredBitcoinRPCClient
from backend.tracing.analysis import BlockchainAnalysisWalker
from backend.tracing.engine import TraceEngine
from backend.tracing.reverse_analysis import ReverseBlockchainAnalysisWalker
from backend.tracing.reverse_provenance import ReverseProvenanceWalker

logger = logging.getLogger("lineage.analysis_worker")

# Terminal job states the browser stops polling on.
ACTIVE_ANALYSIS_STATES = ("QUEUED", "RUNNING")

_STALE_INTERRUPT_MESSAGE = (
    "Analysis was interrupted by an application restart. "
    "Use Re-check / continue to resume."
)

_SANITIZED_ERROR_LIMIT = 200


def sanitize_error_message(exc: BaseException) -> str:
    """Return a short, user-facing error line (never a stack trace)."""
    message = " ".join(str(exc).split())
    if not message:
        message = "Analysis failed with an unknown error."
    if len(message) > _SANITIZED_ERROR_LIMIT:
        message = message[:_SANITIZED_ERROR_LIMIT].rstrip() + "..."
    return message


class AnalysisWorker:
    """Bound, background executor for BLOCKCHAIN_ANALYSIS jobs."""

    def __init__(
        self,
        settings: Settings,
        max_concurrent_jobs: int | None = None,
        db_path: str | None = None,
        rpc_factory: Callable[[Settings], Any] | None = None,
        walker_factory: Callable[[TraceEngine], BlockchainAnalysisWalker] | None = None,
    ) -> None:
        self.settings = settings
        self._max_concurrent = max(
            1, max_concurrent_jobs if max_concurrent_jobs is not None else settings.analysis_max_concurrent_jobs
        )
        self._db_path = db_path or settings.database_path
        self._rpc_factory = rpc_factory
        self._walker_factory = walker_factory
        self._executor: ThreadPoolExecutor | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the bounded worker pool (idempotent)."""
        if self._executor is not None:
            return
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_concurrent,
            thread_name_prefix="analysis",
        )
        logger.info("analysis worker online (max_concurrent_jobs=%d)", self._max_concurrent)

    def shutdown(self, wait: bool = True) -> None:
        """Stop the worker pool. ``wait=False`` lets queued work exit fast."""
        if self._executor is not None:
            self._executor.shutdown(wait=wait)
            self._executor = None
            logger.info("analysis worker offline")

    @property
    def running(self) -> bool:
        return self._executor is not None

    def submit(
        self,
        job_id: int,
        trace_id: int,
        operation: str,
        follow_target: tuple[str, int] | None = None,
    ) -> None:
        """Queue a persisted job for background execution (non-blocking)."""
        if self._executor is None:
            raise RuntimeError("analysis worker is not running")
        self._executor.submit(
            self._run_job_sync, job_id, trace_id, operation, follow_target
        )
        logger.info(
            "analysis job %d queued trace=%d operation=%s",
            job_id,
            trace_id,
            operation,
        )

    # ------------------------------------------------------------------
    # Job execution
    # ------------------------------------------------------------------

    def _run_job_sync(
        self,
        job_id: int,
        trace_id: int,
        operation: str,
        follow_target: tuple[str, int] | None,
    ) -> None:
        try:
            asyncio.run(
                self._run_job_async(job_id, trace_id, operation, follow_target)
            )
        except Exception:  # pragma: no cover - defensive only
            logger.exception("analysis worker crashed while running job %d", job_id)

    async def _run_job_async(
        self,
        job_id: int,
        trace_id: int,
        operation: str,
        follow_target: tuple[str, int] | None,
    ) -> None:
        # Each job gets a fresh thread-bound stack: Database, RPC client,
        # engine and walker are created inside THIS thread's event loop so no
        # aiosqlite / httpx object is shared across loops.
        db = Database(self._db_path)
        await db.connect()
        try:
            rpc = self._make_rpc()
            engine = TraceEngine(
                db, rpc, max_depth=self.settings.max_trace_depth
            )
            trace = await db.get_trace(trace_id)
            trace_mode = trace.trace_mode if trace is not None else None
            walker = self._build_walker(engine, trace_mode)

            await db.update_analysis_job_state(job_id, "RUNNING")
            logger.info("analysis job %d running trace=%d operation=%s", job_id, trace_id, operation)

            async def on_progress(transactions: int, outpoints: int, hop: int) -> None:
                await db.update_analysis_job_progress(job_id, transactions, outpoints, hop)
                if outpoints % 25 == 0 or outpoints == 0:
                    logger.info(
                        "analysis job %d progress transactions=%d outpoints=%d hop=%d",
                        job_id, transactions, outpoints, hop,
                    )

            if operation == "FOLLOW" and follow_target is not None:
                walk = await walker.follow(
                    trace_id,
                    follow_target[0],
                    follow_target[1],
                    progress_callback=on_progress,
                )
            else:
                walk = await walker.rewalk(trace_id, progress_callback=on_progress)

            # Reverse walkers report predecessors without available history;
            # Forward Analysis has no such key and keeps NULL (P1-3c, SK-3).
            if "unresolved" in walk:
                await db.set_analysis_job_unresolved_count(
                    job_id, len(set(walk["unresolved"]))
                )
            if walk.get("ending_condition", "").endswith("LIMIT_REACHED"):
                await db.update_analysis_job_state(job_id, "LIMIT_REACHED")
                logger.info(
                    "analysis job %d limit_reached trace=%d reason=%r",
                    job_id, trace_id, walk.get("limit_reason"),
                )
            else:
                await db.update_analysis_job_state(job_id, "COMPLETE")
                logger.info("analysis job %d complete trace=%d", job_id, trace_id)
        except Exception as exc:  # noqa: BLE001 - terminal state for any failure
            # Commit the trace-level outcome BEFORE the terminal job state so
            # a poller seeing FAILED can never observe a still-quoted error state.
            # ERROR regardless of the previous status (P1-3b, SK-3): a failed
            # re-check / follow on a finished trace has already deleted and only
            # partly rebuilt its edges, so it must never keep claiming COMPLETE.
            trace = await db.get_trace(trace_id)
            if trace is not None:
                await db.update_trace(trace_id, status="ERROR")
            await db.update_analysis_job_state(
                job_id, "FAILED", error_summary=sanitize_error_message(exc)
            )
            logger.error(
                "analysis job %d failed trace=%d: %s",
                job_id, trace_id, sanitize_error_message(exc),
            )
        finally:
            await db.close()

    def _make_rpc(self) -> Any:
        if self._rpc_factory is not None:
            return self._rpc_factory(self.settings)
        # Default: network + IBD guard, same as the request path (P1-4, D-03).
        return ConfiguredBitcoinRPCClient(self.settings)

    def _build_walker(self, engine: TraceEngine, trace_mode: str | None) -> BlockchainAnalysisWalker:
        if self._walker_factory is not None:
            return self._walker_factory(engine)
        if trace_mode == TraceMode.REVERSE_PROVENANCE:
            return ReverseProvenanceWalker(
                engine,
                max_hops=self.settings.reverse_provenance_max_hops,
                max_transactions=self.settings.reverse_provenance_max_transactions,
                max_outpoints=self.settings.reverse_provenance_max_outpoints,
            )
        if trace_mode == TraceMode.BLOCKCHAIN_ANALYSIS_REVERSE:
            return ReverseBlockchainAnalysisWalker(
                engine,
                max_hops=self.settings.analysis_max_hops,
                max_transactions=self.settings.analysis_max_transactions,
                max_outpoints=self.settings.analysis_max_outpoints,
            )
        return BlockchainAnalysisWalker(
            engine,
            max_hops=self.settings.analysis_max_hops,
            max_transactions=self.settings.analysis_max_transactions,
            max_outpoints=self.settings.analysis_max_outpoints,
        )


async def recover_stale_analysis_jobs(db: Database) -> int:
    """Mark QUEUED/RUNNING jobs left by a previous process as FAILED.

    Runs once at application startup. Traces of interrupted RUNNING jobs are
    marked ERROR regardless of their previous status (an interrupted re-check
    / follow leaves a partly rebuilt graph behind a COMPLETE trace, P1-3b).
    A QUEUED job never touched the graph, so its trace only becomes ERROR
    when it was left IN_PROGRESS. Either way the UI offers a re-check.
    """
    stale = await db.list_stale_analysis_jobs()
    for job in stale:
        await db.update_analysis_job_state(
            job["id"], "FAILED", error_summary=_STALE_INTERRUPT_MESSAGE
        )
        trace = await db.get_trace(job["trace_id"])
        if trace is not None and (
            job["state"] == "RUNNING" or trace.status == "IN_PROGRESS"
        ):
            await db.update_trace(job["trace_id"], status="ERROR")
    if stale:
        logger.warning(
            "marked %d stale analysis job(s) FAILED after restart", len(stale)
        )
    return len(stale)
