"""Acquisition Tracking v1 (ACQUISITION_TRACKING_V1_REQUIREMENTS.md).

Reusable backend service path so a future CSV import can call the exact same
validation -> resolution -> persistence -> trace-linking flow instead of
duplicating business logic in the frontend.
"""