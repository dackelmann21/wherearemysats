"""Explicit request-local report context, passed alongside the shared TraceEngine.

Default (empty) context reproduces plain API behaviour: no saved position
facts, origin resolutions loaded per call, and unbounded shared ancestry sets.
"""
from dataclasses import dataclass, field


@dataclass
class ReportContext:
    position_context: object = None
    origin_resolutions: object = None
    transaction_sets: dict = field(default_factory=dict)
