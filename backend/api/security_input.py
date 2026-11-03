"""Detection of sensitive / secret input patterns (REQ 36).

The application never stores seed phrases, passphrases, xpriv keys, WIF keys
or other private key material. If a user happens to paste such material into
an input field, we refuse it and warn.

The implementation lives in the layer-neutral ``backend.validation`` module;
this module re-exports it for the existing API-layer importers.
"""

from __future__ import annotations

from backend.validation import canonical_txid, detect_sensitive_input  # noqa: F401  re-export for existing importers
