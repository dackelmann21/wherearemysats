"""Exit-Provenance CSV v2: one row per Exit x origin anchor (read-only).

A condensed answer to an exchange's source-of-funds question: "this deposit
descends from these anchors, with only own custody in between". Class and
anchors come exclusively from ``exit_provenance_rows`` (the evaluator), never
from ``exchange_exit_sources``. No shares, percentages, per-anchor sat
allocation, fiat or tax values. v1 (``exit_provenance.py``) stays unchanged.
"""
import asyncio
import csv
import io
import tempfile
from datetime import datetime, timezone
from dataclasses import dataclass, field
from decimal import Decimal

from backend.acquisitions.exchange_exit_service import get_exchange_exit
from backend.reporting.csv_exports import ExportValidationError, safe_text
from backend.reporting.exit_provenance import exit_events, exit_provenance_rows
from backend.reporting.no_acquisition import annotations
from backend.reporting.read_data import ReadBudgetExceeded, ReadData, revision, rows
from backend.version import APP_VERSION

CSV_VERSION = 'exit-provenance-v2'
BASE_COLUMNS = (
    'exit_txid', 'exit_vout', 'exit_amount_sats', 'exit_block_height', 'exit_block_time_utc',
    'report_class', 'allocation_check',
    'anchor_type', 'anchor_txid', 'anchor_vout', 'anchor_amount_sats',
    'anchor_block_height', 'anchor_block_time_utc',
    'user_acquisition_date', 'user_source', 'user_reference',
    'hop_count', 'custody_basis_weakest',
    'csv_version', 'app_version', 'exported_at_utc', 'data_revision',
)
FREE_TEXT_COLUMNS = ('user_broker', 'user_notes', 'user_customer_reference', 'review_comment')
YEAR_RANGE = (1000, 9999)

# Self-custody basis of one path output, strongest first. The weakest basis on
# a path (and over all paths of an anchor) is reported. Derivation from a
# user-supplied xpub/descriptor is reproducible from public data; an imported
# address list is an assertion by the operator; a manual confirmation is an
# assertion about a single address or output. NOT_ESTABLISHED never occurs on a
# resolved path (the evaluator requires OWNED outputs there) and marks gaps.
BASIS_ORDER = ('WALLET_DERIVATION', 'ADDRESS_IMPORT', 'USER_CONFIRMATION', 'NOT_ESTABLISHED')
SOURCE_BASIS = {
    'XPUB_DISCOVERY': 'WALLET_DERIVATION', 'XPUB': 'WALLET_DERIVATION', 'DESCRIPTOR': 'WALLET_DERIVATION',
    'MANUAL_IMPORT': 'ADDRESS_IMPORT', 'ADDRESS_REGISTRY': 'ADDRESS_IMPORT', 'KNOWN_ADDRESS': 'ADDRESS_IMPORT',
    'USER_CONFIRMED': 'USER_CONFIRMATION', 'USER': 'USER_CONFIRMATION',
}
# Address-level evidence outranks the engine's display "primary" source: an
# address that is also derivable from an xpub is not weaker for being confirmed too.
ADDRESS_LEVEL = {'XPUB_DISCOVERY', 'MANUAL_IMPORT', 'USER_CONFIRMED', 'ADDRESS_REGISTRY', 'KNOWN_ADDRESS'}


def validate_years(years):
    """``None`` = all years, else a non-empty sorted list of plausible UTC years."""
    if years is None:
        return None
    if isinstance(years, (str, bytes)) or not isinstance(years, (list, tuple, set, frozenset)):
        raise ExportValidationError('Select at least one year or all years.')
    cleaned = sorted(set(years))
    if not cleaned:
        raise ExportValidationError('Select at least one year or all years.')
    if any(type(y) is not int or not YEAR_RANGE[0] <= y <= YEAR_RANGE[1] for y in cleaned):
        raise ExportValidationError('Years must be four-digit calendar years (UTC).')
    return cleaned


def filename_for(years):
    """wams-exit-provenance-v2-2025.csv, ...-2024-2025.csv, ...-all.csv."""
    if years is None:
        label = 'all'
    elif len(years) == years[-1] - years[0] + 1:
        label = str(years[0]) if len(years) == 1 else f'{years[0]}-{years[-1]}'
    else:
        label = '_'.join(str(y) for y in years)  # gaps: never imply the years in between
    return f'wams-exit-provenance-v2-{label}.csv'


def exit_year(record):
    """UTC year of the confirmed Exit block time; None while unconfirmed (as in v1's date filter)."""
    if record.get('block_time') is None or record.get('block_height') is None:
        return None
    return datetime.fromtimestamp(record['block_time'], timezone.utc).year


def identity(value):
    """Undo v1's spreadsheet protection (apostrophe prefix on TXID/outpoint cells)."""
    return (value or '').removeprefix("'")


def to_sats(btc_text):
    return None if btc_text in (None, '') else int(Decimal(btc_text) * 100_000_000)


def protect_identity(value):
    # Same Excel guard as v1: numeric/exponent-looking TXIDs must stay text.
    return '' if not value else "'" + str(value)


def split_outpoint(outpoint):
    txid, _, vout = identity(outpoint).rpartition(':')
    return txid, int(vout)


def basis_of(out, evidence_rows):
    """Self-custody basis of one output from its effective ownership evidence."""
    if out is None or out.ownership_status != 'OWNED':
        return 'NOT_ESTABLISHED'
    source = str(out.classification_source or '').upper()
    candidates = {SOURCE_BASIS.get(source, 'NOT_ESTABLISHED')}
    if source in ADDRESS_LEVEL:
        candidates |= {SOURCE_BASIS.get(str(r.get('source') or '').upper(), 'NOT_ESTABLISHED')
                       for r in evidence_rows} - {'NOT_ESTABLISHED'}
        # Strongest effective address evidence, not the display primary.
        candidates = {min(candidates, key=BASIS_ORDER.index)}
    elif any(SOURCE_BASIS.get(str(r.get('source') or '').upper()) == 'WALLET_DERIVATION'
             for r in evidence_rows):
        # S-7 (owner decision 2026-10-30): derivation evidence on the address wins
        # over a per-outpoint decision (e.g. manual OWNED, USER).
        candidates = {'WALLET_DERIVATION'}
    return candidates.pop()


def weakest(bases):
    return max(bases, key=BASIS_ORDER.index) if bases else ''


class OwnershipNeutralEngine:
    """Engine view for the FX-2 allocation check that never writes ownership.

    The check loads the Exit transaction like the Exit view does; the default
    ``persist_ownership=True`` would reconcile stale EXTERNAL rows as a side effect.
    """

    def __init__(self, engine):
        self._engine = engine

    def __getattr__(self, name):
        return getattr(self._engine, name)

    async def load_transaction(self, txid, **kwargs):
        return await self._engine.load_transaction(txid, **dict(kwargs, persist_ownership=False))


async def allocation_checks(db, engine, exit_ids):
    """Exit id -> '' (matches), 'DIFFERS: ...' or 'UNAVAILABLE' (read-only FX-2 comparison)."""
    checks = {}
    for exit_id in sorted(exit_ids):
        exit_ = await get_exchange_exit(db, exit_id, OwnershipNeutralEngine(engine))
        checks[exit_id] = ('' if exit_.allocation_check == 'MATCHES' else
                           'DIFFERS: ' + ' | '.join(exit_.allocation_differences) if exit_.allocation_check == 'DIFFERS'
                           else 'UNAVAILABLE')
    return checks


def group_paths(v1_rows):
    """exit key -> {path_id: [hop rows by hop_index]} from the evaluator projection."""
    exits = {}
    for r in v1_rows:
        exits.setdefault((r['exit_id'], r['exit_vout']), {}).setdefault(r['path_id'], []).append(r)
    return exits


def path_anchor(rows_of_path):
    """(anchor type, canonical (txid, vout)); an Acquisition's origin outpoint is unique."""
    r = rows_of_path[0]
    if r['acquisition_id'] is not None:
        return 'ACQUISITION', split_outpoint(r['acquisition_outpoint'])
    if r['no_acquisition_anchor_outpoint']:
        return 'NO_ACQUISITION', split_outpoint(r['no_acquisition_anchor_outpoint'])
    return None, None


def connected_path(rows_of_path):
    """An established path: evaluator status CONNECTED, with a real anchor and hops."""
    r = rows_of_path[0]
    return (all(x['path_status'] == 'CONNECTED' for x in rows_of_path)
            and r['hop_index'] is not None and path_anchor(rows_of_path)[0] is not None)


def path_nodes(path_rows):
    """Self-custody nodes: the anchor and every intermediate output, not the Exit output."""
    keys = [split_outpoint(path_rows[0]['from_outpoint'])]
    keys += [split_outpoint(r['to_outpoint']) for r in path_rows[:-1]]
    return keys


@dataclass
class ExitEvidence:
    """Evaluator-derived inputs shared by CSV v2 and the Blockchain History Report (B1)."""
    events: list
    checks: dict
    by_exit: dict
    acquisitions: dict
    resolutions: dict
    basis: dict
    revision: tuple
    # Raw No-Acquisition annotations by anchor key (the report shows user text as entered).
    annotations: dict = field(default_factory=dict)

    async def ensure_unchanged(self, db):
        if await revision(db) != self.revision:
            raise ReadBudgetExceeded('Evidence changed during export; retry.')


async def load_exit_evidence(db, engine, settings, deadline, *, years=None, exit_ids=None):
    """Selected Exits (UTC year, id scope) with their evaluator paths and custody bases."""
    years = validate_years(years)
    from_date = f'{years[0]:04d}-01-01' if years else None
    to_date = f'{years[-1]:04d}-12-31' if years else None
    events = [e for e in await exit_events(db, from_date, to_date)
              if (years is None or exit_year(e) in years)
              and (exit_ids is None or e['id'] in exit_ids)]
    if not events:
        return ExitEvidence([], {}, {}, {}, {}, {}, await revision(db))
    wanted = {(e['id'], e['exit_vout']) for e in events}
    # The comparison may cache node facts (outpoints/transactions, as in the Exit view),
    # so it runs first; everything after it is pinned to one database revision.
    checks = await allocation_checks(db, engine, {e['id'] for e in events})
    before = await revision(db)
    v1_rows = [r for r in await exit_provenance_rows(db, engine, settings, deadline,
               from_date=from_date, to_date=to_date) if (r['exit_id'], r['exit_vout']) in wanted]
    by_exit = group_paths(v1_rows)
    acquisitions = {a['id']: a for a in await rows(
        db, 'SELECT id, reference, acquisition_date, source FROM acquisitions')}
    saved = await annotations(db, engine)
    resolutions = {(r['txid'], r['vout']): r['comment'] for r in await rows(
        db, 'SELECT txid, vout, comment FROM provenance_origin_resolutions')}
    node_keys = set()
    for paths in by_exit.values():
        for path_rows in paths.values():
            if connected_path(path_rows):
                node_keys.update(path_nodes(path_rows))
    data = await ReadData.load(db, engine, {k[0] for k in node_keys}, limit=settings.coverage_max_outpoints)
    basis = {key: basis_of(data.outputs.get(key), data.evidence.get(data.outputs[key].address, [])
                           if key in data.outputs and data.outputs[key].address else [])
             for key in node_keys}
    return ExitEvidence(events, checks, by_exit, acquisitions, resolutions, basis, before, saved)


async def build_records(db, engine, settings, deadline, *, years=None, exit_ids=None,
                        include_free_text=False, exported_at, data_revision):
    """Row dicts for ``COLUMNS``; read-only, evaluator-derived."""
    evidence = await load_exit_evidence(db, engine, settings, deadline, years=years, exit_ids=exit_ids)
    events, checks, by_exit = evidence.events, evidence.checks, evidence.by_exit
    acquisitions, resolutions, basis = evidence.acquisitions, evidence.resolutions, evidence.basis
    if not events:
        return []
    nodes_of = path_nodes
    result = []
    for event in events:
        await asyncio.sleep(0)
        exit_key = (event['id'], event['exit_vout'])
        paths = by_exit.get(exit_key, {})
        any_rows = next((p[0] for p in paths.values()), None)
        report_class = any_rows['provenance_class'] if any_rows else 'UNRESOLVED'
        exit_part = {
            'exit_txid': protect_identity(event['txid']), 'exit_vout': event['exit_vout'],
            'exit_amount_sats': event['amount_sats'], 'exit_block_height': event['block_height'],
            'exit_block_time_utc': _iso(event['block_time']),
            'report_class': report_class, 'allocation_check': safe_text(checks[event['id']]),
            'csv_version': CSV_VERSION, 'app_version': APP_VERSION,
            'exported_at_utc': exported_at, 'data_revision': data_revision,
        }
        if include_free_text:
            exit_part.update(user_broker=safe_text(event.get('broker')), user_notes=safe_text(event.get('notes')),
                             user_customer_reference=safe_text(event.get('reference')))
        anchors = {}
        for path_rows in paths.values():
            if not connected_path(path_rows):
                continue  # recorded-only (NO_PATH_EVIDENCE) and incomplete paths are not anchors
            kind, anchor = path_anchor(path_rows)
            entry = anchors.setdefault((kind, anchor), dict(type=kind, rows=path_rows, hops=[], bases=[]))
            entry['hops'].append(len(path_rows))
            entry['bases'] += [basis[k] for k in nodes_of(path_rows)]
        for (kind, (txid, vout)), entry in sorted(anchors.items(), key=lambda kv: (kv[0][0] != 'ACQUISITION', kv[0][1])):
            first = entry['rows'][0]
            if kind == 'ACQUISITION':
                part = dict(anchor_amount_sats=to_sats(first['acquisition_amount_btc']),
                            anchor_block_height=first['acquisition_block_height'],
                            anchor_block_time_utc=first['acquisition_block_time'],
                            user_acquisition_date=first['acquisition_date'], user_source=first['acquisition_source'],
                            user_reference=safe_text(acquisitions.get(first['acquisition_id'], {}).get('reference')))
            else:
                part = dict(anchor_amount_sats=to_sats(first['from_amount_btc']),
                            anchor_block_height=first['from_block_height'], anchor_block_time_utc=first['from_block_time'],
                            user_source=first['no_acquisition_source'])
                if include_free_text:
                    part['review_comment'] = safe_text(resolutions.get((txid, vout)))
            result.append(dict(exit_part, anchor_type=kind, anchor_txid=protect_identity(txid),
                               anchor_vout=vout, hop_count=min(entry['hops']),
                               custody_basis_weakest=weakest(entry['bases']), **part))
        if report_class == 'UNRESOLVED':
            result.append(dict(exit_part))  # the unexplained part: no anchor, no chain
    await evidence.ensure_unchanged(db)
    return result


def _iso(timestamp):
    return '' if timestamp is None else datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace('+00:00', 'Z')


def write_csv(records, include_free_text, cancel=None):
    """Semicolon CSV with BOM and CRLF in a private, unlinked tempfile (like v1/Excel)."""
    columns = BASE_COLUMNS + (FREE_TEXT_COLUMNS if include_free_text else ())
    artifact = tempfile.TemporaryFile(mode='w+b')
    try:
        stream = io.TextIOWrapper(artifact, encoding='utf-8-sig', newline='')
        writer = csv.writer(stream, delimiter=';', lineterminator='\r\n')
        writer.writerow(columns)
        for record in records:
            if cancel is not None and cancel.is_set():
                raise InterruptedError('Export cancelled.')
            writer.writerow(['' if record.get(c) is None else record[c] for c in columns])
        stream.flush()
        stream.detach()
        artifact.seek(0, 2)
        size = artifact.tell()
        artifact.seek(0)
        return artifact, len(records), size
    except BaseException:
        artifact.close()
        raise
