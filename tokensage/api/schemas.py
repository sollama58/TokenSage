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
ReferentKind = Literal[
    "famous_animal", "meme", "person", "coin", "event", "concept", "place", "other"
]
Severity = Literal["info", "warn", "high"]


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
    kind: ReferentKind
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


class XMatchField(_Model):
    score: float = Field(ge=0, le=1)
    how: str = Field(
        description="name: exact|normalized|segment|fuzzy|none; "
        "ticker: cashtag|bare|hashtag|fuzzy|none"
    )
    detail: str


class XMatchImage(_Model):
    score: float = Field(ge=0, le=1)
    best_distance: int | None = Field(
        default=None, description="Smallest perceptual-hash Hamming distance to the logo"
    )
    media_checked: int = 0
    detail: str


class XMatchReferent(_Model):
    x_label: str | None = Field(default=None, description="What the post alone is about")
    x_kind: str | None = None
    agrees: bool | None = Field(
        default=None,
        description="Whether it is the referent the name, ticker and image point to on "
        "their own; null when either side has no confident referent",
    )
    confidence: float = Field(default=0, ge=0, le=1)


class XMatch(_Model):
    """How well the linked X post (or profile) matches the token (depth=full)."""

    name: XMatchField
    ticker: XMatchField
    image: XMatchImage
    referent: XMatchReferent
    x_categories: list[Category] = Field(
        default=[], description="Categories of the post text read on its own"
    )
    fit: float = Field(ge=0, le=1, description="Overall match, 0-1 (uncalibrated until Phase 6)")
    verdict: Literal["about_this_coin", "related", "unrelated", "unknown"]


class XQuoted(_Model):
    """The tweet the linked tweet quotes (full depth). Often the real narrative: a launch
    post that quote-tweets someone else's earlier post."""

    id: str
    url: str | None = None
    status: Literal["ok", "deleted", "failed"] = "ok"
    author: XAuthor | None = None
    text: str | None = None
    created_at: datetime | None = None
    predates_token_by_s: int | None = Field(
        default=None, description="Seconds the quoted post predates the token; negative if after"
    )


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
    quoted: XQuoted | None = Field(
        default=None, description="Present when the linked tweet is a quote tweet (full depth)"
    )
    match: XMatch | None = Field(
        default=None,
        description="Post/profile vs token comparison (full depth, tweet or profile links)",
    )


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
    severity: Severity
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


class TokenHints(_Model):
    """Metadata the caller already holds (e.g. from pump.fun). Untrusted: cleaned and
    URL-checked exactly like fetched metadata. When present, the IPFS metadata fetch is
    skipped, and a mint not yet visible on-chain is analysed instead of returning 404."""

    name: str | None = Field(default=None, max_length=256)
    symbol: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=4000)
    image_url: str | None = Field(default=None, max_length=2048)
    twitter: str | None = Field(default=None, max_length=2048)
    telegram: str | None = Field(default=None, max_length=2048)
    website: str | None = Field(default=None, max_length=2048)
    created_at: datetime | None = Field(
        default=None, description="Token creation time; skips the on-chain history lookup"
    )


class BatchRequestItem(_Model):
    ca: str
    hints: TokenHints | None = None


class TokenRequest(_Model):
    """Optional body of POST /v1/tokens/{ca} (the query parameters are those of GET)."""

    hints: TokenHints | None = None


class BatchRequest(_Model):
    cas: list[str] = Field(
        default=[],
        max_length=50,
        description="CAs to prefetch. Use `items` instead to pass hints; both may be combined "
        "(at most 50 in total, at least one)",
    )
    items: list[BatchRequestItem] = Field(default=[], max_length=50)
    depth: Depth = "basic"
    callback_url: str | None = Field(
        default=None,
        description="Optional https URL that receives a signed JobResponse per finished job",
    )


class BatchItem(_Model):
    ca: str
    status: Status | Literal["invalid"]
    analysis: Analysis | None = None
    job_id: int | None = None
    error: str | None = Field(
        default=None,
        description=(
            "Why the item has no job: the CA is invalid, or status 'failed' with "
            "'quota_exceeded' / 'overloaded' when that item was rejected (others still queue)"
        ),
    )
    retry_after_s: int | None = Field(
        default=None, description="For a rejected item: seconds to wait before retrying it"
    )


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
