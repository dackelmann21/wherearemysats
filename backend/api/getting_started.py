"""Getting-started checklist for the dashboard (beta package B4).

``GET /api/getting-started`` is read-only and fast: the node status that
``/api/health`` reads plus three ``EXISTS`` queries. It never computes the
dashboard report. Node errors give 200 with ``unknown`` node steps.
Labels and hints live in the frontend; this endpoint reports states and facts.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from backend.api.reports import checked_node_status
from backend.api.runtime import state
from backend.rpc.bitcoin import RpcError

router = APIRouter()

DONE, TODO, IN_PROGRESS, UNKNOWN = "done", "todo", "in_progress", "unknown"


def _step(key: str, state: str, required: bool = True, **facts: Any) -> dict[str, Any]:
    return dict(key=key, state=state, required=required, **facts)


def _index_step(name: str, status: dict[str, Any] | None) -> dict[str, Any]:
    if status is None:
        return _step(name, UNKNOWN)
    present = any(idx["name"] == name for idx in status["indexes"])
    synced = bool(status.get(f"{name}_synced"))
    return _step(name, DONE if synced else IN_PROGRESS if present else TODO, present=present,
                 progress=status.get(f"{name}_progress"),
                 indexed_height=status.get(f"{name}_indexed_height"),
                 target_height=status.get(f"{name}_target_height"))


async def _exists(db, sql: str) -> bool:
    cursor = await db.db.execute(f"SELECT EXISTS ({sql})")
    return bool((await cursor.fetchone())[0])


@router.get("/api/getting-started")
async def getting_started() -> dict[str, Any]:
    srv = state()
    try:
        status = await checked_node_status(srv.engine)
    except RpcError as exc:
        # node_status() catches only the first call; a later failure is the
        # same "node not usable right now" state.
        status = {"reachable": False, "rpc_error": exc.message}
    connected = bool(status.get("reachable") and status.get("network_match"))
    node = _step("node", DONE if connected else TODO, reachable=bool(status.get("reachable")),
                 network=status.get("network"), expected_network=status.get("expected_network"),
                 network_match=status.get("network_match"), reason=status.get("rpc_error"))
    # Without a connected node of the configured network, the node facts are not known.
    usable = status if connected else None
    blockchain = (_step("blockchain", DONE if usable["blockchain_synced"] else IN_PROGRESS,
                        block_height=usable.get("block_height"))
                  if usable else _step("blockchain", UNKNOWN))
    db = srv.db
    wallet = await _exists(db, "SELECT 1 FROM known_addresses WHERE key_access = 1")
    acquisition = await _exists(db, "SELECT 1 FROM acquisitions")
    exit_ = await _exists(db, "SELECT 1 FROM exchange_exits")
    steps = [node, blockchain, _index_step("txindex", usable), _index_step("txospenderindex", usable),
             _step("wallet", DONE if wallet else TODO),
             _step("acquisition", DONE if acquisition else TODO),
             _step("exit", DONE if exit_ else TODO, required=False)]
    return {"all_set": all(s["state"] == DONE for s in steps if s["required"]), "steps": steps}
