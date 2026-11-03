"""Generic Acquisition CSV: acquisition_date;txid;vout;source;reference;notes.

TXID + bitcoin_address is accepted for local, ambiguity-safe VOUT resolution.
Preview and creation use the same service; no amounts are accepted as facts.
"""
import csv
import io
import re
from dataclasses import dataclass, field


@dataclass
class ImportedRow:
    line_number: int
    original: str
    acquisition_date: str = ""
    txid: str = ""
    origin_vout: int | None = None
    address: str = ""
    source: str = ""
    reference: str = ""
    notes: str = ""
    error: str = ""


@dataclass
class AcquisitionCsvParseResult:
    rows: list[ImportedRow] = field(default_factory=list)
    delimiter: str = ""
    header_columns: list[str] = field(default_factory=list)
    ignored_columns: list[str] = field(default_factory=list)
    blank_count: int = 0
    error: str = ""


def parse_acquisition_csv(text: str) -> AcquisitionCsvParseResult:
    result = AcquisitionCsvParseResult()
    text = (text or "").lstrip("\ufeff")
    if not text.strip():
        return result
    aliases = {"vout": "origin_vout", "bitcoin_address": "address"}
    columns = set(ImportedRow.__dataclass_fields__) - {"line_number", "original", "error"}
    for delimiter in (";", ",", "\t"):
        try:
            rows = list(csv.reader(io.StringIO(text), delimiter=delimiter, strict=True))
        except csv.Error:
            continue
        header = [c.strip().lower() for c in rows[0]] if rows else []
        names = [aliases.get(c, c) for c in header]
        if "txid" in names and ("origin_vout" in names or "address" in names):
            break
    else:
        result.error = "CSV requires txid and vout (or bitcoin_address) columns."
        return result
    if len(names) != len(set(names)):
        result.error = "Duplicate CSV columns are not supported."
        return result
    result.delimiter = delimiter
    result.header_columns = header
    result.ignored_columns = [c for c, name in zip(header, names) if name not in columns]
    for number, cells in enumerate(rows[1:], 2):
        if not any(c.strip() for c in cells):
            result.blank_count += 1
            continue
        values = {name: value.strip() for name, value in zip(names, cells) if name in columns}
        vout = values.pop("origin_vout", "")
        item = ImportedRow(number, delimiter.join(cells), **values)
        if len(cells) != len(header):
            item.error = "Row has a different number of columns than the header."
        elif vout and not re.fullmatch(r"[0-9]+", vout):
            item.error = "vout must be a non-negative integer."
        elif not item.txid or (not vout and not item.address):
            item.error = "txid and vout (or bitcoin_address) are required."
        else:
            item.origin_vout = int(vout) if vout else None
        result.rows.append(item)
    return result
