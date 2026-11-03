"""Position filter predicate shared by the dashboard and the provenance views.

Kept apart from ``backend.reporting.dashboard`` so that
``backend.reporting.no_acquisition`` (which ``dashboard`` imports at module
level) can use it without a lazy, cyclic import. The filter vocabulary
(``FILTERS``) stays in ``dashboard``; this predicate only evaluates it.
"""


def matches(p, filters):
    return (('coverage' not in filters or p['coverage_status'] == filters['coverage'])
        and ('provenance_class' not in filters or p['provenance_class'] == filters['provenance_class'])
        and ('origin_type' not in filters or
             (bool(p['acquisition_ids']) if filters['origin_type'] == 'ACQUISITION' else
              bool(p['no_acquisition_anchor_outpoints']) if filters['origin_type'] == 'NO_ACQUISITION' else
              p['provenance_class'] == 'UNRESOLVED')))
