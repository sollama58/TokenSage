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
    assert a.relation == "official_account"
    # a renamed account is context about the account, not a verdict on the coin (rules 0.23.0)
    assert ("recycled_x_account", "info") in {(f[0], f[1]) for f in a.flags}
    a = xsignals.assess("search", None, None, None, TOKEN_T, None, "m", [])
    assert a.relation == "search_only"


# ----------------------------------------------------------------- quote tweets

QTID = "1790000000000000001"
FX_QUOTE = {
    "code": 200,
    "status": {
        "id": "1850000000000000002",
        "text": "launching $NUT on pump.fun",
        "created_timestamp": 1727787600,  # 2024-10-01 13:00 UTC
        "author": {"id": "9", "screen_name": "nutdev", "name": "nut dev", "followers": 40},
        "quote": {
            "id": QTID,
            "text": "Peanut the squirrel deserved better",
            "created_timestamp": 1727773200,  # 4 h earlier
            "author": {
                "id": "44196397",
                "screen_name": "elonmusk",
                "name": "Elon Musk",
                "followers": 190000000,
                "verification": {"verified": True, "type": "individual"},
            },
        },
    },
}


@respx.mock
async def test_fx_quote_is_parsed_and_round_trips(http: httpx.AsyncClient) -> None:
    respx.get(f"{xs.FX}/status/1850000000000000002").mock(
        return_value=httpx.Response(200, json=FX_QUOTE)
    )
    t = await xs.fetch_tweet(http, "1850000000000000002")
    assert t.quoted_tweet_id == QTID
    q = t.quoted
    assert q is not None and q.status == "ok" and q.id == QTID
    assert q.author_handle == "elonmusk" and q.followers == 190000000
    assert q.text == "Peanut the squirrel deserved better"
    assert q.created_at == datetime(2024, 10, 1, 9, 0, tzinfo=UTC)
    back = xs.TweetData.from_json(t.to_json())
    assert back.quoted is not None and back.quoted.author_handle == "elonmusk"
    assert back.quoted.created_at == q.created_at
    # old cache rows without the key still load
    old = t.to_json()
    old.pop("quoted")
    assert xs.TweetData.from_json(old).quoted is None


@respx.mock
async def test_vx_and_syndication_quotes_are_parsed(http: httpx.AsyncClient) -> None:
    vx = dict(VX_OK, qrt={"tweetID": QTID, "text": "quoted via vx", "user_screen_name": "a"})
    respx.get(f"{xs.FX}/status/{TID}").mock(return_value=httpx.Response(500))
    respx.get(f"{xs.VX}/i/status/{TID}").mock(return_value=httpx.Response(200, json=vx))
    t = await xs.fetch_tweet(http, TID)
    assert t.source == "vxtwitter" and t.quoted is not None
    assert t.quoted.text == "quoted via vx" and t.quoted.author_handle == "a"

    synd = dict(
        SYND_OK,
        quoted_tweet={
            "id_str": QTID,
            "text": "quoted via syndication",
            "created_at": "2024-05-01T00:00:00.000Z",
            "user": {"screen_name": "b", "id_str": "7", "verified_type": "Business"},
        },
    )
    respx.get(f"{xs.VX}/i/status/{TID}").mock(return_value=httpx.Response(500))
    respx.get(url__startswith=xs.SYND).mock(return_value=httpx.Response(200, json=synd))
    t2 = await xs.fetch_tweet(http, TID)
    assert t2.source == "syndication" and t2.quoted_tweet_id == QTID
    assert t2.quoted is not None and t2.quoted.author_handle == "b"
    assert t2.quoted.verified_type == "business"


def test_quoting_an_earlier_big_account_post_borrows_its_narrative() -> None:
    quoted = _tweet(
        id=QTID,
        text="Peanut the squirrel deserved better",
        created_at=TOKEN_T - timedelta(hours=4),
    )
    launch = _tweet(
        author_handle="nutdev",
        followers=40,
        verified_type=None,
        author_joined=TOKEN_T - timedelta(days=200),
        created_at=TOKEN_T + timedelta(minutes=2),
        text="launching $NUT",
        quoted_tweet_id=QTID,
        quoted=quoted,
    )
    a = xsignals.assess("tweet", "nutdev", launch, None, TOKEN_T, "NUT", "mint", ["nut"])
    assert a.relation == "official_account"  # the linked tweet itself is the launch post
    assert a.quoted is not None and a.quoted.author_handle == "elonmusk"
    assert a.quoted.text == "Peanut the squirrel deserved better"
    kinds = {e.kind for e in a.evidence}
    assert {"x_quote_timing", "x_quote_author"} <= kinds
    assert "borrowed_narrative" in {f[0] for f in a.flags}
    # the author's size is a hint, too weak to make a celebrity coin alone (rules 0.23.0)
    size = [e for e in a.evidence if e.kind == "x_quote_author"]
    assert size and all(e.weight < 0.2 for e in size)


def test_quote_of_own_post_or_later_post_adds_no_narrative() -> None:
    own = _tweet(
        id=QTID, author_handle="nutdev", followers=40, created_at=TOKEN_T - timedelta(days=1)
    )
    later = _tweet(id=QTID, created_at=TOKEN_T + timedelta(hours=1))
    for q in (own, later):
        launch = _tweet(
            author_handle="nutdev",
            followers=40,
            verified_type=None,
            created_at=TOKEN_T + timedelta(minutes=2),
            quoted=q,
        )
        a = xsignals.assess("tweet", "nutdev", launch, None, TOKEN_T, "NUT", "mint", [])
        assert a.quoted is not None and a.quoted.text
        assert not {"x_quote_timing", "x_quote_author"} & {e.kind for e in a.evidence}
        assert "borrowed_narrative" not in {f[0] for f in a.flags}


def test_deleted_quoted_tweet_is_reported_without_signals() -> None:
    launch = _tweet(
        author_handle="nutdev",
        followers=40,
        created_at=TOKEN_T + timedelta(minutes=2),
        quoted=xs.TweetData(id=QTID, status="deleted"),
    )
    a = xsignals.assess("tweet", "nutdev", launch, None, TOKEN_T, "NUT", "mint", [])
    assert a.quoted is not None and a.quoted.status == "deleted" and a.quoted.text is None
    assert not any(e.kind.startswith("x_quote") for e in a.evidence)


# ----------------------------------------------------------------- media for x.match


@respx.mock
async def test_media_urls_are_kept_from_each_source(http: httpx.AsyncClient) -> None:
    fx = {
        "code": 200,
        "status": dict(
            FX_OK["status"],  # type: ignore[arg-type]
            media={
                "photos": [{"url": "https://pbs.twimg.com/media/A.jpg"}],
                "videos": [{"thumbnail_url": "https://pbs.twimg.com/ext_tw_video_thumb/B.jpg"}],
            },
        ),
    }
    respx.get(f"{xs.FX}/status/{TID}").mock(return_value=httpx.Response(200, json=fx))
    t = await xs.fetch_tweet(http, TID)
    assert t.media_urls == [
        "https://pbs.twimg.com/media/A.jpg",
        "https://pbs.twimg.com/ext_tw_video_thumb/B.jpg",
    ]
    assert xs.TweetData.from_json(t.to_json()).media_urls == t.media_urls
    old = t.to_json()
    old.pop("media_urls")
    assert xs.TweetData.from_json(old).media_urls == []  # rows cached before this field

    vx = dict(
        VX_OK,
        media_extended=[
            {"type": "image", "url": "https://pbs.twimg.com/media/C.jpg"},
            {
                "type": "video",
                "url": "https://video.twimg.com/x.mp4",
                "thumbnail_url": "https://pbs.twimg.com/D.jpg",
            },
            {"type": "image", "url": "http://insecure/x.jpg"},
        ],
    )
    respx.get(f"{xs.FX}/status/{TID}").mock(return_value=httpx.Response(500))
    respx.get(f"{xs.VX}/i/status/{TID}").mock(return_value=httpx.Response(200, json=vx))
    t = await xs.fetch_tweet(http, TID)
    assert t.media_urls == ["https://pbs.twimg.com/media/C.jpg", "https://pbs.twimg.com/D.jpg"]

    synd = dict(
        SYND_OK,
        mediaDetails=[{"media_url_https": f"https://pbs.twimg.com/{i}.jpg"} for i in range(6)],
    )
    respx.get(f"{xs.VX}/i/status/{TID}").mock(return_value=httpx.Response(500))
    respx.get(url__startswith=xs.SYND).mock(return_value=httpx.Response(200, json=synd))
    t = await xs.fetch_tweet(http, TID)
    assert len(t.media_urls) == xs.MAX_MEDIA == 4


def test_media_selection_for_tweets_and_profiles() -> None:
    from tokensage import fulldepth

    q = _tweet(id="2", media_urls=["https://pbs.twimg.com/media/Q.jpg"])
    t = _tweet(media_urls=["https://pbs.twimg.com/media/T.jpg"], quoted=q)
    assert fulldepth.media_urls(t, None) == [
        "https://pbs.twimg.com/media/T.jpg",
        "https://pbs.twimg.com/media/Q.jpg",
    ]
    p = xs.ProfileData(
        handle="a",
        status="ok",
        avatar_url="https://pbs.twimg.com/profile_images/1/a_normal.jpg",
        banner_url="https://pbs.twimg.com/profile_banners/1/2",
    )
    assert fulldepth.media_urls(None, p) == [p.avatar_url, p.banner_url]
    assert fulldepth.media_urls(xs.TweetData(id="1", status="deleted"), None) == []
    assert fulldepth._small("https://pbs.twimg.com/media/T.jpg").endswith("?name=small")
    assert "_400x400." in fulldepth._small(p.avatar_url)  # type: ignore[arg-type]
