"""Blockchain History Report (B1): read model, read-only.

Same source and rules as Exit-Provenance CSV v2 (``exit_provenance_v2``): Exits,
class and anchors come exclusively from ``exit_provenance_rows``; the
self-custody basis uses v2's ``basis_of``. This module only reshapes the
evaluator's path rows into a per-Exit graph. No shares, percentages, fiat
values, current holdings, wallet structure, xpubs/descriptors or sat-level
attribution between inputs (§2.5, §17.5). Free text is only read on request.
"""
from datetime import datetime, timezone

from backend.reporting.exit_provenance_v2 import (
    connected_path, identity, load_exit_evidence, path_anchor, path_nodes, split_outpoint, to_sats)
from backend.reporting.read_data import input_keys, selected_rows
from backend.reporting.exit_provenance import parsed
from backend.version import APP_VERSION

REPORT_CLASSES = ('ACQUISITION', 'NO_ACQUISITION', 'MIXED', 'UNRESOLVED')


def epoch(iso):
    """Epoch seconds from the evaluator's ISO UTC timestamp; None when unavailable."""
    if not iso:
        return None
    return int(datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone(timezone.utc).timestamp())


def node_facts(key, **facts):
    return dict(txid=key[0], vout=key[1], **facts)


def hop_record(row, basis):
    source, dest = split_outpoint(row['from_outpoint']), split_outpoint(row['to_outpoint'])
    return dict(
        hop=row['hop_index'] + 1, source=source, spending_txid=identity(row['spending_txid']), dest=dest,
        source_amount_sats=to_sats(row['from_amount_btc']), dest_amount_sats=to_sats(row['to_amount_btc']),
        source_height=row['from_block_height'], source_time=epoch(row['from_block_time']),
        block_height=row['block_height'], block_time=epoch(row['block_time']),
        # Basis of the receiving output; None for the Exit output (end of the chain).
        dest_basis=basis.get(dest))


def anchor_record(kind, key, first, evidence, include_free_text):
    # User text is read raw (B5-03): the v1 rows carry the CSV formula guard.
    if kind == 'ACQUISITION':
        acquisition = evidence.acquisitions.get(first['acquisition_id'], {})
        record = dict(type=kind, amount_sats=to_sats(first['acquisition_amount_btc']),
                      block_height=first['acquisition_block_height'], block_time=epoch(first['acquisition_block_time']),
                      user=dict(date=acquisition.get('acquisition_date'), source=acquisition.get('source'),
                                reference=acquisition.get('reference')))
    else:
        record = dict(type=kind, amount_sats=to_sats(first['from_amount_btc']),
                      block_height=first['from_block_height'], block_time=epoch(first['from_block_time']),
                      user=dict(source=evidence.annotations.get(key, {}).get('source')))
        if include_free_text:
            record['user']['review_comment'] = evidence.resolutions.get(key)
    return dict(record, txid=key[0], vout=key[1])


def transaction_inputs(raw_rows):
    return {r['txid']: len(input_keys(parsed(r.get('raw_json')))) for r in raw_rows}


def common_suffix_length(paths):
    """Longest common suffix of the hop sequences, compared by (spending tx, receiving output).

    The source output of a hop is per-path (the merging transaction has one input per
    path), so it does not take part in the comparison. Every established path ends with
    the Exit transaction and the Exit output, so the suffix is never empty.
    """
    hops = [p['hops'] for p in paths]
    limit = min(len(h) for h in hops)
    length = 0
    while length < limit and len({(h[-1 - length]['spending_txid'], h[-1 - length]['dest']) for h in hops}) == 1:
        length += 1
    return length


def merged_view(entry):
    """Shared-section view for an Exit with several anchors (owner decision 2026-10-24).

    The common section is the longest common suffix of all established paths; hops
    before it are the feeder hops of the individual anchors. Per anchor, the entry
    point is the first consolidation on its path: the first transaction it shares
    with another anchor's path (at the latest the Exit transaction). The trunk is
    every output from the entry points onward, for the graph. Sums are totals of
    whole anchor outputs; no sat attribution between anchors is derived (§2.5, §17.5).
    """
    paths = entry['paths']
    suffix = common_suffix_length(paths)
    representative = min(paths, key=lambda p: (len(p['hops']), str(p['anchor'])))
    common = [dict(h, hop=i) for i, h in enumerate(representative['hops'][-suffix:], 1)]
    entry_sources = {p['hops'][-suffix]['source'] for p in paths}
    if len(entry_sources) > 1:
        # The merging hop: its inputs are per-anchor facts, not part of the common section.
        common[0].update(source=None, source_amount_sats=None, source_height=None, source_time=None)
    tx_anchors = {}
    for path in paths:
        for hop in path['hops']:
            tx_anchors.setdefault(hop['spending_txid'], set()).add(path['anchor'][1])
    per_anchor = {}
    for path in paths:
        own = path['hops'][:-suffix]
        info = per_anchor.setdefault(path['anchor'][1], dict(hops=[], seen=set(), rank=None, rep=None))
        rank = (len(path['hops']), [(h['spending_txid'], h['dest']) for h in path['hops']])
        if info['rank'] is None or rank < info['rank']:
            info['rank'], info['rep'] = rank, path
        for hop in own:
            signature = (hop['source'], hop['spending_txid'], hop['dest'])
            if signature not in info['seen']:
                info['seen'].add(signature)
                info['hops'].append(hop)
    ordered = sorted(entry['anchors'],
                     key=lambda a: (a['block_height'] is None, a['block_height'] or 0, a['txid'], a['vout']))
    anchors, feeders, trunk = [], [], set()
    for nr, a in enumerate(ordered, 1):
        info = per_anchor[(a['txid'], a['vout'])]
        hops = info['rep']['hops']
        merge = next(i for i, h in enumerate(hops) if len(tx_anchors[h['spending_txid']]) > 1)
        trunk.update(h['dest'] for h in hops[merge:])
        anchors.append(dict(a, nr=nr, feeder_hops=len(hops) - suffix,
                            entry_node=hops[merge]['dest'], entry_txid=hops[merge]['spending_txid']))
        if info['hops']:
            feeders.append(dict(nr=nr, anchor=(a['txid'], a['vout']),
                                hops=[dict(h, hop=i) for i, h in enumerate(info['hops'], 1)]))
    return dict(anchors=anchors, feeders=feeders, common=common, trunk=sorted(trunk),
                entry_source_count=len(entry_sources),
                anchor_total_sats=sum(a['amount_sats'] or 0 for a in ordered))


async def build_report(db, engine, settings, deadline, *, years=None, exit_ids=None, include_free_text=False,
                       scope=None, created_at, data_revision):
    """Report structure for the selection (same filters as the v2 CSV export)."""
    evidence = await load_exit_evidence(db, engine, settings, deadline, years=years, exit_ids=exit_ids)
    spending = {identity(r['spending_txid']) for paths in evidence.by_exit.values()
                for rows_of in paths.values() if connected_path(rows_of) for r in rows_of}
    input_counts = transaction_inputs(await selected_rows(db, 'transactions', 'txid', spending)) if spending else {}
    exits = []
    for event in evidence.events:
        paths = evidence.by_exit.get((event['id'], event['exit_vout']), {})
        any_rows = next((p[0] for p in paths.values()), None)
        report_class = any_rows['provenance_class'] if any_rows else 'UNRESOLVED'
        entry = dict(
            txid=event['txid'], vout=event['exit_vout'], amount_sats=event['amount_sats'],
            block_height=event['block_height'], block_time=event['block_time'],
            report_class=report_class, allocation_check=evidence.checks[event['id']],
            anchors=[], paths=[], edges=[], nodes={}, open_parts=[])
        if include_free_text:
            entry['free_text'] = dict(broker=event.get('broker'), notes=event.get('notes'),
                                      reference=event.get('reference'))
        anchors, edges, node_info = {}, {}, {}
        for path_rows in paths.values():
            if not connected_path(path_rows):
                continue  # recorded-only and incomplete paths are not established chains
            kind, key = path_anchor(path_rows)
            first = path_rows[0]
            anchors.setdefault((kind, key), anchor_record(kind, key, first, evidence, include_free_text))
            hops = [hop_record(r, evidence.basis) for r in path_rows]
            entry['paths'].append(dict(anchor=(kind, key), hops=hops))
            for hop in hops:
                edges.setdefault((hop['source'], hop['dest']), dict(
                    source=hop['source'], dest=hop['dest'], spending_txid=hop['spending_txid'],
                    block_height=hop['block_height'], block_time=hop['block_time']))
                node_info.setdefault(hop['source'], dict(
                    amount_sats=hop['source_amount_sats'], block_height=hop['source_height'],
                    block_time=hop['source_time'], basis=evidence.basis.get(hop['source'])))
                node_info.setdefault(hop['dest'], dict(
                    amount_sats=hop['dest_amount_sats'], block_height=hop['block_height'],
                    block_time=hop['block_time'], basis=hop['dest_basis']))
        # Co-inputs: inputs of the spending transaction that are not drawn nodes of this graph.
        # Count only, never an attribution (§2.5).
        for edge in edges.values():
            total = input_counts.get(edge['spending_txid'])
            drawn_inputs = len({s for (s, d) in edges if d[0] == edge['spending_txid']})
            edge['other_inputs'] = None if total is None else max(0, total - drawn_inputs)
        entry['anchors'] = [anchors[k] for k in sorted(anchors, key=lambda k: (k[0] != 'ACQUISITION', k[1]))]
        entry['edges'] = sorted(edges.values(), key=lambda e: (e['source'], e['dest']))
        exit_key = (event['txid'], event['exit_vout'])
        entry['nodes'] = dict(node_info)
        entry['exit_key'] = exit_key
        if exit_key in entry['nodes']:
            entry['nodes'][exit_key].update(amount_sats=event['amount_sats'], basis=None)
        entry['anchor_keys'] = [(a['txid'], a['vout']) for a in entry['anchors']]
        # Exits with several anchors additionally carry the shared-section view; the
        # canonical anchors/paths/edges above stay identical to the CSV v2 selection.
        entry['merged'] = merged_view(entry) if len(entry['anchors']) >= 2 else None
        exits.append(entry)
    await evidence.ensure_unchanged(db)
    return dict(
        meta=dict(app_version=APP_VERSION, created_at_utc=created_at, data_revision=data_revision,
                  years='all' if years is None else list(years), scope=scope, include_free_text=include_free_text),
        overview=dict(exit_count=len(exits), total_exit_sats=sum(e['amount_sats'] or 0 for e in exits),
                      class_counts={c: sum(e['report_class'] == c for e in exits) for c in REPORT_CLASSES}),
        exits=exits, appendix=appendix(exits))


def appendix(exits):
    """Every TXID in the report with its block height, sorted by height (unconfirmed last)."""
    heights = {}
    for e in exits:
        heights.setdefault(e['txid'], e['block_height'])
        for key, info in e['nodes'].items():
            if heights.get(key[0]) is None:
                heights[key[0]] = info['block_height']
        for edge in e['edges']:
            if heights.get(edge['spending_txid']) is None:
                heights[edge['spending_txid']] = edge['block_height']
        for a in e['anchors']:
            if heights.get(a['txid']) is None:
                heights[a['txid']] = a['block_height']
    return [dict(txid=t, block_height=h) for t, h in
            sorted(heights.items(), key=lambda kv: (kv[1] is None, kv[1] or 0, kv[0]))]
