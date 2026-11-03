"""Current wallet presentation, distinct from immutable ownership evidence.

The Registry is the canonical assignment. Other wallets are current only when
explicitly SHARED with it. KEEP/MOVED decisions are not ownership deletions.
No wallet label has special meaning here.
"""
from collections import defaultdict


def effective_wallet_matches(known, evidence, conflicts=()):
    if not known:
        return []
    canonical = known.get("wallet_name") or ""
    # A move supersedes older sharing involving the previous assignment, not
    # independent shared claims between other wallets at the same address. Evaluate
    # surviving pair decisions in resolution order, never evidence insertion order.
    decisions = sorted((c for c in conflicts if c.get("status") in {"KEEP", "MOVED", "SHARED"}),
                       key=lambda c: (c.get("resolved_at") or "", c.get("id", 0)))
    shared = set()
    for decision in decisions:
        pair = frozenset((decision["wallet_name"], decision["evidence_wallet_name"]))
        if decision["status"] == "MOVED":
            shared = {p for p in shared if decision["wallet_name"] not in p}
        elif decision["status"] == "SHARED":
            shared.add(pair)
        else:
            shared.discard(pair)
    wallets = {canonical}
    while True:
        expanded = wallets | {wallet for pair in shared if pair & wallets for wallet in pair}
        if expanded == wallets:
            break
        wallets = expanded

    by_wallet = defaultdict(list)
    for item in evidence:
        if item["wallet_name"] in wallets:
            by_wallet[item["wallet_name"]].append(item)
    result = []
    for wallet in sorted(wallets):
        records = by_wallet[wallet]
        # Prefer derivation-backed presentation within the effective assignment;
        # the full manual/confirmation history remains separate and untouched.
        derived = [e for e in records if e["source"] in {"XPUB_DISCOVERY", "DESCRIPTOR"}]
        records = derived or records
        if not records:
            result.append({"wallet_id": wallet, "wallet_label": wallet, "source": "ADDRESS_REGISTRY"})
        for item in sorted(records, key=lambda e: e.get("id", 0)):
            result.append({"wallet_id": wallet, "wallet_label": wallet,
                "derivation_path": item.get("derivation_path"), "branch": item.get("branch_index"),
                "index": item.get("address_index"), "source": item["source"],
                "evidence_id": item.get("id"), "discovery_id": item.get("discovery_id")})
    return result
