"""Deterministic archival CSV, with no external services or accounting values.

Exit rows distinguish explicit recorded sources from established ancestry
paths. Neither infers ownership or a later sale. The output amount repeats per relationship;
it is never presented as that Acquisition's objectively attributed sat amount.
"""
import asyncio
import csv
import io
import json
import time
import unicodedata
from datetime import date, datetime, timezone
from decimal import Decimal

from backend.config import get_settings
from backend.reporting.coverage import DocumentationGraph, Evidence, acquisition_provenance
from backend.reporting.provenance import graph_for_outputs
from backend.reporting.read_data import ReadBudgetExceeded, input_keys, revision, rows, selected_rows, valid_input

ACQUISITION_COLUMNS = (
    'acquisition_date', 'txid', 'vout', 'bitcoin_address', 'btc_amount',
    'block_height', 'block_time', 'source', 'reference', 'notes',
)
EXIT_COLUMNS = (
    'exit_id', 'exit_date', 'exit_txid', 'exit_vout', 'exit_address', 'btc_amount',
    'block_height', 'block_time', 'block_time_status', 'recorded_exit_date',
    'acquisition_provenance', 'acquisition_date', 'acquisition_txid',
    'acquisition_vout', 'acquisition_source', 'relationship_basis', 'exit_comment', 'amount_scope',
)


class ExportValidationError(ValueError):
    pass


def date_range(from_date=None, to_date=None):
    def parse(value):
        if value is None:
            return None
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError()
            return int(datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc).timestamp())
        except (TypeError, ValueError, OverflowError) as exc:
            raise ExportValidationError('Dates must use YYYY-MM-DD (UTC).') from exc
    start, end = parse(from_date), parse(to_date)
    if start is not None and end is not None and start > end:
        raise ExportValidationError('From must not be after To.')
    return start, end + 86400 if end is not None else None


def safe_text(value):
    """Prefix spreadsheet-active text with an apostrophe; retain its contents.

    Whitespace/control-prefixed formulas are protected too. This is applied to
    user text, never to canonical Bitcoin identifiers or numeric fact columns.
    CSV quoting alone does not prevent spreadsheet formula execution.
    """
    value = '' if value is None else str(value)
    probe = value
    while probe and (probe[0].isspace() or unicodedata.category(probe[0]) in ('Cc', 'Cf')):
        probe = probe[1:]
    if probe.startswith(('=', '+', '-', '@')) or (value and (value[0] in '\t\r\n' or unicodedata.category(value[0]) == 'Cc')):
        return "'" + value
    return value


def btc(amount):
    return '' if amount is None else format(Decimal(amount) / Decimal(100_000_000), '.8f')


def iso_time(timestamp):
    return '' if timestamp is None else datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace('+00:00', 'Z')


def render_csv(columns, records):
    output = io.StringIO(newline='')
    writer = csv.writer(output, delimiter=';', lineterminator='\r\n')
    writer.writerow(columns)
    for record in records:
        writer.writerow('' if value is None else value for value in record)
    return output.getvalue()


def inventory_csv(report):
    """Exactly one row per outpoint, even with overlapping wallet matches."""
    columns = ('txid', 'vout', 'amount_sats', 'btc_amount', 'wallets', 'address',
               'script_pubkey', 'block_height', 'block_time', 'confirmations',
               'wallet_match', 'ownership_status', 'acquisition_provenance',
               'acquisition_ids', 'derivations', 'inventory_oldest_scan_at',
               'inventory_refresh_required')
    return render_csv(columns, (
        (u.txid, u.vout, u.amount_sats, btc(u.amount_sats),
         safe_text(' | '.join(sorted({m['wallet_label'] for m in u.wallet_matches}))),
         u.address, u.script_pubkey, u.block_height, iso_time(u.block_time), u.confirmations,
         u.wallet_match, u.ownership_status, u.acquisition_provenance,
         ','.join(str(a) for a in u.acquisition_ids),
         safe_text(' | '.join(sorted({m['derivation_path'] for m in u.wallet_matches if m.get('derivation_path')}))),
         report.inventory.get('oldest_scan_at'), report.inventory.get('refresh_required'))
        for u in report.utxos))


def provenance_csv(report):
    """v1: JSON objects map Source/Note to canonical anchors without ambiguity.

    JSON cells start with '[' or '{', never a spreadsheet formula. Preserve the
    exact Unicode text (including leading formula characters inside JSON strings).
    """
    def encoded(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    columns = ('export_schema_version', 'classification_version', 'current_outpoint',
        'amount_sats', 'wallet_matches', 'position_status', 'provenance_class',
        'source', 'note', 'acquisition_ids', 'no_acquisition_anchor_outpoints',
        'anchors', 'lineage_complete', 'unresolved_reasons', 'provenance_link_state')
    return render_csv(columns, (
        (1, report['classification_version'], r['outpoint'], r['amount_sats'],
         encoded(r['wallet_matches']), r['position_status'], r['provenance_class'],
         encoded({a['outpoint']: a['source'] for a in r['anchors']}),
         encoded({a['outpoint']: a['note'] for a in r['anchors']}),
         encoded(r['acquisition_ids']), encoded(r['no_acquisition_anchor_outpoints']),
         encoded(r['anchors']), r['lineage_complete'], encoded(r['unresolved_reasons']),
         r['provenance_link_state']) for r in report['positions']))


async def _acquisitions_csv(db, engine, settings, deadline, **_):
    acquisitions = await rows(db, """SELECT * FROM acquisitions ORDER BY
        acquisition_date IS NULL, acquisition_date ASC,
        origin_block_time IS NULL, origin_block_time ASC, origin_txid ASC, origin_vout ASC""")
    return render_csv(ACQUISITION_COLUMNS, (
        (a['acquisition_date'], a['origin_txid'], a['origin_vout'], a['origin_address'],
         btc(a['origin_amount_sats']), a['origin_block_height'], iso_time(a['origin_block_time']),
         safe_text(a['source']), safe_text(a['reference']), safe_text(a['notes']))
        for a in acquisitions))


async def _exits_csv(db, engine, settings, deadline, *, from_date=None, to_date=None):
    start, end = date_range(from_date, to_date)
    clauses, params = [], []
    if start is not None or end is not None:
        clauses += ['e.block_time IS NOT NULL', 'e.block_height IS NOT NULL']
    if start is not None:
        clauses.append('e.block_time >= ?')
        params.append(start)
    if end is not None:
        clauses.append('e.block_time < ?')
        params.append(end)
    exits = await rows(db, "SELECT e.*, o.vout AS exit_vout, o.address AS exit_address, "
        "o.amount_sats FROM exchange_exits e LEFT JOIN exchange_exit_outputs o "
        "ON o.exchange_exit_id=e.id AND o.is_exchange_deposit=1 " +
        ('WHERE ' + ' AND '.join(clauses) if clauses else '') +
        ' ORDER BY e.block_time IS NULL, e.block_time, e.txid, e.id, o.vout', params)
    if not exits:
        return render_csv(EXIT_COLUMNS, [])
    links = {}
    acquisitions = {a['id']: a for a in await rows(db, 'SELECT * FROM acquisitions')}
    # FX-2 rows without an amount record an unattributed anchor, not a source.
    for row in await rows(db, """SELECT DISTINCT s.exchange_exit_id, a.*
        FROM exchange_exit_sources s JOIN acquisitions a ON a.id=s.acquisition_id
        WHERE s.disposed_sats + s.network_fee_sats > 0
        ORDER BY a.origin_txid, a.origin_vout, a.id"""):
        links.setdefault(row['exchange_exit_id'], []).append(row)
    transactions = {}
    for row in await selected_rows(db, 'transactions', 'txid', {e['txid'] for e in exits}):
        try:
            transactions[row['txid']] = json.loads(row['raw_json'] or 'null')
        except (TypeError, ValueError):
            transactions[row['txid']] = None
    keys = set()
    for raw in transactions.values():
        keys.update(input_keys(raw))
    graph, _data = await graph_for_outputs(db, engine, keys, settings, deadline)
    records = []
    for event in exits:
        recorded = {a['id'] for a in links.get(event['id'], [])}
        related_ids = set(recorded)
        provenance = 'NEEDS_REVIEW'
        raw = transactions.get(event['txid'])
        if DocumentationGraph.complete_transaction(raw, event['exit_vout']):
            evidence = Evidence()
            for vin in raw['vin']:
                if not valid_input(vin):
                    evidence.review.add('Missing input evidence.')
                    continue
                parent = graph.evaluate((vin['txid'], vin['vout']))
                evidence.acquisitions.update(parent.acquisitions)
                evidence.undocumented |= parent.undocumented
                evidence.review.update(parent.review)
            # The recorded relationship is not a claim that every transaction
            # input was documented, or an allocation to a particular output.
            related_ids.update(evidence.acquisitions)
            if recorded <= evidence.acquisitions:
                provenance = acquisition_provenance(evidence.state)
        # An ancestry-only relationship is labelled as such. This read model
        # never creates exchange_exit_sources or changes Position boundaries.
        if not related_ids <= acquisitions.keys():
            raise ReadBudgetExceeded('Acquisition evidence changed during export; retry.')
        related = sorted((acquisitions[aid] for aid in related_ids),
                         key=lambda a: (a['origin_txid'], a['origin_vout'], a['id']))
        confirmed = event['block_time'] is not None and event['block_height'] is not None
        timestamp = iso_time(event['block_time']) if confirmed else ''
        for anchor in related or [None]:
            records.append((event['id'], timestamp[:10], event['txid'], event['exit_vout'],
                event['exit_address'], btc(event['amount_sats']), event['block_height'], timestamp,
                'CONFIRMED' if confirmed else 'UNCONFIRMED_OR_UNKNOWN', safe_text(event['exit_date']),
                provenance, anchor['acquisition_date'] if anchor else '',
                anchor['origin_txid'] if anchor else '', anchor['origin_vout'] if anchor else '',
                safe_text(anchor['source']) if anchor else '',
                ('RECORDED_SOURCE' if anchor['id'] in recorded else 'ONCHAIN_ANCESTRY') if anchor else '',
                safe_text(event['notes']),
                'EXIT_OUTPUT_REPEATED_PER_ACQUISITION'))
            if len(records) > 100_000:
                raise ReadBudgetExceeded('Export row limit exceeded.')
        await asyncio.sleep(0)
    return render_csv(EXIT_COLUMNS, records)


async def export_csv(kind, db, engine, **filters):
    """Buffer before responding: changed evidence or limits never yield a partial CSV."""
    settings = get_settings()
    try:
        async with asyncio.timeout(settings.coverage_timeout):
            before = await revision(db)
            from backend.reporting.exit_provenance import exit_provenance_csv
            result = await {'acquisitions': _acquisitions_csv, 'exits': _exits_csv,
                            'exit-provenance-v1': exit_provenance_csv}[kind](
                db, engine, settings, time.monotonic() + settings.coverage_timeout, **filters)
            if await revision(db) != before:
                raise ReadBudgetExceeded('Evidence changed during export; retry.')
            return result
    except TimeoutError as exc:
        raise ReadBudgetExceeded('Export execution budget exceeded; retry.') from exc
