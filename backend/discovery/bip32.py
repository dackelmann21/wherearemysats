"""Minimal, dependency-free BIP32 public-key derivation and script encoding.

This module implements exactly what one-time wallet discovery needs:

  * parse an extended *public* key (xpub / ypub / zpub) from base58check
  * derive non-hardened children (CKDpub) for a public key
  * turn the resulting compressed public keys into mainnet/testnet addresses
    for p2wpkh / p2sh-p2wpkh / p2pkh / p2tr script types

Everything happens in-process. The extended key is never written to disk,
never logged and never sent to the Bitcoin Core node (privacy
requirement): the node is only queried
with concrete ``addr()`` descriptors for already-derived addresses.

No third-party cryptography: secp256k1 point arithmetic is implemented from
first principles (affine coordinates, pow-based modular inverse). RIPEMD160 is
provided by the OpenSSL-backed ``hashlib`` where available.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# secp256k1
# ---------------------------------------------------------------------------

_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)

_Point = Optional[tuple[int, int]]


def _point_add(p1: _Point, p2: _Point) -> _Point:
    """Addition on the short Weierstrass curve y^2 = x^3 + 7 (mod p)."""
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if x1 == x2 and y1 == y2:
        lam = (3 * x1 * x1) * pow(2 * y1, -1, _P) % _P
    else:
        lam = (y2 - y1) * pow(x2 - x1, -1, _P) % _P
    x3 = (lam * lam - x1 - x2) % _P
    y3 = (lam * (x1 - x3) - y1) % _P
    return (x3, y3)


def _scalar_mult(k: int, point: _Point) -> _Point:
    result: _Point = None
    addend = point
    while k:
        if k & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        k >>= 1
    return result


def _point_from_pubkey(pub: bytes) -> tuple[int, int]:
    if len(pub) != 33 or pub[0] not in (0x02, 0x03):
        raise ValueError("invalid compressed public key")
    x = int.from_bytes(pub[1:], "big")
    alpha = (pow(x, 3, _P) + 7) % _P
    y = pow(alpha, (_P + 1) // 4, _P)  # p % 4 == 3
    if pow(y, 2, _P) != alpha:
        raise ValueError("point is not on secp256k1")
    if (pub[0] == 0x02 and y % 2 != 0) or (pub[0] == 0x03 and y % 2 == 0):
        y = _P - y
    return (x, y)


def _pubkey_from_point(point: tuple[int, int]) -> bytes:
    x, y = point
    prefix = b"\x02" if y % 2 == 0 else b"\x03"
    return prefix + x.to_bytes(32, "big")


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def hash160(data: bytes) -> bytes:
    return hashlib.new("ripemd160", hashlib.sha256(data).digest()).digest()


def _hmac_sha512(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha512).digest()


# ---------------------------------------------------------------------------
# base58check
# ---------------------------------------------------------------------------

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


def _b58check_decode(text: str) -> Optional[bytes]:
    candidate = (text or "").strip()
    if not candidate:
        return None
    num = 0
    try:
        for c in candidate:
            num = num * 58 + _B58_INDEX[c]
    except KeyError:
        return None
    # Leading '1' characters encode leading zero bytes.
    leading = 0
    for c in candidate:
        if c == "1":
            leading += 1
        else:
            break
    leading = 0
    for c in candidate:
        if c == "1":
            leading += 1
        else:
            break
    body = b"" if num == 0 else num.to_bytes((num.bit_length() + 7) // 8, "big")
    raw = b"\x00" * leading + body
    if len(raw) < 5:
        return None
    payload, checksum = raw[:-4], raw[-4:]
    if _dsha256(payload)[:4] != checksum:
        return None
    return payload


def _b58check_encode(payload: bytes) -> str:
    raw = payload + _dsha256(payload)[:4]
    num = int.from_bytes(raw, "big")
    encoded = ""
    while num > 0:
        num, rem = divmod(num, 58)
        encoded = _B58_ALPHABET[rem] + encoded
    leading = 0
    for byte in raw:
        if byte == 0:
            leading += 1
        else:
            break
    return "1" * leading + (encoded or "")


def _dsha256(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


# ---------------------------------------------------------------------------
# bech32 / bech32m encoding
# ---------------------------------------------------------------------------

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3

_BECH32_GENERATORS = (
    0x3B6A57B2,
    0x26508E6D,
    0x1EA119FA,
    0x3D4233DD,
    0x2A1462B3,
)

_HRP_FOR_NETWORK = {"mainnet": "bc", "main": "bc", "testnet": "tb", "regtest": "bcrt"}


def _bech32_polymod(values: list[int]) -> int:
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            if (top >> i) & 1:
                chk ^= _BECH32_GENERATORS[i]
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_convertbits(data: list[int], frombits: int, tobits: int, pad: bool = True) -> list[int]:
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            raise ValueError("invalid bech32 data")
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    return ret


def _bech32_encode(hrp: str, version: int, program: bytes, bech32m: bool = False) -> str:
    """Encode a segwit address (BIP-173 / BIP-350)."""
    const = _BECH32M_CONST if bech32m else _BECH32_CONST
    data = [version] + _bech32_convertbits(list(program), 8, 5)
    combined = _bech32_hrp_expand(hrp) + data
    polymod = _bech32_polymod(combined + [0, 0, 0, 0, 0, 0]) ^ const
    checksum = [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_BECH32_CHARSET[d] for d in data + checksum)


# ---------------------------------------------------------------------------
# Extended public keys
# ---------------------------------------------------------------------------

# version bytes for mainnet/testnet public account keys
_XPUB_VERSION = 0x0488B21E
_YPUB_VERSION = 0x049D7CB2
_ZPUB_VERSION = 0x04B24746
_XPUB_TEST = 0x043587CF
_YPUB_TEST = 0x044A5262
_ZPUB_TEST = 0x045F1CF6


class ExtendedKeyError(ValueError):
    pass


@dataclass(frozen=True)
class ExtendedPublicKey:
    """A BIP32 extended public key (xpub / ypub / zpub)."""

    key_type: str            # "xpub" | "ypub" | "zpub" | "unknown"
    derived_path: str        # descriptive path, e.g. "m/84'/0'/0'"
    network: str             # "mainnet" | "testnet" | "unknown"
    pubkey: bytes            # 33-byte compressed public key
    chaincode: bytes         # 32 bytes

    @property
    def fingerprint(self) -> bytes:
        return hash160(self.pubkey)[:4]

    def derive_public(self, indices: list[int]) -> tuple[bytes, bytes]:
        """CKDpub for a non-hardened index list. Returns (pubkey, chaincode)."""
        pub, cc = self.pubkey, self.chaincode
        for index in indices:
            if index >= 0x80000000:
                raise ExtendedKeyError("hardened derivation is not possible from a public key")
            data = pub + index.to_bytes(4, "big")
            i = _hmac_sha512(cc, data)
            il = int.from_bytes(i[:32], "big")
            if il >= _N:
                raise ExtendedKeyError("derivation produced an invalid scalar")
            parent = _point_from_pubkey(pub)
            child = _point_add(parent, _scalar_mult(il, _G))
            if child is None:
                raise ExtendedKeyError("derivation produced the point at infinity")
            pub = _pubkey_from_point(child)
            cc = i[32:]
        return pub, cc

    def derive_address(
        self,
        branch_index: int,
        address_index: int,
        script_type: str,
        network: str = "mainnet",
    ) -> str:
        """Derive the address for ``m/.../<branch>/<address>``.

        ``branch_index`` is the *first* non-hardened child applied on top of
        this key (0 = receive, 1 = change), ``address_index`` the position
        within that branch.
        """
        pub, _ = self.derive_public([branch_index, address_index])
        return encode_pubkey(pub, script_type, network)


def encode_pubkey(pubkey: bytes, script_type: str, network: str = "mainnet") -> str:
    script_type = (script_type or "").lower()
    hrp = _HRP_FOR_NETWORK.get(network, "bc")
    if script_type in ("p2wpkh", "wpkh", "segwit"):
        return _bech32_encode(hrp, 0, hash160(pubkey), bech32m=False)
    if script_type in ("p2sh-p2wpkh", "sh(wpkh)"):
        redeem = b"\x00\x14" + hash160(pubkey)
        version = b"\x05" if network in ("mainnet", "main") else b"\xc4"
        return _b58check_encode(version + hash160(redeem))
    if script_type in ("p2pkh", "pkh"):
        version = b"\x00" if network in ("mainnet", "main") else b"\x6f"
        return _b58check_encode(version + hash160(pubkey))
    if script_type in ("p2tr", "tr"):
        # x-only internal key: drop the parity byte, use bech32m (BIP-350).
        return _bech32_encode(hrp, 1, pubkey[1:], bech32m=True)
    raise ValueError(f"unsupported script type: {script_type}")


def parse_extended_public_key(text: str) -> ExtendedPublicKey:
    """Parse an xpub / ypub / zpub string into an ExtendedPublicKey.

    Network version bytes are validated against the known prefix sets. The
    derived path description is best-effort (we only know the prefix family).
    """
    raw = _b58check_decode(text)
    if raw is None:
        raise ExtendedKeyError("the supplied string is not a valid base58check extended key")
    if len(raw) != 78:
        raise ExtendedKeyError("extended key payload must be 78 bytes")
    version = int.from_bytes(raw[0:4], "big")

    key_type: str
    network: str
    path: str
    if version == _XPUB_VERSION:
        key_type, network, path = "xpub", "mainnet", "m"
    elif version == _YPUB_VERSION:
        key_type, network, path = "ypub", "mainnet", "m/49'/0'/0'"
    elif version == _ZPUB_VERSION:
        key_type, network, path = "zpub", "mainnet", "m/84'/0'/0'"
    elif version == _XPUB_TEST:
        key_type, network, path = "xpub", "testnet", "m"
    elif version == _YPUB_TEST:
        key_type, network, path = "ypub", "testnet", "m/49'/1'/0'"
    elif version == _ZPUB_TEST:
        key_type, network, path = "zpub", "testnet", "m/84'/1'/0'"
    else:
        raise ExtendedKeyError("unsupported / unrecognised extended key version")

    pubkey = raw[45:78]
    if len(pubkey) != 33 or pubkey[0] not in (0x02, 0x03):
        raise ExtendedKeyError("extended key does not contain a compressed public key")

    return ExtendedPublicKey(
        key_type=key_type,
        derived_path=path,
        network=network,
        pubkey=pubkey,
        chaincode=raw[13:45],
    )


def network_for_extended_key(text: str) -> Optional[str]:
    """Return the network implied by an xpub-like string, else None."""
    try:
        return parse_extended_public_key(text).network
    except ExtendedKeyError:
        return None