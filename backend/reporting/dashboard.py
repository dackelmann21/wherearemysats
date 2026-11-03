"""Revision-bound saved-fact reports; all classifications use existing services.

No RPC is performed. Inventory observation time is explicitly separate from export
time. A dedicated SQLite read transaction isolates the complete projection. The
one-entry cache is invalidated by same-connection and worker commits.
"""
import asyncio
import dataclasses
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import urlencode

import aiosqlite

from backend.acquisitions.position import PositionContext, descendant_summary, open_movements
from backend.acquisitions.position_queries import (
    custodial_boundaries, exit_deposit_boundaries_by_acquisition, explained_sources_by_acquisition)
from backend.acquisitions.service import position_state_from_row
from backend.config import get_settings
from backend.persistence.database import Database
from backend.persistence.inventory import metadata, population_keys
from backend.reporting.context import ReportContext
from backend.reporting.csv_exports import btc
from backend.reporting.filters import matches
from backend.reporting.no_acquisition import annotations, anchor_record, positions, network_scope, saved_wallet_observations
from backend.reporting.read_data import ReadData, ReadBudgetExceeded, input_keys, revision, rows, selected_rows
from backend.tracing.engine import TraceEngine

OUTSIDE = 'ACQUISITION_NOT_IN_CURRENT_WALLET'
KINDS = ('current-wallet', 'acquisitions', 'provenance', 'exchange-exits', 'full-report')
POSITION_STATES = ('CURRENT', 'NEEDS_REVIEW', 'PARTIALLY_DISPOSED', 'DISPOSED', 'TRACING', OUTSIDE)
FILTERS = {'coverage': ('COVERED', 'MIXED', 'UNCOVERED', 'UNRESOLVED'),
           'origin_type': ('ACQUISITION', 'NO_ACQUISITION', 'UNRESOLVED'),
           'provenance_class': ('ACQUISITION', 'NO_ACQUISITION', 'MIXED', 'UNRESOLVED'),
           'position': POSITION_STATES,
           # Acquisition records needing review: position_state or wallet membership (review_required).
           'review': ('required',)}
ACQUISITION_FILTERS = {'position', 'review'}


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def path(view, wallet=None, **filters):
    return '/#' + view + '?' + urlencode({k: v for k, v in {'wallet': wallet, **filters}.items() if v is not None})


def validate_filters(kind, filters):
    for key, value in filters.items():
        if key not in FILTERS or value not in FILTERS[key]:
            raise ValueError('Unknown report filter.')
    allowed = (ACQUISITION_FILTERS if kind == 'acquisitions' else set() if kind == 'exchange-exits'
               else set(FILTERS) - ACQUISITION_FILTERS)
    if set(filters) - allowed:
        raise ValueError('Filter is incompatible with this dataset.')


class SavedRPC:
    def __init__(self, outputs, settings):
        self.outputs, self.settings = outputs, settings

    async def get_txout(self, txid, vout, **_):
        out = self.outputs.get((txid, vout))
        if not out or out.get('spend_status') != 'UNSPENT':
            return None
        return {'value': btc(out['amount_sats']), 'confirmations': out.get('confirmations')}


class PositionBatch:
    @classmethod
    async def load(cls, db, engine, *, acquisitions=None):
        self = cls()
        trace_ids = None if acquisitions is None else {a["trace_id"] for a in acquisitions if a.get("trace_id")}
        self.traces = {r['id']: r for r in (await rows(db, 'SELECT id FROM traces') if trace_ids is None
                                           else await selected_rows(db, 'traces', 'id', trace_ids))}
        self.edges, self.bridges = defaultdict(list), defaultdict(set)
        keys = set()
        # Same order as Database.get_edges (P2-N7).
        for e in await selected_rows(db, 'trace_edges', 'trace_id', trace_ids, limit=1_000_000, order_by='hop_number'):
            self.edges[e['trace_id']].append(SimpleNamespace(**e))
            keys.update(((e['source_txid'], e['source_vout']), (e['destination_txid'], e['destination_vout'])))
        for b in await selected_rows(db, 'custody_bridges', 'trace_id', trace_ids):
            if b.get('destination_txid'):
                key = (b['destination_txid'], b['destination_vout'])
                self.bridges[b['trace_id']].add(key)
                keys.add(key)
        for a in (acquisitions if acquisitions is not None else await rows(db, 'SELECT origin_txid,origin_vout FROM acquisitions')):
            if a['origin_txid'] is not None and (acquisitions is None or a.get('trace_id') in self.traces):
                keys.add((a['origin_txid'], a['origin_vout']))
        data = await ReadData.load(db, engine, {k[0] for k in keys}, limit=1_000_000)
        self.by_tx = defaultdict(dict)
        self.stored_by_tx = defaultdict(dict)
        for k, v in data.outputs.items():
            self.by_tx[k[0]][k] = v
        for k, v in data.outpoints.items():
            self.stored_by_tx[k[0]][k] = v
        # Same query source as the per-Acquisition path of descendant_summary (D-15).
        self.global_boundaries = await custodial_boundaries(db)
        self.exit_boundaries = await exit_deposit_boundaries_by_acquisition(db, limit=1_000_000)
        self.explained = await explained_sources_by_acquisition(db, limit=1_000_000)
        return self

    def boundaries(self, aid):
        return self.global_boundaries | self.exit_boundaries[aid]

    def data_for(self, keys):
        txids = {k[0] for k in keys}
        return SimpleNamespace(outputs={k: v for txid in txids for k, v in self.by_tx[txid].items()},
                               outpoints={k: v for txid in txids for k, v in self.stored_by_tx[txid].items()})


def review_reasons(wallet_reasons, descendant, position_state, movements):
    """Needs-Attention reasons for one Acquisition (FIX-UI-7 point 4).

    When open movements exist they are shown instead of the generic review
    sentence (the descendant note), which is therefore dropped here — also
    where the wallet-position reasons repeat it. Other wallet-position reasons
    are kept; nothing but presentation text changes."""
    reasons = list(wallet_reasons)
    if position_state == 'NEEDS_REVIEW' and not movements:
        reasons.append(descendant.note or 'Acquisition position requires review.')
    if movements and descendant.note:
        reasons = [r for r in reasons if r != descendant.note]
    return sorted(set(reasons))


async def build(db, source_engine):
    settings = get_settings()
    deadline = time.monotonic() + max(120, settings.coverage_timeout)
    outputs = {(r['txid'], r['vout']): r for r in await rows(db, '''WITH relevant(txid) AS (
        SELECT txid FROM wallet_inventory_outputs UNION SELECT origin_txid FROM acquisitions
        UNION SELECT source_txid FROM trace_edges UNION SELECT destination_txid FROM trace_edges
        UNION SELECT destination_txid FROM custody_bridges
        UNION SELECT txid FROM no_acquisition_annotations UNION SELECT txid FROM provenance_origin_resolutions)
        SELECT o.*,t.confirmations FROM relevant r JOIN outpoints o ON o.txid=r.txid
        LEFT JOIN transactions t ON t.txid=o.txid''', limit=1_000_000)}
    rpc = SavedRPC(outputs, SimpleNamespace(btc_network=network_scope(source_engine)))
    engine = TraceEngine(db, rpc, max_depth=source_engine.max_depth)
    context = PositionContext(settings)
    context.deadline = deadline
    context.summaries = {}
    spends = {(r['source_txid'], r['source_vout']): r for r in await selected_rows(db, 'spends', 'source_txid',
                {k[0] for k in outputs}, limit=1_000_000)}
    context.saved_facts = {k: {'spend_status': o['spend_status'],
        'spending_txid': spends.get(k, {}).get('spending_txid')} for k, o in outputs.items()}
    context.saved_outputs = {k: {'value': btc(o['amount_sats']), 'confirmations': o.get('confirmations')}
                             for k, o in outputs.items() if o['spend_status'] == 'UNSPENT'}
    context.batch = await PositionBatch.load(db, engine)
    coverage = await saved_wallet_observations(db, engine, settings, deadline,
                                               report=ReportContext(position_context=context))
    # Same row mapping as /api/acquisitions: btc_amount derives from the origin.
    acquisitions = [db._acquisition_from_row(r) for r in await rows(db, 'SELECT * FROM acquisitions ORDER BY id')]
    survey = await db.get_allocation_survey()
    locations = {r['acquisition_id']: r for r in coverage.acquisition_wallet_positions}
    links = defaultdict(set)
    exits = await rows(db, 'SELECT * FROM exchange_exits ORDER BY id')
    sources, selected = defaultdict(list), defaultdict(list)
    for r in await rows(db, 'SELECT * FROM exchange_exit_sources', limit=1_000_000):
        sources[r['exchange_exit_id']].append(r)
        if r['acquisition_id'] is not None:
            links[r['acquisition_id']].add(r['exchange_exit_id'])
    for r in await rows(db, 'SELECT * FROM exchange_exit_outputs WHERE is_exchange_deposit=1'):
        selected[r['exchange_exit_id']].append(r)
    disposals = defaultdict(list)
    for r in await rows(db, '''SELECT d.*,l.acquisition_id,l.allocated_sats,l.disposed_sats,l.network_fee_sats
        FROM disposal_acquisition_links l JOIN disposals d ON d.id=l.disposal_id''', limit=1_000_000):
        disposals[r['acquisition_id']].append(dict(r, disposal_id=r['id']))
    exit_summaries = defaultdict(list)
    for r in await rows(db, '''SELECT e.*,s.acquisition_id,
        SUM(s.disposed_sats) AS disposed_sats,SUM(s.network_fee_sats) AS network_fee_sats,
        SUM(s.disposed_sats+s.network_fee_sats) AS allocated_sats,
        SUM(s.disposed_sats+s.network_fee_sats=0) AS unattributed_inputs
        FROM exchange_exit_sources s JOIN exchange_exits e ON e.id=s.exchange_exit_id
        WHERE s.acquisition_id IS NOT NULL GROUP BY e.id,s.acquisition_id''', limit=1_000_000):
        exit_summaries[r['acquisition_id']].append(dict(r, exit_id=r['id']))
    for a in acquisitions:
        descendant = await descendant_summary(db, engine, a, context)
        a['descendant'] = descendant.model_dump()
        a['allocated_sats'] = survey.get(a['id'], 0)
        a['disposals'] = disposals[a['id']]
        a['disposal_count'] = len(a['disposals'])
        a['exchange_exits'] = exit_summaries[a['id']]
        a['exchange_exit_count'] = len(a['exchange_exits'])
        a['position_state'] = position_state_from_row(a, survey.get(a['id'], 0), descendant)
        a['wallet_position'] = locations.get(a['id'], {})
        a['current_outpoints'] = sorted(set(a['wallet_position'].get('current_outpoints', [])))
        a['linked_exchange_exit_ids'] = sorted(links[a['id']])
        a['review_required'] = a['position_state'] == 'NEEDS_REVIEW' or a['wallet_position'].get('state') == 'NEEDS_REVIEW'
        # FIX-UI-7 point 4: the first open movement replaces the generic
        # review sentence (the descendant note, which the wallet-position
        # reasons repeat); other wallet-position reasons are kept.
        movements = open_movements(descendant)
        a['open_movement'] = movements[0] if movements else None
        a['open_movement_count'] = len(movements)
        a['review_reasons'] = review_reasons(a['wallet_position'].get('reasons', []),
                                             descendant, a['position_state'], movements)
    # Drop position inputs before serializing detailed lineage from the shared DAG.
    del context, spends
    report = await positions(db, engine, coverage, limit=coverage.utxo_count)
    saved = await annotations(db, engine)
    decisions = {(r['txid'], r['vout']): r for r in await rows(db, 'SELECT * FROM provenance_origin_resolutions')}
    anchor_keys = set(saved) | set(decisions)
    for p in report['positions']:
        anchor_keys.update((a['txid'], a['vout']) for a in p['anchors'])
    anchors = [dict(anchor_record(k, saved), comment=decisions.get(k, {}).get('comment')) for k in sorted(anchor_keys)]
    for e in exits:
        e['sources'] = sources[e['id']]
        e['selected_outputs'] = selected[e['id']]
    # Historical membership is evidence attribution, never current ownership.
    attribution = defaultdict(set)
    for r in await rows(db, 'SELECT address,wallet_name FROM known_addresses UNION SELECT address,wallet_name FROM address_evidence'):
        attribution[r['address']].add(r['wallet_name'] or '')
    point_wallets = {f'{k[0]}:{k[1]}': attribution.get(o['address'], set()) for k, o in outputs.items()}
    for a in acquisitions:
        point_wallets.setdefault(f"{a['origin_txid']}:{a['origin_vout']}", set()).update(attribution.get(a['origin_address'], set()))
    return dict(snapshot_id=uuid.uuid4().hex, data_as_of_utc=coverage.inventory.get('oldest_scan_at'),
        built_at_utc=utc_now(), stale=coverage.inventory.get('refresh_required', False),
        inventory=coverage.inventory, positions=report['positions'], acquisitions=acquisitions,
        anchors=anchors, exits=exits, point_wallets=point_wallets,
        wallets=sorted({w for ws in attribution.values() for w in ws}))


async def build_dashboard(db, source_engine):
    """Compact transient projection, derived in batches; no full report/Exit/text.

    The same coverage, provenance and Acquisition evaluators supply every
    field. Nothing is persisted. Only finished per-scope metrics are cached.
    """
    from backend.reporting.origin_resolution import OriginResolutions

    settings = get_settings()
    deadline = time.monotonic() + max(120, settings.coverage_timeout)
    population = await population_keys(db, limit=settings.coverage_max_outpoints)
    rpc = SavedRPC({}, SimpleNamespace(btc_network=network_scope(source_engine)))
    engine = TraceEngine(db, rpc, max_depth=source_engine.max_depth)
    report = ReportContext(origin_resolutions=await OriginResolutions.load(db), transaction_sets={'provenance': set()})
    attribution = defaultdict(set)
    for r in await rows(db, 'SELECT address,wallet_name FROM known_addresses UNION SELECT address,wallet_name FROM address_evidence'):
        attribution[r['address']].add(r['wallet_name'] or '')
    result = dict(snapshot_id=uuid.uuid4().hex, positions=[], acquisitions=[], anchors=[], exits=[],
                  point_wallets={}, wallets=sorted({w for ws in attribution.values() for w in ws}))

    async def saved(keys):
        outputs = {}
        txids = list({k[0] for k in keys})
        for offset in range(0, len(txids), 400):
            part = txids[offset:offset+400]
            sql = 'SELECT o.*,t.confirmations FROM outpoints o LEFT JOIN transactions t ON t.txid=o.txid WHERE o.txid IN (' + ','.join('?' for _ in part) + ')'
            for o in await rows(db, sql, part, limit=settings.coverage_max_outpoints):
                outputs[o['txid'], o['vout']] = o
        if len(outputs) > settings.coverage_max_outpoints:
            raise ReadBudgetExceeded('Dashboard batch size limit exceeded.')
        rpc.outputs = outputs
        return outputs

    # Current UTXO classification uses the canonical evaluators, but does not
    # materialize detailed anchors/origin objects or all Acquisition positions.
    ordered = sorted(population)
    for offset in range(0, len(ordered), 256):
        group = set(ordered[offset:offset+256])
        await saved(group)
        coverage = await saved_wallet_observations(db, engine, settings, deadline,
            population=population, observed_keys=group, acquisitions=[], report=report)
        classified = await positions(db, engine, coverage, limit=coverage.utxo_count, compact=True, report=report)
        result['positions'].extend(classified['positions'])
        del coverage, classified

    # Stream only the fields consumed by existing state/membership services.
    # Free text and complete report relationships are not Dashboard inputs.
    count = 0
    async with db.db.execute('SELECT id,trace_id,origin_txid,origin_vout,origin_amount_sats,origin_address,status FROM acquisitions ORDER BY id') as cursor:
        while batch := await cursor.fetchmany(256):
            acquisitions = [dict(r) for r in batch]
            count += len(acquisitions)
            if count > settings.coverage_max_outpoints:
                raise ReadBudgetExceeded('Dashboard Acquisition limit exceeded.')
            context = PositionContext(settings)
            context.deadline, context.summaries = deadline, {}
            context.batch = await PositionBatch.load(db, engine, acquisitions=acquisitions)
            keys = {(a['origin_txid'], a['origin_vout']) for a in acquisitions if a['origin_txid'] is not None}
            keys.update(k for tx in context.batch.by_tx.values() for k in tx)
            outputs = await saved(keys)
            spends = {(r['source_txid'], r['source_vout']): r for r in await selected_rows(db, 'spends', 'source_txid', {k[0] for k in outputs})}
            context.saved_facts = {k: {'spend_status': o['spend_status'], 'spending_txid': spends.get(k, {}).get('spending_txid')} for k, o in outputs.items()}
            context.saved_outputs = {k: {'value': btc(o['amount_sats']), 'confirmations': o.get('confirmations')} for k, o in outputs.items() if o['spend_status'] == 'UNSPENT'}
            coverage = await saved_wallet_observations(db, engine, settings, deadline,
                population=population, observed_keys=set(), acquisitions=acquisitions,
                report=dataclasses.replace(report, position_context=context))
            locations = {r['acquisition_id']: r for r in coverage.acquisition_wallet_positions}
            survey = await db.get_allocation_survey()
            for a in acquisitions:
                descendant = await descendant_summary(db, engine, a, context)
                state = position_state_from_row(a, survey.get(a['id'], 0), descendant)
                location = locations.get(a['id'], {})
                # Same FIX-UI-7 presentation fields as the full snapshot (D-15).
                movements = open_movements(descendant)
                result['acquisitions'].append(dict(id=a['id'], origin_txid=a['origin_txid'], origin_vout=a['origin_vout'],
                    position_state=state, wallet_position={'state': location.get('state')}, current_outpoints=[],
                    review_required=state == 'NEEDS_REVIEW' or location.get('state') == 'NEEDS_REVIEW',
                    open_movement=movements[0] if movements else None, open_movement_count=len(movements),
                    review_reasons=review_reasons(location.get('reasons', []), descendant, state, movements)))
                key = f"{a['origin_txid']}:{a['origin_vout']}"
                result['point_wallets'].setdefault(key, set()).update(attribution.get(a['origin_address'], set()))
            del context, coverage, locations, outputs, spends

    anchor_keys = {(r['txid'], r['vout']) for r in await rows(db,
        'SELECT txid,vout FROM no_acquisition_annotations WHERE network=? UNION SELECT txid,vout FROM provenance_origin_resolutions', (network_scope(engine),))}
    for p in result['positions']:
        anchor_keys.update((k.rsplit(':', 1)[0], int(k.rsplit(':', 1)[1])) for k in p['no_acquisition_anchor_outpoints'])
    result['anchors'] = [{'outpoint': f'{k[0]}:{k[1]}'} for k in sorted(anchor_keys)]
    # Match historical exact-origin address attribution, including a stored
    # output's address as well as the Acquisition's preserved origin address.
    historical_keys = anchor_keys | {(a['origin_txid'], a['origin_vout']) for a in result['acquisitions'] if a['origin_txid'] is not None}
    txids = list({k[0] for k in historical_keys})
    for offset in range(0, len(txids), 400):
        part = txids[offset:offset+400]
        for o in await rows(db, 'SELECT txid,vout,address FROM outpoints WHERE txid IN (' + ','.join('?' for _ in part) + ')', part):
            key = o['txid'], o['vout']
            if key in historical_keys:
                result['point_wallets'].setdefault(f'{key[0]}:{key[1]}', set()).update(attribution.get(o['address'], set()))
    inventory = await metadata(db)
    result.update(data_as_of_utc=inventory.get('oldest_scan_at'), built_at_utc=utc_now(), stale=inventory.get('refresh_required', False))
    return result


async def wallet_receipts(db, source_engine):
    """Cached owned entry outputs without an Acquisition, including spent receipts.

    Reuse the strict graph's exact origins. Internal descendants of those entries
    do not become additional records; incomplete evidence stays reviewable.
    """
    from backend.reporting.no_acquisition import provenance_graph
    from backend.reporting.origin_resolution import lineage_summary
    settings = get_settings()
    deadline = time.monotonic() + max(120, settings.coverage_timeout)
    candidates = await rows(db, '''SELECT o.* FROM outpoints o
        JOIN known_addresses k ON k.address=o.address ORDER BY o.txid,o.vout''',
        limit=settings.coverage_max_outpoints)
    keys = {(o['txid'], o['vout']) for o in candidates}
    engine = TraceEngine(db, SavedRPC({}, SimpleNamespace(btc_network=network_scope(source_engine))),
                         max_depth=source_engine.max_depth)
    report = ReportContext(transaction_sets={'provenance': set()})
    result = []
    ordered = sorted(keys)
    for offset in range(0, len(ordered), 256):
        batch = set(ordered[offset:offset + 256])
        graph, data = await provenance_graph(db, engine, batch, settings, deadline, report=report)
        for key in sorted(batch):
            output = data.outputs.get(key)
            if not output or output.ownership_status != 'OWNED' or key in graph.boundaries:
                continue
            evidence = graph.evaluate(key)
            resolution = graph.origin_resolutions.resolve(key)
            if resolution['type'] == 'ACQUISITION' or evidence.acquisitions:
                continue
            # Exact entry identities only; exclude internal transfers and change.
            parents = input_keys(graph.transactions.get(key[0]))
            pending_entry = bool(parents) and not evidence.no_acquisition_anchors and all(
                not data.outputs.get(p) or data.outputs[p].ownership_status != 'OWNED' for p in parents)
            if key not in evidence.no_acquisition_anchors and key not in evidence.unresolved_origins and not pending_entry:
                continue
            stored = data.outpoints[key]
            summary = lineage_summary(evidence, graph.origin_resolutions)
            result.append(dict(txid=key[0], vout=key[1], outpoint=output.outpoint,
                address=output.address, amount_sats=stored['amount_sats'],
                block_time=stored.get('block_time'), spend_status=stored['spend_status'],
                wallet_name=data.known.get(output.address, {}).get('wallet_name'),
                origin_resolution=resolution, review_status=summary['review_status'],
                review_reasons=sorted(evidence.review)))
    return dict(records=result, scope_note='Saved wallet entry evidence, including spent outputs. Refresh wallet observations to discover additional receipts.')


class Reports:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.key = self.value = None
        self.dashboard_values = {}
        self.snapshot_id = None
        self.receipts = None

    async def _read(self, db, engine, builder):
        isolated = Database(db.path)
        async with aiosqlite.connect(db.path) as conn:
            conn.row_factory = aiosqlite.Row
            isolated.db = conn
            await conn.execute('PRAGMA query_only=ON')
            await conn.execute('BEGIN')
            async with asyncio.timeout(180):
                result = await builder(isolated, engine)
            await conn.rollback()
        return result

    async def _key(self, db, engine):
        if db.db.in_transaction:
            raise ReadBudgetExceeded('Evidence transaction pending; retry.')
        key = (id(db), id(engine), await revision(db))
        if key != self.key:
            # Drop old full details before building a new revision's summary.
            self.key, self.value, self.snapshot_id = key, None, uuid.uuid4().hex
            self.dashboard_values.clear()
            self.receipts = None
        return key

    async def get(self, db, engine):
        async with self.lock:
            key = await self._key(db, engine)
            if self.value is not None:
                return self.value
            result = await self._read(db, engine, build)
            if db.db.in_transaction or await revision(db) != key[2]:
                raise ReadBudgetExceeded('Evidence changed; retry.')
            result['snapshot_id'] = self.snapshot_id
            self.value = result
            return result

    async def get_receipts(self, db, engine):
        async with self.lock:
            key = await self._key(db, engine)
            if self.receipts is None:
                result = await self._read(db, engine, wallet_receipts)
                if db.db.in_transaction or await revision(db) != key[2]:
                    raise ReadBudgetExceeded('Evidence changed; retry.')
                result['snapshot_id'] = self.snapshot_id
                self.receipts = result
            return self.receipts

    async def get_summary(self, db, engine, *, wallet=None):
        async with self.lock:
            key = await self._key(db, engine)
            if wallet in self.dashboard_values:
                return self.dashboard_values[wallet]
            if self.value is not None:
                self.dashboard_values[wallet] = summary(self.value, select(self.value, wallet), preview_only=True)[0]
                return self.dashboard_values[wallet]
            compact = await self._read(db, engine, build_dashboard)
            if db.db.in_transaction or await revision(db) != key[2]:
                raise ReadBudgetExceeded('Evidence changed; retry.')
            compact['snapshot_id'] = self.snapshot_id
            if wallet is not None and wallet not in compact['wallets']:
                raise ValueError('Unknown wallet scope.')
            # Retain cards only, never compact rows, ancestry, text or queues.
            for scope in [None, *compact['wallets']]:
                self.dashboard_values[scope] = summary(compact, select(compact, scope), preview_only=True)[0]
            return self.dashboard_values[wallet]


def select(snapshot, wallet=None, filters=None, kind='full-report'):
    filters = filters or {}
    validate_filters(kind, filters)
    if wallet is not None and wallet not in snapshot['wallets']:
        raise ValueError('Unknown wallet scope.')
    points = [p for p in snapshot['positions'] if wallet is None or
              any(m['wallet_id'] == wallet for m in p['wallet_matches'])]
    current_a = {i for p in points for i in p['acquisition_ids']}
    current_n = {i for p in points for i in p['no_acquisition_anchor_outpoints']}
    def belongs(key):
        return wallet is None or wallet in snapshot['point_wallets'].get(key, set())
    acquisitions = [a for a in snapshot['acquisitions'] if wallet is None or a['id'] in current_a or
                    belongs(f"{a['origin_txid']}:{a['origin_vout']}")]
    anchors = [a for a in snapshot['anchors'] if belongs(a['outpoint']) or a['outpoint'] in current_n]
    ids = {a['id'] for a in acquisitions}
    exits = [e for e in snapshot['exits'] if wallet is None or any(
        s['acquisition_id'] in ids or belongs(f"{s['source_txid']}:{s['source_vout']}") for s in e['sources'])]
    if wallet is not None:
        scoped_keys = {p['outpoint'] for p in points}
        acquisitions = [dict(a, current_outpoints=[k for k in a['current_outpoints'] if k in scoped_keys or belongs(k)],
            wallet_position={k: ([v for v in value if v in scoped_keys or belongs(v)] if k.endswith('outpoints') else value)
                             for k, value in a['wallet_position'].items()}) for a in acquisitions]
        exits = [dict(e, sources=[s for s in e['sources'] if s['acquisition_id'] in ids or
                                 belongs(f"{s['source_txid']}:{s['source_vout']}")]) for e in exits]
    points = [p for p in points if matches(p, filters)]
    if kind == 'acquisitions' and filters:
        if 'position' in filters:
            state = filters['position']
            acquisitions = [a for a in acquisitions if (a['wallet_position'].get('state') == state
                            if state == OUTSIDE else a['position_state'] == state)]
        if 'review' in filters:
            # Same predicate as the Needs Attention ACQUISITION tile (D-06).
            acquisitions = [a for a in acquisitions if a['review_required']]
    elif filters:
        aids = {i for p in points for i in p['acquisition_ids']}
        nkeys = {i for p in points for i in p['no_acquisition_anchor_outpoints']}
        acquisitions = [a for a in acquisitions if a['id'] in aids]
        anchors = [a for a in anchors if a['outpoint'] in nkeys]
        exits = [e for e in exits if any(s['acquisition_id'] in aids for s in e['sources'])]
    return dict(positions=points, acquisitions=acquisitions, anchors=anchors, exits=exits,
                wallet=wallet, filters=filters)


def summary(snapshot, selected, *, preview_only=False):
    points, acquisitions, anchors = (selected[k] for k in ('positions', 'acquisitions', 'anchors'))
    wallet = selected['wallet']
    metrics, queue = [], []
    review_counts = Counter()
    def review(item_type, object_id, reasons, detail_path, **extra):
        review_counts[item_type] += 1
        if not preview_only or len(queue) < 5:
            queue.append(dict(review_item_type=item_type, object_id=object_id,
                              reasons=reasons, detail_path=detail_path, **extra))
    def metric(key, dimension, unit, count, value=None, view=None, **filters):
        metrics.append(dict(metric_key=key, dimension=dimension, unit=unit, count=count,
                            value_sats=value, value_btc=btc(value) if value is not None else None,
                            detail_path=path(view or 'current-wallet', wallet, **filters)))
    metric('current_self_custody', 'holdings', 'UTXO', len(points), sum(p['amount_sats'] for p in points))
    for status in FILTERS['coverage']:
        group = [p for p in points if p['coverage_status'] == status]
        metric(status, 'coverage', 'UTXO', len(group), sum(p['amount_sats'] for p in group), coverage=status)
    metric('acquisition_records', 'origins', 'ACQUISITION_RECORD', len(acquisitions), view='acquisitions')
    metric('known_current_acquisition_references', 'origins', 'ACQUISITION_ID',
           len({i for p in points for i in p['acquisition_ids']}), origin_type='ACQUISITION')
    # All known anchors in scope (D-06): the link opens exactly selected['anchors'].
    metric('no_acquisition_anchors', 'origins', 'ANCHOR_OUTPOINT', len(anchors), view='acquisitions', type='anchors')
    metric('known_current_anchor_references', 'origins', 'ANCHOR_OUTPOINT',
           len({i for p in points for i in p['no_acquisition_anchor_outpoints']}), origin_type='NO_ACQUISITION')
    metric('unresolved_current_origins', 'origins', 'UTXO', sum(p['provenance_class'] == 'UNRESOLVED' for p in points), origin_type='UNRESOLVED')
    counts = Counter(a['position_state'] for a in acquisitions)
    for state in dict.fromkeys(POSITION_STATES + tuple(sorted(counts))):
        count = sum(a['wallet_position'].get('state') == OUTSIDE for a in acquisitions) if state == OUTSIDE else counts[state]
        metric(state, 'positions', 'ACQUISITION_RECORD', count, view='acquisitions', position=state)
    for p in points:
        if p['review_status'] == 'UNRESOLVED':
            review('CURRENT_UTXO', p['outpoint'], sorted(set(p['unresolved_reasons'])),
                   path('current-wallet', wallet, outpoint=p['outpoint']))
    for a in acquisitions:
        if a['review_required']:
            review('ACQUISITION', str(a['id']), a['review_reasons'],
                   path('acquisitions', wallet, acquisition_id=a['id']),
                   open_movement=a.get('open_movement'),
                   open_movement_count=a.get('open_movement_count') or 0)
    for item_type in ('CURRENT_UTXO', 'ACQUISITION'):
        metric(item_type, 'attention', item_type, review_counts[item_type],
               view='current-wallet' if item_type == 'CURRENT_UTXO' else 'acquisitions',
               **({'coverage': 'UNRESOLVED'} if item_type == 'CURRENT_UTXO' else {'review': 'required'}))
    return dict(snapshot_id=snapshot['snapshot_id'], data_as_of_utc=snapshot['data_as_of_utc'],
                built_at_utc=snapshot['built_at_utc'], stale=snapshot['stale'], scope=wallet,
                wallets=snapshot['wallets'], metrics=metrics, review_preview=queue[:5]), queue
