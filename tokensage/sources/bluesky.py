"""Bluesky post search: is the coin's name being posted about right now (full depth only).

Bluesky's public AppView answers searchPosts without a key, so this is a free per-name
mention count. Bluesky is far smaller than X, so the counts are a floor, not X's volume.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from tokensage.net.breaker import breaker
from tokensage.sources.gnews import _CRYPTO

log = structlog.get_logger("bluesky")
URL = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
LIMIT = 100  # the most one call returns


@dataclass
class Post:
    text: str
    created_at: str | None  # ISO 8601
    likes: int
    reposts: int
    author: str | None  # handle
    uri: str | None


def _int(v: Any) -> int:
    return v if isinstance(v, int) and v >= 0 else 0


def parse(j: Any) -> list[Post]:
    out: list[Post] = []
    for p in (j.get("posts") if isinstance(j, dict) else None) or []:
        if not isinstance(p, dict):
            continue
        rec: dict[str, Any] = p["record"] if isinstance(p.get("record"), dict) else {}
        author: dict[str, Any] = p["author"] if isinstance(p.get("author"), dict) else {}
        out.append(
            Post(
                text=str(rec.get("text") or "")[:500],
                created_at=rec.get("createdAt") or p.get("indexedAt"),
                likes=_int(p.get("likeCount")),
                reposts=_int(p.get("repostCount")),
                author=author.get("handle"),
                uri=p.get("uri"),
            )
        )
    return out


async def search(http: httpx.AsyncClient, phrase: str) -> list[Post] | None:
    """The newest posts containing the quoted phrase, or None when Bluesky is unavailable."""
    src = "bluesky"
    if not phrase or not breaker.allow(src):
        return None
    try:
        r = await http.get(
            URL, params={"q": f'"{phrase}"', "sort": "latest", "limit": LIMIT}, timeout=8.0
        )
    except httpx.HTTPError as e:
        breaker.failure(src)
        log.info("bluesky.error", error=f"{type(e).__name__}: {e}"[:120])
        return None
    if r.status_code != 200:
        breaker.failure(src)
        log.info("bluesky.bad_response", status=r.status_code)
        return None
    try:
        posts = parse(r.json())
    except ValueError:
        breaker.failure(src)
        return None
    breaker.success(src)
    return posts


def relevant(posts: list[dict], phrase: str, symbol: str | None = None) -> list[dict]:
    """Posts that name the phrase and are not about a coin (cashtags, prices, pump.fun):
    a coin's own shilling is not its subject being popular. posts: as bsky_for caches them."""
    pat = re.compile(r"(?<!\w)" + re.escape(phrase.lower()) + r"(?!\w)")
    sym = (symbol or "").strip().lower()
    sym_pat = re.compile(r"\$" + re.escape(sym) + r"(?!\w)") if len(sym) >= 2 else None
    out = []
    for p in posts:
        t = " ".join(str(p.get("text") or "").lower().split())
        if not pat.search(t) or _CRYPTO.search(t) or (sym_pat and sym_pat.search(t)):
            continue
        out.append(p)
    return out


def created(p: dict) -> datetime | None:
    raw = str(p.get("created_at") or "").strip()
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).astimezone(UTC)
