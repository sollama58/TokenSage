"""CA (mint) helpers for TokenSage (reference; tested in test_pump_ca.py).

- validate a Solana base58 address
- derive the pump.fun bonding-curve PDA for a mint: seeds ["bonding-curve", mint]
- decode the BondingCurve account (tolerant of older, shorter layouts)

Production code may use `solders` (Pubkey.find_program_address) instead of the
pure-Python ed25519 on-curve check below.
"""
from __future__ import annotations

import hashlib
import struct

from pump_event import b58encode

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
BONDING_CURVE_DISC = bytes([23, 183, 248, 55, 96, 216, 172, 96])
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_IDX = {c: i for i, c in enumerate(_B58)}


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58_IDX[c]  # KeyError on invalid characters
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + body


def parse_ca(raw: str) -> str:
    """Return the canonical mint address or raise ValueError. Accepts surrounding
    whitespace and pump.fun / explorer URLs ending in the address."""
    s = raw.strip().rstrip("/").split("/")[-1].split("?")[0]
    if not 32 <= len(s) <= 44:
        raise ValueError("not a Solana address (length)")
    try:
        b = b58decode(s)
    except KeyError:
        raise ValueError("not a Solana address (base58)") from None
    if len(b) != 32:
        raise ValueError("not a Solana address (32 bytes)")
    return s


# --- ed25519 on-curve check (needed to emulate find_program_address) ---
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def _on_curve(b: bytes) -> bool:
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    y2 = y * y % _P
    u, v = (y2 - 1) % _P, (_D * y2 + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    if x2 == 0:
        return True
    return pow(x2, (_P - 1) // 2, _P) == 1  # quadratic residue => decompressible


def find_program_address(seeds: list[bytes], program_id: str) -> tuple[str, int]:
    pid = b58decode(program_id)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + pid + b"ProgramDerivedAddress").digest()
        if not _on_curve(h):
            return b58encode(h), bump
    raise ValueError("no viable bump")


def bonding_curve_pda(mint: str) -> str:
    return find_program_address([b"bonding-curve", b58decode(mint)], PUMP_PROGRAM)[0]


_BC_FIELDS = [
    ("virtual_token_reserves", "<Q"), ("virtual_quote_reserves", "<Q"),
    ("real_token_reserves", "<Q"), ("real_quote_reserves", "<Q"),
    ("token_total_supply", "<Q"), ("complete", "?"), ("creator", "pk"),
    ("is_mayhem_mode", "?"), ("is_cashback_coin", "?"), ("quote_mint", "pk"),
    ("creator_fee_bps", "<Q"), ("can_edit_creator_fee", "?"), ("is_holder_reward", "?"),
]


def decode_bonding_curve(data: bytes) -> dict:
    """Decode BondingCurve account data (from getAccountInfo, base64-decoded).
    Older accounts are shorter: missing trailing fields are simply absent.
    A creator of all zeros / quote_mint of all zeros means 'unset' / SOL."""
    if data[:8] != BONDING_CURVE_DISC:
        raise ValueError("not a pump.fun BondingCurve account")
    o, out = 8, {}
    for name, fmt in _BC_FIELDS:
        size = 32 if fmt == "pk" else struct.calcsize(fmt)
        if o + size > len(data):
            break
        chunk = data[o:o + size]
        out[name] = b58encode(chunk) if fmt == "pk" else struct.unpack(fmt, chunk)[0]
        o += size
    return out
