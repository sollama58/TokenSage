"""Off-chain metadata (guide §4.2): fetch the JSON at `uri`, validate it, fetch the image
header, cache by CID, and schedule retries when it cannot be resolved yet."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import asyncpg
import httpx
import structlog

from tokensage.config import Settings
from tokensage.net.breaker import breaker
from tokensage.net.ipfs import fetch_ipfs, parse_ipfs
from tokensage.net.safe_fetch import Fetched, FetchError, UnsafeUrl, safe_get

log = structlog.get_logger("metadata")

DEAD_GATEWAYS = ("cf-ipfs.com", "cloudflare-ipfs.com")
ALLOWED_LINK_HOSTS_X = {
    "x.com",
    "twitter.com",
    "www.x.com",
    "www.twitter.com",
    "mobile.twitter.com",
}
RETRY_SCHEDULE_S = [30, 120, 300, 900, 1800, 3600]  # ~1.3 h total, then unresolved
IMAGE_MAGIC = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
}


@dataclass
class Metadata:
    status: str  # ok | unresolved | invalid | pending
    content_key: str | None = None
    name: str | None = None
    symbol: str | None = None
    description: str | None = None
    image_url: str | None = None
    twitter: str | None = None
    telegram: str | None = None
    website: str | None = None
    created_on: str | None = None
    raw: dict[str, Any] | None = None
    error: str | None = None
    image_content_key: str | None = None
    image_mime: str | None = None
    image_size: int | None = None
    image_error: str | None = None
    extra_links: list[str] = field(default_factory=list)
    image_bytes: bytes | None = None  # transient; never persisted
    origin: str = "fetched"  # fetched | hints (supplied by the API caller; never cached)


def _sniff_image(body: bytes) -> str | None:
    for magic, mime in IMAGE_MAGIC.items():
        if body.startswith(magic):
            return mime
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    if body[4:12] in (b"ftypavif", b"ftypavis"):
        return "image/avif"
    if body.lstrip()[:4] == b"<svg" or body.lstrip()[:5] == b"<?xml":
        return "image/svg+xml"
    return None


def _clean_str(v: Any, limit: int) -> str | None:
    if v is None or isinstance(v, bool):
        return None
    if not isinstance(v, str):
        v = str(v)
    v = v.replace("\x00", "").strip()
    return v[:limit] if v else None


def clean_url(v: Any) -> str | None:
    """Only https URLs survive. Everything else (javascript:, data:, garbage) becomes None."""
    s = _clean_str(v, 2048)
    if not s:
        return None
    if "://" not in s and s.lower().startswith(("x.com/", "twitter.com/", "t.me/", "www.")):
        s = "https://" + s
    if s.lower().startswith("http://"):
        s = "https://" + s[7:]
    parts = urlsplit(s)
    if parts.scheme != "https" or not parts.hostname:
        return None
    return s


def clean_social(v: Any) -> str | None:
    """pump.fun socials may be a bare handle; keep a sanitised string, URL or not."""
    s = _clean_str(v, 512)
    if not s:
        return None
    if "://" in s or "/" in s or "." in s:
        return clean_url(s)
    return s if s.lstrip("@").replace("_", "").isalnum() else None


def rewrite_dead_gateways(url: str) -> str:
    for host in DEAD_GATEWAYS:
        if host in url:
            return url.replace(f"https://{host}", "https://ipfs.io").replace(
                f"http://{host}", "https://ipfs.io"
            )
    return url


def content_key_for(uri: str, body: bytes) -> str:
    ref = parse_ipfs(uri)
    if ref:
        return "ipfs:" + ref.key
    return "sha256:" + hashlib.sha256(body).hexdigest()


def parse_metadata_json(body: bytes) -> dict[str, Any]:
    text = body.decode("utf-8", "replace").lstrip("﻿")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("metadata JSON is not an object")
    return data


def build(uri: str, body: bytes) -> Metadata:
    try:
        data = parse_metadata_json(body)
    except (ValueError, json.JSONDecodeError) as e:
        return Metadata(
            status="invalid", error=f"bad json: {e}", content_key=content_key_for(uri, body)
        )
    m = Metadata(status="ok", content_key=content_key_for(uri, body), raw=data)
    m.name = _clean_str(data.get("name"), 256)
    m.symbol = _clean_str(data.get("symbol"), 64)
    m.description = _clean_str(data.get("description"), 4000)
    m.image_url = clean_url(rewrite_dead_gateways(str(data.get("image") or "")))
    m.twitter = clean_social(data.get("twitter"))
    m.telegram = clean_social(data.get("telegram"))
    m.website = clean_url(data.get("website"))
    m.created_on = _clean_str(data.get("createdOn"), 256)
    for k in ("discord", "github", "medium", "youtube", "tiktok", "instagram"):
        u = clean_url(data.get(k))
        if u:
            m.extra_links.append(u)
    return m


async def fetch_url(
    client: httpx.AsyncClient, url: str, settings: Settings, *, max_bytes: int, accept: str
) -> Fetched:
    url = rewrite_dead_gateways(url)
    ref = parse_ipfs(url)
    if ref:
        return await fetch_ipfs(
            client,
            ref,
            settings.ipfs_gateway_list,
            max_bytes=max_bytes,
            timeout=settings.fetch_total_timeout_s,
            accept=accept,
        )
    source = "host:" + (urlsplit(url).hostname or "?")
    if not breaker.allow(source):
        raise FetchError(f"circuit open for {source}", retryable=True)
    try:
        f = await safe_get(
            client, url, max_bytes=max_bytes, timeout=settings.fetch_total_timeout_s, accept=accept
        )
    except FetchError as e:
        if e.retryable:
            breaker.failure(source)
        raise
    breaker.success(source)
    return f


async def fetch_metadata(client: httpx.AsyncClient, uri: str, settings: Settings) -> Metadata:
    """Fetch and validate the JSON at uri; then fetch the image header. Never raises."""
    try:
        f = await fetch_url(
            client,
            uri,
            settings,
            max_bytes=settings.metadata_max_bytes,
            accept="application/json, */*",
        )
    except UnsafeUrl as e:
        return Metadata(status="invalid", error=f"unsafe uri: {e}")
    except FetchError as e:
        return Metadata(status="unresolved" if e.retryable else "invalid", error=str(e))
    m = build(uri, f.body)
    if m.status != "ok" or not m.image_url:
        return m
    await attach_image(client, m, settings)
    return m


HINT_FIELDS = ("name", "symbol", "description", "image_url", "twitter", "telegram", "website")


def from_hints(hints: dict[str, Any]) -> Metadata:
    """Metadata from values the API caller already had (e.g. from pump.fun), cleaned exactly
    like fetched metadata JSON. Untrusted: URLs go through the same cleaning here and the same
    SSRF-guarded fetch when the image is downloaded."""
    data = {k: hints.get(k) for k in HINT_FIELDS}
    body = json.dumps(data, sort_keys=True, default=str).encode()
    m = Metadata(
        status="ok",
        content_key="hints:" + hashlib.sha256(body).hexdigest(),
        raw={k: v for k, v in data.items() if v is not None},
        origin="hints",
    )
    m.name = _clean_str(data.get("name"), 256)
    m.symbol = _clean_str(data.get("symbol"), 64)
    m.description = _clean_str(data.get("description"), 4000)
    m.image_url = clean_url(rewrite_dead_gateways(str(data.get("image_url") or "")))
    m.twitter = clean_social(data.get("twitter"))
    m.telegram = clean_social(data.get("telegram"))
    m.website = clean_url(data.get("website"))
    return m


async def attach_image(client: httpx.AsyncClient, m: Metadata, settings: Settings) -> None:
    """Download and sniff m.image_url (SSRF-guarded, size/time capped). Never raises."""
    if not m.image_url:
        return
    try:
        img = await fetch_url(
            client, m.image_url, settings, max_bytes=settings.image_max_bytes, accept="image/*,*/*"
        )
        mime = _sniff_image(img.body)
        if mime is None:
            m.image_error = f"not an image (content-type {img.content_type or 'unknown'})"
        else:
            m.image_mime = mime
            m.image_size = len(img.body)
            m.image_bytes = img.body
            m.image_content_key = content_key_for(m.image_url, img.body)
    except UnsafeUrl as e:
        m.image_error = f"unsafe image url: {e}"
    except FetchError as e:
        m.image_error = str(e)


async def load_cached(conn: asyncpg.Connection, mint: str) -> Metadata | None:
    row = await conn.fetchrow("select * from token_metadata where mint=$1", mint)
    if not row or row["status"] != "ok":
        return None
    raw = row["raw"] or {}
    return Metadata(
        status="ok",
        content_key=row["content_key"],
        raw=raw,
        name=_clean_str(raw.get("name"), 256),
        symbol=_clean_str(raw.get("symbol"), 64),
        description=row["description"],
        image_url=row["image_url"],
        twitter=row["twitter"],
        telegram=row["telegram"],
        website=row["website"],
        created_on=_clean_str(raw.get("createdOn"), 256),
        image_content_key=row["image_content_key"],
    )


async def persist(conn: asyncpg.Connection, mint: str, m: Metadata, attempts: int) -> None:
    next_retry = None
    if m.status == "unresolved" and attempts < len(RETRY_SCHEDULE_S):
        next_retry = RETRY_SCHEDULE_S[attempts]
    await conn.execute(
        """
        insert into token_metadata (mint, status, content_key, description, image_url, twitter,
          telegram, website, raw, attempts, next_retry_at, fetched_at, image_content_key)
        values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,
                case when $11::int is null then null else now() + make_interval(secs => $11) end,
                case when $2 = 'ok' then now() end, $12)
        on conflict (mint) do update set status=excluded.status,
          content_key=coalesce(excluded.content_key, token_metadata.content_key),
          description=excluded.description, image_url=excluded.image_url,
          twitter=excluded.twitter, telegram=excluded.telegram, website=excluded.website,
          raw=coalesce(excluded.raw, token_metadata.raw), attempts=excluded.attempts,
          next_retry_at=excluded.next_retry_at,
          fetched_at=coalesce(excluded.fetched_at, token_metadata.fetched_at),
          image_content_key=coalesce(excluded.image_content_key, token_metadata.image_content_key)
        """,
        mint,
        m.status,
        m.content_key,
        m.description,
        m.image_url,
        m.twitter,
        m.telegram,
        m.website,
        m.raw,
        attempts,
        next_retry,
        m.image_content_key,
    )
    if m.image_content_key:
        await conn.execute(
            """insert into image (content_key, sha256, mime, bytes)
               values ($1, $2, $3, $4) on conflict (content_key) do nothing""",
            m.image_content_key,
            m.image_content_key.split(":", 1)[1]
            if m.image_content_key.startswith("sha256:")
            else None,
            m.image_mime,
            m.image_size,
        )
    if m.status == "ok" and (m.name or m.symbol):
        await conn.execute(
            """update token set launcher = coalesce(launcher, $2) where mint=$1""",
            mint,
            m.created_on or (urlsplit(m.image_url or "").hostname if m.image_url else None),
        )
