"""Tolerant decoder for pump.fun CreateEvent `Program data:` log lines."""

from __future__ import annotations

import base64
import struct

CREATE_EVENT_DISC = bytes.fromhex("1b72a94ddeeb6376")
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


class _R:
    def __init__(self, buf: bytes):
        self.b, self.o = buf, 0

    def left(self) -> int:
        return len(self.b) - self.o

    def take(self, n: int) -> bytes:
        if self.left() < n:
            raise EOFError
        v = self.b[self.o : self.o + n]
        self.o += n
        return v

    def string(self) -> str:
        (n,) = struct.unpack("<I", self.take(4))
        return self.take(n).decode("utf-8", "replace")

    def pubkey(self) -> str:
        return b58encode(self.take(32))

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.take(8))[0]

    def boolean(self) -> bool:
        return self.take(1) != b"\0"


# (field, reader) in IDL order. Fields are appended over time; older events are shorter.
_FIELDS = [
    ("name", "string"),
    ("symbol", "string"),
    ("uri", "string"),
    ("mint", "pubkey"),
    ("bonding_curve", "pubkey"),
    ("user", "pubkey"),
    ("creator", "pubkey"),
    ("timestamp", "i64"),
    ("virtual_token_reserves", "u64"),
    ("virtual_sol_reserves", "u64"),
    ("real_token_reserves", "u64"),
    ("token_total_supply", "u64"),
    ("token_program", "pubkey"),
    ("is_mayhem_mode", "boolean"),
    ("is_cashback_enabled", "boolean"),
    ("quote_mint", "pubkey"),
    ("virtual_quote_reserves", "u64"),
    ("creator_fee_bps", "u64"),
    ("is_holder_reward", "boolean"),
]
_REQUIRED = {"name", "symbol", "uri", "mint", "bonding_curve", "user"}


def decode_create_event(program_data_b64: str) -> dict | None:
    """Return the decoded event, or None if the line is not a CreateEvent."""
    raw = base64.b64decode(program_data_b64)
    if raw[:8] != CREATE_EVENT_DISC:
        return None
    r, out = _R(raw[8:]), {}
    for name, kind in _FIELDS:
        try:
            out[name] = getattr(r, kind)()
        except EOFError:
            break
    if not _REQUIRED <= out.keys():
        raise ValueError("truncated CreateEvent")
    out["_unparsed_tail_bytes"] = r.left()  # >0 means the IDL grew: log it
    return out
