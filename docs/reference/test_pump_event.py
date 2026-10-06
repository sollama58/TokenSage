from pathlib import Path

from pump_event import decode_create_event

HERE = Path(__file__).parent


def test_real_create_events_decode():
    lines = (HERE / "create_events.txt").read_text().split()
    assert lines
    for line in lines:
        e = decode_create_event(line)
        assert e["name"] and e["symbol"] and e["uri"].startswith("http")
        assert len(e["mint"]) in (43, 44)
        assert e["_unparsed_tail_bytes"] == 0


def test_non_create_returns_none():
    assert decode_create_event("AAAAAAAAAAA=") is None
