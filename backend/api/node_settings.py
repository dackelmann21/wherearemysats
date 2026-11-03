"""Write-only-secret Node Settings API, separate from domain persistence."""

import asyncio
import json
from dataclasses import replace

from fastapi import APIRouter, Request

from backend.api.http_common import reply, same_origin_ok
from backend.node_settings import CHAINS, INVALID, NodeSettingsError
from backend.rpc.bitcoin import (
    BitcoinRPCClient, NodeUnreachableError, RpcAuthenticationError,
    RpcError, RpcForbiddenError, RpcTimeoutError,
)


async def probe(node, settings):
    client = BitcoinRPCClient(replace(node.apply(settings), rpc_timeout=10))
    try:
        async with asyncio.timeout(10):
            info = await client.get_blockchain_info()
        # Return a small typed allowlist, never an arbitrary node response.
        if (not isinstance(info, dict) or info.get("chain") not in CHAINS.values()
                or type(info.get("blocks")) is not int or info["blocks"] < 0
                or type(info.get("initialblockdownload")) is not bool):
            raise RpcError("Invalid blockchain info")
        result = {"network": info["chain"], "block_height": info["blocks"],
                  "initial_block_download": info["initialblockdownload"]}
        if info["chain"] != CHAINS[node.network]:
            return result | {"ok": False, "state": "wrong_network",
                             "message": "Connected, but Bitcoin network does not match the selected network."}
        if info["initialblockdownload"]:
            return result | {"ok": True, "state": "initial_block_download",
                             "message": "Connected. Initial block download is still running; Bitcoin-dependent operations are not ready."}
        return result | {"ok": True, "state": "connected", "message": "Connected."}
    except RpcAuthenticationError:
        state, message = "authentication_failed", "RPC authentication failed. Check username and password."
    except RpcForbiddenError:
        state, message = "forbidden", "RPC access forbidden. Check Bitcoin Core access restrictions."
    except (RpcTimeoutError, TimeoutError):
        state, message = "timeout", "Bitcoin Core did not respond within 10 seconds."
    except NodeUnreachableError:
        state, message = "unreachable", "Cannot reach Bitcoin Core. Check host, port, DNS, TLS and firewall."
    except Exception:
        state, message = "rpc_error", "Bitcoin Core returned an invalid response or an RPC error."
    finally:
        await client.close()
    return {"ok": False, "state": state, "message": message}


def create_node_settings_router(store, active, on_saved=None):
    router = APIRouter(prefix="/api/node-settings")
    lock = asyncio.Lock()

    @router.get("")
    async def read_settings():
        return reply(store.view(active))

    async def candidate(request):
        # Require same-origin JSON + a custom header. No CORS permission is
        # granted: another website cannot submit saved-secret tests or changes.
        if (request.headers.get("x-wams-settings") != "1"
                or request.headers.get("content-type", "").split(";")[0] != "application/json"
                or not same_origin_ok(request)):
            raise NodeSettingsError("Use same-origin Node Settings requests with JSON and X-WAMS-Settings: 1.")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 16384:
                raise NodeSettingsError(INVALID)
        try:
            data = json.loads(body)
        except (ValueError, UnicodeError):
            raise NodeSettingsError(INVALID) from None
        if not isinstance(data, dict):
            raise NodeSettingsError(INVALID)
        return store.candidate(data)

    @router.post("/test")
    async def test_connection(request: Request):
        try:
            node = await candidate(request)
        except NodeSettingsError as exc:
            return reply({"ok": False, "state": "invalid_configuration", "detail": str(exc)}, 400)
        return reply(await probe(node, active))

    @router.put("")
    async def save_settings(request: Request):
        async with lock:
            try:
                node = await candidate(request)
                store.save(node)
                if on_saved is not None:
                    await on_saved()
            except NodeSettingsError as exc:
                return reply({"ok": False, "detail": str(exc)}, 400)
        return reply(store.view(active) | {"saved": True,
                     "message": "Saved. Restart WAMS to activate changes for all workers."})

    return router
