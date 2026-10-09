"""The v1 contract: the Analysis document (guide §3) and the response envelope (§6.4).

Compatibility rules: adding fields or labels is a minor change; renaming, removing or
changing meaning needs a new schema_version served under a new path.
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SCHEMA_VERSION = "1"

Depth = Literal["basic", "full"]
Status = Literal["complete", "partial", "pending", "failed"]
JobStatus = Literal["pending", "running", "done", "failed"]
ReferentKind = Literal[
    "famous_animal",
    "meme",
    "person",
    "coin",
    "event",
    "concept",
    "place",
    "other",
    "animal",
    "media",
    "project",
    "object",
    "organization",
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


class PairReferent(_Model):
    label: str
    kind: ReferentKind
    desc: str | None = None
    confidence: float = Field(ge=0, le=1)


class Pair(_Model):
    """The token the coin trades against on its bonding curve. SOL, stablecoins and majors
    (wrapped BTC/ETH/ZEC, PUMP) carry no meaning; any other token, often another pump.fun
    coin, feeds the analysis (category crypto_native/paired_ecosystem,
    derivative/pair_family when the name builds on it, flag non_sol_pair) and the summary
    names it by ticker and name."""

    mint: str
    symbol: str | None = None
    name: str | None = None
    kind: Literal["sol", "stablecoin", "lst", "major", "token", "tokenized_stock"] = Field(
        description="sol, stablecoin, lst (staked SOL) and major (wrapped BTC/ETH/ZEC, PUMP) "
        "carry no meaning; token and tokenized_stock (an xStock such as TSLAx) feed the analysis"
    )
    underlying: str | None = Field(
        default=None, description="For a tokenized stock: the stock ticker (TSLA for TSLAx)"
    )
    source: str | None = Field(
        default=None,
        description="Where the pair token was identified: neutral (SOL/stablecoin), "
        "known_coin, analysis (a stored TokenSage analysis), db, onchain, none",
    )
    pumpfun: bool | None = Field(
        default=None,
        description="The pair token is itself a pump.fun coin (it has a pump.fun bonding "
        "curve; pump.fun pairs coins with any other pump.fun coin). Null when not looked up "
        "(SOL, stablecoins and majors) or unknown",
    )
    builds_on: bool = Field(
        default=False, description="The coin's name or ticker builds on the pair token's"
    )
    builds_on_detail: str | None = None
    referent: PairReferent | None = Field(
        default=None, description="What the pair token itself refers to, when known"
    )
    categories: list[Category] = []


class FeeRecipient(_Model):
    """One shareholder of a fee-sharing config (rules 0.19.0)."""

    address: str = Field(
        description="The shareholder address as stored on-chain: a wallet, or a Pump Fees "
        "PDA for a GitHub/social or charity recipient"
    )
    share: float = Field(ge=0, le=1, description="Its share of the creator fee (share_bps / 10000)")
    share_bps: int = Field(ge=0, le=10_000)
    kind: Literal[
        "creator", "wallet", "github", "x", "pump", "social", "charity", "program", "unresolved"
    ] = Field(
        description="creator: the wallet that controls the split (the launch wallet, or a "
        "community-takeover admin); wallet: any other plain wallet; github / x / pump: a "
        "social-fee PDA claimable by that linked account (platform 2 = GitHub); charity: a "
        "donate.gg donation PDA; program: an account of some other program; unresolved: not "
        "classified (RPC failure)"
    )
    is_creator: bool = False
    platform: str | None = Field(
        default=None, description="For a social recipient: github, x or pump"
    )
    user_id: str | None = Field(
        default=None,
        description="For a social recipient: the platform's user id (GitHub: the numeric "
        "account id)",
    )
    github_login: str | None = Field(
        default=None,
        description="Always null since rules 0.22.0: the GitHub login is not looked up; "
        "use user_id (kept for v1 compatibility)",
    )
    url: str | None = None
    charity_config_id: str | None = Field(
        default=None,
        description="For a charity recipient: the donate.gg config id the fee is escrowed for",
    )
    lifetime_received: float | None = Field(
        default=None,
        description="For a social recipient: what that account has claimed so far across all "
        "its coins, in SOL; for a charity recipient: what this coin has donated so far, in "
        "its quote token",
    )


class CreatorFee(_Model):
    """Where the coin's creator fee goes (rules 0.19.0; all depths)."""

    destination: Literal[
        "creator",
        "wallet",
        "split",
        "holder_rewards",
        "charity",
        "github",
        "social",
        "other",
        "cashback",
        "unknown",
    ] = Field(
        description="creator: the creator wallet; wallet: one other wallet; split: several "
        "recipients, where no charity, GitHub or social recipient holds half (or two of them "
        "hold half each); holder_rewards: set aside for holders, paid out by pump.fun; "
        "charity: donate.gg charities hold at least half; github: GitHub-linked accounts hold "
        "at least half; social: X- or pump.fun-linked accounts hold at least half; other: a "
        "single recipient that is an account of some other program; cashback: back to "
        "traders (deprecated coin type); unknown: could not be "
        "determined. New values may be added"
    )
    mechanism: Literal["direct", "sharing_config", "holder_rewards", "cashback"] = Field(
        description="direct: the fee accrues to the wallet in the bonding curve's creator "
        "field; sharing_config: the curve points at a Pump Fees SharingConfig whose "
        "shareholders are paid; holder_rewards / cashback: the coin types"
    )
    creator_fee_bps: int | None = Field(
        default=None,
        description="A custom creator fee rate set at creation (custom pairs only); 0 or null "
        "means pump.fun's standard schedule",
    )
    admin: str | None = Field(
        default=None, description="Fee sharing: the wallet that controls the split (market.creator)"
    )
    sharing_config: str | None = Field(
        default=None, description="Fee sharing: the SharingConfig PDA"
    )
    sharing_version: int | None = None
    mutable: bool | None = Field(
        default=None,
        description="Fee sharing: true when the admin can still change the split (admin not "
        "revoked); false once it is final",
    )
    split: bool = Field(default=False, description="More than one recipient")
    shares: dict[str, float] = Field(
        default={},
        description="Share of the fee per recipient kind, e.g. {charity: 0.99, creator: 0.01}",
    )
    recipients: list[FeeRecipient] = []
    summary: str = Field(
        description="One plain sentence, e.g. 'creator fees go to charity: 100% to a charity "
        "via donate.gg'"
    )


class Market(_Model):
    complete: bool | None = None
    curve_progress: float | None = Field(default=None, ge=0, le=1)
    graduated_pool: str | None = None
    creator: str | None = Field(
        default=None,
        description="The creator wallet. Since rules 0.19.0 never a PDA: on a fee-shared coin "
        "it is the sharing config's admin; on a holder-rewards coin it is the launch wallet "
        "when a source gave it, else null. The raw on-chain value is creator_onchain",
    )
    creator_onchain: str | None = Field(
        default=None,
        description="The bonding curve's creator field as stored: a wallet, a fee-sharing "
        "config PDA or the holder-rewards PDA (see creator_kind)",
    )
    creator_kind: Literal["wallet", "sharing_config", "holder_rewards_pda", "unknown"] | None = None
    creator_fee: CreatorFee | None = Field(
        default=None,
        description="Where the creator fee goes (since rules 0.19.0); null for a non-pump.fun "
        "mint or a coin analysed from hints before it is visible on-chain",
    )
    is_mayhem_mode: bool | None = None
    quote_mint: str | None = None
    pair: Pair | None = Field(
        default=None, description="The token the coin trades against (from quote_mint)"
    )


class ReferentWave(_Model):
    """How many coins TokenSage resolved to the same referent around now ("narrative heat")."""

    launches_1h: int = Field(
        description="Coins TokenSage resolved to this referent that launched in the last hour "
        "(this one included when it launched in that window)"
    )
    launches_6h: int
    launches_24h: int
    first_seen_at: datetime | None = Field(
        default=None,
        description="Launch time of the first coin with this referent in the last 7 days",
    )
    rank_24h: int | None = Field(
        default=None,
        description="This coin's place by launch time among the last 24 h launches on this "
        "referent (1 = the first); null when it launched before that window",
    )


class Referent(_Model):
    label: str = Field(
        description="What the coin refers to; when `generic` is true, a generic word for its "
        'kind ("frog", "crypto project") rather than a specific entity'
    )
    kind: ReferentKind = Field(
        description="famous_animal, meme, person, coin, event, concept, place, other; since "
        "rules 0.17.0 also animal (an animal, not a specific famous one), media (a film, game, "
        "show or franchise), project (a crypto or AI product, protocol or launchpad), "
        "object (food, an object, an abstract thing) and organization (a company, exchange "
        "or listed stock)"
    )
    desc: str | None = None
    source: str | None = None
    confidence: float = Field(
        ge=0,
        le=1,
        description="Bands: 0.3-0.49 only the kind is known (`generic`) or a weak guess; "
        "0.5-0.69 a named referent from one input; 0.7+ two or more independent inputs agree",
    )
    supported_by: list[str] = Field(
        default=[],
        description="The independent inputs that point at this referent (name, symbol, "
        "description, image, x, trend, db, copy_of), strongest first for a generic one. A "
        "ticker that spells the name counts as the name. copy_of = inherited from the coin "
        "this one copies",
    )
    generic: bool = Field(
        default=False,
        description="Only the kind is known: the name, ticker, image or description make it "
        "plain what sort of coin this is, but no specific entity was identified",
    )
    wave: ReferentWave | None = Field(
        default=None,
        description="Launch counts on this referent, by TokenSage's normalised referent "
        "(coins TokenSage has analysed only)",
    )


class Category(_Model):
    label: str = Field(
        description="A taxonomy label from GET /v1/meta: a top-level theme (crypto_native) or a "
        "sub-label (crypto_native/person). A sub-label also lifts its parent, except "
        "crypto_native/paired_ecosystem. crypto_native's sub-labels say what kind of crypto "
        "thing the coin is about: slang, person, chain_or_coin, company, trading, tech, "
        "launchpad, pumpfun_meta, cto, utility_claim, paired_ecosystem. New labels are added "
        "over time"
    )
    confidence: float = Field(ge=0, le=1)
    inputs: list[str] | None = Field(
        default=None,
        description="The independent inputs that agree on this label, strongest first: name, "
        "symbol, description, image, x, trend, db, copy_of. A ticker that spells the name "
        "counts as the name. More agreeing inputs make the label more trustworthy",
    )
    wave_1h: int | None = Field(
        default=None,
        description="Coins TokenSage analysed in the last hour that carry this label (top-level "
        "document categories only; coins TokenSage has analysed only)",
    )


class OriginalMarket(_Model):
    """The copied coin's bonding curve when this coin was read."""

    complete: bool | None = None
    curve_progress: float | None = Field(default=None, ge=0, le=1)
    graduated_pool: str | None = None
    as_of: datetime | None = Field(
        default=None, description="When the copied coin's curve was read"
    )


LineageKind = Literal["original", "early_copy", "copy", "late_copy", "reference", "unknown"]


class Lineage(_Model):
    """Which copy this is, and of what: the strongest copy relation in one place."""

    kind: LineageKind = Field(
        description="original: no earlier coin with its name, ticker or logo in the copycat "
        "window. early_copy: rank <= 3 and the original launched less than 6 h earlier. "
        "late_copy: rank > 10, or the original launched more than 24 h earlier. copy: any "
        "other copy. reference: builds on an established coin (copy_of recent false). "
        "unknown: no launch time"
    )
    of_mint: str | None = None
    of_name: str | None = None
    of_ticker: str | None = None
    of_created_at: datetime | None = None
    match: list[Literal["name", "ticker", "image"]] = []
    rank: int | None = Field(
        default=None,
        description="Place by launch time among coins sharing its name or ticker within "
        "window_hours either side (as copy_of[].rank); for a logo-only copy, among the earlier "
        "coins with a near-identical logo",
    )
    rank_of: int | None = None
    window_hours: int | None = None
    siblings_1h: int | None = Field(
        default=None,
        description="Coins with the same name, ticker or a near-identical logo launched in the "
        "hour up to this coin's launch, this one included",
    )
    siblings_6h: int | None = None
    siblings_24h: int | None = None
    logo_reuse_24h: int | None = Field(
        default=None,
        description="Other coins whose logo is a near-duplicate of this one's, launched in the "
        "24 h up to this coin's launch",
    )
    logo_first_seen_at: datetime | None = Field(
        default=None,
        description="Launch time of the first coin TokenSage saw with this logo (any "
        "near-duplicate), within the logo scan window (7 d)",
    )


class CopyOf(_Model):
    ticker: str | None = None
    name: str | None = None
    mint: str | None = None
    signals: list[str] = []
    created_at: datetime | None = Field(
        default=None, description="When the copied token launched (when known)"
    )
    recent: bool | None = Field(
        default=None,
        description="True: it launched within the copycat window (30 d) before this token, so "
        "this is a copy of a live coin (flag copycat). False: an established coin this one "
        "builds on (flag references_known_coin)",
    )
    rank: int | None = Field(
        default=None,
        description="This token's place by launch time among the coins with this name or "
        "ticker launched within rank_window_hours of it (1 = the earliest); set on the recent "
        "same-name copy",
    )
    rank_of: int | None = Field(
        default=None,
        description="How many coins with this name or ticker launched within rank_window_hours "
        'of it, this one included ("3rd of 41")',
    )
    rank_window_hours: int | None = None
    original_age_s: int | None = Field(
        default=None, description="Seconds from the copied coin's launch to this coin's"
    )
    original_market: OriginalMarket | None = Field(
        default=None, description="The copied coin's bonding curve at the time of this read"
    )
    match: list[Literal["name", "ticker", "image"]] = Field(
        default=[], description="Which of this coin's inputs match the copied coin"
    )
    image_distance: int | None = Field(
        default=None,
        description="pHash Hamming distance between the two logos, when they were compared",
    )


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
    basis: list[
        Literal[
            "profile_name", "profile_bio", "profile_image", "post_text", "post_image", "cashtag"
        ]
    ] = Field(
        default=[],
        description="What the fit rests on. A profile matched only by its own name, handle "
        "or avatar (no bio match) on an account not older than the token by a day is capped "
        "below about_this_coin",
    )


class XQuoted(_Model):
    """A tweet the linked tweet points at (full depth): the one it quotes (x.quoted) or the
    one it replies to (x.replied_to). Often the real narrative: a launch post that quotes
    or answers someone else's earlier post."""

    id: str
    url: str | None = None
    status: Literal["ok", "deleted", "failed"] = "ok"
    author: XAuthor | None = None
    text: str | None = None
    created_at: datetime | None = None
    predates_token_by_s: int | None = Field(
        default=None, description="Seconds the post predates the token; negative if after"
    )


class XAccount(_Model):
    """An account involved in the linked post. Its display name and handle are read like
    the post text (a reply to @elonmusk points at Elon Musk)."""

    role: Literal["author", "quoted_author", "replied_to_author", "mentioned"]
    handle: str | None = None
    name: str | None = Field(default=None, description="Display name, when known")
    followers: int | None = None
    verified_type: str | None = None


class XLinkAccount(_Model):
    """The account behind the link: the linked profile, or the linked post's author."""

    handle: str | None = None
    created_at: datetime | None = None
    age_at_launch_s: int | None = Field(
        default=None,
        description="Seconds from the account's creation to the token's; negative if the "
        "account is younger than the token",
    )
    posts_total: int | None = Field(default=None, description="Posts on the account, when known")
    posts_about_coin: int | None = Field(
        default=None, description="Posts about the coin; null until a source provides counts"
    )
    name_changes: int | None = None
    verified_type: str | None = None
    made_for_coin: bool = Field(
        default=False,
        description="Created less than a day before the token (or after it) with a handle or "
        "display name that is the coin's name or ticker",
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
    reuse_rank: int | None = Field(
        default=None,
        description="This coin's place by launch time among the analysed coins linking the "
        "same post/profile/community (1 = the first)",
    )
    reuse_first_at: datetime | None = Field(
        default=None, description="Launch time of the first analysed coin to link it"
    )
    fetch_source: str | None = None
    status: Literal["ok", "deleted", "suspended", "not_fetched", "failed", "none"] = "none"
    quoted: XQuoted | None = Field(
        default=None, description="Present when the linked tweet is a quote tweet (full depth)"
    )
    replied_to: XQuoted | None = Field(
        default=None, description="Present when the linked tweet is a reply (full depth)"
    )
    accounts: list[XAccount] = Field(
        default=[],
        description="Accounts involved: author (or linked profile), quoted and replied-to "
        "authors, @mentions (full depth)",
    )
    match: XMatch | None = Field(
        default=None,
        description="Post/profile vs token comparison (full depth, tweet or profile links)",
    )
    account: XLinkAccount | None = Field(
        default=None,
        description="The account behind the link (full depth, when the author or profile is known)",
    )
    credibility: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="How much the account behind the link is worth, apart from whether it "
        "matches: age at launch, followers, posts, verification, renames, link reuse "
        "(uncalibrated until Phase 6). Context about the account: since rules 0.23.0 renames, "
        "a made-for-coin account and late reuse cost little, and nothing else in the read "
        "depends on this score",
    )


class TrendTerm(_Model):
    term: str
    spike: float | None = Field(
        default=None, description="Wikipedia only: the day's views over the usual (median)"
    )
    source: str = Field(description="wikipedia, google_trends, x_trends, news or bluesky")
    headline: str | None = Field(
        default=None,
        description="A news headline about the term, the story behind a Google Trends search, "
        "or (bluesky) the most liked matching post",
    )
    score: float | None = Field(
        default=None,
        description="Strength of this trend, 0-1: Wikipedia by spike (30x = 1), Google Trends "
        "by search count (100 = 0, 100,000 = 1), news by headline count (8 = 1), X trends by "
        "rank and hours listed (#1 for 12 h = 1), Bluesky by posts in 24 h (25 = 1); halved "
        "for perennially popular articles",
    )
    seen_at: datetime | None = Field(
        default=None,
        description="How fresh the hit is: the UTC day of the Wikipedia spike (day "
        "granularity), when Google Trends or X first listed it, or the newest matching "
        "news headline or Bluesky post",
    )
    matched_on: str | None = Field(
        default=None,
        description="What matched: name, symbol, description or x (the coin's own text), "
        "referent (the referent it resolves to) or alias (an alias of that referent)",
    )
    partial: bool | None = Field(
        default=None,
        description="true when the coin's one-word name is one word of a longer trending "
        'label ("Leoncio" of "Leoncio Gomez"), not the whole label (since rules 0.18.0)',
    )
    searches: int | None = Field(
        default=None, description="Google Trends only: Google's approximate search count"
    )
    rank: int | None = Field(
        default=None, description="X trends only: best position on X's trending list (1-50)"
    )
    hours: int | None = Field(
        default=None,
        description="X trends only: hourly trending lists it was on in the last 24 h (any region)",
    )
    posts: int | None = Field(
        default=None,
        description="Bluesky only: posts naming it in the last 24 h (from at least 3 accounts; "
        "coin chatter excluded; at most 100)",
    )


class TrendSource(_Model):
    source: str = Field(description="wikipedia, google_trends, x_trends, news or bluesky")
    status: Literal["ok", "stale", "failed", "skipped", "unavailable"] = Field(
        description="ok: the source had current data; stale: only old data (see detail); "
        "failed: the lookup failed and nothing was cached; skipped: not looked up for this "
        "coin (news, bluesky: the name is too generic to search, see detail); unavailable: no "
        "data loaded at all"
    )
    as_of: datetime | None = Field(
        default=None, description="The newest data the source gave (Wikipedia: its UTC day)"
    )
    terms: int | None = Field(
        default=None,
        description="Trending terms the source contributed (news: relevant headlines found; "
        "bluesky: matching posts in the last 24 h)",
    )
    detail: str | None = None


class Trend(_Model):
    matched: bool = False
    score: float | None = Field(
        default=None,
        description="Strength of the strongest matching trend, 0-1 (full depth; 0 when "
        "nothing matched)",
    )
    terms: list[TrendTerm] = []
    sources: list[TrendSource] = Field(
        default=[], description="Per-source status of the trend lookups (full depth)"
    )


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
    where: str | None = Field(
        default=None,
        description="Which input it came from: name, symbol, description, image, x, trend, "
        "chain, db, copy_of (inherited from the original this coin copies)",
    )


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
    main_category: Category | None = Field(
        default=None,
        description="The coin's main category: its strongest top-level theme from "
        "categories[] (animal, celebrity, ...). derivative is the relation to another coin, "
        "not a theme: it stays in categories[] and copy_of/lineage, and is the main category "
        "only when the coin has no theme at all; so is regional_language (since rules 0.21.0). "
        "null when categories[] has no top-level label (since rules 0.20.0)",
    )
    ticker_explanation: str | None = None
    copy_of: list[CopyOf] = []
    lineage: Lineage | None = None
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
            "Why the item has no analysis: the CA is invalid; status 'failed' with "
            "'quota_exceeded' / 'overloaded' when that item was rejected (others still "
            "queue); status 'failed' with 'token_not_found' / 'not_a_token_mint' / "
            "'not_pumpfun' when its analysis failed for good recently; or status "
            "'failed' with the analyzer's error when its job failed"
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


def stored_analysis(doc: dict[str, Any]) -> Analysis:
    """A stored analysis document as the API serves it. The API and the worker deploy
    separately, so a document written by a newer worker can carry fields or enum values this
    process does not know yet: v1 only ever adds them. An unknown field is dropped, and an
    unknown value of an open enum (creator_fee.destination, referent.kind, ...) is served as
    that enum's catch-all ("unknown", else "other") rather than failing the request. Any
    other mismatch still raises."""
    try:
        return Analysis.model_validate(doc)
    except ValidationError as e:
        errs = e.errors()
        if not errs or any(not _tolerated(x) for x in errs):
            raise
        doc = copy.deepcopy(doc)
        for x in errs:
            *path, last = x["loc"]
            node: Any = doc
            for part in path:
                node = node[part]
            if x["type"] == "extra_forbidden":
                if isinstance(node, dict):
                    node.pop(last, None)
            else:
                node[last] = _catch_all(x)
        return Analysis.model_validate(doc)


def _catch_all(err: Any) -> str | None:
    expected = str((err.get("ctx") or {}).get("expected") or "")
    for v in ("unknown", "other"):
        if f"'{v}'" in expected:
            return v
    return None


def _tolerated(err: Any) -> bool:
    if err["type"] == "extra_forbidden":
        return True
    return err["type"] == "literal_error" and _catch_all(err) is not None
