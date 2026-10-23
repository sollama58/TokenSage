"""Admin > Environment's TokenSage half: which expected env vars each service has set, never
their values (tokensage/envreport.py, GET /admin/v1/env)."""

from __future__ import annotations

import json
from pathlib import Path

import asyncpg
import httpx
import pytest
import yaml

from tests.conftest import MONITOR_KEY
from tokensage import envreport

RENDER = Path(__file__).resolve().parent.parent / "render.yaml"
NAMES = {
    "tokensage-api": "api",
    "tokensage-analyzer": "analyzer",
    "tokensage-knowledge": "knowledge",
    "tokensage-maintenance": "maintenance",
}


def render_keys() -> dict[str, set[str]]:
    doc = yaml.safe_load(RENDER.read_text())
    groups = {g["name"]: g["envVars"] for g in doc["envVarGroups"]}
    out: dict[str, set[str]] = {s: set() for s in NAMES.values()}
    for svc in doc["services"]:
        sid = NAMES.get(svc["name"])
        if sid is None:
            continue
        for v in svc.get("envVars", []):
            for e in groups[v["fromGroup"]] if "fromGroup" in v else [v]:
                out[sid].add(e["key"])
    return out


def test_manifest_lists_every_render_yaml_variable_on_its_service() -> None:
    keys = render_keys()
    assert "TRUSTED_HINT_KEYS" in keys["analyzer"]
    for sid in NAMES.values():
        listed = {v.key for v in envreport.MANIFEST if sid in v.on}
        assert keys[sid] - listed == set(), sid
    names = [v.key for v in envreport.MANIFEST]
    assert len(names) == len(set(names))
    risky = [
        v.key
        for v in envreport.MANIFEST
        if v.kind == "setting" and v.key.endswith(("KEY", "KEYS_SECRET", "SECRET", "_URL"))
    ]
    assert risky == []


def test_describe_never_carries_a_secret() -> None:
    assert envreport.describe("secret", None).state == "missing"
    assert envreport.describe("secret", "  ").state == "empty"
    s = envreport.describe("secret", "super-secret-key")
    assert (s.state, s.hint) == ("set", "16 chars")
    assert s.print and "super" not in json.dumps(s.as_json())
    u = envreport.describe("url", "https://mainnet.helius-rpc.com/?api-key=abc123")
    assert u.hint == "mainnet.helius-rpc.com" and "abc123" not in json.dumps(u.as_json())
    assert envreport.describe("url", "postgresql://u:pw@db:5432/x").hint == "postgresql://db"
    assert envreport.describe("url", "nonsense").hint == "set, not a URL"
    assert envreport.describe("setting", " trenchscanner ").hint == "trenchscanner"


async def test_env_route_lays_services_side_by_side(
    client: httpx.AsyncClient, migrated_db: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = await asyncpg.connect(migrated_db)
    try:
        await conn.execute("truncate service_env")
        await envreport.publish(
            conn,
            "analyzer",
            {
                "SOLANA_RPC_URL": "https://rpc.example/?api-key=zzz",
                "COINGECKO_API_KEY": "cg-a",
                "TRUSTED_HINT_KEYS": "",
                "RENDER_GIT_COMMIT": "0123456789abcdef",
            },
        )
        await envreport.publish(conn, "knowledge", {"COINGECKO_API_KEY": "cg-b"})
    finally:
        await conn.close()
    monkeypatch.setenv("MONITOR_KEY", "live-monitor-key")
    r = await client.get("/admin/v1/env", headers={"Authorization": f"Bearer {MONITOR_KEY}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "zzz" not in r.text and "live-monitor-key" not in r.text and "print" not in r.text
    by_id = {s["id"]: s for s in body["services"]}
    assert by_id["api"]["live"] is True
    assert by_id["analyzer"]["commit"] == "0123456789ab"
    assert by_id["maintenance"]["reported_at"] is None
    rows = {v["key"]: v for v in body["vars"]}
    assert rows["TRUSTED_HINT_KEYS"]["cells"]["analyzer"] == {
        "need": "expected",
        "state": "empty",
        "hint": None,
    }
    assert rows["SOLANA_RPC_URL"]["cells"]["analyzer"]["hint"] == "rpc.example"
    assert rows["MONITOR_KEY"]["cells"]["api"]["state"] == "set"
    assert rows["COINGECKO_API_KEY"]["differs"] is True
    assert rows["DATABASE_URL"]["cells"]["maintenance"]["state"] == "unknown"
    # the consumer key the client sends by default can't read it
    assert (await client.get("/admin/v1/env")).status_code == 403
