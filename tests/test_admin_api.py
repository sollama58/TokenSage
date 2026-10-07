"""The admin API: managed keys, usage, status, jobs."""

from __future__ import annotations

import asyncpg
import httpx
import pytest

from tests.conftest import ADMIN_KEY
from tokensage.api.auth import KEY_PREFIX

pytestmark = pytest.mark.asyncio
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}
MINT = "So11111111111111111111111111111111111111112"


def bearer(k: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {k}"}


async def test_admin_routes_need_the_admin_key(client: httpx.AsyncClient) -> None:
    for path in ("/admin/v1/status", "/admin/v1/keys", "/admin/v1/usage", "/admin/v1/jobs"):
        r = await client.get(path)  # consumer key
        assert r.status_code == 403, path
        assert r.json()["error"]["code"] == "forbidden"
        assert (await client.get(path, headers=bearer("nope"))).status_code == 403
        assert (await client.get(path, headers=ADMIN)).status_code == 200, path


async def test_key_lifecycle(client: httpx.AsyncClient, migrated_db: str) -> None:
    r = await client.post(
        "/admin/v1/keys", headers=ADMIN, json={"name": "app-server", "full_per_day": 20000}
    )
    assert r.status_code == 201, r.text
    created = r.json()
    raw = created["key"]
    assert raw.startswith(KEY_PREFIX)
    assert created["source"] == "db"
    assert created["full_per_day"] == 20000
    assert created["rate_per_min"] == 60  # service default
    assert r.headers["cache-control"] == "no-store"

    # the raw key is never stored
    conn = await asyncpg.connect(migrated_db)
    try:
        stored = await conn.fetchval("select key_sha256 from api_key where name='app-server'")
    finally:
        await conn.close()
    assert stored and raw not in stored

    # the new key works on /v1 at once and its usage is counted under its name
    assert (await client.get("/v1/meta", headers=bearer(raw))).status_code == 200
    keys = (await client.get("/admin/v1/keys", headers=ADMIN)).json()["keys"]
    by_name = {k["name"]: k for k in keys}
    assert by_name["tester"]["source"] == "env"
    assert by_name["app-server"]["usage_today"]["requests"] == 1

    # duplicate names (managed or env) are refused
    dup = await client.post("/admin/v1/keys", headers=ADMIN, json={"name": "app-server"})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "key_exists"
    dup = await client.post("/admin/v1/keys", headers=ADMIN, json={"name": "tester"})
    assert dup.status_code == 409

    # limits change
    r = await client.patch("/admin/v1/keys/app-server", headers=ADMIN, json={"rate_per_min": 1})
    assert r.status_code == 200 and r.json()["rate_per_min"] == 1
    assert r.json()["full_per_day"] == 20000
    assert (await client.get("/v1/meta", headers=bearer(raw))).status_code == 200
    limited = await client.get("/v1/meta", headers=bearer(raw))
    assert limited.status_code == 429
    await client.patch("/admin/v1/keys/app-server", headers=ADMIN, json={"rate_per_min": 600})

    # rotation: old key dies, new one works
    r = await client.post("/admin/v1/keys/app-server/rotate", headers=ADMIN)
    assert r.status_code == 200
    raw2 = r.json()["key"]
    assert raw2 != raw
    assert (await client.get("/v1/meta", headers=bearer(raw))).status_code == 401
    assert (await client.get("/v1/meta", headers=bearer(raw2))).status_code == 200

    # revoke
    r = await client.delete("/admin/v1/keys/app-server", headers=ADMIN)
    assert r.status_code == 200 and r.json()["revoked_at"]
    assert (await client.get("/v1/meta", headers=bearer(raw2))).status_code == 401
    assert (await client.delete("/admin/v1/keys/app-server", headers=ADMIN)).status_code == 404
    names = {k["name"] for k in (await client.get("/admin/v1/keys", headers=ADMIN)).json()["keys"]}
    assert "app-server" not in names
    all_keys = await client.get("/admin/v1/keys?include_revoked=true", headers=ADMIN)
    assert "app-server" in {k["name"] for k in all_keys.json()["keys"]}
    # a revoked name is not reused
    again = await client.post("/admin/v1/keys", headers=ADMIN, json={"name": "app-server"})
    assert again.status_code == 409


async def test_env_keys_are_read_only(client: httpx.AsyncClient) -> None:
    for method, path in (
        ("PATCH", "/admin/v1/keys/tester"),
        ("POST", "/admin/v1/keys/tester/rotate"),
        ("DELETE", "/admin/v1/keys/tester"),
    ):
        r = await client.request(
            method, path, headers=ADMIN, json={"rate_per_min": 5} if method == "PATCH" else None
        )
        assert r.status_code == 409, (method, r.text)
        assert r.json()["error"]["code"] == "read_only"
    assert (await client.get("/v1/meta")).status_code == 200  # API_KEY still works


async def test_unknown_key_and_bad_input(client: httpx.AsyncClient) -> None:
    r = await client.patch("/admin/v1/keys/ghost", headers=ADMIN, json={"rate_per_min": 5})
    assert r.status_code == 404 and r.json()["error"]["code"] == "key_not_found"
    for body in ({"name": "has space"}, {"name": ""}, {"name": "ok", "rate_per_min": 0}):
        r = await client.post("/admin/v1/keys", headers=ADMIN, json=body)
        assert r.status_code == 422, body


async def test_keys_made_elsewhere_are_picked_up_on_reload(
    client: httpx.AsyncClient, migrated_db: str
) -> None:
    from tokensage.api.auth import sha256_hex

    conn = await asyncpg.connect(migrated_db)
    try:
        await conn.execute(
            "insert into api_key (name, key_sha256) values ('other', $1)", sha256_hex("other-raw")
        )
    finally:
        await conn.close()
    assert (await client.get("/v1/meta", headers=bearer("other-raw"))).status_code == 401
    # any admin write reloads; the periodic reloader does the same on other instances
    await client.post("/admin/v1/keys", headers=ADMIN, json={"name": "trigger"})
    assert (await client.get("/v1/meta", headers=bearer("other-raw"))).status_code == 200


async def test_status_usage_and_jobs(client: httpx.AsyncClient, migrated_db: str) -> None:
    await client.get("/v1/meta")
    conn = await asyncpg.connect(migrated_db)
    try:
        failed_id = await conn.fetchval(
            """insert into job (kind, mint, depth, status, attempts, last_error, error_code,
                                finished_at, payload)
               values ('analyze', $1, 'basic', 'failed', 3, 'boom', 'internal', now(),
                       '{"hints": {"name": "X"}}'::jsonb)
               returning id""",
            MINT,
        )
        done_id = await conn.fetchval(
            """insert into job (kind, mint, depth, status, finished_at)
               values ('analyze', $1, 'full', 'done', now()) returning id""",
            MINT,
        )
    finally:
        await conn.close()

    s = (await client.get("/admin/v1/status", headers=ADMIN)).json()
    assert s["queue"]["failed_24h"] == 1 and s["queue"]["done_24h"] == 1
    assert {u["key_name"] for u in s["usage_today"]} == {"tester"}
    assert "depths" in s["recall_24h"]

    u = (await client.get("/admin/v1/usage?days=1&key=tester", headers=ADMIN)).json()
    assert len(u["rows"]) == 1 and u["rows"][0]["requests"] >= 1

    jobs = (await client.get("/admin/v1/jobs?status=failed", headers=ADMIN)).json()["jobs"]
    assert [j["id"] for j in jobs] == [failed_id]
    assert jobs[0]["last_error"] == "boom"

    r = await client.post(f"/admin/v1/jobs/{done_id}/retry", headers=ADMIN)
    assert r.status_code == 409 and r.json()["error"]["code"] == "job_not_failed"
    assert (await client.post("/admin/v1/jobs/999999/retry", headers=ADMIN)).status_code == 404

    r = await client.post(f"/admin/v1/jobs/{failed_id}/retry", headers=ADMIN)
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["id"] != failed_id and new["requested_by"] == "admin"
    assert new["mint"] == MINT and new["depth"] == "basic"
    conn = await asyncpg.connect(migrated_db)
    try:
        payload = await conn.fetchval("select payload::text from job where id=$1", new["id"])
    finally:
        await conn.close()
    assert '"name": "X"' in payload

    assert (await client.get("/admin/v1/recall?hours=1", headers=ADMIN)).status_code == 200


async def test_admin_api_is_in_the_openapi_schema(client: httpx.AsyncClient) -> None:
    paths = (await client.get("/openapi.json")).json()["paths"]
    assert "/admin/v1/keys" in paths and "/admin/v1/status" in paths
