"""Custodial / pooled-funds boundary support.

This package implements the *analysis* side of custody handling:

- `detect`        : conservative POSSIBLE_CUSTODIAL_BOUNDARY detection
- `candidates`    : search + explainable scoring of continuation candidates

State (persisted boundaries / bridges) lives in the SQLite layer, tracing
behavior lives in the engine, API/UI/export wiring lives in the server and
frontend. The core correctness rule: a CUSTODY_BRIDGE is a logical/economic
relationship, never cryptographically proven UTXO lineage.
"""