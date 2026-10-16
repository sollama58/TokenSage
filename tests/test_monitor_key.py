"""The read-only MONITOR_KEY: it reads the monitoring GETs under /admin/v1 and nothing else."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from tests.conftest import ADMIN_KEY, MONITOR_KEY
from tokensage.api.auth import KeyStore
from tokensage.config import Settings

MONITOR_PATHS = (
    "/admin/v1/upstreams?hours=24",
    "/admin/v1/helius",
    "/admin/v1/queue",
    "/admin/v1/signals",
    "/admin/v1/status",
    "/admin/v1/usage",
    "/admin/v1/recall",
)


def bearer(k: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {k}"}


async def test_monitor_key_reads_the_monitoring_routes(client: httpx.AsyncClient) -> None:
    for path in MONITOR_PATHS:
        r = await client.get(path, headers=bearer(MONITOR_KEY))
        assert r.status_code == 200, (path, r.text)
        # the admin key still works there, a consumer key or junk does not
        assert (await client.get(path, headers=bearer(ADMIN_KEY))).status_code == 200, path
        assert (await client.get(path)).status_code == 403, path
        assert (await client.get(path, headers=bearer("nope"))).status_code == 403, path


async def test_monitor_key_cannot_manage_keys_or_jobs(client: httpx.AsyncClient) -> None:
    m = bearer(MONITOR_KEY)
    created = await client.post(
        "/admin/v1/keys", headers=bearer(ADMIN_KEY), json={"name": "victim"}
    )
    assert created.status_code == 201, created.text
    calls = [
        ("GET", "/admin/v1/keys", None),
        ("POST", "/admin/v1/keys", {"name": "monitor-made"}),
        ("PATCH", "/admin/v1/keys/victim", {"rate_per_min": 1}),
        ("POST", "/admin/v1/keys/victim/rotate", None),
        ("DELETE", "/admin/v1/keys/victim", None),
        ("GET", "/admin/v1/jobs", None),
        ("POST", "/admin/v1/jobs/1/retry", None),
    ]
    for method, path, body in calls:
        r = await client.request(method, path, headers=m, json=body)
        assert r.status_code == 403, (method, path, r.text)
        assert r.json()["error"]["code"] == "forbidden"
    keys = (await client.get("/admin/v1/keys", headers=bearer(ADMIN_KEY))).json()["keys"]
    by_name = {k["name"]: k for k in keys}
    assert "monitor-made" not in by_name
    assert by_name["victim"]["revoked_at"] is None
    assert by_name["victim"]["rate_per_min"] != 1
    # /readyz stays admin-only too
    assert (await client.get("/readyz", headers=m)).status_code == 403


def test_empty_monitor_key_never_matches() -> None:
    store = KeyStore(Settings(admin_key=ADMIN_KEY, monitor_key="", _env_file=None))  # type: ignore[call-arg]
    assert not store.is_monitor("")
    assert not store.is_monitor("anything")
    assert not store.is_monitor(MONITOR_KEY)
    on = KeyStore(Settings(admin_key=ADMIN_KEY, monitor_key=MONITOR_KEY, _env_file=None))  # type: ignore[call-arg]
    assert on.is_monitor(MONITOR_KEY)
    assert not on.is_monitor("") and not on.is_monitor(ADMIN_KEY)
    assert not on.is_admin(MONITOR_KEY)


@pytest.fixture
async def no_monitor_client(
    settings: Settings, clean_tables: None
) -> AsyncIterator[httpx.AsyncClient]:
    from tokensage.api.app import create_app

    app = create_app(settings.model_copy(update={"monitor_key": ""}))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c


async def test_unset_monitor_key_authenticates_nothing(
    no_monitor_client: httpx.AsyncClient,
) -> None:
    for path in MONITOR_PATHS:
        for headers in (
            bearer(MONITOR_KEY),
            {"Authorization": "Bearer "},
            {"Authorization": "Bearer"},
            {},
        ):
            r = await no_monitor_client.get(path, headers=headers)
            assert r.status_code in (401, 403), (path, headers, r.status_code)
        assert (await no_monitor_client.get(path, headers=bearer(ADMIN_KEY))).status_code == 200
