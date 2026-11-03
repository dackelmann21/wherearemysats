"""Provenance v1 overlay on the existing Coverage DAG and Wallet Inventory.

The trusted local installation is the owner scope (no multi-user accounts).
Annotations additionally isolate Bitcoin networks; wallet labels are attribution,
not principals. Classification is request-local and is never backfilled as truth.
"""
import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

from backend.config import get_settings
from backend.reporting.coverage import (DocumentationGraph, Evidence, _ancestry, documentation_context,
                                        CoverageUTXO, DocumentationCoverage, CoverageBucket,
                                        outside_current_wallets, wallet_observation_context)
from backend.persistence.inventory import metadata as inventory_metadata
from backend.reporting.context import ReportContext
from backend.reporting.filters import matches
from backend.reporting.wallet_positions import classify_locations
from backend.reporting.read_data import ReadData, ReadBudgetExceeded, input_keys, revision, rows
from backend.reporting.origin_resolution import (OriginResolutions, LineageSummary, lineage_summary,
                                                 COVERAGE_OF_CLASS, PROVENANCE_CLASSES as CLASSES)
CLASSIFICATION_VERSION = 2


class InventoryUTXO(CoverageUTXO, LineageSummary):
    provenance_class: str
    position_status: str
    lineage_complete: bool
    unresolved_reasons: list[str]
    no_acquisition_anchor_outpoints: list[str]
    anchors: list[dict]
    documented_path_acquisition_ids: list[int]


class InventoryReport(DocumentationCoverage):
    utxos: list[InventoryUTXO]
    coverage_totals: dict[str, CoverageBucket]
    classification_version: int


def network_scope(engine):
    return getattr(getattr(engine.rpc, "settings", None), "btc_network", get_settings().btc_network)


def outpoint(key):
    return f"{key[0]}:{key[1]}"


async def provenance_graph(db, engine, keys, settings, deadline, *, capture_paths=False, report=None):
    report = report or ReportContext()
    resolutions = report.origin_resolutions or await OriginResolutions.load(db)
    data = await ReadData.load(db, engine, {k[0] for k in keys}, limit=settings.coverage_max_outpoints)
    anchors, boundaries, required, conflicts = await documentation_context(db, data.outputs, origin_resolutions=resolutions)
    frontier_txids = set()

    async def entry_stops(frontier):
        frontier_txids.update(k[0] for k in frontier)
        batch = await ReadData.load(db, engine, {k[0] for k in frontier},
                                    limit=settings.coverage_max_outpoints)
        return {k for k, o in batch.outputs.items()
                if o.ownership_status != "OWNED"}

    transactions = await _ancestry(db, keys, set(anchors) | boundaries | set(resolutions.decisions),
                                  settings.coverage_max_transactions, frontier_stops=entry_stops,
                                  transaction_budget=report.transaction_sets.get('provenance'))
    txids = {k[0] for k in keys} | set(transactions) | frontier_txids
    for raw in transactions.values():
        txids.update(txid for txid, _ in input_keys(raw))
    data = await ReadData.load(db, engine, txids, limit=settings.coverage_max_outpoints)
    anchors, boundaries, required, conflicts = await documentation_context(db, data.outputs, origin_resolutions=resolutions)
    return DocumentationGraph(transactions, anchors, boundaries, required, conflicts,
                             deadline=deadline, self_custody_outputs=data.outputs, capture_paths=capture_paths,
                             origin_resolutions=resolutions), data


async def saved_wallet_observations(db, engine, settings, deadline, *, population=None,
                                    observed_keys=None, acquisitions=None, report=None):
    """Saved report membership with the strict origin frontier, without legacy ancestry."""
    observed = await wallet_observation_context(db, engine, settings, deadline,
        population=population, observed_keys=observed_keys, acquisitions=acquisitions, report=report)
    try:
        async with asyncio.timeout(settings.coverage_timeout):
            graph, _ = await provenance_graph(db, engine, set(observed.current_keys), settings, deadline, report=report)
    except TimeoutError as exc:
        raise ReadBudgetExceeded('Provenance execution budget exceeded; retry.') from exc
    managed, outside = [], []
    for key in sorted(observed.current_keys):
        item = observed.observations[key]
        if key in observed.keys:
            managed.append(item)
            continue
        def evidence_link():
            evidence = graph.evaluate(key)
            # A current value conflict withholds the linkage (as in the legacy diagnostic).
            return evidence, 'NEEDS_REVIEW' if item.reasons else evidence.link_state
        connected = outside_current_wallets(observed, key, evidence_link)
        if connected is not None:
            outside.append(SimpleNamespace(outpoint=item.outpoint, acquisition_ids=sorted(connected)))
    return SimpleNamespace(population='CURRENT_WALLET_INVENTORY',
        inventory=await inventory_metadata(db), utxos=managed, utxo_count=len(managed),
        total_sats=sum(u.amount_sats for u in managed), lineage_graph=graph,
        acquisition_wallet_positions=classify_locations(observed.locations, managed, outside))


async def annotations(db, engine):
    return {(r["txid"], r["vout"]): r for r in await rows(db,
        "SELECT * FROM no_acquisition_annotations WHERE network=? ORDER BY txid,vout",
        (network_scope(engine),))}


def anchor_record(key, saved):
    row = saved.get(key, {})
    return dict(outpoint=outpoint(key), txid=key[0], vout=key[1],
                source=row.get("source"), note=row.get("note"), revision=row.get("revision", 0),
                created_at=row.get("created_at"), updated_at=row.get("updated_at"))


def evidence_fields(evidence, saved, resolutions=None, *, include_details=True):
    return dict(provenance_class=evidence.provenance_class,
        acquisition_ids=sorted(evidence.acquisitions),
        no_acquisition_anchor_outpoints=[outpoint(k) for k in sorted(evidence.no_acquisition_anchors)],
        anchors=[anchor_record(k, saved) for k in sorted(evidence.no_acquisition_anchors)] if include_details else [],
        lineage_complete=evidence.provenance_class != "UNRESOLVED",
        unresolved_reasons=sorted(evidence.review) or (
            ["No established self-custody origin."] if evidence.provenance_class == "UNRESOLVED" else []),
        **({k: v for k, v in lineage_summary(evidence, resolutions, include_origins=include_details).items() if k != 'acquisition_ids'}
           if resolutions is not None else {}))


async def positions(db, engine, coverage, *, provenance_class=None, position_state=None,
                    wallet=None, offset=0, limit=100, origin_type=None, coverage_status=None, compact=False,
                    report=None):
    settings = get_settings()
    before = await revision(db)
    try:
        async with asyncio.timeout(settings.coverage_timeout):
            keys = {(u.txid, u.vout) for u in coverage.utxos}
            graph = getattr(coverage, 'lineage_graph', None)
            if graph is None:
                graph, _ = await provenance_graph(db, engine, keys, settings,
                                                 time.monotonic() + settings.coverage_timeout, report=report)
            saved = {} if compact else await annotations(db, engine)
            result = []
            buckets = {c: dict(utxo_count=0, amount_sats=0) for c in CLASSES}
            for u in coverage.utxos:
                e = graph.evaluate((u.txid, u.vout))
                if e.entry_frontier:
                    e = replace(e, unresolved_origins=e.unresolved_origins | {(u.txid, u.vout)},
                                review=e.review | {'Current output is an unresolved ownership/custody frontier.'},
                                review_issues={**e.review_issues, (u.txid, u.vout): e.review_issues.get((u.txid, u.vout), set()) |
                                    {('EVIDENCE', 'Current output is an unresolved ownership/custody frontier.')}})
                # The current output must agree with the live node observation.
                if "Cached output value differs from current node evidence." in u.reasons:
                    e = replace(e, review=e.review | {"Cached output value differs from current node evidence."},
                                unresolved_origins=e.unresolved_origins | {(u.txid, u.vout)},
                                review_issues={**e.review_issues, (u.txid, u.vout): e.review_issues.get((u.txid, u.vout), set()) |
                                    {('EVIDENCE', 'Cached output value differs from current node evidence.')}})
                fields = evidence_fields(e, saved, graph.origin_resolutions, include_details=not compact)
                bucket = buckets[fields["provenance_class"]]
                bucket["utxo_count"] += 1
                bucket["amount_sats"] += u.amount_sats
                # CURRENT here describes the verified UTXO, not an Acquisition's
                # independent custody/accounting position_state.
                if compact:
                    result.append(dict(outpoint=u.outpoint, amount_sats=u.amount_sats,
                        wallet_matches=[{'wallet_id': m['wallet_id']} for m in u.wallet_matches],
                        **{k: fields[k] for k in ('provenance_class', 'coverage_status', 'review_status',
                            'unresolved_reasons', 'acquisition_ids', 'no_acquisition_anchor_outpoints')}))
                else:
                    result.append(dict(u.model_dump(), **{
                        **fields, "position_status": "CURRENT",
                        **({'documented_path_acquisition_ids': u.acquisition_ids}
                           if hasattr(u, 'acquisition_ids') else {
                               'legacy_documentation_status': 'NOT_REQUESTED',
                               'documentation_state': None, 'acquisition_provenance': None,
                               'provenance_link_state': None, 'documented_path_acquisition_ids': None})}))
                await asyncio.sleep(0)
            selected = [r for r in result if
                (provenance_class is None or r["provenance_class"] == provenance_class)
                and (position_state is None or r["position_status"] == position_state)
                and (wallet is None or any(m["wallet_id"] == wallet for m in r["wallet_matches"]))]
            if origin_type is not None or coverage_status is not None:
                filters = {k: v for k, v in {'origin_type': origin_type, 'coverage': coverage_status}.items() if v is not None}
                selected = [r for r in selected if matches(r, filters)]
            if await revision(db) != before:
                raise ReadBudgetExceeded("Evidence changed; retry.")
            return dict(api_version=1, classification_version=CLASSIFICATION_VERSION,
                population=coverage.population, inventory=coverage.inventory,
                total_sats=coverage.total_sats, utxo_count=coverage.utxo_count,
                class_totals=buckets, filtered_count=len(selected), offset=offset, limit=limit,
                coverage_totals={COVERAGE_OF_CLASS[c]: b for c, b in buckets.items()},
                positions=selected[offset:offset + limit],
                acquisition_path_coverage=(dict(
                    percentage_bps=coverage.provenance_coverage_percentage_bps,
                    linked_sats=coverage.provenance_linked_sats,
                    total_sats=coverage.total_sats,
                    linked_utxo_count=coverage.provenance_linked_utxo_count)
                    if hasattr(coverage, 'provenance_coverage_percentage_bps') else dict(
                        status='NOT_REQUESTED', percentage_bps=None, linked_sats=None,
                        total_sats=coverage.total_sats, linked_utxo_count=None)))
    except TimeoutError as exc:
        raise ReadBudgetExceeded("Provenance execution budget exceeded; retry.") from exc


async def anchor_detail(db, engine, key):
    """Resolve derived OR previously annotated historical anchors without writes."""
    settings = get_settings()
    before = await revision(db)
    saved = await annotations(db, engine)
    try:
        async with asyncio.timeout(settings.coverage_timeout):
            graph, _ = await provenance_graph(db, engine, {key}, settings,
                                             time.monotonic() + settings.coverage_timeout)
            e = graph.evaluate(key)
    except (ReadBudgetExceeded, TimeoutError):
        if key not in saved:
            raise
        # A historical identity remains authorized even when current lineage
        # cannot be evaluated. In particular users must still be able to clear
        # sensitive annotation text; this grants no new classification evidence.
        e = Evidence(review={"Historical anchor lineage is unavailable or execution-limited."})
    if key not in e.no_acquisition_anchors and key not in saved:
        return None
    if await revision(db) != before:
        raise ReadBudgetExceeded("Evidence changed; retry.")
    return dict(anchor_record(key, saved), **evidence_fields(e, saved),
                currently_derived_anchor=key in e.no_acquisition_anchors)


async def origin_detail(db, engine, key):
    """Origin actions target a frontier, not a multi-origin descendant."""
    settings = get_settings()
    before = await revision(db)
    async with asyncio.timeout(settings.coverage_timeout):
        graph, data = await provenance_graph(db, engine, {key}, settings,
                                             time.monotonic() + settings.coverage_timeout)
        resolution = graph.origin_resolutions.resolve(key)
        e = graph.evaluate(key)
        if await revision(db) != before:
            raise ReadBudgetExceeded('Evidence changed; retry.')
        return dict(txid=key[0], vout=key[1], outpoint=outpoint(key), origin_resolution=resolution,
                    can_resolve_no_acquisition=(key in data.outputs and key not in graph.boundaries
                        and key not in graph.conflicts and resolution['type'] != 'ACQUISITION'
                        and (resolution['type'] == 'NO_ACQUISITION' or e.entry_frontier or
                             (e.unresolved_origins == {key} and not e.acquisitions and not e.no_acquisition_anchors))),
                    **evidence_fields(e, await annotations(db, engine), graph.origin_resolutions))
