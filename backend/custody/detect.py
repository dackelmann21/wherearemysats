"""Detection of likely pooled / custodial situations.

Per MIXED_PROVENANCE_BATCH_REQUIREMENTS.md (sections 12-14) the detector
returns NO suggestions on its own. Bitcoin does not prove input->output
mapping in multi-input/multi-output transactions, so the following must never
independently produce a custody flag:

- a large output vs. a small traced input        (pooled value growth)
- strong fan-in (many unrelated inputs merged)   (merge / consolidation)
- a batch-like structure (multi-in / multi-out)  (batch / payroll flows)
- many outputs, or consolidation of change
- the output already being classified OWNED (stale propagation)

Such signals are tracked as *topology* and *provenance* instead (MIXED /
AMBIGUOUS, likely_batch). They never mutate ownership and never create a
boundary. Custody is only ever established by explicit user confirmation
(marking a CUSTODIAL_DEPOSIT boundary). Without independent service-side
evidence (exchange/account history - deliberately out of scope here), there
is no legitimate way to turn topology into a custody suggestion, so this
module deliberately produces an empty list.
"""

from __future__ import annotations

from typing import Any

from backend.models.schemas import FullTrace


def detect_possible_boundaries(
    full: FullTrace,
    dismissed: set[tuple[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """Return possible boundary outpoints derived from heuristic signals.

    Always empty: topology/fan-out/pooling structure must not be interpreted
    as custody evidence. The function is kept as the (stable) custody
    detection surface so the UI and API semantics stay intact - custody can
    only be confirmed explicitly by the user.

    The ``dismissed`` parameter is accepted for API compatibility and is
    ignored (there is nothing to suppress anymore).
    """
    return []