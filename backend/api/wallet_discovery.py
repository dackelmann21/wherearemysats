"""No-store, bounded input API. Validation errors never echo watch material."""

from fastapi import APIRouter, Request

from backend.api.http_common import ApiError, body, reply, same_origin_ok
from backend.discovery.service import DiscoveryConfig
from backend.discovery.wallet_scan import SessionError
from backend.models.schemas import WalletScanRequest, WalletScanConfirmation


def create_wallet_discovery_router(manager):
    router = APIRouter(prefix="/api/discovery/sessions")

    @router.post("")
    async def start(request: Request):
        payload = None
        try:
            payload = await body(request, WalletScanRequest)
            return reply(await manager().start(DiscoveryConfig(**payload.model_dump())), 202)
        except (ApiError, SessionError) as exc:
            return reply({"detail": str(exc)}, exc.status)
        finally:
            if payload:
                payload.key = ""

    @router.get("/{handle}")
    async def status(handle: str):
        try:
            return reply(await manager().status(handle))
        except SessionError as exc:
            return reply({"detail": str(exc)}, exc.status)

    @router.post("/{handle}/commit")
    async def commit(handle: str, request: Request):
        try:
            payload = await body(request, WalletScanConfirmation)
            return reply(await manager().commit(handle, payload.revision))
        except (ApiError, SessionError) as exc:
            return reply({"detail": str(exc)}, exc.status)

    @router.delete("/{handle}")
    async def discard(handle: str, request: Request):
        if not same_origin_ok(request):
            return reply({"detail": "Use same-origin requests."}, 400)
        await manager().discard(handle)
        return reply({"discarded": True,
                      "message": "Pending result discarded. Bitcoin Core may still be finishing its scan."})

    return router
