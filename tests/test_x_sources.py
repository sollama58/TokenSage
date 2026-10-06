"""X fetch chain and relation signals, fully offline (respx)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from tokensage.engine import xsignals
from tokensage.engine.xref import syndication_token
from tokensage.sources import x as xs

TID = "1791351500217754008"
FX_OK = {
    "code": 200,
    "status": {
        "id": TID,
        "text": "Peanut did nothing wrong $PNUT",
        "created_timestamp": 1715800000,
        "author": {
            "id": "44196397",
            "screen_name": "elonmusk",
            "name": "Elon Musk",
            "followers": 190000000,
            "joined": "2009-06-02T20:12:29.000Z",
            "verification": {"verified": True, "type": "individual"},
        },
        "media": {"photos": [{"url": "x"}]},
        "likes": 5,
        "replies": 1,
        "reposts": 2,
        "views": 100,
    },
}
VX_OK = {
    "tweetID": TID,
    "text": "vx text",
    "date_epoch": 1715800000,
    "user_screen_name": "someone",
    "user_name": "Some One",
    "likes": 1,
    "replies": 0,
    "retweets": 0,
    "mediaURLs": [],
}
SYND_OK = {
    "__typename": "Tweet",
    "id_str": TID,
    "text": "synd text",
    "created_at": "2024-05-15T19:06:40.000Z",
    "user": {"screen_name": "jack", "id_str": "12", "name": "jack", "is_blue_verified": True},
    "favorite_count": 3,
    "conversation_count": 1,
}


@pytest.fixture
async def http():  # type: ignore[no-untyped-def]
    async with httpx.AsyncClient(headers={"User-Agent": "test"}) as c:
        yield c


@respx.mock
async def test_chain_prefers_fxtwitter(http: httpx.AsyncClient) -> None:
    respx.get(f"{xs.FX}/status/{TID}").mock(return_value=httpx.Response(200, json=FX_OK))
    t = await xs.fetch_tweet(http, TID)
    assert t.status == "ok" and t.source == "fxtwitter"
    assert t.author_handle == "elonmusk" and t.followers == 190000000
    assert t.created_at == datetime(2024, 5, 15, 19, 6, 40, tzinfo=UTC)
    assert t.media_count == 1 and t.verified_type == "blue"
    # round-trips through the cache format
    assert xs.TweetData.from_json(t.to_json()).created_at == t.created_at


@respx.mock
async def test_chain_falls_through_to_vx_then_syndication_then_oembed(
    http: httpx.AsyncClient,
) -> None:
    respx.get(f"{xs.FX}/status/{TID}").mock(return_value=httpx.Response(503))
    respx.get(f"{xs.VX}/i/status/{TID}").mock(return_value=httpx.Response(200, json=VX_OK))
    t = await xs.fetch_tweet(http, TID)
    assert t.status == "ok" and t.source == "vxtwitter" and t.author_handle == "someone"

    respx.get(f"{xs.VX}/i/status/{TID}").mock(return_value=httpx.Response(500))
    tok = syndication_token(TID)
    respx.get(f"{xs.SYND}?id={TID}&lang=en&token={tok}").mock(
        return_value=httpx.Response(200, json=SYND_OK)
    )
    t = await xs.fetch_tweet(http, TID)
    assert t.source == "syndication" and t.author_handle == "jack" and t.verified_type == "blue"

    respx.get(f"{xs.SYND}?id={TID}&lang=en&token={tok}").mock(
        return_value=httpx.Response(200, json={})
    )
    respx.get(url__startswith=xs.OEMBED).mock(
        return_value=httpx.Response(
            200,
            json={
                "author_name": "jack",
                "author_url": "https://twitter.com/jack",
                "html": (
                    '<blockquote><p lang="en">just setting up my twttr</p>&mdash; jack</blockquote>'
                ),
            },
        )
    )
    t = await xs.fetch_tweet(http, TID)
    assert (
        t.source == "oembed" and t.text == "just setting up my twttr" and t.author_handle == "jack"
    )


@respx.mock
async def test_deleted_tweet_and_all_down(http: httpx.AsyncClient) -> None:
    respx.get(f"{xs.FX}/status/{TID}").mock(return_value=httpx.Response(404))
    respx.get(f"{xs.VX}/i/status/{TID}").mock(return_value=httpx.Response(404))
    respx.get(url__startswith=xs.SYND).mock(
        return_value=httpx.Response(200, json={"__typename": "TweetTombstone"})
    )
    respx.get(url__startswith=xs.OEMBED).mock(return_value=httpx.Response(404))
    t = await xs.fetch_tweet(http, TID)
    assert t.status == "deleted"

    respx.get(f"{xs.FX}/status/{TID}").mock(side_effect=httpx.ConnectError("boom"))
    respx.get(f"{xs.VX}/i/status/{TID}").mock(side_effect=httpx.ConnectError("boom"))
    respx.get(url__startswith=xs.SYND).mock(side_effect=httpx.ConnectError("boom"))
    respx.get(url__startswith=xs.OEMBED).mock(side_effect=httpx.ConnectError("boom"))
    t = await xs.fetch_tweet(http, "999")
    assert t.status == "failed"


@respx.mock
async def test_profile_chain_and_username_changes(http: httpx.AsyncClient) -> None:
    respx.get(f"{xs.FX}/profile/elonmusk").mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 200,
                "user": {
                    "id": "44196397",
                    "screen_name": "elonmusk",
                    "name": "Elon Musk",
                    "followers": 190000000,
                    "following": 800,
                    "statuses": 50000,
                    "joined": "2009-06-02T20:12:29.000Z",
                    "verification": {"verified": True, "type": None},
                },
            },
        )
    )
    respx.get(f"{xs.FX}/profile/elonmusk/about").mock(
        return_value=httpx.Response(200, json={"about_account": {"username_changes": {"count": 2}}})
    )
    p = await xs.fetch_profile(http, "elonmusk")
    assert p.status == "ok" and p.followers == 190000000 and p.username_changes == 2
    respx.get(f"{xs.FX}/profile/nobody").mock(return_value=httpx.Response(503))
    respx.get(f"{xs.VX}/nobody").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 1,
                "screen_name": "nobody",
                "followers_count": 3,
                "created_at": "Wed Oct 05 18:40:30 +0000 2022",
            },
        )
    )
    p = await xs.fetch_profile(http, "nobody")
    assert p.source == "vxtwitter" and p.followers == 3 and p.joined is not None


# ------------------------------------------------------------- relation signals


def _tweet(**over) -> xs.TweetData:  # type: ignore[no-untyped-def]
    base = dict(
        id=TID,
        status="ok",
        source="fxtwitter",
        text="hello",
        author_handle="elonmusk",
        followers=190000000,
        verified_type="blue",
        created_at=datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
        author_joined=datetime(2009, 6, 2, tzinfo=UTC),
    )
    base.update(over)
    return xs.TweetData(**base)  # type: ignore[arg-type]


TOKEN_T = datetime(2026, 10, 1, 13, 0, tzinfo=UTC)


def test_narrative_reference_and_borrowed_narrative() -> None:
    a = xsignals.assess("tweet", "elonmusk", _tweet(), None, TOKEN_T, "PNUT", "mint", ["peanut"])
    assert a.relation == "narrative_reference"
    codes = {f[0] for f in a.flags}
    assert "borrowed_narrative" in codes and "spoofed_tweet_handle" not in codes
    assert any(e.label == "news_event" for e in a.evidence)


def test_spoofed_handle_is_high_flag() -> None:
    a = xsignals.assess(
        "tweet", "elonmusk", _tweet(author_handle="randomdev"), None, TOKEN_T, "PNUT", "mint", []
    )
    assert a.relation == "spoofed"
    assert ("spoofed_tweet_handle", "high") in {(f[0], f[1]) for f in a.flags}


def test_launch_announcement_and_fresh_account() -> None:
    t = _tweet(
        author_handle="pnutcoin",
        followers=12,
        verified_type=None,
        created_at=TOKEN_T + timedelta(minutes=5),
        author_joined=TOKEN_T - timedelta(days=2),
        text="launching $PNUT now on pump.fun",
    )
    a = xsignals.assess("tweet", "pnutcoin", t, None, TOKEN_T, "PNUT", "mint", [])
    assert a.relation == "official_account"
    assert "fresh_x_account" in {f[0] for f in a.flags}
    assert any(e.kind == "x_mentions" for e in a.evidence)


def test_deleted_and_recycled() -> None:
    a = xsignals.assess(
        "tweet", None, xs.TweetData(id=TID, status="deleted"), None, TOKEN_T, None, "m", []
    )
    assert a.status == "deleted" and "tweet_deleted" in {f[0] for f in a.flags}
    p = xs.ProfileData(
        handle="x",
        status="ok",
        followers=10,
        username_changes=3,
        joined=datetime(2020, 1, 1, tzinfo=UTC),
    )
    a = xsignals.assess("profile", None, None, p, TOKEN_T, None, "m", [])
    assert a.relation == "official_account" and "recycled_x_account" in {f[0] for f in a.flags}
    a = xsignals.assess("search", None, None, None, TOKEN_T, None, "m", [])
    assert a.relation == "search_only"
