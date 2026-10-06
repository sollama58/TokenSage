"""The v1 contract: the Analysis document (guide §3) and the response envelope (§6.4).

Compatibility rules: adding fields or labels is a minor change; renaming, removing or
changing meaning needs a new schema_version served under a new path.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1"

Depth = Literal["basic", "full"]
Status = Literal["complete", "partial", "pending", "failed"]
JobStatus = Literal["pending", "running", "done", "failed"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- Analysis parts


class RawFields(_Model):
    name: str | None = None
    symbol: str | None = None
    description: str | None = None
    image_url: str | None = None
    twitter: str | None = None
    telegram: str | None = None
    website: str | None = None


class Normalized(_Model):
    name_tokens: list[str] = []
    ticker: str | None = None
    ticker_base: str | None = None
    markers: list[str] = []
    emoji_keywords: list[str] = []
    obfuscation: list[str] = []


class Market(_Model):
    complete: bool | None = None
    curve_progress: float | None = Field(default=None, ge=0, le=1)
    graduated_pool: str | None = None
    creator: str | None = None
    is_mayhem_mode: bool | None = None
    quote_mint: str | None = None


class Referent(_Model):
    label: str
    kind: Literal["famous_animal", "meme", "person", "coin", "event", "concept", "place", "other"]
    desc: str | None = None
    source: str | None = None
    confidence: float = Field(ge=0, le=1)


class Category(_Model):
    label: str
    confidence: float = Field(ge=0, le=1)


class CopyOf(_Model):
    ticker: str | None = None
    name: str | None = None
    mint: str | None = None
    signals: list[str] = []


class ImageLabel(_Model):
    label: str
    score: float
    model: str


class NearDuplicate(_Model):
    mint: str | None = None
    known_coin: str | None = None
    template: str | None = None
    distance: int


class ImageInfo(_Model):
    status: Literal["ok", "missing", "failed", "skipped"] = "skipped"
    source_url: str | None = None
    phash: str | None = None
    pdq: str | None = None
    ocr: list[str] = []
    palette: list[str] = []
    near_duplicates: list[NearDuplicate] = []
    labels: list[ImageLabel] = []
    animated: bool | None = None


class XRef(_Model):
    kind: Literal[
        "tweet",
        "profile",
        "community",
        "search",
        "list",
        "shortlink",
        "foreign",
        "homepage",
        "unknown",
        "invalid",
        "empty",
    ]
    tweet_id: str | None = None
    community_id: str | None = None
    url_handle: str | None = None
    handle: str | None = None
    user_id: str | None = None
    query: str | None = None
    url: str | None = None


class XAuthor(_Model):
    handle: str | None = None
    user_id: str | None = None
    name: str | None = None
    verified_type: str | None = None
    followers: int | None = None
    joined: datetime | None = None
    username_changes: int | None = None


class XInfo(_Model):
    ref: XRef
    object_time: datetime | None = None
    predates_token_by_s: int | None = None
    author: XAuthor | None = None
    text: str | None = None
    relation: (
        Literal[
            "narrative_reference",
            "launch_announcement",
            "official_account",
            "spoofed",
            "search_only",
            "unknown",
        ]
        | None
    ) = None
    reuse_count: int = 0
    fetch_source: str | None = None
    status: Literal["ok", "deleted", "suspended", "not_fetched", "failed", "none"] = "none"


class TrendTerm(_Model):
    term: str
    spike: float | None = None
    source: str
    headline: str | None = None


class Trend(_Model):
    matched: bool = False
    terms: list[TrendTerm] = []


class Flag(_Model):
    code: str
    severity: Literal["info", "warn", "high"]
    detail: str


class Evidence(_Model):
    kind: str
    label: str
    weight: float = Field(ge=0, le=1)
    detail: str
    source: str
    url: str | None = None


class Versions(_Model):
    rules: str
    lexicon: str
    known_coins: str | None = None
    models: dict[str, str] = {}


class Analysis(_Model):
    schema_version: str = SCHEMA_VERSION
    mint: str
    created_at: datetime | None = None
    launchpad: Literal["pump.fun", "unknown"] = "unknown"
    market: Market = Market()
    raw: RawFields = RawFields()
    normalized: Normalized = Normalized()
    referent: Referent | None = None
    categories: list[Category] = []
    ticker_explanation: str | None = None
    copy_of: list[CopyOf] = []
    image: ImageInfo = ImageInfo()
    x: XInfo | None = None
    trend: Trend = Trend()
    flags: list[Flag] = []
    summary: str
    evidence: list[Evidence] = []
    caveats: list[str] = []
    depth: Depth
    analyzed_at: datetime
    versions: Versions


# ---------------------------------------------------------------- Envelope


class UpstreamError(_Model):
    source: str
    code: str
    detail: str | None = None


class Freshness(_Model):
    analyzed_at: datetime | None = None
    age_s: int | None = None
    max_age_s: int | None = None
    from_cache: bool = False


class TokenResponse(_Model):
    ca: str
    status: Status
    depth: Depth
    analysis: Analysis | None = None
    stale_analysis: Analysis | None = None
    freshness: Freshness = Freshness()
    errors: list[UpstreamError] = []
    job_id: int | None = None
    request_id: str


class JobResponse(_Model):
    job_id: int
    status: JobStatus
    ca: str | None = None
    depth: Depth | None = None
    result: TokenResponse | None = None
    error: str | None = None
    request_id: str


class BatchRequest(_Model):
    cas: list[str] = Field(min_length=1, max_length=50)
    depth: Depth = "basic"


class BatchItem(_Model):
    ca: str
    status: Status | Literal["invalid"]
    analysis: Analysis | None = None
    job_id: int | None = None
    error: str | None = None


class BatchResponse(_Model):
    items: list[BatchItem]
    request_id: str


class TaxonomyEntry(_Model):
    label: str
    description: str


class FlagEntry(_Model):
    code: str
    severity: Literal["info", "warn", "high"]
    description: str


class MetaResponse(_Model):
    schema_version: str
    service_version: str
    versions: Versions
    categories: list[TaxonomyEntry]
    flags: list[FlagEntry]
    depths: list[Depth]
    disclaimer: str


class ErrorBody(_Model):
    code: str
    message: str
    request_id: str


class ErrorResponse(_Model):
    error: ErrorBody


def analysis_json_schema() -> dict[str, Any]:
    return Analysis.model_json_schema()
