"""Metaplex Token Metadata: PDA derivation and a minimal decoder (name, symbol, uri)."""

from __future__ import annotations

import struct

from tokensage.resolve.pump_ca import b58decode, find_program_address

METAPLEX_PROGRAM = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"


def metadata_pda(mint: str) -> str:
    return find_program_address(
        [b"metadata", b58decode(METAPLEX_PROGRAM), b58decode(mint)], METAPLEX_PROGRAM
    )[0]


def _borsh_string(data: bytes, offset: int) -> tuple[str, int]:
    (n,) = struct.unpack_from("<I", data, offset)
    offset += 4
    raw = data[offset : offset + n]
    return raw.decode("utf-8", "replace").rstrip("\x00").strip(), offset + n


def decode_metadata(data: bytes) -> dict[str, str]:
    """Metadata account: key(1) + update_authority(32) + mint(32) + Data{name, symbol, uri,...}.
    Strings are Borsh (u32 len + bytes) and NUL-padded to fixed widths."""
    if len(data) < 1 + 32 + 32 + 4:
        raise ValueError("metadata account too short")
    off = 1 + 32 + 32
    name, off = _borsh_string(data, off)
    symbol, off = _borsh_string(data, off)
    uri, off = _borsh_string(data, off)
    return {"name": name, "symbol": symbol, "uri": uri}


def encode_metadata_for_tests(name: str, symbol: str, uri: str) -> bytes:
    """Produce a byte layout the decoder accepts (used by tests/fixtures only)."""

    def s(v: str, width: int) -> bytes:
        b = v.encode()[:width].ljust(width, b"\x00")
        return struct.pack("<I", width) + b

    return bytes([4]) + bytes(32) + bytes(32) + s(name, 32) + s(symbol, 10) + s(uri, 200)
