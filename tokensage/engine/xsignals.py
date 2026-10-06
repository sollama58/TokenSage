"""S7 (full depth): turn fetched X content into relation, flags and evidence (guide §5.7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from tokensage.engine.context import Ev
from tokensage.sources.x import ProfileData, TweetData

BIG_ACCOUNT = 50_000
FRESH_DAYS = 14


@dataclass
class XAssessment:
    # narrative_reference | launch_announcement | official_account | spoofed | search_only
    relation: str | None = None
    status: str = "none"
    flags: list[tuple[str, str, str]] = field(default_factory=list)  # (code, severity, detail)
    evidence: list[Ev] = field(default_factory=list)
    text: str | None = None
    author_handle: str | None = None
    author_id: str | None = None
    author_name: str | None = None
    followers: int | None = None
    verified_type: str | None = None
    joined: datetime | None = None
    username_changes: int | None = None
    fetch_source: str | None = None


def _days(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return (a - b).total_seconds() / 86400


def assess(
    kind: str,
    url_handle: str | None,
    tweet: TweetData | None,
    profile: ProfileData | None,
    token_created: datetime | None,
    ticker: str | None,
    mint: str,
    name_tokens: list[str],
) -> XAssessment:
    a = XAssessment()
    if kind == "search":
        a.relation, a.status = "search_only", "none"
        return a
    if kind not in ("tweet", "profile", "community"):
        return a

    # ---- fetched tweet
    if kind == "tweet":
        if tweet is None or tweet.status == "failed":
            a.status = "failed"
            return a
        a.fetch_source = tweet.source
        if tweet.status == "deleted":
            a.status = "deleted"
            a.flags.append(("tweet_deleted", "warn", "the linked tweet no longer exists"))
            return a
        a.status = "ok"
        a.text = tweet.text
        a.author_handle = tweet.author_handle
        a.author_id = tweet.author_id
        a.author_name = tweet.author_name
        a.followers = tweet.followers
        a.verified_type = tweet.verified_type
        a.joined = tweet.author_joined
        if profile and profile.status == "ok":
            a.followers = a.followers if a.followers is not None else profile.followers
            a.joined = a.joined or profile.joined
            a.username_changes = profile.username_changes
            a.verified_type = a.verified_type or profile.verified_type
        # spoofed handle: the URL claims one author, the tweet has another
        if url_handle and tweet.author_handle and url_handle.lower() != tweet.author_handle.lower():
            a.relation = "spoofed"
            a.flags.append(
                (
                    "spoofed_tweet_handle",
                    "high",
                    f"URL says @{url_handle} but the tweet is by @{tweet.author_handle}",
                )
            )
        gap_days = _days(token_created, tweet.created_at)
        big = (a.followers or 0) >= BIG_ACCOUNT or a.verified_type in ("business", "government")
        text_l = (tweet.text or "").lower()
        mentions = bool(ticker) and (
            f"${ticker.lower()}" in text_l  # type: ignore[union-attr]
            or mint.lower() in text_l
            or "pump.fun" in text_l
        )
        if a.relation != "spoofed":
            if gap_days is not None and gap_days > 0:
                a.relation = "narrative_reference"
                detail = (
                    f"the linked tweet (by @{tweet.author_handle}) predates the token by "
                    f"{_fmt_days(gap_days)}"
                )
                a.evidence.append(
                    Ev("x_timing", "news_event", 0.45 if gap_days < 3 else 0.25, detail, "x", "x")
                )
                if big:
                    a.flags.append(
                        (
                            "borrowed_narrative",
                            "info",
                            f"@{tweet.author_handle} is a large/verified account unrelated to "
                            "the deployer; the coin borrows the narrative",
                        )
                    )
                    a.evidence.append(
                        Ev(
                            "x_author",
                            "celebrity",
                            0.4,
                            f"narrative tweet by a large account @{tweet.author_handle} "
                            f"({a.followers or '?'} followers, {a.verified_type or 'unverified'})",
                            "x",
                            "x",
                        )
                    )
            else:
                a.relation = "launch_announcement" if not mentions else "official_account"
        if mentions:
            a.evidence.append(
                Ev(
                    "x_mentions",
                    "crypto_native/pumpfun_meta",
                    0.3,
                    "the tweet mentions the ticker, CA or pump.fun directly",
                    "x",
                    "x",
                )
            )
        if tweet.possibly_sensitive:
            a.evidence.append(
                Ev(
                    "x_sensitive",
                    "humor_crude_offensive",
                    0.3,
                    "X marks the tweet possibly sensitive",
                    "x",
                    "x",
                )
            )

    # ---- profile link
    if kind == "profile":
        if profile is None or profile.status == "failed":
            a.status = "failed"
            return a
        a.fetch_source = profile.source
        if profile.status == "suspended":
            a.status = "suspended"
            a.flags.append(("account_suspended", "warn", "the linked X account is suspended"))
            return a
        if profile.status == "not_found":
            a.status = "deleted"
            a.flags.append(("account_suspended", "warn", "the linked X account does not exist"))
            return a
        a.status = "ok"
        a.relation = "official_account"
        a.author_handle = profile.handle
        a.author_id = profile.user_id
        a.author_name = profile.name
        a.followers = profile.followers
        a.verified_type = profile.verified_type
        a.joined = profile.joined
        a.username_changes = profile.username_changes
        a.text = profile.description
        big = (profile.followers or 0) >= BIG_ACCOUNT
        if big and token_created and profile.joined and _days(token_created, profile.joined) > 365:  # type: ignore[operator]
            a.relation = "narrative_reference"
            a.flags.append(
                (
                    "borrowed_narrative",
                    "info",
                    f"@{profile.handle} is an established large account; a memecoin linking "
                    "it is usually borrowing the name",
                )
            )

    if kind == "community":
        a.status = "not_fetched"  # community details need the paid tier (guide §4.3)
        return a

    # ---- account-quality flags (tweet or profile)
    if a.username_changes:
        a.flags.append(
            (
                "recycled_x_account",
                "high",
                f"@{a.author_handle} has changed its username {a.username_changes} time(s)",
            )
        )
    age = _days(token_created, a.joined)
    if age is not None and 0 <= age <= FRESH_DAYS and not (a.followers or 0) >= BIG_ACCOUNT:
        a.flags.append(
            (
                "fresh_x_account",
                "info",
                f"@{a.author_handle} was created {_fmt_days(age)} before the token",
            )
        )
    return a


def _fmt_days(d: float) -> str:
    if d < 1 / 24:
        return f"{int(d * 1440)} min"
    if d < 1:
        return f"{d * 24:.1f} h"
    return f"{d:.1f} d"
