"""Reference-counted Coverage requests: share work, never retain a stale score."""

import asyncio
from dataclasses import dataclass
from backend.reporting.read_data import ReadBudgetExceeded


class CoverageDisconnected(Exception):
    pass


@dataclass
class Flight:
    task: asyncio.Task
    waiters: int = 0
    scope: object = None


class CoverageRequests:
    def __init__(self):
        self.flights = {}

    async def run(self, key, factory, request, *, scope=None):
        if scope is not None:
            for old_key, old in list(self.flights.items()):
                if old.scope == scope and old_key != key and not old.task.done():
                    old.task.cancel()  # DB revision changed: obsolete, even for other tabs.
        flight = self.flights.get(key)
        if flight is None or flight.task.done():
            flight = Flight(asyncio.create_task(factory()), scope=scope)
            self.flights[key] = flight
        flight.waiters += 1
        stopped = False

        async def disconnected():
            # Starlette's is_disconnected() can swallow the cancel below (it
            # polls inside an already cancelled anyio scope), so the loop also
            # ends on the flag instead of relying on that cancel alone.
            while not await request.is_disconnected():
                if stopped:
                    return
                await asyncio.sleep(0.05)

        watcher = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait([flight.task, watcher], return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                raise CoverageDisconnected
            if flight.task.cancelled():
                raise ReadBudgetExceeded("Coverage evidence changed; retry.")
            return flight.task.result()
        finally:
            stopped = True
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            flight.waiters -= 1
            if not flight.waiters:
                if self.flights.get(key) is flight:
                    del self.flights[key]
                flight.task.cancel()
                await asyncio.gather(flight.task, return_exceptions=True)
