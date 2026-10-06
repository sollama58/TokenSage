import struct
from pathlib import Path

import pytest

from pump_ca import (BONDING_CURVE_DISC, b58decode, bonding_curve_pda,
                     decode_bonding_curve, parse_ca)
from pump_event import b58encode, decode_create_event

HERE = Path(__file__).parent


def test_bonding_curve_pda_matches_real_events():
    pairs = [("3arUrpH3nzaRJbbpVgY42dcqSq9A5BFgUxKozZ4npump",
              "45YS7EqqWbhug1w5p2iAyVJb4JrqtS3T6mRpjb6Nz3fS")]  # frontend-api sample
    for line in (HERE / "create_events.txt").read_text().split():
        e = decode_create_event(line)
        pairs.append((e["mint"], e["bonding_curve"]))
    for mint, curve in pairs:
        assert bonding_curve_pda(mint) == curve, mint


def test_parse_ca():
    m = "3arUrpH3nzaRJbbpVgY42dcqSq9A5BFgUxKozZ4npump"
    assert parse_ca(f"  {m} ") == m
    assert parse_ca(f"https://pump.fun/coin/{m}?include-nsfw=true") == m
    for bad in ["", "hello", "0" * 44, m[:-1] + "0", "1" * 50]:
        with pytest.raises(ValueError):
            parse_ca(bad)


def test_decode_bonding_curve_full_and_short():
    creator = b58decode("B8wtc55J62sZ9reiyWLCkJ46b9YnQeMqSbmeyZUg95vR")
    body = struct.pack("<QQQQQ?", 1, 2, 0, 85_000_000_000, 10**15, True) + creator
    short = decode_bonding_curve(BONDING_CURVE_DISC + body)
    assert short["complete"] is True and short["creator"] == b58encode(creator)
    assert "quote_mint" not in short
    full = decode_bonding_curve(BONDING_CURVE_DISC + body + b"\0\0" + bytes(32)
                                + struct.pack("<Q??", 0, False, True))
    assert full["is_holder_reward"] is True
    with pytest.raises(ValueError):
        decode_bonding_curve(b"\0" * 100)
