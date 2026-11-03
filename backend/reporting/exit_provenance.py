"""Human-auditable Exit evidence, v1. No RPC, ownership writes or allocation.

Enumerate paths over the parent links captured by DocumentationGraph.evaluate;
do not trace saved trace_edges (which can include non-on-chain custody bridges).
The final selected Exit output is an endpoint, not a boundary to cross. Its
provenance describes the evaluated INPUT ancestry before that final boundary.
"""
import asyncio
import json
from dataclasses import dataclass

from backend.reporting.coverage import DocumentationGraph, Evidence
from backend.reporting.no_acquisition import provenance_graph, annotations, outpoint
from backend.reporting.read_data import ReadBudgetExceeded, input_keys, rows, selected_rows
from backend.reporting.csv_exports import btc, date_range, iso_time, render_csv, safe_text
from backend.rpc.bitcoin import sats

SCHEMA_VERSION = 1
MAX_ROWS = 100_000
MAX_PATHS = 10_000
MAX_BYTES = 64 * 1024 * 1024
AMOUNT_NOTE = ('Whole output values; shared hops repeat across paths. '
               'Do not sum rows or interpret values as per-Acquisition allocations.')
COLUMNS = (
    'export_schema_version', 'exit_id', 'exit_date', 'exit_txid', 'exit_vout', 'exit_outpoint', 'exit_address',
    'exit_amount_btc', 'exit_block_height', 'exit_block_time',
    'acquisition_id', 'acquisition_date', 'acquisition_source', 'acquisition_outpoint',
    'acquisition_address', 'acquisition_amount_btc', 'acquisition_block_height', 'acquisition_block_time',
    'no_acquisition_anchor_outpoint', 'no_acquisition_source', 'no_acquisition_note',
    'path_id', 'hop_index', 'from_outpoint', 'from_address', 'from_amount_btc',
    'from_block_height', 'from_block_time', 'spending_txid', 'to_outpoint', 'to_address',
    'to_amount_btc', 'block_height', 'block_time', 'provenance_class', 'path_status', 'evidence_notes',
)


@dataclass(frozen=True)
class EvidencePath:
    nodes: tuple
    acquisition_id: int | None = None
    no_acquisition_anchor: tuple | None = None
    status: str = 'CONNECTED'

    def sort_key(self):
        if self.status == 'NO_PATH_EVIDENCE':
            kind = 3
        elif self.acquisition_id is not None:
            kind = 0
        else:
            kind = 1 if self.no_acquisition_anchor else 2
        return kind, self.nodes, self.acquisition_id or 0, self.status


def text_identity(value):
    # Unlike CSV quoting alone, an apostrophe protects numeric-only / exponent-
    # looking TXIDs from spreadsheet auto-conversion. Remove one leading quote
    # when pasting into Core; the canonical identifier after it is unchanged.
    return '' if not value else "'" + str(value)


def parsed(raw):
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None


def valid_transaction(raw, key):
    if not DocumentationGraph.complete_transaction(raw, key[1]) or raw.get('txid', key[0]) != key[0]:
        return False
    output_ids = [o.get('n') for o in raw['vout']]
    return (all(type(n) is int and n >= 0 for n in output_ids)
            and len(set(output_ids)) == len(output_ids)
            and len(input_keys(raw)) == len(raw['vin']))


def paths_from_input(graph, key, exit_key):
    """Expand captured evaluator links, with its boundaries and memoized evidence.

    No topology/ownership decisions are made here. An entering external branch
    ends at the OWNED receive anchor already identified by the evaluator.
    Missing/limited frontiers retain only their observable downstream fragment.
    """
    stack = [(key, (exit_key,), frozenset())]
    while stack:
        graph._charge()
        current, downstream, visited = stack.pop()
        if current in visited or len(visited) >= 200:
            yield EvidencePath(tuple(reversed(downstream)), status='INCOMPLETE')
            continue
        chain = (current,) + tuple(reversed(downstream))
        evidence = graph.memo.get(current)
        if evidence is None:
            yield EvidencePath(chain, status='INCOMPLETE')
            continue
        own_ids = graph.anchors.get(current, set()) & evidence.acquisitions
        if own_ids and not evidence.entry_frontier:
            for aid in sorted(own_ids):
                yield EvidencePath(chain, acquisition_id=aid)
            continue
        is_anchor = current in evidence.no_acquisition_anchors
        if is_anchor:
            yield EvidencePath(chain, no_acquisition_anchor=current)
        # A malformed reference must not become a purported canonical hop.
        observed = set(input_keys(graph.transactions.get(current[0])))
        parents = sorted(set(graph.evaluated_parents.get(current, ())) & observed, reverse=True)
        traversed = False
        for parent in parents:
            parent_evidence = graph.memo.get(parent)
            if is_anchor and parent_evidence and parent_evidence.entry_frontier:
                continue
            traversed = True
            stack.append((parent, downstream + (current,), visited | {current}))
        if not traversed and not is_anchor:
            yield EvidencePath(chain, status='INCOMPLETE')


def add_input_evidence(evidence, e):
    """Merge one Exit input's evaluated evidence into the Exit evidence.

    Shared by the Exit projection below and the Exit editor candidates (FX-1),
    so both read the same class and anchors per input.
    """
    evidence.acquisitions.update(e.acquisitions)
    evidence.no_acquisition_anchors.update(e.no_acquisition_anchors)
    evidence.acquisition_origins.update(e.acquisition_origins)
    evidence.unresolved_origins.update(e.unresolved_origins)
    evidence.review.update(e.review)
    if e.entry_frontier:
        evidence.review.add('An Exit input has no established self-custody Acquisition or No-Acquisition origin.')


async def exit_input_evidence(db, engine, inputs_by_exit, settings, deadline):
    """Per-input and per-Exit evidence for Exit transactions not yet recorded.

    ``inputs_by_exit`` maps an exit txid to its input outpoints. Same graph and
    evaluator as exit_provenance_rows; read-only, no allocation.
    """
    keys = {key for inputs in inputs_by_exit.values() for key in inputs}
    graph, _ = await provenance_graph(db, engine, keys, settings, deadline)
    per_input, per_exit = {}, {}
    for txid, inputs in inputs_by_exit.items():
        per_exit[txid] = Evidence()
        for key in inputs:
            per_input[key] = graph.evaluate(key)
            add_input_evidence(per_exit[txid], per_input[key])
    return per_input, per_exit


async def exit_events(db, from_date=None, to_date=None):
    start, end = date_range(from_date, to_date)
    clauses, params = [], []
    if start is not None or end is not None:
        clauses += ['e.block_time IS NOT NULL', 'e.block_height IS NOT NULL']
    if start is not None:
        clauses.append('e.block_time >= ?'); params.append(start)
    if end is not None:
        clauses.append('e.block_time < ?'); params.append(end)
    return await rows(db, "SELECT e.*, o.vout AS exit_vout, o.address AS exit_address, o.amount_sats "
        "FROM exchange_exits e LEFT JOIN exchange_exit_outputs o "
        "ON o.exchange_exit_id=e.id AND o.is_exchange_deposit=1 " +
        ('WHERE ' + ' AND '.join(clauses) if clauses else '') +
        ' ORDER BY e.block_time IS NULL, e.block_time, e.txid, e.id, o.vout', params)


async def exit_provenance_rows(db, engine, settings, deadline, *, from_date=None, to_date=None):
    """Reusable tabular evidence projection; serialization is separate below."""
    events = await exit_events(db, from_date, to_date)
    if not events:
        return []
    transactions = {r['txid']: r for r in await selected_rows(db, 'transactions', 'txid', {e['txid'] for e in events})}
    if sum(len(r.get('raw_json') or '') for r in transactions.values()) > MAX_BYTES:
        raise ReadBudgetExceeded('Exit transaction evidence size limit exceeded.')
    raw_exits = {txid: parsed(r['raw_json']) for txid, r in transactions.items()}
    inputs = {k for raw in raw_exits.values() for k in input_keys(raw)}
    graph, data = await provenance_graph(db, engine, inputs, settings, deadline, capture_paths=True)
    raw_by_txid = dict(graph.transactions, **raw_exits)
    validity = {}

    def valid_path_transaction(key):
        if key not in validity:
            graph._charge()
            validity[key] = valid_transaction(raw_by_txid.get(key[0]), key)
        return validity[key]
    acquisitions = {a['id']: a for a in await rows(db, 'SELECT * FROM acquisitions ORDER BY id')}
    by_origin = {(a['origin_txid'], a['origin_vout']): a for a in acquisitions.values()}
    recorded = {}
    for link in await rows(db, 'SELECT exchange_exit_id, acquisition_id FROM exchange_exit_sources ORDER BY acquisition_id'):
        recorded.setdefault(link['exchange_exit_id'], set()).add(link['acquisition_id'])
    saved = await annotations(db, engine)
    txids = set(graph.transactions) | {k[0] for k in data.outpoints} | set(transactions)
    transactions.update({r['txid']: r for r in await selected_rows(db, 'transactions', 'txid', txids,
                                                                limit=settings.coverage_max_transactions)})
    facts = dict(data.outpoints)
    for r in await selected_rows(db, 'outpoints', 'txid', {e['txid'] for e in events}, limit=settings.coverage_max_outpoints):
        facts[r['txid'], r['vout']] = r

    def output_facts(key):
        tx = transactions.get(key[0], {})
        a = by_origin.get(key, {})
        # Original Acquisition facts were node-resolved, not user amount inputs.
        fallback = dict(address=a.get('origin_address'), amount_sats=a.get('origin_amount_sats'),
                        block_height=a.get('origin_block_height') if a.get('origin_block_height') is not None else tx.get('block_height'),
                        block_time=a.get('origin_block_time') if a.get('origin_block_time') is not None else tx.get('block_time'))
        stored = facts.get(key, {})
        return {field: stored[field] if stored.get(field) is not None else value for field, value in fallback.items()}

    result, bytes_used = [], 0
    for event in events:
        graph._charge()
        exit_key = (event['txid'], event['exit_vout'])
        raw = raw_exits.get(event['txid'])
        keys = input_keys(raw)
        evidence = Evidence()
        paths, estimated_rows = set(), 0
        if not event['verified']:
            evidence.review.add('Recorded Exit has not been verified.')
        final_valid = valid_path_transaction(exit_key)
        if not final_valid:
            evidence.review.add('Required Exit transaction input/output evidence is missing or inconsistent.')
        for key in keys:
            add_input_evidence(evidence, graph.evaluate(key))
            if not final_valid:
                continue
            for path in paths_from_input(graph, key, exit_key):
                if path in paths:
                    continue
                paths.add(path)
                estimated_rows += max(1, len(path.nodes) - 1)
                if len(paths) > MAX_PATHS or estimated_rows + len(result) > MAX_ROWS:
                    raise ReadBudgetExceeded('Exit provenance path/row limit exceeded.')
        connected = {p.acquisition_id for p in paths if p.acquisition_id is not None}
        missing_links = recorded.get(event['id'], set()) - connected
        if missing_links:
            evidence.review.add('A recorded Acquisition relationship has no established on-chain path in the available evidence.')
            for aid in sorted(missing_links):
                paths.add(EvidencePath((), acquisition_id=aid, status='NO_PATH_EVIDENCE'))
        if not paths:
            paths.add(EvidencePath((), status='NO_PATH_EVIDENCE'))
        if len(paths) > MAX_PATHS:
            raise ReadBudgetExceeded('Exit provenance path limit exceeded.')
        if any(p.status != 'CONNECTED' for p in paths):
            evidence.review.add('Some required ancestry paths are incomplete or unavailable.')
        inconsistent_paths = {p for p in paths if any(not valid_path_transaction(dest)
            for _, dest in zip(p.nodes, p.nodes[1:]))}
        if inconsistent_paths:
            evidence.review.add('Required path transaction input/output evidence is inconsistent.')
        if final_valid:
            raw_output = next(o for o in raw['vout'] if o['n'] == event['exit_vout'])
            try:
                if sats(raw_output['value']) != event['amount_sats']:
                    evidence.review.add('Recorded Exit amount differs from cached transaction evidence.')
            except (ValueError, KeyError, TypeError, ArithmeticError):
                evidence.review.add('Exit output value cannot be verified from cached transaction evidence.')
        provenance = evidence.provenance_class
        notes = AMOUNT_NOTE + (' ' + ' '.join(sorted(evidence.review)) if evidence.review else '')
        vout_label = event['exit_vout'] if event['exit_vout'] is not None else 'unknown'
        for index, path in enumerate(sorted(paths, key=EvidencePath.sort_key), 1):
            graph._charge(len(path.nodes) + 1)
            a = acquisitions.get(path.acquisition_id, {})
            origin = output_facts((a['origin_txid'], a['origin_vout'])) if a else {}
            n = path.no_acquisition_anchor
            metadata = saved.get(n, {})
            base = dict(export_schema_version=SCHEMA_VERSION, exit_id=event['id'],
                exit_date=safe_text(event['exit_date']),
                exit_txid=text_identity(event['txid']), exit_vout=event['exit_vout'],
                exit_outpoint=text_identity(outpoint(exit_key)) if event['exit_vout'] is not None else '',
                exit_address=safe_text(event['exit_address']), exit_amount_btc=btc(event['amount_sats']),
                exit_block_height=event['block_height'], exit_block_time=iso_time(event['block_time']),
                acquisition_id=path.acquisition_id, acquisition_date=safe_text(a.get('acquisition_date')),
                acquisition_source=safe_text(a.get('source')),
                acquisition_outpoint=text_identity(outpoint((a['origin_txid'], a['origin_vout']))) if a else '',
                acquisition_address=safe_text(a.get('origin_address') if a.get('origin_address') is not None else origin.get('address')),
                acquisition_amount_btc=btc(a.get('origin_amount_sats')),
                acquisition_block_height=a.get('origin_block_height') if a.get('origin_block_height') is not None else origin.get('block_height'),
                acquisition_block_time=iso_time(a.get('origin_block_time') if a.get('origin_block_time') is not None else origin.get('block_time')),
                no_acquisition_anchor_outpoint=text_identity(outpoint(n)) if n else '',
                no_acquisition_source=safe_text(metadata.get('source')), no_acquisition_note=safe_text(metadata.get('note')),
                path_id=f"exit-{event['id']}-vout-{vout_label}-path-{index:04d}",
                provenance_class=provenance, path_status=path.status, evidence_notes=notes)
            hops = list(zip(path.nodes, path.nodes[1:]))
            if path in inconsistent_paths:
                base['path_status'] = 'INCOMPLETE'
            if not hops:
                rows_for_path = [dict(base, hop_index=None, evidence_notes=notes + ' No on-chain hop is asserted by this diagnostic row.')]
            else:
                rows_for_path = []
                for hop, (source, dest) in enumerate(hops):
                    f, t = output_facts(source), output_facts(dest)
                    tx = transactions.get(dest[0], {})
                    rows_for_path.append(dict(base, hop_index=hop,
                        from_outpoint=text_identity(outpoint(source)), from_address=safe_text(f.get('address')),
                        from_amount_btc=btc(f.get('amount_sats')), from_block_height=f.get('block_height'),
                        from_block_time=iso_time(f.get('block_time')), spending_txid=text_identity(dest[0]),
                        to_outpoint=text_identity(outpoint(dest)), to_address=safe_text(t.get('address')),
                        to_amount_btc=btc(t.get('amount_sats')), block_height=tx.get('block_height'), block_time=iso_time(tx.get('block_time'))))
            for row in rows_for_path:
                result.append(row)
                bytes_used += sum(len(str(v).encode('utf-8')) for v in row.values() if v is not None)
                if len(result) > MAX_ROWS or bytes_used > MAX_BYTES:
                    raise ReadBudgetExceeded('Exit provenance export size limit exceeded.')
            await asyncio.sleep(0)
    return result


async def exit_provenance_csv(db, engine, settings, deadline, **filters):
    records = await exit_provenance_rows(db, engine, settings, deadline, **filters)
    return render_csv(COLUMNS, ([r.get(c) for c in COLUMNS] for r in records))
