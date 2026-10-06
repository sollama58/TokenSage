"""Replies and the accounts involved: the post a linked tweet answers, and the names of
everyone in the conversation, feed the analysis like a quoted post does."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
import pytest
import respx

from tests.conftest import needs_db
from tests.fixtures.chain import CID_META, T22_MINT, FakeChain, install_web, metadata_json
from tests.test_x_sources import FX_OK, SYND_OK, TID, VX_OK
from tokensage import fulldepth
from tokensage.api.schemas import TokenResponse
from tokensage.engine import xsignals
from tokensage.engine.pipeline import EngineInput, account_text, run_full
from tokensage.sources import x as xs

REPLY_ID = "1850000000000000009"
TOKEN_T = datetime(2026, 10, 1, 13, 0, tzinfo=UTC)
META_URI = f"https://ipfs.io/ipfs/{CID_META}"


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(headers={"User-Agent": "test"}) as c:
        yield c


def _tweet(**over: object) -> xs.TweetData:
    base: dict[str, object] = dict(
        id=REPLY_ID,
        status="ok",
        source="fxtwitter",
        text="this one's for him",
        author_handle="nutdev",
        author_name="nut dev",
        followers=40,
        created_at=TOKEN_T + timedelta(minutes=2),
        author_joined=TOKEN_T - timedelta(days=200),
    )
    base.update(over)
    return xs.TweetData(**base)  # type: ignore[arg-type]


def _elon_post() -> xs.TweetData:
    return _tweet(
        id=TID,
        text="Peanut the squirrel did nothing wrong",
        author_handle="elonmusk",
        author_name="Elon Musk",
        followers=190_000_000,
        verified_type="blue",
        created_at=TOKEN_T - timedelta(hours=5),
        author_joined=datetime(2009, 6, 2, tzinfo=UTC),
    )


# ----------------------------------------------------------------- sources


@respx.mock
async def test_fx_reply_fields_v2_and_v1(http: httpx.AsyncClient) -> None:
    reply_to = {"screen_name": "elonmusk", "post": TID}
    v2 = {"code": 200, "status": {**FX_OK["status"], "id": REPLY_ID, "replying_to": reply_to}}  # type: ignore[dict-item]
    respx.get(f"{xs.FX}/status/{REPLY_ID}").mock(return_value=httpx.Response(200, json=v2))
    t = await xs.fx_tweet(http, REPLY_ID)
    assert t is not None and t.replying_to_id == TID and t.replying_to_handle == "elonmusk"

    v1 = {"code": 200, "status": {**FX_OK["status"], "replying_to": "elonmusk",  # type: ignore[dict-item]
                                  "replying_to_status": TID}}  # fmt: skip
    respx.get(f"{xs.FX}/status/{TID}").mock(return_value=httpx.Response(200, json=v1))
    t1 = await xs.fx_tweet(http, TID)
    assert t1 is not None and t1.replying_to_id == TID and t1.replying_to_handle == "elonmusk"


@respx.mock
async def test_vx_syndication_and_paid_reply_fields(http: httpx.AsyncClient) -> None:
    respx.get(f"{xs.VX}/i/status/{REPLY_ID}").mock(
        return_value=httpx.Response(200, json=dict(VX_OK, replyingTo="elonmusk", replyingToID=TID))
    )
    t = await xs.vx_tweet(http, REPLY_ID)
    assert t is not None and t.replying_to_id == TID and t.replying_to_handle == "elonmusk"

    elon = {"screen_name": "elonmusk", "name": "Elon Musk", "id_str": "44196397"}
    parent = dict(SYND_OK, text="Peanut did nothing wrong", user=elon)
    synd = dict(SYND_OK, id_str=REPLY_ID, in_reply_to_status_id_str=TID,
                in_reply_to_screen_name="elonmusk", parent=parent)  # fmt: skip
    respx.get(url__startswith=xs.SYND).mock(return_value=httpx.Response(200, json=synd))
    t2 = await xs.syndication_tweet(http, REPLY_ID)
    assert t2 is not None and t2.replying_to_id == TID
    assert t2.replied_to is not None and t2.replied_to.text == "Peanut did nothing wrong"
    assert t2.replied_to.author_name == "Elon Musk"
    back = xs.TweetData.from_json(t2.to_json())
    assert back.replied_to is not None and back.replied_to.author_handle == "elonmusk"

    paid = {"status": "success", "tweets": [{"id": REPLY_ID, "text": "x", "inReplyToId": TID,
            "inReplyToUsername": "elonmusk", "author": {"userName": "nutdev"}}]}  # fmt: skip
    respx.get(f"{xs.PAID}/tweets").mock(return_value=httpx.Response(200, json=paid))
    t3 = await xs.paid_tweet(http, REPLY_ID, "key")
    assert t3 is not None and t3.replying_to_id == TID and t3.replying_to_handle == "elonmusk"


def test_garbage_reply_fields_are_dropped() -> None:
    assert xs._id("12ab") is None and xs._id(True) is None and xs._id(123) == "123"
    assert xs._handle("@elonmusk") == "elonmusk" and xs._handle("not a handle!") is None


# ----------------------------------------------------------------- signals


def test_reply_to_an_earlier_big_account_post_borrows_its_narrative() -> None:
    reply = _tweet(replying_to_id=TID, replying_to_handle="elonmusk", replied_to=_elon_post())
    a = xsignals.assess("tweet", "nutdev", reply, None, TOKEN_T, "DNW", "mint", ["dnw"])
    assert a.replied_to is not None and a.replied_to.text == "Peanut the squirrel did nothing wrong"
    assert {"x_reply_timing", "x_reply_author"} <= {e.kind for e in a.evidence}
    flag = next(f for f in a.flags if f[0] == "borrowed_narrative")
    assert "replies to @elonmusk" in flag[2]
    roles = {(acc.role, acc.handle) for acc in a.accounts}
    assert {("author", "nutdev"), ("replied_to_author", "elonmusk")} <= roles


def test_unfetched_reply_target_still_names_the_account() -> None:
    reply = _tweet(replying_to_id=TID, replying_to_handle="elonmusk")
    a = xsignals.assess("tweet", "nutdev", reply, None, TOKEN_T, "DNW", "mint", ["dnw"])
    assert a.replied_to is not None and a.replied_to.status == "failed"
    assert a.replied_to.author_handle == "elonmusk"
    assert ("replied_to_author", "elonmusk") in {(x.role, x.handle) for x in a.accounts}


def test_mentions_are_collected_once_and_capped() -> None:
    text = "@elonmusk @elonmusk @a1 @a2 @a3 @a4 @a5 @a6 hi, mail me at x@y.com"
    a = xsignals.assess("tweet", "nutdev", _tweet(text=text), None, TOKEN_T, "D", "m", ["d"])
    mentioned = [x.handle for x in a.accounts if x.role == "mentioned"]
    assert mentioned == ["elonmusk", "a1", "a2", "a3", "a4"]


def test_account_text_segments_handles() -> None:
    assert account_text("Elon Musk", "elonmusk").split()[:2] == ["elon", "musk"]
    assert account_text(None, None) == ""


# ----------------------------------------------------------------- engine


def _engine(tweet: xs.TweetData, name: str = "Did Nothing Wrong", symbol: str = "DNW"):  # type: ignore[no-untyped-def]
    return run_full(
        EngineInput(
            mint=T22_MINT,
            name=name,
            symbol=symbol,
            description=None,
            image_bytes=None,
            created_at=TOKEN_T,
            x_kind="tweet",
            x_url_handle=tweet.author_handle,
            tweet=tweet,
        )
    )


def test_replied_to_post_and_account_names_feed_the_meaning() -> None:
    plain = _engine(_tweet())
    reply = _engine(_tweet(replying_to_id=TID, replying_to_handle="elonmusk",
                           replied_to=_elon_post()))  # fmt: skip
    assert plain.agg.referent is None or "Peanut" not in plain.agg.referent.label
    assert reply.agg.referent is not None and "Peanut" in reply.agg.referent.label
    details = [e.detail for e in reply.evidence]
    assert any(d.startswith("the replied-to account @elonmusk:") for d in details)
    assert "celebrity/elon" in dict(reply.agg.categories)
    # x.match compares the coin with the replied-to post too
    assert reply.x_match is not None and reply.x_match.name.score > 0


def test_account_names_alone_give_only_a_weak_referent() -> None:
    out = _engine(_tweet(text="gm", replying_to_id=TID, replying_to_handle="elonmusk"))
    elon = [e for e in out.evidence if e.label == "referent" and "Elon" in e.detail]
    assert elon and all(e.weight <= 0.6 for e in elon)


# ----------------------------------------------------------------- end to end


@needs_db
async def test_full_depth_fetches_the_replied_to_post(
    client: httpx.AsyncClient, migrated_db: str
) -> None:
    from tests.fixtures.chain import public_resolver
    from tokensage.net import safe_fetch

    conn = await asyncpg.connect(migrated_db)
    await conn.execute("truncate x_tweet, x_profile, x_media, x_ref, token_metadata cascade")
    await conn.close()
    fulldepth._trend_cache = None
    reply = {"code": 200, "status": {
        "id": REPLY_ID, "text": "this one's for him", "created_timestamp": 1715900000,
        "author": {"id": "9", "screen_name": "nutdev", "name": "nut dev", "followers": 40},
        "replying_to": {"screen_name": "elonmusk", "post": TID}}}  # fmt: skip
    elon_post = {
        "code": 200,
        "status": {**FX_OK["status"], "text": "Peanut the squirrel did nothing wrong"},  # type: ignore[dict-item]
    }
    chain = FakeChain()
    chain.add_t22_pump(T22_MINT, "Did Nothing Wrong", "DNW", META_URI)
    meta = metadata_json(
        name="Did Nothing Wrong",
        symbol="DNW",
        description="",
        twitter=f"https://x.com/nutdev/status/{REPLY_ID}",
    )
    old = safe_fetch.DEFAULT_RESOLVER
    safe_fetch.DEFAULT_RESOLVER = public_resolver
    try:
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{xs.FX}/status/{REPLY_ID}").mock(
                return_value=httpx.Response(200, json=reply)
            )
            parent = router.get(f"{xs.FX}/status/{TID}").mock(
                return_value=httpx.Response(200, json=elon_post)
            )
            install_web(router, chain, meta=meta)
            r = await client.get(f"/v1/tokens/{T22_MINT}?depth=full&wait=10")
    finally:
        safe_fetch.DEFAULT_RESOLVER = old
    assert r.status_code == 200, r.text
    a = TokenResponse.model_validate(r.json()).analysis
    assert a is not None and a.x is not None and parent.called
    rt = a.x.replied_to
    assert rt is not None and rt.status == "ok" and rt.id == TID
    assert rt.author is not None and rt.author.handle == "elonmusk"
    assert rt.text and "Peanut" in rt.text
    roles = {(acc.role, acc.handle) for acc in a.x.accounts}
    assert {("author", "nutdev"), ("replied_to_author", "elonmusk")} <= roles
    assert a.referent is not None and "Peanut" in a.referent.label


# ----------------------------------------------------------------- staleness and fallbacks


@respx.mock
async def test_mirror_without_reply_info_is_supplemented_by_syndication(
    http: httpx.AsyncClient,
) -> None:
    # FxTwitter answers but reports no reply (format drift, or it drops the field)
    plain = {"code": 200, "status": {**FX_OK["status"], "id": REPLY_ID}}  # type: ignore[dict-item]
    respx.get(f"{xs.FX}/status/{REPLY_ID}").mock(return_value=httpx.Response(200, json=plain))
    elon = {"screen_name": "elonmusk", "name": "Elon Musk", "id_str": "44196397"}
    parent = dict(SYND_OK, id_str=TID, text="Peanut the squirrel did nothing wrong", user=elon)
    synd = dict(SYND_OK, id_str=REPLY_ID, in_reply_to_status_id_str=TID,
                in_reply_to_screen_name="elonmusk", parent=parent)  # fmt: skip
    respx.get(url__startswith=xs.SYND).mock(return_value=httpx.Response(200, json=synd))
    t = await xs.fetch_tweet(http, REPLY_ID)
    assert t.source == "fxtwitter"  # the mirror's copy stays the record
    assert t.replying_to_id == TID and t.replied_to is not None
    assert t.replied_to.author_handle == "elonmusk"


@needs_db
async def test_cached_tweet_from_before_reply_support_is_rechecked(
    migrated_db: str, clean_tables: None, http: httpx.AsyncClient
) -> None:
    from tokensage.config import Settings

    conn = await asyncpg.connect(migrated_db)
    try:
        from tokensage.db import _init_connection

        await _init_connection(conn)
        await conn.execute("truncate x_tweet")
        old = _tweet().to_json()
        for k in ("replying_to_id", "replying_to_handle"):
            old.pop(k)
        await conn.execute(
            """insert into x_tweet (tweet_id, first_snapshot, latest, status, source, fetched_at)
               values ($1, $2, $2, 'ok', 'fxtwitter', now())""",
            REPLY_ID,
            old,
        )
        reply = {"code": 200, "status": {**FX_OK["status"], "id": REPLY_ID,  # type: ignore[dict-item]
                 "replying_to": {"screen_name": "elonmusk", "post": TID}}}  # fmt: skip
        with respx.mock(assert_all_called=False) as router:
            route = router.get(f"{xs.FX}/status/{REPLY_ID}").mock(
                return_value=httpx.Response(200, json=reply)
            )
            settings = Settings(database_url=migrated_db, _env_file=None)  # type: ignore[call-arg]
            t = await fulldepth.tweet_cached(conn, http, settings, REPLY_ID)
        assert route.called  # fresh (minutes old) but pre-reply: re-checked anyway
        assert t.replying_to_id == TID and t.replying_to_handle == "elonmusk"
    finally:
        await conn.close()


def test_account_names_only_count_when_the_account_is_the_entity() -> None:
    """ "American Eagle" is a bird in WordNet and "american" an alias of America; neither says
    anything about a coin launched on the brand's tweet. @elonmusk, by contrast, does."""
    from tokensage.engine.pipeline import DbContext

    def run(name: str, symbol: str, post: xs.TweetData) -> object:
        return run_full(
            EngineInput(
                mint=T22_MINT,
                name=name,
                symbol=symbol,
                description=None,
                image_bytes=None,
                created_at=TOKEN_T,
                x_kind="tweet",
                x_url_handle=post.author_handle,
                tweet=post,
                x_object_time=post.created_at,
                ctx=DbContext(),
            )
        )

    brand = _tweet(
        text="great jeans",
        author_handle="AmericanEagle",
        author_name="American Eagle",
        followers=500_000,
        created_at=TOKEN_T - timedelta(hours=3),
    )
    out = run("great jeans", "JEANS", brand)
    account_rows = [e for e in out.evidence if "the posting account" in e.detail]  # type: ignore[attr-defined]
    assert account_rows == []
    assert "animal/bird" not in dict(out.agg.categories)  # type: ignore[attr-defined]
    assert not (out.agg.referent and out.agg.referent.label == "America")  # type: ignore[attr-defined]

    out2 = run("did nothing wrong", "WRONG", _elon_post())
    account_rows = [e for e in out2.evidence if "the posting account @elonmusk" in e.detail]  # type: ignore[attr-defined]
    assert account_rows and all(e.kind == "entity" for e in account_rows)
    assert dict(out2.agg.categories).get("celebrity/elon", 0) > 0  # type: ignore[attr-defined]
