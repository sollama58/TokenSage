"""Regression tests for the 2026-10-08 audit of PRs #40 to #48 (rules 0.24.0)."""

from __future__ import annotations

import asyncio
import base64
import copy
import struct
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import asyncpg
import httpx
import respx

from tokensage import fulldepth
from tokensage.api.schemas import Analysis, Versions, stored_analysis
from tokensage.engine import trends
from tokensage.engine.knowledge import load_knowledge
from tokensage.engine.pipeline import EngineInput, run_full
from tokensage.resolve import fees, resolver
from tokensage.resolve.pump_ca import b58decode
from tokensage.sources import gnews
from tokensage.sources.x import TweetData

T = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _tweet(
    tid: str,
    handle: str,
    text: str,
    hours: float,
    followers: int,
    verified: str | None,
    quoted: TweetData | None = None,
) -> TweetData:
    return TweetData(
        id=tid,
        status="ok",
        source="fxtwitter",
        text=text,
        created_at=T - timedelta(hours=hours),
        author_handle=handle,
        author_name=handle,
        followers=followers,
        verified_type=verified,
        author_joined=T - timedelta(days=4000),
        quoted=quoted,
        quoted_tweet_id=quoted.id if quoted else None,
    )


def _labels(name: str, symbol: str, tweet: TweetData, handle: str) -> list[str]:
    out = run_full(
        EngineInput(
            mint="M" * 44,
            name=name,
            symbol=symbol,
            description=None,
            image_bytes=None,
            created_at=T,
            x_kind="tweet",
            x_url_handle=handle,
            tweet=tweet,
        )
    )
    return [lbl for lbl, _ in out.agg.categories]


# ----------------------------------------------------------------- X account size (#46)


def test_large_account_size_alone_does_not_make_an_everyday_name_a_celebrity_coin() -> None:
    for followers, verified in ((2_000_000, "business"), (2_000, None)):
        t = _tweet("1", "SportsCenter", "Unreal finish tonight.", 5, followers, verified)
        labels = _labels("Speed", "SPEED", t, "SportsCenter")
        assert not any(lbl.startswith("celebrity") for lbl in labels), (followers, labels)


def test_size_hints_of_quoting_outlets_do_not_stack_into_a_category() -> None:
    q = _tweet(
        "2", "Reuters", "Storm makes landfall on the coast tonight", 30, 2_000_000, "business"
    )
    t = _tweet("1", "nytimes", "Live updates on the storm", 20, 2_000_000, "business", quoted=q)
    labels = _labels("Landfall", "LAND", t, "nytimes")
    assert "celebrity" not in labels and "news_event" in labels


def test_the_account_name_still_makes_a_celebrity_coin() -> None:
    t = _tweet("1", "elonmusk", "Unreal finish tonight.", 5, 200_000_000, "business")
    assert "celebrity" in _labels("Unreal", "UNRL", t, "elonmusk")


# ----------------------------------------------------------------- news and Bluesky


async def test_stale_cached_news_keeps_only_headlines_inside_the_window(migrated_db: str) -> None:
    from tokensage.db import _init_connection

    db = await asyncpg.connect(migrated_db)
    await _init_connection(db)
    await db.execute("delete from lookup_cache where key = 'gnews:q:moo deng'")
    now = datetime.now(UTC)
    await db.execute(
        """insert into lookup_cache (key, value, fetched_at)
           values ('gnews:q:moo deng', $1, now() - interval '30 days')""",
        [
            {
                "title": "Moo Deng old story",
                "source": "CNN",
                "published": format_datetime(now - timedelta(days=30)),
            },
            {
                "title": "Moo Deng new story",
                "source": "AP",
                "published": format_datetime(now - timedelta(hours=3)),
            },
            {"title": "Moo Deng undated", "source": "BBC", "published": None},
        ],
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith="https://news.google.com/").mock(
            return_value=httpx.Response(503)
        )
        async with httpx.AsyncClient() as http:
            found = await fulldepth.news_lookup(db, http, "Moo Deng", exact=True)
    await db.close()
    assert found is not None and found.stale
    assert [h["source"] for h in found.headlines] == ["AP"]


def test_news_phrase_drops_coin_affixes_and_reads_curly_apostrophes() -> None:
    assert gnews.name_query("Moo Deng Sol") == "Moo Deng"
    assert gnews.name_query("Official Moo Deng") == "Moo Deng"
    assert gnews.name_query("Moo Deng Coin") == "Moo Deng"
    assert gnews.name_query("Trump’s Cat") == "Trump's Cat"
    heads = [
        {"title": "Trump’s cat goes viral", "source": "CNN"},
        {"title": "Trump's cat again", "source": "Fox"},
    ]
    assert len(gnews.relevant(heads, "Trump's Cat")) == 2


def test_a_ticker_written_as_an_ordinary_word_keeps_the_headline() -> None:
    heads = [
        {"title": "Moo Deng the baby hippo goes viral", "source": "BBC"},
        {"title": "Moo Deng jumps as $HIPPO lists", "source": "X"},
        {"title": "Moo Deng: HIPPO lists cross-chain", "source": "Y"},
    ]
    assert [h["source"] for h in gnews.relevant(heads, "Moo Deng", "HIPPO")] == ["BBC"]


def test_one_bluesky_account_counts_at_most_twice() -> None:
    now = datetime.now(UTC)
    posts = [
        {"text": "Moo Deng!", "created_at": (now - timedelta(hours=1)).isoformat(), "author": a}
        for a in ["spam"] * 8 + ["a", "b"]
    ]
    hit = trends.bluesky_hit("Moo Deng", posts, now)
    assert hit is not None and hit.term.views == 4


# ----------------------------------------------------------------- trend surfaces


def test_trending_labels_match_when_written_as_one_word() -> None:
    k = load_knowledge()
    idx = trends.TrendIndex(
        [
            trends.TrendTerm("Chat GPT", 0.0, 4, source="x_trends", rank=3),
            trends.TrendTerm("TikTok", 5.0, 100_000, source="wikipedia"),
        ],
        k,
    )
    assert [h.term.term for h in idx.match("ChatGPT is down", "description")] == ["Chat GPT"]
    assert [h.term.term for h in idx.match("#TikTok ban is back", "x")] == ["TikTok"]


# ----------------------------------------------------------------- creator fees

_MINT = "CwfsRHzXg2kcfA7EaqVkAvb8RDpGfrNHtF6QcjPC73ZQ"
_ADMIN = "8PQxd6VmfGPMyg8WPnfkT9jUTmtE7UsnDmvBKXeAVP9z"
_CHARITY = "CYoJ4CC1oGt1Hk1y2XZQ9aDJHsh7kZSFSHMWvChAgyxC"


def _sharing(holders: list[tuple[str, int]]) -> dict:
    b = (
        fees.SHARING_CONFIG_DISC
        + bytes([1, 2, 1])
        + b58decode(_MINT)
        + b58decode(_ADMIN)
        + bytes([1])
        + struct.pack("<I", len(holders))
    )
    for a, bps in holders:
        b += b58decode(a) + struct.pack("<H", bps)
    return {"owner": fees.PUMP_FEES_PROGRAM, "data": [base64.b64encode(b).decode(), "base64"]}


def _social(uid: str, platform: int = 2) -> dict:
    u = uid.encode()
    b = (
        fees.SOCIAL_FEE_PDA_DISC
        + b"\x01\x01"
        + struct.pack("<I", len(u))
        + u
        + bytes([platform])
        + struct.pack("<QQQ", 10**9, 0, 0)
    )
    return {"owner": fees.PUMP_FEES_PROGRAM, "data": [base64.b64encode(b).decode(), "base64"]}


def _donation() -> dict:
    z = b58decode(_MINT)
    b = (
        fees.DONATION_FEE_PDA_DISC
        + b"\x01\x01"
        + z
        + z
        + b"\0" * 32
        + z
        + struct.pack("<Qq", 5 * 10**9, 0)
    )
    return {"owner": fees.PUMP_FEES_PROGRAM, "data": [base64.b64encode(b).decode(), "base64"]}


class _Rpc:
    def __init__(self, accs: dict[str, dict]):
        self.accs = accs

    async def get_multiple_accounts(self, addrs: list[str], **kw: object) -> list:
        return [self.accs.get(a) for a in addrs]


async def _fee(holders: list[tuple[str, int]], accs: dict[str, dict]) -> fees.CreatorFee:
    curve = {"creator": fees.sharing_config_pda(_MINT), "creator_fee_bps": 0, "quote_mint": None}
    cf = await fees.resolve_creator_fee(_MINT, curve, _sharing(holders), rpc=_Rpc(accs), conn=None)
    assert cf is not None
    return cf


async def test_fee_destination_does_not_depend_on_shareholder_order() -> None:
    gh = fees.social_fee_pda("258455447", 2)
    accs = {gh: _social("258455447"), _CHARITY: _donation()}
    for a, b, want in ((_ADMIN, gh, "github"), (_CHARITY, gh, "split")):
        assert (await _fee([(a, 5000), (b, 5000)], accs)).destination == want
        assert (await _fee([(b, 5000), (a, 5000)], accs)).destination == want


async def test_x_and_pump_linked_shares_count_together_as_social() -> None:
    x = fees.social_fee_pda("44196397", 1)
    pump = fees.social_fee_pda("abc", 0)
    wallet = "7iRo63vR8jM4f3hZVFNy7f8j3oFNvRCBLC8gBMpfuWTN"
    accs = {x: _social("44196397", 1), pump: _social("abc", 0)}
    cf = await _fee([(x, 3000), (pump, 3000), (wallet, 4000)], accs)
    assert cf.destination == "social"


async def test_a_github_user_id_that_is_not_a_number_is_never_echoed() -> None:
    gh = fees.social_fee_pda("torvalds", 2)
    cf = await _fee([(gh, 10_000)], {gh: _social("torvalds")})
    assert cf.recipients[0].user_id is None and "torvalds" not in cf.describe()


def test_off_curve_system_account_is_a_wallet_that_is_not_cached() -> None:
    gh = fees.social_fee_pda("258455447", 2)
    info = fees.classify_recipient_account(gh, {"owner": fees.SYSTEM_PROGRAM, "lamports": 10**6})
    assert info["kind"] == "wallet" and info.get("off_curve")
    assert not fees.classify_recipient_account(_ADMIN, {"owner": fees.SYSTEM_PROGRAM}).get(
        "off_curve"
    )


def test_donations_use_the_quote_tokens_decimals() -> None:
    usdt = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
    assert fees._lamports_to_quote(5_000_000, usdt) == 5.0
    assert fees._lamports_to_quote(5 * 10**9, None) == 5.0
    assert (
        fees._lamports_to_quote(5_000_000, "Unknown1111111111111111111111111111111111111") is None
    )


# ----------------------------------------------------------------- serving stored documents


def test_a_new_enum_value_from_a_newer_worker_is_served_as_its_catch_all() -> None:
    doc = Analysis(
        mint="M",
        summary="s",
        depth="basic",
        analyzed_at=datetime.now(UTC),
        versions=Versions(rules="x", lexicon="y"),
    ).model_dump(mode="json")
    doc["market"]["creator_fee"] = {
        "destination": "burn",
        "mechanism": "direct",
        "split": False,
        "shares": {},
        "recipients": [],
        "summary": "s",
    }
    doc["referent"] = {"label": "x", "kind": "vehicle", "source": "s", "confidence": 0.5}
    served = stored_analysis(copy.deepcopy(doc))
    assert served.market.creator_fee is not None
    assert served.market.creator_fee.destination == "unknown"
    assert served.referent is not None and served.referent.kind == "other"


# ----------------------------------------------------------------- curve reads


async def test_the_curve_time_bound_covers_the_global_account_read() -> None:
    from tokensage.resolve.pump_ca import BONDING_CURVE_DISC, PUMP_PROGRAM

    data = BONDING_CURVE_DISC + b"\0" * 256

    class SlowRpc:
        async def get_multiple_accounts(self, addrs: list[str], **kw: object) -> list:
            return [
                {"owner": PUMP_PROGRAM, "data": [base64.b64encode(data).decode(), "base64"]}
            ] * len(addrs)

        async def get_account_info(self, *a: object, **kw: object) -> None:
            await asyncio.sleep(5)
            return None

    class Conn:
        async def fetch(self, *a: object) -> list:
            return []

        async def execute(self, *a: object) -> None:
            return None

    resolver._initial_real_cache = None  # type: ignore[attr-defined]
    started = asyncio.get_running_loop().time()
    await resolver.curves_now(Conn(), SlowRpc(), ["A" * 44, "B" * 44], rpc_timeout_s=0.3)  # type: ignore[arg-type]
    assert asyncio.get_running_loop().time() - started < 2
