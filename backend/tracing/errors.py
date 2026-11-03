"""Tracing-specific exceptions (consolidation package P3-1).

Kept in a tiny module so the engine and the walkers can share the class
without importing each other.
"""

from __future__ import annotations

from backend.rpc.bitcoin import RpcError


class TraceNotFoundError(RpcError):
    """The referenced trace does not exist (any more).

    Subclass of :class:`RpcError` so every existing ``except RpcError`` path
    (worker job FAILED handling, walkers) keeps behaving identically; the API
    maps it to 404 instead of the generic 400.
    """

    def __init__(self, message: str = "Trace not found."):
        super().__init__(message)
