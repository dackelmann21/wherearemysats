"""Local-operator, same-origin, non-echoing annotation API."""

import aiosqlite
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from typing import Literal

from backend.api.http_common import ApiError, body, reply, same_origin_ok, unavailable
from backend.api.security_input import canonical_txid
from backend.persistence.database import Database
from backend.reporting import no_acquisition as model
from backend.reporting.read_data import ReadBudgetExceeded, revision
from backend.reporting.csv_exports import provenance_csv


class AnnotationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0, strict=True)
    source: StrictStr | None = Field(default=None, max_length=200)
    note: StrictStr | None = Field(default=None, max_length=4000)


class ResolutionPut(BaseModel):
    model_config = ConfigDict(extra='forbid')
    type: Literal['NO_ACQUISITION']
    comment: StrictStr | None = Field(default=None, max_length=4000)


def key_for(txid, vout):
    try:
        txid = canonical_txid(txid)
    except ValueError:
        raise HTTPException(400, "Invalid outpoint.") from None
    if vout < 0:
        raise HTTPException(400, "Invalid outpoint.")
    return txid, vout


def create_provenance_router(context, coverage_endpoint, check_sensitive):
    router = APIRouter(prefix="/api/provenance")

    @router.get('/origins/{txid}/{vout}')
    async def origin(txid: str, vout: int):
        try:
            return reply(await model.origin_detail(*context(), key_for(txid, vout)))
        except (ReadBudgetExceeded, TimeoutError) as exc:
            return unavailable('Origin evidence unavailable', exc)

    @router.put('/origins/{txid}/{vout}/resolution')
    @router.delete('/origins/{txid}/{vout}/resolution')
    async def resolution(txid: str, vout: int, request: Request):
        key = key_for(txid, vout)
        removing = request.method == 'DELETE'
        try:
            if removing:
                if not same_origin_ok(request):
                    raise ApiError('Use same-origin requests.', 400)
            else:
                payload = await body(request, ResolutionPut, max_bytes=65536)
                check_sensitive(payload.comment, 'comment')
        except ApiError:
            return reply({'detail': 'Invalid same-origin origin resolution request (comment ≤4000 characters).'}, 400)
        except HTTPException as exc:
            return reply({'detail': exc.detail}, exc.status_code)
        db, engine = context()
        isolated = Database(db.path)
        async with aiosqlite.connect(db.path) as conn:
            conn.row_factory = aiosqlite.Row
            isolated.db = conn
            await conn.execute('PRAGMA busy_timeout=5000')
            try:
                await conn.execute('BEGIN IMMEDIATE')
                if removing:
                    # Removing review never removes Acquisition or annotation data,
                    # and remains possible even when lineage is unavailable.
                    await conn.execute('DELETE FROM provenance_origin_resolutions WHERE txid=? AND vout=?', key)
                else:
                    current = await model.origin_detail(isolated, engine, key)
                    if current['origin_resolution']['type'] == 'ACQUISITION':
                        return reply({'detail': 'This outpoint is already linked to Acquisition #' +
                                      str(current['origin_resolution']['acquisition_id']) + '.'}, 409)
                    if not current['can_resolve_no_acquisition']:
                        return reply({'detail': 'Review the individual lineage origins; this output is not a resolvable origin.'}, 409)
                    await conn.execute('''INSERT INTO provenance_origin_resolutions(txid,vout,resolution_type,comment)
                        VALUES(?,?,'NO_ACQUISITION',?) ON CONFLICT(txid,vout) DO UPDATE SET
                        comment=excluded.comment,updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')''',
                        (*key, payload.comment))
                await conn.commit()
            except aiosqlite.IntegrityError:
                return reply({'detail': 'Origin resolution conflicts with an Acquisition.'}, 409)
            except (ReadBudgetExceeded, TimeoutError, aiosqlite.OperationalError):
                return reply({'detail': 'Origin resolution unavailable; retry.'}, 503)
            finally:
                await conn.rollback()
        from backend.reporting.origin_resolution import OriginResolutions
        return reply((await OriginResolutions.load(db)).resolve(key))

    @router.get("")
    @router.get("/export.csv")
    async def listing(request: Request,
        provenance_class: Literal["ACQUISITION", "NO_ACQUISITION", "MIXED", "UNRESOLVED"] | None = None,
        position_state: Literal["CURRENT", "PARTIALLY_DISPOSED", "DISPOSED", "NEEDS_REVIEW", "TRACING"] | None = None,
        origin_type: Literal['ACQUISITION', 'NO_ACQUISITION', 'UNRESOLVED'] | None = None,
        coverage_status: Literal['COVERED', 'MIXED', 'UNCOVERED', 'UNRESOLVED'] | None = None,
        wallet: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)):
        db, engine = context()
        before = await revision(db)
        coverage = await coverage_endpoint(request, Response(), None, None)
        if isinstance(coverage, Response):
            return coverage
        try:
            if await revision(db) != before:
                raise ReadBudgetExceeded("Evidence changed.")
            exporting = request.url.path.endswith("/export.csv")
            result = await model.positions(db, engine, coverage,
                provenance_class=provenance_class, position_state=position_state, wallet=wallet,
                origin_type=origin_type, coverage_status=coverage_status,
                offset=0 if exporting else offset, limit=coverage.utxo_count if exporting else limit)
            if exporting:
                return Response(provenance_csv(result), media_type="text/csv; charset=utf-8", headers={
                    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                    "Content-Disposition": 'attachment; filename="wams-provenance-v1.csv"'})
            return reply(result)
        except (ReadBudgetExceeded, TimeoutError) as exc:
            return unavailable("Provenance unavailable", exc)

    @router.get("/anchors/{txid}/{vout}")
    async def detail(txid: str, vout: int):
        key = key_for(txid, vout)
        try:
            result = await model.anchor_detail(*context(), key)
            return reply(result if result else {"detail": "Anchor not found in this scope."}, 200 if result else 404)
        except (ReadBudgetExceeded, TimeoutError) as exc:
            return unavailable("Anchor evidence unavailable", exc)

    @router.get("/anchors")
    async def history():
        db, engine = context()
        try:
            saved = await model.annotations(db, engine)
            return reply([model.anchor_record(key, saved) for key in sorted(saved)])
        except ReadBudgetExceeded as exc:
            return unavailable("Annotation history unavailable", exc)

    @router.get("/positions/{txid}/{vout}")
    async def position_detail(txid: str, vout: int, request: Request):
        key = key_for(txid, vout)
        db, engine = context()
        before = await revision(db)
        coverage = await coverage_endpoint(request, Response(), None, None)
        if isinstance(coverage, Response):
            return coverage
        try:
            if await revision(db) != before:
                raise ReadBudgetExceeded("Evidence changed.")
            result = await model.positions(db, engine, coverage, limit=coverage.utxo_count)
            item = next((r for r in result["positions"] if r["outpoint"] == model.outpoint(key)), None)
            return reply(item if item else {"detail": "Current wallet position not found."}, 200 if item else 404)
        except (ReadBudgetExceeded, TimeoutError) as exc:
            return unavailable("Provenance evidence unavailable", exc)

    @router.patch("/anchors/{txid}/{vout}")
    async def edit(txid: str, vout: int, request: Request):
        key = key_for(txid, vout)
        try:
            # 4200 Unicode characters can exceed the discovery body's 16 KiB
            # cap, especially when JSON uses surrogate-pair escapes.
            payload = await body(request, AnnotationPatch, max_bytes=65536)
            check_sensitive(payload.source, "source")
            check_sensitive(payload.note, "note")
        except ApiError:
            return reply({"detail": "Invalid same-origin JSON annotation request (source ≤200, note ≤4000 characters)."}, 400)
        except HTTPException as exc:
            return reply({"detail": exc.detail}, exc.status_code)
        db, engine = context()
        # Dedicated connection: authorization and revision check are protected
        # by the same SQLite write lock as the update, without intermediate commits.
        isolated = Database(db.path)
        async with aiosqlite.connect(db.path) as conn:
            conn.row_factory = aiosqlite.Row
            isolated.db = conn
            await conn.execute("PRAGMA busy_timeout=5000")
            try:
                await conn.execute("BEGIN IMMEDIATE")
                current = await model.anchor_detail(isolated, engine, key)
                if current is None:
                    return reply({"detail": "Anchor not found in this scope."}, 404)
                if payload.revision != current["revision"]:
                    return reply({"detail": "Annotation changed; reload before saving."}, 409)
                changes = payload.model_dump(exclude_unset=True)
                source = changes.get("source", current["source"])
                note = changes.get("note", current["note"])
                source = None if source is None or not source.strip() else source
                note = None if note is None or not note.strip() else note
                await conn.execute("""INSERT INTO no_acquisition_annotations(network,txid,vout,source,note)
                    VALUES(?,?,?,?,?) ON CONFLICT(network,txid,vout) DO UPDATE SET
                    source=excluded.source,note=excluded.note,revision=revision+1,
                    updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
                    (model.network_scope(engine), *key, source, note))
                await conn.commit()
            except (ReadBudgetExceeded, TimeoutError):
                return reply({"detail": "Anchor evidence unavailable; retry."}, 503)
            except aiosqlite.OperationalError as exc:
                # Same message as the global SQLite-busy handler (server.py).
                if "database is locked" in str(exc):
                    return reply({"detail": "Database busy, please retry."}, 503)
                return reply({"detail": "Annotation storage is temporarily unavailable; retry."}, 503)
            finally:
                await conn.rollback()
        return await detail(*key)

    return router
