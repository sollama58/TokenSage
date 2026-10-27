"""Rules 0.37.0 (model audit 2026-10-26 §16): a full read carries its story heat, and stands
as the mint's `now` refresh, so a consumer's first decision on a coin sees the trend match and
the story's market without a second call."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tokensage import analyzer, now
from tokensage.api.schemas import Analysis, Lineage, NowItem, Referent, StoryHeat, Trend
from tokensage.versions import RULES_VERSION

MINT = "MooDengJr111111111111111111111111111111pump"


def _doc(referent: Referent | None = None, lineage: Lineage | None = None) -> Analysis:
    return Analysis(
        mint=MINT,
        referent=referent,
        lineage=lineage,
        trend=Trend(matched=True, score=0.4, novelty=0.9),
        summary="s",
        depth="full",
        analyzed_at=datetime(2026, 10, 27, 12, 0, tzinfo=UTC),
        versions={"rules": RULES_VERSION, "lexicon": "x"},  # type: ignore[arg-type]
    )


def _referent() -> Referent:
    return Referent(
        label="Moo Deng", kind="famous_animal", source="entities", confidence=0.7, generic=False
    )


def _heat() -> StoryHeat:
    return StoryHeat(story="Moo Deng", story_kind="referent", status="ok", coins=2)


def _ctx(timeout_s: float = 5.0) -> SimpleNamespace:
    return SimpleNamespace(settings=SimpleNamespace(story_heat_timeout_s=timeout_s), http=None)


async def test_full_read_carries_the_story_heat(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[dict] = []

    async def fake_heat(_conn: object, _http: object, body: dict) -> StoryHeat:
        asked.append(body)
        return _heat()

    monkeypatch.setattr(now, "story_heat", fake_heat)
    doc = _doc(referent=_referent())
    heat, done = await analyzer._read_story_heat(None, _ctx(), doc)  # type: ignore[arg-type]
    assert done and heat is not None and heat.story == "Moo Deng"
    # the lookup sees the read's own lineage and referent, as the refresh does
    assert asked[0]["referent"]["label"] == "Moo Deng" and asked[0]["lineage"] is None
    assert doc.caveats == []


async def test_a_coin_without_a_story_is_done_without_a_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never(*_a: object) -> StoryHeat:
        raise AssertionError("no story: nothing to search")

    monkeypatch.setattr(now, "story_heat", never)
    generic = _referent().model_copy(update={"generic": True})
    assert await analyzer._read_story_heat(None, _ctx(), _doc(referent=generic)) == (None, True)  # type: ignore[arg-type]
    assert await analyzer._read_story_heat(None, _ctx(), _doc()) == (None, True)  # type: ignore[arg-type]


async def test_a_slow_lookup_leaves_the_block_to_tokens_now(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lookup has its own cap on top of the full-depth deadline: past it the read is
    complete without the block, says so, and is not stored as the refresh."""

    async def slow(*_a: object) -> StoryHeat:
        await asyncio.sleep(5)
        return _heat()

    monkeypatch.setattr(now, "story_heat", slow)
    doc = _doc(referent=_referent())
    heat, done = await analyzer._read_story_heat(None, _ctx(0.01), doc)  # type: ignore[arg-type]
    assert (heat, done) == (None, False)
    assert doc.caveats == [
        "story heat: did not answer within 0.01 s; POST /v1/tokens:now refreshes it"
    ]


async def test_a_failed_lookup_is_a_caveat_not_a_failed_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*_a: object) -> StoryHeat:
        raise RuntimeError("geckoterminal down")

    monkeypatch.setattr(now, "story_heat", boom)
    doc = _doc(referent=_referent())
    assert await analyzer._read_story_heat(None, _ctx(), doc) == (None, False)  # type: ignore[arg-type]
    assert doc.caveats == ["story heat: lookup failed; POST /v1/tokens:now refreshes it"]


@pytest.mark.parametrize("status", ["failed", "rate_limited"])
async def test_a_heat_that_came_back_failed_is_not_done(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """now.story_heat answers a GeckoTerminal failure or 429 with a heat of that status, not a
    raise. The read keeps the block, but it is not done, so the read is not stored as the
    mint's refresh for NOW_MAX_AGE_S (2026-10-27 audit TS-4)."""

    async def failed(*_a: object) -> StoryHeat:
        return StoryHeat(story="Moo Deng", story_kind="referent", status=status, as_of=None)  # type: ignore[arg-type]

    monkeypatch.setattr(now, "story_heat", failed)
    doc = _doc(referent=_referent())
    heat, done = await analyzer._read_story_heat(None, _ctx(), doc)  # type: ignore[arg-type]
    assert not done and heat is not None and heat.status == status
    detail = "rate limited" if status == "rate_limited" else "lookup failed"
    assert doc.caveats == [f"story heat: {detail}; POST /v1/tokens:now refreshes it"]


def test_the_read_is_a_complete_now_item() -> None:
    """What the analyzer stores under now:<mint> is what tokens:now serves: the read's trend
    and heat, as of the read, under the current rules (service.fresh checks both)."""
    doc = _doc(referent=_referent())
    doc.story_heat = _heat()
    body = now.body_from_read(doc)
    assert body["rules"] == RULES_VERSION and body["status"] == "complete"
    item = NowItem.model_validate({k: v for k, v in body.items() if k != "rules"})
    assert item.as_of == doc.analyzed_at
    assert item.trend == doc.trend
    assert item.story_heat == doc.story_heat
    # a read without a story stores a null heat, as a refresh of it would
    doc.story_heat = None
    assert now.body_from_read(doc)["story_heat"] is None


def test_the_analysis_schema_keeps_the_now_shape() -> None:
    """The block in the analysis is the block in the refresh, so a consumer maps one way."""
    doc = _doc(referent=_referent())
    doc.story_heat = _heat()
    dumped = doc.model_dump(mode="json")
    assert dumped["story_heat"] == _heat().model_dump(mode="json")
    assert Analysis.model_validate(dumped).story_heat == _heat()
    # a document stored by older rules has no block: null, not an error
    dumped.pop("story_heat")
    assert Analysis.model_validate(dumped).story_heat is None
