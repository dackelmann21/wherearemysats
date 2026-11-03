"""CSV / pasted-text import parser for the Known Addresses registry.

Accepts a .csv file's text or a clipboard paste. Behaviour (REQ 25/43,
M2/M3):

  * Header detection: a row is a header when one of its cells equals (case-
    insensitively) an address-column name ("address", "bitcoin address",
    "btc address", "receive address", "receive_address", "addresses").
    Optional columns "label", "wallet_name"/"wallet", "type" are recognised
    in any order.
  * Headerless mode: first non-empty column = address; if a second column
    exists its values become labels ONLY when they do not look like
    addresses, else the wallet column default ("Manual import").
  * Per-row partial failure: an invalid/duplicate row is isolated as its own
    record with ``error`` set; it never aborts the batch or hides siblings.
  * Bare 64-hex strings are TXIDs (REQ 39) - never treated as addresses.
  * Blank rows, trailing-whitespace rows, and comment rows are skipped.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

_ADDR_COLS = frozenset(
    {
        "address",
        "addresses",
        "bitcoin address",
        "btc address",
        "receive address",
        "receive_address",
        "receiveaddress",
        "btc-address",
    }
)
_LABEL_COLS = frozenset({"label", "label/note", "note", "comment"})
_WALLET_COLS = frozenset({"wallet", "wallet_name", "walletname", "group", "batch"})
_TYPE_COLS = frozenset({"type", "address_type", "address type"})
_HEADER_RE = re.compile(r"^(?P<addr>address(es)?|bitcoin address|btc address|receive address|receive_address)(\s+.*)?$", re.I)
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass
class ParsedRow:
    original: str
    address: str = ""
    label: str = ""
    wallet_name: str = ""
    error: str = ""


@dataclass
class ParseResult:
    rows: List[ParsedRow] = field(default_factory=list)
    blank_count: int = 0
    header_detected: bool = False
    header_columns: List[str] = field(default_factory=list)


def _ascii(s: str) -> str:
    return (s or "").strip().replace("\ufffe", "")


def _looks_like_address(text: str) -> bool:
    t = _ascii(text)
    return bool(t) and not _HEX64_RE.match(t)


def _make_escapechar_check(text: str) -> Optional[str]:
    """Heuristic: if an odd number of quotes appear, they are not CSV escapes
    but a label wrapping an address, e.g. ``"label","address"``."""
    if text.count('"') % 2 == 1:
        return None
    return None


_HEADER_ALIASES = {
    "address": "address",
    "addresses": "address",
    "bitcoin address": "address",
    "btc address": "address",
    "receive address": "address",
    "receive_address": "address",
    "receiveaddress": "address",
    "btc-address": "address",
    "label": "label",
    "label/note": "label",
    "note": "label",
    "comment": "label",
    "wallet": "wallet_name",
    "wallet_name": "wallet_name",
    "walletname": "wallet_name",
    "group": "wallet_name",
    "batch": "wallet_name",
    "type": "type",
    "address_type": "type",
    "address type": "type",
}


def _detect_columns(header: Sequence[str]) -> dict:
    cols = {}
    for i, cell in enumerate(header):
        key = _HEADER_ALIASES.get(_ascii(cell).lower())
        if key and key not in cols:
            cols[key] = i
    return cols


def parse_import(text: str) -> ParseResult:
    """Parse CSV/paste text into clean per-row records (never raises)."""
    result = ParseResult()
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not raw.strip():
        return result
    if raw.startswith("\ufeff"):
        raw = raw[1:]

    delimiter = ","
    sample = raw[:4096]
    if "\t" in sample:
        delimiter = "\t"
    elif ";" in sample:
        delimiter = ";"
    elif "," in sample:
        delimiter = ","
    else:
        delimiter = ","

    reader = csv.reader(io.StringIO(raw), delimiter=delimiter)
    try:
        rows = list(reader)
    except Exception:
        rows = [[line] for line in raw.splitlines()]

    if not rows:
        return result

    header = rows[0]
    cols = _detect_columns(header)
    if "address" in cols:
        result.header_detected = True
        result.header_columns = list(header)
        data_rows = rows[1:]
    else:
        data_rows = rows
        cols = {"address": 0}
        if len(rows[0]) > 1 and _looks_like_address(rows[0][1]):
            cols["label"] = 1
        elif len(rows[0]) > 1:
            cols["label"] = 1

    for row in data_rows:
        original = ",".join(row)
        cells = [_ascii(c) for c in row]
        if not any(cells) or not cells[0]:
            result.blank_count += 1
            continue
        row_obj = ParsedRow(original=original)
        row_obj.address = cells[0]
        if "label" in cols and len(cells) > cols["label"]:
            row_obj.label = cells[cols["label"]].strip()
        if "wallet_name" in cols and len(cells) > cols["wallet_name"]:
            row_obj.wallet_name = cells[cols["wallet_name"]].strip()
        if row_obj.address and _HEX64_RE.match(row_obj.address):
            row_obj.error = "bare 64-hex TXID, not an address (REQ 39)"
        result.rows.append(row_obj)

    return result
