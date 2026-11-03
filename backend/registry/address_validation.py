"""Mainnet Bitcoin address validation (pure local, stdlib-only).

Decodes and classifies *mainnet* addresses with no external calls, no keys,
no descriptors/xpubs, no seeds (REQ 34/39/43, M2/M3). A string only counts
as a *known address* when it classifies as one unambiguous mainnet type:

  * bech32  (``bc1...``)  -> P2WPKH / P2WSH  (BIP-173, hrp ''bc'', v0)
  * bech32m (``bc1...``)  -> P2TR             (BIP-350, v1 taproot)
  * base58  (``1...``)    -> P2PKH            (version 0x00)
  * base58  (``3...``)    -> P2SH             (version 0x05)

Anything else is rejected: testnet/signet/regtest (``tb1``/``bcrt1``/``2``/``m``/
``n``), bare TXIDs (64-hex) which are NOT addresses (REQ 39), non-address
strings, and off-mainnet versions.
"""

from __future__ import annotations

import hashlib
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Bech32 / bech32m (BIP-173 / BIP-350)
# ---------------------------------------------------------------------------

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_CHARSET_REV = {c: i for i, c in enumerate(_CHARSET)}
_HRP = "bc"

_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3

_BECH32_GENERATORS = (
    0x3B6A57B2,
    0x26508E6D,
    0x1EA119FA,
    0x3D4233DD,
    0x2A1462B3,
)


def _polymod(values: List[int]) -> int:
    """Compute the bech32 polymod of 5-bit values."""
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            if (top >> i) & 1:
                chk ^= _BECH32_GENERATORS[i]
    return chk


def _hrp_expand(hrp: str) -> List[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _convertbits(
    data: List[int], frombits: int, tobits: int, pad: bool = True
) -> Optional[List[int]]:
    """Convert between bit group sizes (BIP-173 convertbits)."""
    acc = 0
    bits = 0
    ret: List[int] = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def _bech32_decode(
    address: str,
) -> Tuple[Optional[str], Optional[List[int]], Optional[int]]:
    """Decode a bech32(m) mainnet string into (hrp, data_without_checksum,
    const_marker). Returns (None, None, None) on any failure.

    const_marker is _BECH32_CONST (bech32) or _BECH32M_CONST (bech32m)."""
    text = (address or "").strip()
    if not text:
        return None, None, None
    if text.lower() != text and text.upper() != text:
        return None, None, None
    text = text.lower()
    pos = text.rfind("1")
    if pos < 1 or pos + 7 > len(text):
        return None, None, None
    hrp = text[:pos]
    data_part = text[pos + 1 :]
    if len(data_part) < 6:
        return None, None, None
    data: List[int] = []
    for c in data_part:
        if c not in _CHARSET_REV:
            return None, None, None
        data.append(_CHARSET_REV[c])

    const = _polymod(_hrp_expand(hrp) + data)
    if const not in (_BECH32_CONST, _BECH32M_CONST):
        return None, None, None
    return hrp, data[:-6], const


def _decode_segwit(
    address: str,
) -> Tuple[Optional[str], Optional[List[int]], Optional[int]]:
    """Decode a bech32(m) mainnet address into (address_class, program_bytes,
    const_marker). Returns (None, None, None) on failure."""
    hrp, data, const = _bech32_decode(address)
    if hrp != _HRP or data is None or not data or const is None:
        return None, None, None
    version = data[0]
    program = _convertbits(data[1:], 5, 8, pad=False)
    if program is None:
        return None, None, None
    return version, program, const


def classify_segwit_address(address: str) -> Optional[str]:
    """Classify a mainnet segwit address (bech32/bech32m). Returns
    P2WPKH, P2WSH or P2TR; else None."""
    version, program, const = _decode_segwit(address)
    if version is None or program is None:
        return None
    if version == 0:
        if const != _BECH32_CONST:
            return None
        if len(program) == 20:
            return "P2WPKH"
        if len(program) == 32:
            return "P2WSH"
        return None
    if version == 1:
        if const != _BECH32M_CONST:
            return None
        if len(program) == 32:
            return "P2TR"
        return None
    return None


# ---------------------------------------------------------------------------
# Base58check (mainnet)
# ---------------------------------------------------------------------------

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}

_VERSION_P2PKH = bytes.fromhex("00")
_VERSION_P2SH = bytes.fromhex("05")


def _sha256d(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def _base58check_decode(text: str) -> Optional[bytes]:
    """Decode a base58check string. Returns version||payload bytes with the
    4-byte checksum verified, else None."""
    candidate = (text or "").strip()
    if not candidate:
        return None
    num = 0
    try:
        for c in candidate:
            num = num * 58 + _B58_INDEX[c]
    except KeyError:
        return None

    # Count leading '1's (each encodes a leading zero byte).
    leading = 0
    for c in candidate:
        if c == "1":
            leading += 1
        else:
            break

    if num == 0:
        body = b""
    else:
        body = num.to_bytes((num.bit_length() + 7) // 8, "big")
    raw = b"\x00" * leading + body
    if len(raw) < 25:
        return None

    payload, checksum = raw[:-4], raw[-4:]
    if _sha256d(payload)[:4] != checksum:
        return None
    return payload


def classify_base58_address(address: str) -> Optional[str]:
    """Decode+classify a mainnet base58check address. Returns P2PKH, P2SH or
    None."""
    payload = _base58check_decode(address)
    if payload is None:
        return None
    if len(payload) != 21:
        return None
    if payload[:1] == _VERSION_P2PKH:
        return "P2PKH"
    if payload[:1] == _VERSION_P2SH:
        return "P2SH"
    return None


# ---------------------------------------------------------------------------
# Combined classifier
# ---------------------------------------------------------------------------

_TXID_HEX = frozenset("0123456789abcdefABCDEF")


def _looks_like_txid(text: str) -> bool:
    """A bare 64-hex string is a TXID (never an address, REQ 39)."""
    if len(text) != 64:
        return False
    return all(c in _TXID_HEX for c in text)


def classify_address(address: str) -> Optional[str]:
    """Classify a *mainnet* address, else None. Token in:
    P2PKH, P2SH, P2WPKH, P2WSH, P2TR."""
    candidate = (address or "").strip()
    if not candidate:
        return None
    if _looks_like_txid(candidate):
        return None
    if candidate[0] in ("1", "3"):
        return classify_base58_address(candidate)
    if candidate.lower().startswith("bc1"):
        return classify_segwit_address(candidate)
    return None


def is_valid_mainnet_address(address: str) -> bool:
    return classify_address(address) is not None


def address_to_script(address: str) -> Optional[str]:
    """Recompute the raw (witness/program) output script of a mainnet address.

    Returns the hex scriptPubKey without length prefix, or None when the
    address is not a valid mainnet address. Used by the local Electrs
    fallback (scripthash = sha256(script)[::-1].hex()). The reverse-address
    discovery endpoint relies on this so the app never needs to touch external
    explorers for script derivation.
    """
    cls = classify_address(address)
    if cls is None:
        return None
    if cls == "P2PKH":
        payload = _base58check_decode(address)
        return "76a914" + payload[1:21].hex() + "88ac"
    if cls == "P2SH":
        payload = _base58check_decode(address)
        return "a914" + payload[1:21].hex() + "87"
    version, program, _const = _decode_segwit(address)
    if version is None or program is None:
        return None
    if version == 0 and cls == "P2WPKH":
        return "0014" + bytes(program).hex()
    if version == 0 and cls == "P2WSH":
        return "0020" + bytes(program).hex()
    if version == 1 and cls == "P2TR":
        return "5120" + bytes(program).hex()
    return None


def address_type_name(address: str) -> str:
    """Best-effort address type name ('' when invalid)."""
    return classify_address(address) or ""
