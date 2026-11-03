"""Local-operator report API; no public artifacts, bounded cancellable export jobs."""
import asyncio
import re
import threading
import time
import uuid
from collections import Counter
import aiosqlite

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, StrictBool
from typing import Literal

from backend.acquisitions.exchange_exit_service import refresh_unconfirmed_exit_blocks
from backend.api.http_common import ApiError, body, reply, same_origin_ok
from backend.api.security_input import canonical_txid
from backend.reporting.dashboard import Reports, select, summary, utc_now, FILTERS
from backend.reporting.origin_resolution import PROVENANCE_CLASSES
from backend.reporting.read_data import ReadBudgetExceeded, rows
from backend.reporting.blockchain_history import build_report
from backend.reporting.blockchain_history_html import render_report
from backend.config import get_settings
from backend.reporting.csv_exports import ExportValidationError
from backend.reporting.exit_provenance_v2 import (build_records, exit_year, filename_for, validate_years,
                                                  write_csv as write_exit_csv)

EXIT_V2 = 'exit-provenance-v2'


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    dataset: Literal['exit-provenance-v2'] = EXIT_V2
    wallet: str | None = None
    include_free_text: StrictBool = False
    # 'all' or a non-empty list of UTC block-time years (required).
    years: list[int] | Literal['all'] | None = None


def _canonical_outpoint(value):
    """`TXID:VOUT` detail filter with the canonical lower-case TXID, or None."""
    txid, sep, vout = value.partition(':')
    if not sep or not re.fullmatch(r'\d+', vout):
        return None
    try:
        return f'{canonical_txid(txid)}:{vout}'
    except ValueError:
        return None


def create_dashboard_router(context, reports=None):
    router = APIRouter()
    reports, jobs = reports or Reports(), {}

    async def report():
        try:
            return await reports.get(*context())
        except (ReadBudgetExceeded, TimeoutError, aiosqlite.Error):
            raise HTTPException(503, 'Report unavailable: evidence changed or execution limit reached. Retry.')

    def scope(snapshot, wallet, filters=None, kind='full-report'):
        try:
            return select(snapshot, wallet, filters, kind)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    def query(request, extra=()):
        allowed = {'wallet'} | set(extra)
        if set(request.query_params) - allowed or any(len(request.query_params.getlist(k)) != 1 for k in request.query_params):
            raise HTTPException(400, 'Unknown or repeated report parameter.')

    def public_ids(value, field=''):
        # SQLite identities can exceed JavaScript's exact integer range. Keep
        # identifiers lossless in this additive API; canonical sats stay numeric.
        if isinstance(value, dict):
            return {k: public_ids(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [public_ids(v, 'id' if field in ('acquisition_ids', 'linked_exchange_exit_ids', 'documented_path_acquisition_ids') else '') for v in value]
        if isinstance(value, int) and not isinstance(value, bool) and (field == 'id' or field.endswith('_id')):
            return str(value)
        return value

    @router.get('/api/reports/workspace')
    async def workspace(request: Request):
        query(request, {'part', 'offset', 'limit', 'position', 'review'})
        q = request.query_params
        part = q.get('part', 'provenance')
        if part not in ('provenance', 'coverage', 'acquisitions', 'counts', 'no-acquisition'):
            raise HTTPException(400, 'Unknown workspace part.')
        if ('position' in q or 'review' in q) and part != 'acquisitions':
            raise HTTPException(400, 'Position and review filters require Acquisition records.')
        try:
            offset, limit = int(q.get('offset', 0)), int(q.get('limit', 1000))
            if offset < 0 or not 1 <= limit <= 1000:
                raise ValueError()
        except ValueError:
            raise HTTPException(400, 'Invalid workspace pagination.')
        if part == 'no-acquisition':
            try:
                receipts = await reports.get_receipts(*context())
            except (ReadBudgetExceeded, TimeoutError, aiosqlite.Error):
                raise HTTPException(503, 'Wallet entry evidence unavailable. Retry.')
            records = receipts['records']
            if q.get('wallet'):
                snapshot = await report()
                scope(snapshot, q['wallet'])
                records = [r for r in records if r['wallet_name'] == q['wallet']]
            return reply(public_ids(dict(receipts, records=records[offset:offset+limit], total=len(records))))
        snapshot = await report()
        selected = scope(snapshot, q.get('wallet'), {k: q[k] for k in ('position', 'review') if k in q},
                         'acquisitions' if part == 'acquisitions' else 'full-report')
        positions, acquisitions = selected['positions'], selected['acquisitions']
        metadata = dict(snapshot_id=snapshot['snapshot_id'], data_as_of_utc=snapshot['data_as_of_utc'],
                        stale=snapshot['stale'], scope=selected['wallet'])
        if part == 'acquisitions':
            return reply(public_ids(dict(metadata, records=acquisitions)))
        if part == 'counts':
            counts = {'total': len(acquisitions)}
            for a in acquisitions:
                counts[a['position_state']] = counts.get(a['position_state'], 0) + 1
            counts['ACQUISITION_NOT_IN_CURRENT_WALLET'] = sum(
                a['wallet_position'].get('state') == 'ACQUISITION_NOT_IN_CURRENT_WALLET' for a in acquisitions)
            return reply(dict(metadata, **counts))
        if part == 'coverage':
            return reply(public_ids(dict(metadata, inventory=snapshot['inventory'],
                acquisition_wallet_positions=[dict(a['wallet_position'], acquisition_id=a['id']) for a in acquisitions],
                documented_not_in_current_wallet_utxos=[], legacy_documentation_status='NOT_REQUESTED')))
        classes = {c: dict(utxo_count=0, amount_sats=0) for c in PROVENANCE_CLASSES}
        for p in positions:
            classes[p['provenance_class']]['utxo_count'] += 1
            classes[p['provenance_class']]['amount_sats'] += p['amount_sats']
        return reply(public_ids(dict(metadata, population='CURRENT_WALLET_INVENTORY',
            inventory=snapshot['inventory'], positions=positions[offset:offset+limit],
            filtered_count=len(positions), utxo_count=len(positions), total_sats=sum(p['amount_sats'] for p in positions),
            class_totals=classes, acquisition_path_coverage=dict(status='NOT_REQUESTED', percentage_bps=None))))

    @router.get('/api/dashboard')
    async def dashboard(request: Request):
        query(request)
        try:
            return reply(await reports.get_summary(*context(), wallet=request.query_params.get('wallet')))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except (ReadBudgetExceeded, TimeoutError, aiosqlite.Error):
            raise HTTPException(503, 'Report unavailable: evidence changed or execution limit reached. Retry.')

    @router.get('/api/reports/exit-years')
    async def exit_years(request: Request):
        """Years (UTC block time) with confirmed Exits and their counts, newest first."""
        query(request)
        if not same_origin_ok(request):
            # B5-10: like the other report endpoints; the FX-8 back-fill below writes.
            raise HTTPException(400, 'Use same-origin requests.')
        await refresh_unconfirmed_exit_blocks(*context())  # FX-8: before the year filter
        snapshot = await report()
        selected = scope(snapshot, request.query_params.get('wallet'), None, 'exchange-exits')
        counts, unconfirmed = Counter(), 0
        for e in selected['exits']:
            year = exit_year(e)
            if year is None:
                unconfirmed += 1
            else:
                counts[year] += max(1, len(e['selected_outputs']))  # Exit identity is TXID:VOUT
        return reply(dict(snapshot_id=snapshot['snapshot_id'], data_as_of_utc=snapshot['data_as_of_utc'],
                          scope=selected['wallet'], unconfirmed_exits=unconfirmed,
                          years=[dict(year=y, exit_count=counts[y]) for y in sorted(counts, reverse=True)]))

    @router.get('/api/reports/view')
    async def view(request: Request):
        query(request, {'dataset', 'offset', 'limit', 'outpoint', 'acquisition_id', 'exit_id', 'review_type'} | set(FILTERS))
        q = request.query_params
        dataset = q.get('dataset', 'current-wallet')
        if dataset not in ('current-wallet', 'acquisitions', 'origins', 'exchange-exits', 'review'):
            raise HTTPException(400, 'Unknown report dataset.')
        try:
            offset, limit = int(q.get('offset', 0)), int(q.get('limit', 25))
            if offset < 0 or not 1 <= limit <= 100:
                raise ValueError()
        except ValueError:
            raise HTTPException(400, 'Invalid pagination.')
        snapshot = await report()
        selected = scope(snapshot, q.get('wallet'), {k: q[k] for k in FILTERS if k in q},
                         'acquisitions' if dataset == 'acquisitions' else 'exchange-exits' if dataset == 'exchange-exits' else 'full-report')
        report_summary, queue = summary(snapshot, selected)
        records = selected[{'current-wallet': 'positions', 'acquisitions': 'acquisitions',
                            'origins': 'anchors', 'exchange-exits': 'exits'}.get(dataset, 'positions')] if dataset != 'review' else queue
        for name, expected, field in [('outpoint', 'current-wallet', 'outpoint'), ('acquisition_id', 'acquisitions', 'id'), ('exit_id', 'exchange-exits', 'id')]:
            if name in q:
                value = _canonical_outpoint(q[name]) if name == 'outpoint' else q[name]
                valid = value is not None if name == 'outpoint' else re.fullmatch(r'[1-9][0-9]*', q[name])
                if dataset != expected or not valid:
                    raise HTTPException(400, 'Invalid detail filter.')
                records = [r for r in records if str(r[field]) == value]
        if 'review_type' in q:
            if dataset != 'review' or q['review_type'] not in ('CURRENT_UTXO', 'ACQUISITION'):
                raise HTTPException(400, 'Invalid review filter.')
            records = [r for r in records if r['review_item_type'] == q['review_type']]
        page = records[offset:offset+limit]
        if dataset == 'current-wallet' and 'outpoint' not in q:
            # Main pages stay compact even when one UTXO has thousands of origins.
            hidden = {'acquisition_ids', 'documented_path_acquisition_ids', 'no_acquisition_anchor_outpoints',
                      'anchors', 'origins', 'historical_wallet_evidence'}
            page = [dict({k: v for k, v in r.items() if k not in hidden},
                         known_acquisition_count=len(set(r['acquisition_ids'])),
                         known_no_acquisition_anchor_count=len(set(r['no_acquisition_anchor_outpoints']))) for r in page]
        return reply(dict(snapshot_id=snapshot['snapshot_id'], data_as_of_utc=snapshot['data_as_of_utc'],
                          stale=snapshot['stale'], scope=selected['wallet'], filters=selected['filters'],
                          wallets=snapshot['wallets'],
                          total=len(records), value_sats=sum(r['amount_sats'] for r in records) if dataset == 'current-wallet' else None,
                          offset=offset, limit=limit, records=public_ids(page)))

    async def remove(handle):
        job = jobs.pop(handle, None)
        if job:
            job['cancel'].set()
            if job.get('expiry') and job['expiry'] is not asyncio.current_task():
                job['expiry'].cancel()
            if job.get('stage') == 'READING' and job.get('task'):
                job['task'].cancel()
            if job.get('file'):
                job['file'].close()

    async def expire(handle):
        await asyncio.sleep(300)
        await remove(handle)

    def public(job):
        return {k: v for k, v in job.items() if k in ('id', 'state', 'dataset', 'scope', 'filters', 'years', 'include_free_text',
            'snapshot_id', 'data_as_of_utc', 'exported_at_utc', 'row_counts', 'size_bytes', 'error', 'filename')}

    async def run_exit_v2(job, payload, db, engine):
        """Exit-Provenance CSV v2: read-only evaluator projection, never a partial file."""
        await refresh_unconfirmed_exit_blocks(db, engine)  # FX-8: before the year filter
        snapshot = await reports.get(db, engine)
        selected = scope(snapshot, payload.wallet, {}, 'exchange-exits')
        job.update(snapshot_id=snapshot['snapshot_id'], data_as_of_utc=snapshot['data_as_of_utc'])
        job['stage'] = 'WRITING'
        settings, exported = get_settings(), utc_now()
        async with asyncio.timeout(settings.coverage_timeout):
            records = await build_records(db, engine, settings, time.monotonic() + settings.coverage_timeout,
                years=None if payload.years == 'all' else payload.years,
                exit_ids=None if payload.wallet is None else {e['id'] for e in selected['exits']},
                include_free_text=payload.include_free_text, exported_at=exported,
                data_revision=snapshot['snapshot_id'])
        filename = filename_for(None if payload.years == 'all' else payload.years)
        artifact, count, size = await asyncio.to_thread(write_exit_csv, records, payload.include_free_text, job['cancel'])
        if job['cancel'].is_set():
            artifact.close()
            return
        job.update(file=artifact, row_counts={filename: count}, size_bytes=size, exported_at_utc=exported,
                   state='READY', filename=filename)

    async def run(job, payload, db, engine):
        try:
            await run_exit_v2(job, payload, db, engine)
        except Exception:
            job.update(state='FAILED', error='Export unavailable or invalid scope/filter. Retry after refreshing the report.')

    async def known_wallet(wallet):
        wallets = await rows(context()[0], 'SELECT wallet_name FROM known_addresses UNION SELECT wallet_name FROM address_evidence')
        return wallet in {r['wallet_name'] or '' for r in wallets}

    @router.get('/api/reports/blockchain-history', response_class=HTMLResponse)
    async def blockchain_history(request: Request):
        """B1: printable report page (server-rendered), same selection as the Exit CSV export."""
        query(request, {'years', 'include_free_text'})
        if not same_origin_ok(request):
            raise HTTPException(400, 'Use same-origin requests.')
        q = request.query_params
        raw_years = q.get('years')
        if raw_years == 'all':
            years = None
        elif raw_years and re.fullmatch(r'\d{4}(,\d{4})*', raw_years):
            years = [int(y) for y in raw_years.split(',')]
        else:
            raise HTTPException(400, 'Select at least one year or all years.')
        try:
            validate_years(years)
        except ExportValidationError as exc:
            raise HTTPException(400, str(exc))
        if q.get('include_free_text', '0') not in ('0', '1'):
            raise HTTPException(400, 'Invalid free-text option.')
        wallet = q.get('wallet')
        if wallet is not None and not await known_wallet(wallet):
            raise HTTPException(400, 'Unknown wallet scope.')
        db, engine = context()
        await refresh_unconfirmed_exit_blocks(db, engine)  # FX-8: before the year filter
        snapshot = await report()
        selected = scope(snapshot, wallet, {}, 'exchange-exits')
        settings = get_settings()
        try:
            async with asyncio.timeout(settings.coverage_timeout):
                data = await build_report(
                    db, engine, settings, time.monotonic() + settings.coverage_timeout, years=years,
                    exit_ids=None if wallet is None else {e['id'] for e in selected['exits']},
                    include_free_text=q.get('include_free_text') == '1', scope=wallet,
                    created_at=utc_now(), data_revision=snapshot['snapshot_id'])
        except (ReadBudgetExceeded, TimeoutError, aiosqlite.Error):
            raise HTTPException(503, 'Report unavailable: evidence changed or execution limit reached. Retry.')
        return HTMLResponse(render_report(data), headers={
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
            # B5-14: no scripts or external resources; inline CSS and SVG only.
            'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; img-src data:"})

    @router.post('/api/exports', status_code=202)
    async def create(request: Request):
        try:
            payload = await body(request, ExportRequest)
        except ApiError:
            return reply({'detail': 'Invalid same-origin export request.'}, 400)
        try:
            if payload.years is None:
                raise ExportValidationError('Select at least one year or all years.')
            validate_years(None if payload.years == 'all' else payload.years)
        except ExportValidationError as exc:
            return reply({'detail': str(exc)}, 400)
        if payload.wallet is not None and not await known_wallet(payload.wallet):
            return reply({'detail': 'Unknown wallet scope.'}, 400)
        if len(jobs) >= 2:
            return reply({'detail': 'Two export jobs already exist. Cancel or download one first.'}, 429)
        handle = uuid.uuid4().hex
        job = dict(id=handle, state='PREPARING', dataset=payload.dataset, scope=payload.wallet,
                   filters={}, years=payload.years, include_free_text=payload.include_free_text,
                   cancel=threading.Event(),
                   owner=tuple(id(v) for v in context()), stage='READING')
        jobs[handle] = job
        job['task'] = asyncio.create_task(run(job, payload, *context()))
        job['expiry'] = asyncio.create_task(expire(handle))
        return reply(public(job), 202)

    def lookup(handle):
        job = jobs.get(handle)
        if not job or job['owner'] != tuple(id(v) for v in context()):
            raise HTTPException(404, 'Export job not found or expired.')
        return job

    @router.get('/api/exports/{handle}')
    async def status(handle: str):
        return reply(public(lookup(handle)))

    @router.delete('/api/exports/{handle}')
    async def cancel(handle: str, request: Request):
        if not same_origin_ok(request):
            raise HTTPException(400, 'Use same-origin requests.')
        lookup(handle)
        await remove(handle)
        return reply({'state': 'CANCELLED'})

    @router.get('/api/exports/{handle}/download')
    async def download(handle: str):
        job = lookup(handle)
        if job['state'] != 'READY':
            raise HTTPException(409, 'Export is not ready.')
        job['state'] = 'DOWNLOADING'
        job['expiry'].cancel()
        async def chunks():
            try:
                while chunk := await asyncio.to_thread(job['file'].read, 65536):
                    yield chunk
            finally:
                await remove(handle)
        return StreamingResponse(chunks(), media_type='application/zip' if job['filename'].endswith('.zip') else 'text/csv; charset=utf-8',
            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                     'Content-Disposition': 'attachment; filename="' + job['filename'] + '"'})

    return router
