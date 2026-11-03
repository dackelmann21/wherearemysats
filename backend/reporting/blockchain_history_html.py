"""Blockchain History Report (B1): server-rendered, self-contained HTML with static SVG graphs.

No external library, script, font or network access. Deterministic: the same
report structure yields byte-identical markup. Every user-controlled string
passes ``esc`` (HTML/SVG escaping, control and format characters removed).
"""
import html
import math
import re
import unicodedata
from datetime import datetime, timezone

TITLE = 'Blockchain History Report'
BASIS_LABEL = {
    'WALLET_DERIVATION': 'Wallet derivation', 'ADDRESS_IMPORT': 'Address import',
    'USER_CONFIRMATION': 'User confirmation', 'NOT_ESTABLISHED': 'Not established',
}
BASIS_SHORT = {'WALLET_DERIVATION': 'derivation', 'ADDRESS_IMPORT': 'import',
               'USER_CONFIRMATION': 'confirmed', 'NOT_ESTABLISHED': 'not established'}
CLASS_LABEL = {'ACQUISITION': 'ACQUISITION', 'NO_ACQUISITION': 'No acquisition (verified)',
               'MIXED': 'MIXED', 'UNRESOLVED': 'UNRESOLVED'}
CLASS_COLOR = {'ACQUISITION': '#1f5fa8', 'NO_ACQUISITION': '#8a5a00', 'MIXED': '#5b3a8f', 'UNRESOLVED': '#b3261e'}
DISCLAIMER = ('This report shows observable on-chain connections between the listed transaction outputs. '
              'No individual satoshi identity is asserted across transactions with several inputs or outputs. '
              'Entries marked "user-provided" are statements by the operator and have not been verified. '
              'It is not tax or legal advice and not a proof of ownership.')
MAX_HOPS_BEFORE_CONDENSING = 12
MAX_LISTED_PATHS = 20
GROUP_EXITS_ABOVE = 40
GROUP_ANCHORS_ABOVE = 5  # above this, multi-anchor chain graphs use group nodes


def clean(value):
    text = '' if value is None else str(value)
    return ''.join(' ' if unicodedata.category(c) in ('Cc', 'Cf', 'Zl', 'Zp') else c for c in text)


def esc(value):
    return html.escape(clean(value), quote=True)


def css_text(value):
    """Safe literal for a CSS ``content: "..."`` string (server-generated values only)."""
    return re.sub(r'[^A-Za-z0-9 :._+-]', '', clean(value))


def short(key, head=6, tail=4):
    txid, vout = key
    return f'{txid[:head]}…{txid[-tail:]}:{vout}'


def short_tx(txid, head=8, tail=6):
    return f'{txid[:head]}…{txid[-tail:]}'


def when(timestamp):
    return '—' if timestamp is None else datetime.fromtimestamp(timestamp, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def day(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime('%Y-%m-%d')


def sats(value):
    return '—' if value is None else f'{value:,} sats'


def btc(value):
    return '—' if value is None else format(value / 100_000_000, '.8f')


def height(value):
    return '—' if value is None else str(value)


def basis_label(basis):
    return '—' if basis is None else BASIS_LABEL.get(basis, 'Not established')


# ---------------------------------------------------------------------------
# Shapes (class/anchor type are distinguishable without colour)
# ---------------------------------------------------------------------------

def shape(kind, x, y, color, r=6, hollow=False, dashed=False):
    fill = '#fff' if hollow else color
    stroke = f'stroke="{color}" stroke-width="1.6"' + (' stroke-dasharray="2 2"' if dashed else '')
    if kind == 'circle':
        return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{fill}" {stroke}/>'
    if kind == 'diamond':
        pts = f'{x:.1f},{y - r - 1:.1f} {x + r + 1:.1f},{y:.1f} {x:.1f},{y + r + 1:.1f} {x - r - 1:.1f},{y:.1f}'
        return f'<polygon points="{pts}" fill="{fill}" {stroke}/>'
    if kind == 'square':
        return f'<rect x="{x - r:.1f}" y="{y - r:.1f}" width="{2 * r}" height="{2 * r}" fill="{fill}" {stroke}/>'
    pts = f'{x:.1f},{y - r - 1:.1f} {x + r + 1:.1f},{y + r:.1f} {x - r - 1:.1f},{y + r:.1f}'  # triangle
    return f'<polygon points="{pts}" fill="{fill}" {stroke}/>'


EXIT_SHAPE = {'ACQUISITION': 'circle', 'NO_ACQUISITION': 'diamond', 'MIXED': 'square', 'UNRESOLVED': 'triangle'}
ANCHOR_SHAPE = {'ACQUISITION': 'circle', 'NO_ACQUISITION': 'diamond'}
ANCHOR_COLOR = {'ACQUISITION': CLASS_COLOR['ACQUISITION'], 'NO_ACQUISITION': CLASS_COLOR['NO_ACQUISITION']}
SVG_STYLE = ('<style>text{font-family:Arial,Helvetica,sans-serif;fill:#111}.mono{font-family:Consolas,monospace}'
             '.muted{fill:#444}</style>')


def svg_open(width, height_, label):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height_}" role="img" '
            f'aria-label="{esc(label)}" width="{width}" height="{height_}">{SVG_STYLE}')


# ---------------------------------------------------------------------------
# Overview timeline
# ---------------------------------------------------------------------------

def month_ticks(start, end):
    """First-of-month UTC epochs within [start, end], then thinned to fit."""
    cursor = datetime.fromtimestamp(start, timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    ticks = []
    while cursor.timestamp() <= end:
        if cursor.timestamp() >= start:
            ticks.append((cursor.timestamp(), cursor.strftime('%Y-%m')))
        cursor = cursor.replace(year=cursor.year + (cursor.month == 12), month=cursor.month % 12 + 1)
    return ticks


def year_ticks(start, end):
    first, last = datetime.fromtimestamp(start, timezone.utc).year, datetime.fromtimestamp(end, timezone.utc).year
    ticks = []
    for year in range(first, last + 2):
        stamp = datetime(year, 1, 1, tzinfo=timezone.utc).timestamp()
        if start <= stamp <= end:
            ticks.append((stamp, str(year)))
    return ticks


def timeline_points(report):
    """Anchors feeding a resolved Exit, Exits with a block time, and anchor->Exit links."""
    anchors, exits, links, undated = {}, {}, set(), 0
    for e in report['exits']:
        if e['block_time'] is None:
            undated += 1
            continue
        exit_key = (e['txid'], e['vout'])
        exits[exit_key] = dict(time=e['block_time'], cls=e['report_class'])
        if e['report_class'] == 'UNRESOLVED':
            continue  # no line for an Exit whose origin is not established
        for a in e['anchors']:
            if a['block_time'] is None:
                continue
            key = (a['type'], a['txid'], a['vout'])
            anchors[key] = dict(time=a['block_time'], type=a['type'])
            links.add((key, exit_key))
    return anchors, exits, links, undated


def group_points(points, many, label):
    """Individual points, or (day, kind) groups with a count when there are many Exits."""
    if not many:
        return [dict(keys=[k], time=p['time'], kind=p[label], count=1) for k, p in sorted(points.items(), key=lambda kv: (kv[1]['time'], kv[0]))]
    groups = {}
    for key, p in sorted(points.items(), key=lambda kv: (kv[1]['time'], kv[0])):
        groups.setdefault((day(p['time']), p[label]), dict(keys=[], time=p['time'], kind=p[label], count=0))
        g = groups[(day(p['time']), p[label])]
        g['keys'].append(key); g['count'] += 1
    return list(groups.values())


def timeline_svg(report):
    anchors, exits, links, undated = timeline_points(report)
    unresolved_total = sum(e['report_class'] == 'UNRESOLVED' for e in report['exits'])
    if not exits:
        return '<p class="muted">No Exit with a confirmed block time in this selection; no timeline can be drawn.</p>'
    many = len(report['exits']) > GROUP_EXITS_ABOVE or len(anchors) > GROUP_EXITS_ABOVE
    anchor_groups = group_points(anchors, many, 'type')
    exit_groups = group_points(exits, many, 'cls')
    times = [g['time'] for g in anchor_groups + exit_groups]
    start, end = min(times), max(times)
    if end - start < 86_400 * 30:
        start, end = start - 86_400 * 15, end + 86_400 * 15
    pad = (end - start) * 0.03
    start, end = start - pad, end + pad
    width, left, right = 900, 70, 30
    scale = (width - left - right) / (end - start)
    xof = lambda t: left + (t - start) * scale
    months = (end - start) / (86_400 * 30.44)
    ticks = month_ticks(start, end) if months <= 24 else year_ticks(start, end)
    per_tick = (width - left - right) / max(1, len(ticks))
    thin = max(1, math.ceil(52 / per_tick))
    # FIX-UI-6: anchors sit in at most two rows (not four), Exits keep four lanes.
    top_base, bottom_base, anchor_lanes, exit_lanes, lane_gap = 62, 212, 2, 4, 17
    axis_y, legend_y = 292, 322
    height_ = 430
    out = [svg_open(width, height_, 'Overview timeline: origin anchors above, Exits below, lines where an anchor feeds an Exit')]
    out.append(f'<text x="{left}" y="22" font-size="12" font-weight="bold">Overview timeline (block time, UTC)</text>')
    out.append(f'<text x="8" y="{top_base - 20}" font-size="10" class="muted">Origin anchors</text>')
    out.append(f'<text x="8" y="{bottom_base - 20}" font-size="10" class="muted">Exits</text>')
    out.append(f'<line x1="{left}" y1="{axis_y}" x2="{width - right}" y2="{axis_y}" stroke="#333" stroke-width="1"/>')
    for index, (stamp, label) in enumerate(ticks):
        x = xof(stamp)
        out.append(f'<line x1="{x:.1f}" y1="{axis_y}" x2="{x:.1f}" y2="{axis_y + 5}" stroke="#333"/>')
        out.append(f'<line x1="{x:.1f}" y1="40" x2="{x:.1f}" y2="{axis_y}" stroke="#ddd" stroke-width="0.6"/>')
        if index % thin == 0:
            out.append(f'<text x="{x:.1f}" y="{axis_y + 17}" font-size="9" text-anchor="middle">{esc(label)}</text>')
    position = {}
    for groups, base, lanes in ((anchor_groups, top_base, anchor_lanes),
                                (exit_groups, bottom_base, exit_lanes)):
        for index, g in enumerate(groups):
            pos = (xof(g['time']), base + (index % lanes) * lane_gap)
            for key in g['keys']:
                position[key] = pos
            g['pos'] = pos
    # An Exit fed by more than GROUP_EXITS_ABOVE anchor points gets one narrow
    # bracket "Exit N" under the anchor rows (first to last anchor) plus one
    # connector line instead of a line fan (FIX-UI-6: spans of several Exits
    # are never stacked as filled areas; a very light fill remains only while
    # there is exactly one span). A bracket, like a line, says nothing about
    # amounts or shares.
    linked = {}
    for (a_key, e_key) in links:
        linked.setdefault(e_key, set()).add(a_key)
    funnel_exits = {e_key for e_key, a_keys in linked.items()
                    if len({position[a] for a in a_keys}) > GROUP_EXITS_ABOVE}
    band_y = top_base + anchor_lanes * lane_gap + 8
    ordered_funnels = sorted(funnel_exits, key=lambda k: (exits[k]['time'], k))
    for number, e_key in enumerate(ordered_funnels, 1):
        (ex, ey) = position[e_key]
        xs = sorted({position[a][0] for a in linked[e_key]})
        y = band_y + (number - 1) * 14
        color = CLASS_COLOR[exits[e_key]['cls']]
        if len(ordered_funnels) == 1:
            out.append(f'<polygon points="{xs[0]:.1f},{y:.1f} {xs[-1]:.1f},{y:.1f} {ex:.1f},{ey:.1f}" '
                       f'fill="{color}" fill-opacity="0.08" stroke="none"/>')
        out.append(f'<path class="exit-span" d="M {xs[0]:.1f} {y - 4:.1f} V {y:.1f} H {xs[-1]:.1f} V {y - 4:.1f}" '
                   f'fill="none" stroke="{color}" stroke-width="1"/>')
        out.append(f'<line x1="{(xs[0] + xs[-1]) / 2:.1f}" y1="{y:.1f}" x2="{ex:.1f}" y2="{ey:.1f}" '
                   f'stroke="{color}" stroke-width="0.8" stroke-opacity="0.5"/>')
        out.append(f'<text x="{(xs[0] + xs[-1]) / 2:.1f}" y="{y + 11:.1f}" font-size="9" '
                   f'text-anchor="middle" fill="{color}">Exit {number}</text>')
    drawn = set()
    for (a_key, e_key) in sorted(links):
        if e_key in funnel_exits:
            continue
        (ax, ay), (ex, ey) = position[a_key], position[e_key]
        if (ax, ay, ex, ey) in drawn:
            continue
        drawn.add((ax, ay, ex, ey))
        dash = ' stroke-dasharray="6 4"' if a_key[0] == 'NO_ACQUISITION' else ''
        out.append(f'<line x1="{ax:.1f}" y1="{ay:.1f}" x2="{ex:.1f}" y2="{ey:.1f}" stroke="{ANCHOR_COLOR[a_key[0]]}" '
                   f'stroke-width="1" stroke-opacity="0.65"{dash}/>')
    for g in anchor_groups:
        out.append(shape(ANCHOR_SHAPE[g['kind']], *g['pos'], ANCHOR_COLOR[g['kind']]))
        if g['count'] > 1:
            out.append(f'<text x="{g["pos"][0] + 9:.1f}" y="{g["pos"][1] + 3:.1f}" font-size="9">×{g["count"]}</text>')
    for g in exit_groups:
        out.append(shape(EXIT_SHAPE[g['kind']], *g['pos'], CLASS_COLOR[g['kind']], hollow=g['kind'] == 'UNRESOLVED', dashed=g['kind'] == 'UNRESOLVED'))
        if g['count'] > 1:
            out.append(f'<text x="{g["pos"][0] + 9:.1f}" y="{g["pos"][1] + 3:.1f}" font-size="9">×{g["count"]}</text>')
    legend = [(ANCHOR_SHAPE['ACQUISITION'], ANCHOR_COLOR['ACQUISITION'], False, 'Anchor: Acquisition (solid line)'),
              (ANCHOR_SHAPE['NO_ACQUISITION'], ANCHOR_COLOR['NO_ACQUISITION'], False, 'Anchor: No acquisition (dashed line)')]
    legend += [(EXIT_SHAPE[c], CLASS_COLOR[c], c == 'UNRESOLVED', f'Exit: {CLASS_LABEL[c]}' + (' (no line)' if c == 'UNRESOLVED' else ''))
               for c in ('ACQUISITION', 'NO_ACQUISITION', 'MIXED', 'UNRESOLVED')]
    for index, (kind, color, hollow, text) in enumerate(legend):
        x, y = 12 + (index % 2) * 330, legend_y + (index // 2) * 20
        out.append(shape(kind, x + 6, y, color, r=5, hollow=hollow, dashed=hollow))
        out.append(f'<text x="{x + 20}" y="{y + 3}" font-size="10">{esc(text)}</text>')
    out.append(f'<text x="12" y="{legend_y + 74}" font-size="9" class="muted">Lines only join an origin anchor to an Exit it feeds; '
               f'a line says nothing about amounts or shares.'
               + (' Points of the same day and kind are combined (×N).' if many else '') + '</text>')
    notes = []
    if funnel_exits:
        notes.append(f'{len(funnel_exits)} Exit(s) fed by more than {GROUP_EXITS_ABOVE} anchor points: '
                     'bracket "Exit N" from the first to the last anchor instead of individual lines.')
    if unresolved_total:
        notes.append(f'{unresolved_total} UNRESOLVED Exit(s) drawn without a line.')
    if undated:
        notes.append(f'{undated} unconfirmed Exit(s) not drawn.')
    if notes:
        out.append(f'<text x="12" y="{legend_y + 90}" font-size="9" class="muted">{esc(" ".join(notes))}</text>')
    out.append('</svg>')
    return ''.join(out)


# ---------------------------------------------------------------------------
# Chain graph per Exit
# ---------------------------------------------------------------------------

def layers_of(nodes, edges):
    """Longest-path layer per node (sources at 0); edges is a set of (source, dest)."""
    preds = {n: [] for n in nodes}
    for s, d in edges:
        preds[d].append(s)
    layer, order = {}, sorted(nodes)
    for _ in range(len(nodes) + 1):
        changed = False
        for n in order:
            value = 1 + max((layer.get(p, -1) for p in preds[n]), default=-1) if preds[n] else 0
            if layer.get(n) != value and all(p in layer for p in preds[n]):
                layer[n] = value; changed = True
        if not changed:
            break
    return layer


def longest_hops(nodes, edges):
    layer = layers_of(nodes, edges)
    return max(layer.values(), default=0)


def condense(nodes, edges, protected):
    """Fold long linear runs of plain intermediate nodes into one "… N Hops …" node.

    Applies only when some path has more than 12 hops. A run is a maximal chain of nodes with exactly one
    incoming and one outgoing edge that are neither anchors nor the Exit; it keeps its first and last
    node and folds the middle. The complete chain stays in the hop table.
    """
    nodes, edges, labels = set(nodes), set(edges), {}
    if longest_hops(nodes, edges) <= MAX_HOPS_BEFORE_CONDENSING:
        return nodes, edges, labels
    out_edges, in_edges = {}, {}
    for s, d in edges:
        out_edges.setdefault(s, []).append(d); in_edges.setdefault(d, []).append(s)
    plain = lambda n: n not in protected and len(in_edges.get(n, ())) == 1 and len(out_edges.get(n, ())) == 1
    seen = set()
    for n in sorted(nodes):
        if n in seen or not plain(n) or (plain(in_edges[n][0]) and in_edges[n][0] not in seen):
            continue
        run = [n]
        while plain(run[-1]) and plain(out_edges[run[-1]][0]):
            run.append(out_edges[run[-1]][0])
        seen.update(run)
        if len(run) < 4:
            continue
        middle = run[1:-1]
        summary = ('…', 'fold', run[1])
        hidden = len(run) - 1  # hops between the kept first and last node
        nodes -= set(middle); nodes.add(summary)
        edges -= {(s, d) for (s, d) in edges if s in middle or d in middle}
        edges |= {(run[0], summary), (summary, run[-1])}
        labels[summary] = hidden
    return nodes, edges, labels


def chain_layout(nodes, edges, layer):
    """Column/row positions by layer; one barycentre pass keeps connected nodes near each other."""
    columns = max(layer.values()) + 1
    by_layer = {}
    for n in sorted(nodes, key=lambda n: (str(n[0]), n[1] if isinstance(n[1], int) else 0)):
        by_layer.setdefault(layer[n], []).append(n)
    ypos = {}
    for index in range(columns):
        members = by_layer.get(index, [])
        if index:
            members.sort(key=lambda n: (sum(ypos[s] for s, d in edges if d == n and s in ypos) /
                                        max(1, sum(1 for s, d in edges if d == n and s in ypos)), str(n)))
        for row, n in enumerate(members):
            ypos[n] = row
    rows = max(len(m) for m in by_layer.values())
    width, margin = 900, 95
    step = min(260, (width - 2 * margin) / max(1, columns - 1)) if columns > 1 else 0
    row_h = 112
    height_ = 70 + rows * row_h + 40
    xof = lambda n: (width / 2 if columns == 1 else margin + layer[n] * step)
    yof = lambda n: 56 + ypos[n] * row_h + (rows - len(by_layer[layer[n]])) * row_h / 2
    return width, height_, step, xof, yof


ARROW_DEFS = ('<defs><marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" '
              'orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#333"/></marker></defs>')


def edge_markup(edges, edges_all, xof, yof, narrow, label_t=0.5):
    """Edges with tx labels at parameter ``label_t`` of the curve (0.5 = midpoint)."""
    out = []
    t = label_t
    for (s, d) in sorted(edges, key=str):
        x1, y1, x2, y2 = xof(s) + 9, yof(s), xof(d) - 9, yof(d)
        mid = (x1 + x2) / 2
        tx = (1 - t) ** 3 * x1 + t ** 3 * x2 + (3 * (1 - t) ** 2 * t + 3 * (1 - t) * t ** 2) * mid
        ty = ((1 - t) ** 3 + 3 * (1 - t) ** 2 * t) * y1 + (3 * (1 - t) * t ** 2 + t ** 3) * y2
        out.append(f'<path d="M{x1:.1f},{y1:.1f} C{mid:.1f},{y1:.1f} {mid:.1f},{y2:.1f} {x2:.1f},{y2:.1f}" '
                   f'fill="none" stroke="#333" stroke-width="1.2" marker-end="url(#arrow)"/>')
        info = edges_all.get((s, d))
        if info and not narrow:
            out.append(f'<text x="{tx:.1f}" y="{ty - 5:.1f}" font-size="8" text-anchor="middle" '
                       f'class="mono muted">tx {esc(short_tx(info["spending_txid"], 6, 4))}</text>')
        if info and info.get('other_inputs'):
            out.append(f'<text x="{tx:.1f}" y="{ty + 9:.1f}" font-size="8" text-anchor="middle" '
                       f'class="muted">+{info["other_inputs"]} more input{"s" if info["other_inputs"] != 1 else ""}</text>')
    return out


def chain_svg(exit_entry):
    edges_all = {(e['source'], e['dest']): e for e in exit_entry['edges']}
    if not edges_all:
        return ''
    nodes = {n for pair in edges_all for n in pair}
    anchor_type = {(a['txid'], a['vout']): a['type'] for a in exit_entry['anchors']}
    exit_key = exit_entry['exit_key']
    nodes, edges, folded = condense(nodes, set(edges_all), set(anchor_type) | {exit_key})
    layer = layers_of(nodes, edges)
    width, height_, step, xof, yof = chain_layout(nodes, edges, layer)
    narrow = step < 90
    out = [svg_open(width, height_, 'Chain graph from origin anchors (left) to the Exit output (right)')]
    out.append(ARROW_DEFS)
    out += edge_markup(edges, edges_all, xof, yof, narrow)
    for n in sorted(nodes, key=str):
        x, y = xof(n), yof(n)
        if n in folded:
            out.append(shape('square', x, y, '#555', r=5, hollow=True, dashed=True))
            out.append(f'<text x="{x:.1f}" y="{y + 24:.1f}" font-size="9" text-anchor="middle">… {folded[n]} Hops …</text>')
            continue
        facts = exit_entry['nodes'][n]
        if n == exit_key:
            out.append(shape('square', x, y, CLASS_COLOR[exit_entry['report_class']], r=7))
            lines = [short(n), sats(facts['amount_sats']), when(facts['block_time'])[:10], 'Exit output']
        else:
            if n in anchor_type:
                out.append(shape(ANCHOR_SHAPE[anchor_type[n]], x, y, ANCHOR_COLOR[anchor_type[n]], r=7))
                role = 'Acquisition anchor' if anchor_type[n] == 'ACQUISITION' else 'No-acq. anchor'
            else:
                out.append(shape('circle', x, y, '#333', r=5, hollow=True))
                role = None
            basis = BASIS_SHORT.get(facts.get('basis'), '')
            lines = [short(n), sats(facts['amount_sats']), when(facts['block_time'])[:10]]
            lines += [line for line in (role, basis) if line]
        size = 8 if narrow else 9 if step < 120 else 10
        for i, text in enumerate(lines):
            css = ' class="mono"' if i == 0 else ''
            out.append(f'<text x="{x:.1f}" y="{y + 22 + i * 11:.1f}" font-size="{size}" text-anchor="middle"{css}>{esc(text)}</text>')
    legend_y = height_ - 22
    items = [('circle', ANCHOR_COLOR['ACQUISITION'], False, 'Acquisition anchor'),
             ('diamond', ANCHOR_COLOR['NO_ACQUISITION'], False, 'No-acquisition anchor'),
             ('circle', '#333', True, 'Own-custody output'),
             ('square', CLASS_COLOR[exit_entry['report_class']], False, 'Exit output')]
    for index, (kind, color, hollow, text) in enumerate(items):
        x = 12 + index * 215
        out.append(shape(kind, x + 6, legend_y, color, r=5, hollow=hollow))
        out.append(f'<text x="{x + 18}" y="{legend_y + 3}" font-size="9">{esc(text)}</text>')
    out.append('</svg>')
    return ''.join(out)


def group_label(kind, count):
    if kind == 'ACQUISITION':
        return [f'{count} Acquisition' + ('s' if count != 1 else '')]
    return [f'{count} No-acquisition', '(verified)']


def grouped_chain_svg(exit_entry):
    """Chain graph with anchors combined into group nodes per entry point and anchor type.

    Drawn are the entry points, the common section and one group node per (entry point,
    anchor type); the anchors' feeder hops stay in the feeder table. A group edge carries
    the transaction that produced the entry output, never an amount or a split.
    """
    merged = exit_entry['merged']
    edges_all = {(e['source'], e['dest']): e for e in exit_entry['edges']}
    if not edges_all:
        return ''
    kept = {exit_entry['exit_key']} | set(merged['trunk'])
    edges = {(s, d) for (s, d) in edges_all if s in kept and d in kept}
    groups = {}
    for a in merged['anchors']:
        key = ('group', a['type'], a['entry_node'])
        groups[key] = groups.get(key, 0) + 1
    for key in groups:
        node = key[2]
        edges.add((key, node))
        edges_all.setdefault((key, node), dict(spending_txid=node[0], other_inputs=None))
    nodes = kept | set(groups)
    protected = set(groups) | {exit_entry['exit_key']} | {a['entry_node'] for a in merged['anchors']}
    nodes, edges, folded = condense(nodes, edges, protected)
    layer = layers_of(nodes, edges)
    width, height_, step, xof, yof = chain_layout(nodes, edges, layer)
    height_ += 16  # room for the grouping caption
    narrow = step < 90
    out = [svg_open(width, height_, 'Chain graph: anchor groups (left) through the entry points to the Exit output (right)')]
    out.append(ARROW_DEFS)
    out += edge_markup(edges, edges_all, xof, yof, narrow, label_t=0.25)
    size = 8 if narrow else 9 if step < 120 else 10
    for n in sorted(nodes, key=str):
        x, y = xof(n), yof(n)
        if n in groups:
            kind = n[1]
            out.append(shape(ANCHOR_SHAPE[kind], x, y, ANCHOR_COLOR[kind], r=9))
            lines = group_label(kind, groups[n])
        elif n in folded:
            out.append(shape('square', x, y, '#555', r=5, hollow=True, dashed=True))
            lines = [f'… {folded[n]} Hops …']
        else:
            facts_ = exit_entry['nodes'][n]
            if n == exit_entry['exit_key']:
                out.append(shape('square', x, y, CLASS_COLOR[exit_entry['report_class']], r=7))
                lines = [short(n), sats(facts_['amount_sats']), when(facts_['block_time'])[:10], 'Exit output']
            else:
                out.append(shape('circle', x, y, '#333', r=5, hollow=True))
                basis = BASIS_SHORT.get(facts_.get('basis'), '')
                lines = [short(n), sats(facts_['amount_sats']), when(facts_['block_time'])[:10]]
                lines += [basis] if basis else []
        for i, text in enumerate(lines):
            css = ' class="mono"' if i == 0 and n not in groups and n not in folded else ''
            out.append(f'<text x="{x:.1f}" y="{y + 22 + i * 11:.1f}" font-size="{size}" text-anchor="middle"{css}>{esc(text)}</text>')
    legend_y = height_ - 38
    items = [('circle', ANCHOR_COLOR['ACQUISITION'], 'Acquisition anchor group'),
             ('diamond', ANCHOR_COLOR['NO_ACQUISITION'], 'No-acquisition anchor group'),
             ('circle', '#333', 'Own-custody output'),
             ('square', CLASS_COLOR[exit_entry['report_class']], 'Exit output')]
    for index, (kind, color, text) in enumerate(items):
        x = 12 + index * 215
        out.append(shape(kind, x + 6, legend_y, color, r=5, hollow=color == '#333'))
        out.append(f'<text x="{x + 18}" y="{legend_y + 3}" font-size="9">{esc(text)}</text>')
    out.append(f'<text x="12" y="{height_ - 14}" font-size="9" class="muted">Anchors are grouped by entry point '
               'and anchor type; their feeder hops are listed in the table above.</text>')
    out.append('</svg>')
    return ''.join(out)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

CSS = '''
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;background:#fff;color:#111;font:14px/1.45 Arial,Helvetica,sans-serif}
main{max-width:960px;margin:0 auto;padding:16px}
h1{font-size:26px;margin:0 0 6px}h2{font-size:18px;margin:18px 0 6px}h3{font-size:14px;margin:14px 0 4px}
.muted{color:#444}.mono{font-family:Consolas,Menlo,monospace;overflow-wrap:anywhere}
.nowrap{white-space:nowrap;overflow-wrap:normal}
.screen-note{background:#eef3fa;border:1px solid #9bb4d6;padding:8px 12px;margin-bottom:12px;border-radius:4px}
table{border-collapse:collapse;width:100%;margin:6px 0 10px;font-size:12px}
th,td{border:1px solid #999;padding:3px 6px;text-align:left;vertical-align:top}
th{background:#eee}
.tablewrap,.figure{overflow-x:auto}
.figure svg{width:100%;height:auto;min-width:560px;display:block}
.facts{display:grid;grid-template-columns:max-content 1fr;gap:2px 14px;margin:6px 0}
.facts dt{font-weight:bold}.facts dd{margin:0;overflow-wrap:anywhere}
.badge{display:inline-block;border:2px solid #111;padding:0 8px;font-weight:bold}
.badge.UNRESOLVED{border-style:dashed}
.note{border-left:4px solid #555;padding:4px 10px;margin:8px 0;background:#f5f5f5}
.userdata{font-style:italic}
tr.sum td{background:#eee;font-weight:bold}
.disclaimer{border:1px solid #555;padding:8px 12px;margin-top:18px;font-size:12px}
@page{size:A4 portrait;margin:16mm 14mm 20mm}
@media print{
 .screen-note{display:none}
 body{font-size:10.5pt}main{max-width:none;padding:0}
 .exit,.appendix{break-before:page}
 tr,svg,.note,.facts{break-inside:avoid}
 thead{display:table-header-group}
 h2,h3,.pathcap{break-after:avoid}
 .figure svg{min-width:0}
 .tablewrap,.figure{overflow:visible}
}
'''


def page_margin_css(meta):
    left = css_text(f"WhereAreMySats {meta['app_version']} · created {meta['created_at_utc']} · revision {meta['data_revision']}")
    return ('@page{@bottom-left{content:"' + left + '";font:8pt Arial,sans-serif;color:#444}'
            '@bottom-right{content:"Page " counter(page) " of " counter(pages);font:8pt Arial,sans-serif;color:#444}}')


def facts(pairs):
    return '<dl class="facts">' + ''.join(f'<dt>{esc(k)}</dt><dd>{v}</dd>' for k, v in pairs) + '</dl>'


def user_value(value):
    return f'<span class="userdata">{esc(value)}</span> <span class="muted">(user-provided)</span>' if value not in (None, '') else '—'


def selection_text(meta):
    years = 'all years' if meta['years'] == 'all' else ', '.join(str(y) for y in meta['years'])
    scope = 'All wallets' if meta['scope'] is None else (meta['scope'] or '(unlabelled)')
    return years, scope


def cover(report):
    meta, overview = report['meta'], report['overview']
    years, scope = selection_text(meta)
    counts = ''.join(f'<tr><td>{esc(CLASS_LABEL[c])}</td><td>{n}</td></tr>' for c, n in overview['class_counts'].items())
    return (f'<section class="cover"><h1>{TITLE}</h1>'
            '<p class="muted">On-chain history of coins deposited at an exchange: from the entry into own custody to the deposit output.</p>'
            + facts([('Exit years (UTC)', esc(years)), ('Scope', esc(scope)), ('Exits', str(overview['exit_count'])),
                     ('Total of Exit outputs', f"{esc(sats(overview['total_exit_sats']))} ({btc(overview['total_exit_sats'])} BTC)"),
                     ('Free-text notes', 'included' if meta['include_free_text'] else 'not included')])
            + '<h2>Report classes</h2><div class="tablewrap"><table><thead><tr><th>Class</th><th>Exits</th></tr></thead><tbody>'
            + counts + '</tbody></table></div><h2>Timeline</h2><div class="figure">' + timeline_svg(report) + '</div>'
            + f'<p class="disclaimer">{esc(DISCLAIMER)}</p></section>')


def class_notes(entry):
    notes = []
    cls = entry['report_class']
    if cls == 'NO_ACQUISITION':
        notes.append('The origin anchors below are verified entries into own custody without a recorded acquisition. '
                     'No purchase or source statement is made for them beyond what is marked user-provided.')
    if cls == 'MIXED':
        notes.append('This Exit combines origin anchors with a recorded acquisition and origin anchors without one. '
                     'No split between them is stated.')
    if cls == 'UNRESOLVED':
        notes.append('Part of the input history of this Exit could not be established as an origin in the available data. '
                     'This is an open item, not a statement about the origin or ownership of those coins.')
        if not entry['anchors']:
            notes.append('No established chain is shown for this Exit.')
    if entry['allocation_check']:
        notes.append('A consistency check between the recorded Exit sources and the on-chain evaluation reports '
                     + ('a difference' if entry['allocation_check'].startswith('DIFFERS') else 'no result')
                     + '. Any chain shown follows the on-chain evaluation only.')
    return ''.join(f'<div class="note">{esc(n)}</div>' for n in notes)


def anchor_details(a, sep='<br>'):
    """(type label, user-provided cell) of one anchor record; every value escaped."""
    user = a['user']
    if a['type'] == 'ACQUISITION':
        return 'Acquisition', sep.join((f"Date: {user_value(user.get('date'))}",
                                        f"Source: {user_value(user.get('source'))}",
                                        f"Reference: {user_value(user.get('reference'))}"))
    details = [f"Source: {user_value(user.get('source'))}"]
    if user.get('review_comment'):
        details.append(f"Review comment: {user_value(user['review_comment'])}")
    return 'No acquisition (verified)', sep.join(details)


def anchors_table(entry):
    if not entry['anchors']:
        return ''
    body = []
    for a in entry['anchors']:
        kind, details = anchor_details(a)
        body.append(f'<tr><td class="mono">{esc(a["txid"])}:{a["vout"]}</td><td>{esc(sats(a["amount_sats"]))}</td>'
                    f'<td>{height(a["block_height"])}<br>{esc(when(a["block_time"]))}</td><td>{kind}</td><td>{details}</td></tr>')
    return ('<h3>Origin anchors</h3><div class="tablewrap"><table><thead><tr><th>Outpoint</th><th>Amount</th>'
            '<th>Block / time</th><th>Anchor type</th><th>User-provided data</th></tr></thead><tbody>'
            + ''.join(body) + '</tbody></table></div>')


HOP_HEADER = ('<th>Hop</th><th>From output</th><th>Spending TX</th><th>To output</th>'
              '<th>Block</th><th>Time</th><th>Custody basis (receiving output)</th>')


def hop_row(entry, h, prefix='', source_cell=None):
    source = source_cell if source_cell is not None else f'<td class="mono">{esc(short(h["source"], 8, 6))}</td>'
    basis = 'Exit output' if h['dest'] == entry['exit_key'] else esc(basis_label(h['dest_basis']))
    return (f'<tr>{prefix}<td>{h["hop"]}</td>{source}'
            f'<td class="mono">{esc(short_tx(h["spending_txid"], 8, 6))}</td><td class="mono">{esc(short(h["dest"], 8, 6))}</td>'
            f'<td>{height(h["block_height"])}</td><td>{esc(when(h["block_time"]))}</td>'
            f'<td>{basis}</td></tr>')


def path_tables(entry):
    paths = entry['paths']
    out = ['<h3>Chain by path</h3>']
    for number, path in enumerate(paths[:MAX_LISTED_PATHS], 1):
        kind, (txid, vout) = path['anchor']
        rows = ''.join(hop_row(entry, h) for h in path['hops'])
        out.append(f'<p class="muted pathcap">Path {number} · from anchor <span class="mono">{esc(short((txid, vout)))}</span> · '
                   f'{len(path["hops"])} hop{"s" if len(path["hops"]) != 1 else ""}</p>'
                   '<div class="tablewrap"><table><thead><tr>' + HOP_HEADER + '</tr></thead><tbody>'
                   + rows + '</tbody></table></div>')
    if len(paths) > MAX_LISTED_PATHS:
        out.append(f'<div class="note">{len(paths) - MAX_LISTED_PATHS} further path(s) are not listed individually; '
                   'their transactions are in the graph and the appendix.</div>')
    return ''.join(out)


def merged_anchors_table(entry):
    """Anchor table for an Exit with several anchors: all anchors, no cap, sum without attribution."""
    merged = entry['merged']
    body = []
    for a in merged['anchors']:
        kind, details = anchor_details(a, sep=' · ')
        body.append(f'<tr><td>{a["nr"]}</td><td class="mono">{esc(short((a["txid"], a["vout"])))}</td>'
                    f'<td>{esc(sats(a["amount_sats"]))}</td><td>{height(a["block_height"])}<br>{esc(when(a["block_time"]))}</td>'
                    f'<td>{kind}</td><td>{details}</td><td>{a["feeder_hops"]}</td>'
                    # FIX-UI-6: the shortened Entry-via TXID never wraps.
                    f'<td class="mono nowrap">{esc(short_tx(a["entry_txid"], 8, 6))}</td></tr>')
    # Sum as the table's last row (not a repeating print footer): totals of whole anchor outputs only.
    body.append(f'<tr class="sum"><td></td><td>Sum of the anchor outputs</td><td>{esc(sats(merged["anchor_total_sats"]))}</td>'
                '<td colspan="5">No statement is made about which portion of this sum is contained in the Exit output.</td>'
                '</tr>')
    return (f'<h3>Origin anchors</h3><p class="muted">All {len(merged["anchors"])} anchors of this Exit; '
            'outpoints are shortened, the appendix lists every TXID in full.</p>'
            '<div class="tablewrap"><table><thead><tr><th>Nr.</th><th>Outpoint</th><th>Amount</th><th>Block / time</th>'
            '<th>Anchor type</th><th>User-provided data</th><th>Hops to common section</th><th>Entry via</th></tr></thead>'
            '<tbody>' + ''.join(body) + '</tbody></table></div>')


def feeder_table(entry):
    merged = entry['merged']
    if not merged['feeders']:
        return ''
    rows = ''.join(hop_row(entry, h, prefix=f'<td>{f["nr"]}</td>')
                   for f in merged['feeders'] for h in f['hops'])
    return ('<h3>Feeder hops before the common section</h3>'
            '<p class="muted">Hops of the individual anchors before the common section, grouped by anchor number. '
            'Anchors whose output is spent directly at the entry do not appear here.</p>'
            '<div class="tablewrap"><table><thead><tr><th>Anchor</th>' + HOP_HEADER + '</tr></thead><tbody>'
            + rows + '</tbody></table></div>')


def common_table(entry):
    merged = entry['merged']
    rows = []
    for index, h in enumerate(merged['common']):
        source_cell = None
        if index == 0 and h['source'] is None:
            source_cell = f'<td class="muted">{merged["entry_source_count"]} inputs (listed above)</td>'
        rows.append(hop_row(entry, h, source_cell=source_cell))
    note = ('<div class="note">The common section consists of the Exit transaction only.</div>'
            if len(merged['common']) == 1 else '')
    return ('<h3>Common section</h3>'
            '<p class="muted">Final hops shared by all established paths of this Exit, shown once, '
            'from the entry point to the Exit output.</p>' + note +
            '<div class="tablewrap"><table><thead><tr>' + HOP_HEADER + '</tr></thead><tbody>'
            + ''.join(rows) + '</tbody></table></div>')


def exit_section(number, entry):
    cls = entry['report_class']
    free = entry.get('free_text')
    pairs = [('Exit output', f'<span class="mono">{esc(entry["txid"])}:{entry["vout"]}</span>'),
             ('Amount', f'{esc(sats(entry["amount_sats"]))} ({btc(entry["amount_sats"])} BTC)'),
             ('Block height', height(entry['block_height'])), ('Block time', esc(when(entry['block_time']))),
             ('Report class', f'<span class="badge {esc(cls)}">{esc(CLASS_LABEL[cls])}</span>')]
    if free:
        pairs += [('Broker', user_value(free.get('broker'))), ('Notes', user_value(free.get('notes'))),
                  ('Customer reference', user_value(free.get('reference')))]
    if entry.get('merged'):
        # Several anchors (owner decision 2026-10-24): anchor table, feeder hops, the
        # common section once, then the graph; no per-path tables and no path cap.
        merged = entry['merged']
        graph = grouped_chain_svg(entry) if len(merged['anchors']) > GROUP_ANCHORS_ABOVE else chain_svg(entry)
        body = (merged_anchors_table(entry) + feeder_table(entry) + common_table(entry)
                + ('<h3>Chain graph</h3><div class="figure">' + graph + '</div>' if graph else ''))
    else:
        graph = chain_svg(entry)
        body = (anchors_table(entry)
                + ('<h3>Chain graph</h3><div class="figure">' + graph + '</div>' if graph else '')
                + (path_tables(entry) if entry['paths'] else ''))
    return f'<section class="exit"><h2>Exit {number}</h2>' + facts(pairs) + class_notes(entry) + body + '</section>'


def appendix_section(report):
    rows = ''.join(f'<tr><td>{height(r["block_height"])}</td><td class="mono">{esc(r["txid"])}</td></tr>' for r in report['appendix'])
    return ('<section class="appendix"><h2>Appendix: transactions</h2><p class="muted">All transactions named in this report, '
            'by block height, in full for lookup in any block explorer.</p><div class="tablewrap"><table><thead>'
            '<tr><th>Block</th><th>TXID</th></tr></thead><tbody>' + (rows or '<tr><td colspan="2">None</td></tr>')
            + '</tbody></table></div></section>')


def footer(report):
    meta = report['meta']
    return (f'<footer class="disclaimer"><strong>WhereAreMySats {esc(meta["app_version"])}</strong> · created '
            f'{esc(meta["created_at_utc"])} · data revision {esc(meta["data_revision"])}<br>{esc(DISCLAIMER)}</footer>')


def render_report(report):
    meta = report['meta']
    body = ('<main><div class="screen-note">Save as PDF with Ctrl+P (print dialog → destination “Save as PDF”, paper A4).</div>'
            + cover(report) + ''.join(exit_section(i, e) for i, e in enumerate(report['exits'], 1))
            + appendix_section(report) + footer(report) + '</main>')
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="robots" content="noindex,nofollow"><meta name="referrer" content="no-referrer">'
            f'<title>{TITLE}</title><style>{CSS}{page_margin_css(meta)}</style></head><body>{body}</body></html>')
