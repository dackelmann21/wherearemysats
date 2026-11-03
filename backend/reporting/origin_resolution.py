"""Exact origin identity, independent of ownership and descendant membership.

One bounded request-local index serves trace enrichment and DocumentationGraph.
Only explicit No-Acquisition decisions are stored; Acquisition truth stays in
the existing anchors table. The local chain database is the identity scope.
"""
from backend.reporting.read_data import rows
from pydantic import BaseModel, Field
from typing import Literal


PROVENANCE_CLASSES = ('ACQUISITION', 'NO_ACQUISITION', 'MIXED', 'UNRESOLVED')
# coverage_status is the dashboard/report name of the same classification.
COVERAGE_OF_CLASS = {'ACQUISITION': 'COVERED', 'NO_ACQUISITION': 'UNCOVERED',
                     'MIXED': 'MIXED', 'UNRESOLVED': 'UNRESOLVED'}


def provenance_class(evidence):
    """The single current-UTXO origin classification (D-05).

    Any review reason or unresolved origin, or no established origin at all, is
    UNRESOLVED; otherwise the composition of Acquisition and No-Acquisition origins.
    """
    if evidence.review or evidence.unresolved_origins or not (evidence.acquisitions or evidence.no_acquisition_anchors):
        return 'UNRESOLVED'
    if evidence.acquisitions and evidence.no_acquisition_anchors:
        return 'MIXED'
    return 'ACQUISITION' if evidence.acquisitions else 'NO_ACQUISITION'


def coverage_status(evidence):
    return COVERAGE_OF_CLASS[provenance_class(evidence)]


class LineageSummary(BaseModel):
    acquisition_origin_count: int
    acquisition_ids: list[int]
    no_acquisition_origin_count: int
    unresolved_origin_count: int
    review_status: Literal['REVIEWED', 'UNRESOLVED']
    coverage_status: Literal['COVERED', 'MIXED', 'UNCOVERED', 'UNRESOLVED']
    origins: list[dict]
    origin_review_counts: dict[str, int] = Field(default_factory=dict)


class OriginResolutions:
    def __init__(self, acquisitions, decisions):
        self.acquisitions = {(a['origin_txid'], a['origin_vout']): a for a in acquisitions}
        self.decisions = {(r['txid'], r['vout']): r for r in decisions}
        # Immutable within this request-local index; reused by bounded reports.
        self._anchors = {key: {a['id']} for key, a in self.acquisitions.items()}

    @classmethod
    async def load(cls, db):
        return cls(await rows(db, "SELECT id, origin_txid, origin_vout, acquisition_date, source, "
                              "origin_amount_sats FROM acquisitions WHERE origin_txid IS NOT NULL "
                              "AND origin_vout IS NOT NULL AND origin_amount_sats IS NOT NULL"),
                   await rows(db, "SELECT * FROM provenance_origin_resolutions"))

    @property
    def anchors(self):
        return self._anchors

    def resolve(self, key):
        a = self.acquisitions.get(key)
        if a:
            return dict(type='ACQUISITION', acquisition_id=a['id'], acquisition_type='ONCHAIN',
                        date=a['acquisition_date'], source=a['source'], amount_sats=a['origin_amount_sats'])
        r = self.decisions.get(key)
        if r:
            return dict(type='NO_ACQUISITION', comment=r['comment'],
                        created_at=r['created_at'], updated_at=r['updated_at'])
        return dict(type='UNRESOLVED')

    def enrich(self, outputs):
        for out in outputs:
            out.origin_resolution = self.resolve((out.txid, out.vout))


def lineage_summary(evidence, resolutions, *, include_origins=True):
    """All counts deduplicate exact origins, never amounts or traversal paths."""
    u = evidence.unresolved_origins
    # A conflicted anchor can retain exact metadata while its lineage evidence
    # needs review. Count its outpoint only once, in the unresolved partition.
    a, n = evidence.acquisition_origins - u, evidence.no_acquisition_anchors - u
    coverage = coverage_status(evidence)
    def origin(k):
        resolution = resolutions.resolve(k)
        issues = sorted(evidence.review_issues.get(k, set()))
        if k in u and not issues:
            issues = [("ACQUISITION", "Acquisition missing: record an Acquisition or explicitly review No Acquisition.")]
        ownership_open = any(d == 'OWNERSHIP' for d, _ in issues)
        return dict(txid=k[0], vout=k[1], outpoint=f'{k[0]}:{k[1]}',
                    review_status='UNRESOLVED' if k in u else 'REVIEWED',
                    acquisition_review_status='REVIEWED' if resolution['type'] != 'UNRESOLVED' else
                        'UNRESOLVED' if any(d == 'ACQUISITION' for d, _ in issues) else 'NOT_ESTABLISHED',
                    ownership_review_status='UNRESOLVED' if ownership_open else 'SATISFIED_OR_NOT_REQUIRED',
                    review_reasons=[dict(dimension=d, reason=r) for d, r in issues],
                    origin_resolution=resolution)
    return dict(acquisition_origin_count=len(a), acquisition_ids=sorted(evidence.acquisitions),
                no_acquisition_origin_count=len(n), unresolved_origin_count=len(u),
                review_status='UNRESOLVED' if coverage == 'UNRESOLVED' else 'REVIEWED',
                coverage_status=coverage,
                origin_review_counts=dict(all=len(a | n | u), needs_review=len(u), reviewed=len(a | n)),
                origins=[origin(k) for k in sorted(a | n | u)] if include_origins else [])
