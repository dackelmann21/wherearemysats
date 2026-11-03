"""Legacy Disposal numeric input, kept separate from on-chain Acquisitions."""
from decimal import Decimal, InvalidOperation
from .service import AcquisitionValidationError


def _all_digits(value):
    return bool(value) and all(c in "0123456789" for c in value)


def _expand_currency_groups(body, decimal_sep, group_sep):
    whole, _, fraction = body.rpartition(decimal_sep)
    groups = whole.split(group_sep)
    if (not _all_digits(fraction) or len(groups) < 2
            or any(not _all_digits(g) for g in groups)
            or len(groups[0]) > 3 or any(len(g) != 3 for g in groups[1:])):
        raise AcquisitionValidationError("The value has malformed thousands grouping.")
    return "".join(groups) + "." + fraction


def _normalize_currency_string(value, label):
    if value is None or not str(value).strip():
        raise AcquisitionValidationError(f"{label} is required.")
    value = str(value).strip()
    negative = value.startswith("-")
    body = value[1:] if negative else value
    if not body:
        raise AcquisitionValidationError(f"{label} must be a number.")
    if "," in body and "." in body:
        decimal_sep, group_sep = (",", ".") if body.rfind(",") > body.rfind(".") else (".", ",")
        return ("-" if negative else "") + _expand_currency_groups(body, decimal_sep, group_sep)
    if "," in body:
        parts = body.split(",")
        if len(parts) != 2 or not all(_all_digits(p) for p in parts):
            raise AcquisitionValidationError(f"{label} must contain a single decimal comma with digits on both sides.")
        return ("-" if negative else "") + ".".join(parts)
    try:
        Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise AcquisitionValidationError(f"{label} must be a number.") from exc
    return value


def _parse_simple_decimal(value, label):
    if value is None or not str(value).strip():
        raise AcquisitionValidationError(f"{label} is required.")
    raw = str(value).strip()
    if "," in raw:
        if "." in raw or raw.count(",") != 1 or not all(raw.split(",")):
            raise AcquisitionValidationError(f"{label} must use one decimal separator.")
        raw = raw.replace(",", ".")
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise AcquisitionValidationError(f"{label} must be a number.") from exc
