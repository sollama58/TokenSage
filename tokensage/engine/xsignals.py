"""S7 (full depth): turn fetched X content into relation, flags and evidence (guide §5.7)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from tokensage.engine.context import Ev
from tokensage.sources.x import ProfileData, TweetData

BIG_ACCOUNT = 50_000
FRESH_DAYS = 14
MAX_MENTIONS = 5
MENTION = re.compile(r"(?<![\w@])@([A-Za-z0-9_]{1,15})\b")


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
    quoted: QuotedAssessment | None = None
    replied_to: QuotedAssessment | None = None  # the post the linked tweet replies to
    # every account involved: the author, the quoted and replied-to authors, @mentions
    accounts: list[XAccount] = field(default_factory=list)


@dataclass
class XAccount:
    role: str  # author | quoted_author | replied_to_author | mentioned
    handle: str | None
    name: str | None = None  # display name, when known
    followers: int | None = None
    verified_type: str | None = None


@dataclass
class QuotedAssessment:
    """A post the linked tweet points at: the one it quotes, or the one it replies to. Often
    the real narrative: a launch tweet that quotes or answers someone else's earlier post."""

    id: str
    status: str  # ok | deleted | failed
    text: str | None = None
    created_at: datetime | None = None
    author_handle: str | None = None
    author_id: str | None = None
    author_name: str | None = None
    followers: int | None = None
    verified_type: str | None = None
    joined: datetime | None = None


def _days(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return (_aware(a) - _aware(b)).total_seconds() / 86400


def _aware(d: datetime) -> datetime:
    """Treat a naive datetime (e.g. from an old cache row) as UTC instead of crashing."""
    return d if d.tzinfo is not None else d.replace(tzinfo=UTC)


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
            or (len(mint) >= 32 and mint.lower() in text_l)
            or "pump.fun" in text_l
        )
        # a post naming the ticker or CA is the launch post, even when it predates the
        # mint by a bit; from a large account it is still someone else's narrative unless
        # it carries the CA itself
        is_launch = mentions and (not big or mint.lower() in text_l)
        if a.relation != "spoofed":
            if gap_days is not None and gap_days > 0 and not is_launch:
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
        if tweet.quoted is not None:
            a.quoted = _assess_related(a, tweet, tweet.quoted, "quote", token_created)
        if tweet.replied_to is not None:
            a.replied_to = _assess_related(a, tweet, tweet.replied_to, "reply", token_created)
        elif tweet.replying_to_id:
            a.replied_to = QuotedAssessment(
                id=tweet.replying_to_id, status="failed", author_handle=tweet.replying_to_handle
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

    a.accounts = _accounts(a, tweet if kind == "tweet" else None)

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


_RELATED = {
    "quote": ("quotes", "quoted post", "x_quote_timing", "x_quote_author"),
    "reply": ("replies to", "replied-to post", "x_reply_timing", "x_reply_author"),
}


def _assess_related(
    a: XAssessment,
    tweet: TweetData,
    q: TweetData,
    kind: str,
    token_created: datetime | None,
) -> QuotedAssessment:
    """The quoted or replied-to post: record it and, when it is someone else's earlier post,
    treat it as the narrative the coin borrows."""
    verb, noun, timing_kind, author_kind = _RELATED[kind]
    fallback_id = tweet.quoted_tweet_id if kind == "quote" else tweet.replying_to_id
    qa = QuotedAssessment(id=q.id or (fallback_id or ""), status=q.status)
    if kind == "reply":
        qa.author_handle = tweet.replying_to_handle
    if q.status != "ok":
        return qa
    qa.text = q.text
    qa.created_at = q.created_at
    qa.author_handle = q.author_handle or qa.author_handle
    qa.author_id = q.author_id
    qa.author_name = q.author_name
    qa.followers = q.followers
    qa.verified_type = q.verified_type
    qa.joined = q.author_joined
    same_author = bool(
        qa.author_handle
        and tweet.author_handle
        and qa.author_handle.lower() == tweet.author_handle.lower()
    )
    gap_days = _days(token_created, q.created_at)
    if same_author or gap_days is None or gap_days <= 0:
        return qa
    # The linked tweet points at an earlier post by someone else: that post is the narrative.
    a.evidence.append(
        Ev(
            timing_kind,
            "news_event",
            0.4 if gap_days < 3 else 0.2,
            f"the linked tweet {verb} @{qa.author_handle or '?'}'s post from "
            f"{_fmt_days(gap_days)} before the token",
            "x",
            "x",
        )
    )
    big = (q.followers or 0) >= BIG_ACCOUNT or q.verified_type in ("business", "government")
    if big:
        if not any(code == "borrowed_narrative" for code, _, _ in a.flags):
            a.flags.append(
                (
                    "borrowed_narrative",
                    "info",
                    f"the linked tweet {verb} @{qa.author_handle}, a large/verified account; "
                    "the coin borrows that narrative",
                )
            )
        a.evidence.append(
            Ev(
                author_kind,
                "celebrity",
                0.35,
                f"{noun} by a large account @{qa.author_handle} "
                f"({q.followers or '?'} followers, {q.verified_type or 'unverified'})",
                "x",
                "x",
            )
        )
    return qa


def _accounts(a: XAssessment, tweet: TweetData | None) -> list[XAccount]:
    """Every account involved, once each: the author (or linked profile), the quoted and
    replied-to authors, then accounts @mentioned in any of the posts."""
    out: list[XAccount] = []
    seen: set[str] = set()

    def add(role: str, handle: str | None, name: str | None = None, **kw: object) -> None:
        key = (handle or "").lower() or f"name:{(name or '').lower()}"
        if key in ("", "name:") or key in seen:
            return
        seen.add(key)
        out.append(XAccount(role, handle, name, **kw))  # type: ignore[arg-type]

    add("author", a.author_handle, a.author_name, followers=a.followers,
        verified_type=a.verified_type)  # fmt: skip
    texts = [a.text or ""]
    for role, r in (("quoted_author", a.quoted), ("replied_to_author", a.replied_to)):
        if r is not None:
            add(role, r.author_handle, r.author_name, followers=r.followers,
                verified_type=r.verified_type)  # fmt: skip
            texts.append(r.text or "")
    if tweet is None:
        return out
    mentions = 0
    for t in texts:
        for m in MENTION.finditer(t):
            if mentions >= MAX_MENTIONS:
                return out
            before = len(out)
            add("mentioned", m.group(1))
            mentions += len(out) - before
    return out


def _fmt_days(d: float) -> str:
    if d < 1 / 24:
        return f"{int(d * 1440)} min"
    if d < 1:
        return f"{d * 24:.1f} h"
    return f"{d:.1f} d"
