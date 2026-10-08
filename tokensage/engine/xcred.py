"""S7c (full depth): who is behind the linked X post or profile, and is it worth anything?

`x.match.fit` answers "is the linked thing about this coin". This stage answers a different
question: whether the account behind it carries any weight. A profile made minutes before the
token, named after it, with three posts, matches the coin perfectly and says nothing about it.

`account` holds the facts (age at launch, post count, name changes, verification, whether it
was made for the coin); `credibility` folds them, with followers and link reuse, into one 0-1
score. Weights are hand-set, not yet fitted (Phase 6 calibration). The score is information
for the reader: nothing in the read (categories, referent, summary, fit) depends on it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from tokensage.engine.xsignals import XAssessment
from tokensage.sources.x import ProfileData

DAY_S = 86_400
# Affixes stripped from a handle or display name before comparing it with the coin
# ("pepecoin_sol", "OfficialPepe", "PepeOnSol" all name "pepe").
_AFFIXES = ("official", "onsolana", "onsol", "solana", "coin", "token", "sol", "cto", "the")
_NON_ALNUM = re.compile(r"[^a-z0-9]")

# credibility weights over the known components (renormalised when some are unknown)
W_AGE, W_FOLLOWERS, W_POSTS, W_VERIFIED = 0.35, 0.35, 0.15, 0.15
MADE_FOR_COIN_FACTOR, RENAMED_FACTOR, REUSE_FLOOR = 0.85, 0.85, 0.75
_VERIFIED = {"government": 1.0, "business": 1.0, "legacy": 0.8, "blue": 0.3}


@dataclass
class AccountFacts:
    handle: str | None
    created_at: datetime | None
    age_at_launch_s: int | None  # token creation minus account creation; negative = after
    posts_total: int | None
    posts_about_coin: int | None
    name_changes: int | None
    verified_type: str | None
    followers: int | None
    made_for_coin: bool


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo is not None else d.replace(tzinfo=UTC)


def _compact(s: str | None) -> str:
    return _NON_ALNUM.sub("", (s or "").casefold())


def _strip_affixes(s: str) -> str:
    changed = True
    while changed and s:
        changed = False
        for a in _AFFIXES:
            if s.startswith(a) and len(s) > len(a) + 1:
                s, changed = s[len(a) :], True
            elif s.endswith(a) and len(s) > len(a) + 1:
                s, changed = s[: -len(a)], True
    return s


def names_the_coin(
    handle: str | None, display_name: str | None, name_compact: str, ticker: str | None
) -> bool:
    """Is the account's handle or display name the coin's name or ticker (affixes like
    'official', 'coin', 'onsol' aside)?"""
    targets = {t for t in (name_compact, _compact(ticker)) if len(t) >= 2}
    if not targets:
        return False
    for raw in (handle, display_name):
        c = _compact(raw)
        if c and (c in targets or _strip_affixes(c) in targets):
            return True
    return False


def account_facts(
    xa: XAssessment | None,
    profile: ProfileData | None,
    token_created: datetime | None,
    name_compact: str,
    ticker: str | None,
) -> AccountFacts | None:
    """The account behind the link: the linked profile, or the linked post's author."""
    if xa is None or xa.status != "ok" or not (xa.author_handle or xa.author_id):
        return None
    # the profile, when fetched, is the author's (a tweet's author profile or the link itself)
    same = (
        profile is not None
        and profile.status == "ok"
        and (profile.handle or "").lower() == (xa.author_handle or "").lower()
    )
    created = xa.joined or (profile.joined if same and profile else None)
    age = None
    if created is not None and token_created is not None:
        age = int((_aware(token_created) - _aware(created)).total_seconds())
    made = (
        age is not None
        and age < DAY_S
        and names_the_coin(xa.author_handle, xa.author_name, name_compact, ticker)
    )
    return AccountFacts(
        handle=xa.author_handle,
        created_at=created,
        age_at_launch_s=age,
        posts_total=profile.statuses if same and profile else None,
        posts_about_coin=None,  # no free source counts an account's posts about a coin
        name_changes=xa.username_changes,
        verified_type=xa.verified_type,
        followers=xa.followers,
        made_for_coin=made,
    )


def _log_scale(v: float, full: float) -> float:
    """0 at v<=0, 1 at v>=full, logarithmic between."""
    if v <= 0:
        return 0.0
    return min(1.0, math.log10(1 + v) / math.log10(1 + full))


def credibility(
    acc: AccountFacts | None, relation: str | None, reuse_rank: int | None
) -> float | None:
    """0-1: how much the account behind the link is worth, apart from whether it matches.
    Age at launch, followers, post history and verification, with penalties for an account
    made for the coin, renamed accounts, a spoofed link and being a late re-user of a link.
    None when no account is known."""
    if acc is None:
        return None
    parts: list[tuple[float, float]] = []
    if acc.age_at_launch_s is not None:
        parts.append((W_AGE, _log_scale(acc.age_at_launch_s / DAY_S, 365)))
    if acc.followers is not None:
        parts.append((W_FOLLOWERS, _log_scale(acc.followers, 100_000)))
    if acc.posts_total is not None:
        parts.append((W_POSTS, _log_scale(acc.posts_total, 3_000)))
    parts.append((W_VERIFIED, _VERIFIED.get(acc.verified_type or "", 0.0)))
    w = sum(p[0] for p in parts)
    score = sum(p[0] * p[1] for p in parts) / w if w else 0.0
    # Since rules 0.23.0 the account is context, not a verdict: a made-for-coin or renamed
    # account costs a little, not half. What the post says and the account's name carry the
    # read. A spoofed link still costs most: the link itself lies about who posted.
    if acc.made_for_coin:
        score *= MADE_FOR_COIN_FACTOR
    if acc.name_changes:
        score *= RENAMED_FACTOR
    if relation == "spoofed":
        score *= 0.3
    if reuse_rank is not None and reuse_rank > 1:
        # the 7th coin to link a viral post borrows it; the first may be its own
        score *= max(REUSE_FLOOR, 1.0 / (1.0 + 0.05 * (reuse_rank - 1)))
    return round(max(0.0, min(1.0, score)), 3)
