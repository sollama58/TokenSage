"""2026-10-26: the database stage runs on a bounded number of slots at once."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tokensage import analyzer


@pytest.mark.asyncio
async def test_db_context_runs_on_a_bounded_number_of_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Twelve slots all in db_context at once took the stage from seconds to past the 30 s
    command timeout (2026-10-26, after #91 revived the stuck slots). Only
    db_context_concurrency analyses query at a time; the others wait their turn."""
    running = 0
    peak = 0

    async def fake_queries(*_args: object, **_kwargs: object) -> str:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return "dbc"

    monkeypatch.setattr(analyzer, "_db_context_queries", fake_queries)
    ctx = SimpleNamespace(settings=SimpleNamespace(db_context_concurrency=2), http=None)
    r = SimpleNamespace(mint="M", created_at=None, creator=None)

    async def one() -> str:
        return await analyzer._db_context(None, ctx, r, None, None, None, "")  # type: ignore[arg-type]

    results = await asyncio.gather(*(one() for _ in range(6)))
    assert results == ["dbc"] * 6
    assert peak == 2

    # A new limit from the settings sizes a new gate; the old one is not kept.
    ctx.settings.db_context_concurrency = 3
    peak = 0
    await asyncio.gather(*(one() for _ in range(6)))
    assert peak == 3
