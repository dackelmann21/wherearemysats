"""Collect current Acquisition endpoints using the existing position evaluator.

This is a reporting adapter, not another lineage walker. An endpoint needs both
the existing custody-scoped position and documented ancestry before it can be
reported as a documented position outside current wallet inventory.
"""
from collections import defaultdict

from backend.acquisitions.position import PositionContext, descendant_summary
from backend.reporting.context import ReportContext
from backend.reporting.read_data import rows


async def acquisition_locations(db, engine, current_outputs, settings, deadline, *, acquisitions=None, report=None):
    if acquisitions is None:
        acquisitions = await rows(db, "SELECT * FROM acquisitions ORDER BY id", limit=settings.coverage_max_outpoints)
    if not acquisitions:
        return {}, {}, {}
    allocations = await db.get_allocation_survey()
    context = (report or ReportContext()).position_context or PositionContext(settings, current_outputs=current_outputs)
    context.deadline = deadline
    if context.saved_outputs is not None:
        current_outputs.update(context.saved_outputs)
    candidates, locations, origins = {}, {}, {}
    for acquisition in acquisitions:
        aid = acquisition["id"]
        origin = (acquisition.get("origin_txid"), acquisition.get("origin_vout"))
        location = locations[aid] = {
            "acquisition_id": aid, "trace_id": acquisition.get("trace_id"),
            "state": "NEEDS_REVIEW", "current_outpoints": [], "reasons": [],
        }
        # The active v19 contract is on-chain; never re-import recovery records.
        if (origin[0] is None or origin[1] is None
                or acquisition.get("origin_amount_sats") is None):
            location["state"] = "NOT_APPLICABLE"
            continue
        if 0 < acquisition["origin_amount_sats"] <= allocations.get(aid, 0):
            location["state"] = "NOT_APPLICABLE"
            location["reasons"] = ["Existing Exit/Disposal allocations take precedence."]
            continue
        origins[origin] = acquisition
        summary = await descendant_summary(db, engine, acquisition, context)
        known = summary.verified_unspent_outpoints
        if summary.needs_review or summary.spend_unknown:
            location["reasons"].append(summary.note or "Current position is unresolved.")
        batch = context.batch
        trace = (batch.traces.get(acquisition.get('trace_id')) if batch else
                 await db.get_trace(acquisition['trace_id']) if acquisition.get('trace_id') else None)
        if trace is None:
            # A missing trace does not erase an independently verified unspent
            # exact anchor. A spent anchor without its lineage remains unresolved.
            fact = (await context.spends(db, engine, [origin]))[origin]
            if fact["spend_status"] == "UNSPENT":
                known = [{"txid": origin[0], "vout": origin[1]}]
        for item in known:
            key = (item["txid"], item["vout"])
            candidates.setdefault(key, set()).add(aid)
            location["current_outpoints"].append(f"{key[0]}:{key[1]}")
        if not known:
            location["reasons"].append(summary.note or "No verified current on-chain position.")
    return candidates, locations, origins


def classify_locations(locations, managed, outside):
    """Membership is independent of the existing Acquisition position_state."""
    current = {u.outpoint for u in managed}
    outside_by_acquisition = defaultdict(set)
    for output in outside:
        for aid in output.acquisition_ids:
            outside_by_acquisition[aid].add(output.outpoint)
    for location in locations.values():
        if location["state"] == "NOT_APPLICABLE":
            continue
        aid = location["acquisition_id"]
        missing = outside_by_acquisition[aid]
        known = set(location["current_outpoints"])
        location["managed_outpoints"] = sorted(current & known)
        location["not_in_current_wallet_outpoints"] = sorted(missing & known)
        if location["reasons"] or not known or known - (current | missing):
            location["state"] = "NEEDS_REVIEW"
        elif current & known and missing & known:
            location["state"] = "PARTIALLY_IN_CURRENT_WALLETS"
        elif current & known:
            location["state"] = "CURRENT_MANAGED"
        else:
            location["state"] = "ACQUISITION_NOT_IN_CURRENT_WALLET"
    return list(locations.values())
