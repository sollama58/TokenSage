"""X credibility apart from fit (brief 2026-10-07 §5): account facts, credibility, the
self-made profile cap, match basis and link reuse rank."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from tests.conftest import needs_db
from tokensage.analyzer import _x_info, _x_reuse
from tokensage.engine import xcred, xmatch
from tokensage.engine.xsignals import XAssessment
from tokensage.sources.x import ProfileData

T = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _xa(**kw: object) -> XAssessment:
    a = XAssessment(status="ok", relation="official_account")
    for k, v in kw.items():
        setattr(a, k, v)
    return a


@pytest.mark.parametrize(
    ("handle", "name", "expect"),
    [
        ("glimmerdog", "Glimmerdog", True),
        ("GlimmerdogSol", "Some Name", True),
        ("officialglimmerdogcoin", None, True),
        ("someone", "Glimmer Dog", True),  # display name, spaces folded
        ("GLIMD", None, True),  # the ticker
        ("dogfan", "Dog Fan", False),
        (None, None, False),
    ],
)
def test_names_the_coin(handle: str | None, name: str | None, expect: bool) -> None:
    assert xcred.names_the_coin(handle, name, "glimmerdog", "GLIMD") is expect


def test_account_made_for_coin_needs_youth_and_the_name() -> None:
    young = _xa(author_handle="glimmerdog", joined=T - timedelta(minutes=7))
    acc = xcred.account_facts(young, None, T, "glimmerdog", "GLIMD")
    assert acc is not None and acc.made_for_coin and acc.age_at_launch_s == 420
    # created after the token: still made for it (negative age)
    after = _xa(author_handle="glimmerdog", joined=T + timedelta(hours=1))
    acc = xcred.account_facts(after, None, T, "glimmerdog", "GLIMD")
    assert acc is not None and acc.made_for_coin and acc.age_at_launch_s == -3600
    old = _xa(author_handle="glimmerdog", joined=T - timedelta(days=3))
    acc = xcred.account_facts(old, None, T, "glimmerdog", "GLIMD")
    assert acc is not None and not acc.made_for_coin
    unrelated = _xa(author_handle="dogfan", joined=T - timedelta(minutes=7))
    acc = xcred.account_facts(unrelated, None, T, "glimmerdog", "GLIMD")
    assert acc is not None and not acc.made_for_coin
    # unknown join date: age unknown, never "made for the coin"
    acc = xcred.account_facts(_xa(author_handle="glimmerdog"), None, T, "glimmerdog", "GLIMD")
    assert acc is not None and acc.age_at_launch_s is None and not acc.made_for_coin


def test_account_posts_come_from_the_matching_profile_only() -> None:
    p = ProfileData(handle="Glimmerdog", status="ok", statuses=12, joined=T - timedelta(days=2))
    acc = xcred.account_facts(_xa(author_handle="glimmerdog"), p, T, "glimmerdog", "GLIMD")
    assert acc is not None and acc.posts_total == 12 and acc.created_at == p.joined
    other = ProfileData(handle="someoneelse", status="ok", statuses=99)
    acc = xcred.account_facts(_xa(author_handle="glimmerdog"), other, T, "glimmerdog", "GLIMD")
    assert acc is not None and acc.posts_total is None


def test_no_account_when_nothing_was_fetched() -> None:
    assert xcred.account_facts(None, None, T, "x", None) is None
    assert xcred.account_facts(XAssessment(status="failed"), None, T, "x", None) is None
    assert xcred.credibility(None, None, None) is None


def _acc(**kw: object) -> xcred.AccountFacts:
    base: dict[str, object] = dict(
        handle="a", created_at=None, age_at_launch_s=None, posts_total=None,
        posts_about_coin=None, name_changes=None, verified_type=None, followers=None,
        made_for_coin=False,
    )  # fmt: skip
    base.update(kw)
    return xcred.AccountFacts(**base)  # type: ignore[arg-type]


def test_credibility_orders_accounts_sensibly() -> None:
    self_made = _acc(age_at_launch_s=420, followers=12, posts_total=3, made_for_coin=True)
    small_old = _acc(age_at_launch_s=200 * 86400, followers=800, posts_total=400)
    big_old = _acc(
        age_at_launch_s=5 * 365 * 86400, followers=250_000, posts_total=9000,
        verified_type="business",
    )  # fmt: skip
    c = [xcred.credibility(a, "official_account", None) for a in (self_made, small_old, big_old)]
    assert c[0] is not None and c[1] is not None and c[2] is not None
    assert c[0] < 0.1 < c[1] < c[2]
    assert c[2] >= 0.9
    # renames, spoofing and late reuse all cost credibility
    assert xcred.credibility(_acc(**{**big_old.__dict__, "name_changes": 2}), None, None) < c[2]  # type: ignore[operator]
    assert xcred.credibility(big_old, "spoofed", None) < c[2]  # type: ignore[operator]
    first = xcred.credibility(big_old, None, 1)
    seventh = xcred.credibility(big_old, None, 7)
    assert first == c[2] and seventh is not None and seventh < first  # type: ignore[operator]


def test_account_signals_cost_little() -> None:
    # rules 0.23.0: the post and the account's name lead; renames and reuse are context
    old = _acc(age_at_launch_s=2 * 365 * 86400, followers=5_000, posts_total=1_000)
    base = xcred.credibility(old, None, None)
    assert base is not None
    renamed = xcred.credibility(_acc(**{**old.__dict__, "name_changes": 3}), None, None)
    late = xcred.credibility(old, None, 50)
    assert renamed is not None and late is not None
    assert renamed >= 0.8 * base and late >= 0.7 * base


def test_self_profile_squash_is_continuous_and_below_about() -> None:
    lo, mid, hi = (xmatch.squash_self_profile(f) for f in (0.3, 0.8, 1.0))
    assert xmatch.FIT_RELATED <= lo < mid < hi <= xmatch.PROFILE_SELF_CAP < xmatch.FIT_ABOUT
    assert xmatch.squash_self_profile(xmatch.FIT_RELATED) == xmatch.FIT_RELATED


def test_image_score_is_continuous_and_keeps_its_bands() -> None:
    scores = [xmatch.image_score(d, 8, 14) for d in range(0, 24)]
    assert scores[0] == 1.0 and scores[8] == 0.9 and scores[14] == 0.7
    assert all(a >= b for a, b in zip(scores, scores[1:], strict=False))
    assert scores[20] > 0 and scores[21] == 0.0
    assert len(set(scores[:21])) == 21  # no steps inside the bands


# ----------------------------------------------------------------- reuse rank (DB)


@needs_db
async def test_reuse_rank_counts_earlier_coins_linking_the_same_post(
    migrated_db: str, clean_tables: None
) -> None:
    conn = await asyncpg.connect(migrated_db)
    try:
        await conn.execute("truncate x_ref cascade")
        tid = "1843000000000000000"
        for i, hours in enumerate((-5, -2, 3)):  # two earlier coins, one later
            mint = f"Mint{i}" + "1" * 30
            await conn.execute(
                "insert into token (mint, name, created_at) values ($1, 'x', $2)",
                mint,
                T + timedelta(hours=hours),
            )
            await conn.execute(
                "insert into x_ref (mint, kind, tweet_id) values ($1, 'tweet', $2)", mint, tid
            )
        x = _x_info(f"https://x.com/someone/status/{tid}", T)
        assert x is not None
        n, rank, first = await _x_reuse(conn, x, "ThisMint" + "1" * 30, T)
        assert (n, rank, first) == (3, 3, T - timedelta(hours=5))
        # the first coin to link it
        n, rank, first = await _x_reuse(conn, x, "ThisMint" + "1" * 30, T - timedelta(hours=9))
        assert (n, rank, first) == (3, 1, T - timedelta(hours=9))
        # unknown launch time: count only
        assert await _x_reuse(conn, x, "ThisMint" + "1" * 30, None) == (3, None, None)
        # a profile link counts by handle, case-insensitively
        await conn.execute(
            "insert into token (mint, name, created_at) values ($1, 'p', $2)",
            "Prof" + "1" * 30,
            T - timedelta(days=1),
        )
        await conn.execute(
            "insert into x_ref (mint, kind, handle) values ($1, 'profile', 'GlimmerDog')",
            "Prof" + "1" * 30,
        )
        px = _x_info("https://x.com/glimmerdog", T)
        assert px is not None and px.ref.kind == "profile"
        assert await _x_reuse(conn, px, "ThisMint" + "1" * 30, T) == (
            1,
            2,
            T - timedelta(days=1),
        )
    finally:
        await conn.close()
