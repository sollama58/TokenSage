"""Shared engine types: the evidence record every stage emits, and the normalized view."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Where = Literal["name", "symbol", "description", "image", "x", "trend", "chain", "db", "copy_of"]


@dataclass
class Ev:
    """One piece of evidence. Stages append these; the aggregator turns them into scores."""

    kind: str  # rule family, e.g. "lexicon", "known_coin", "marker", "emoji", "image_hash"
    label: str  # taxonomy label, or "referent", or "flag:<code>"
    weight: float  # 0..1 contribution
    detail: str  # human-readable why
    source: str  # where the knowledge came from, e.g. "slang:wif", "known_coins:PNUT"
    where: Where = "name"
    url: str | None = None
    referent: ReferentCandidate | None = None


@dataclass
class ReferentCandidate:
    label: str
    kind: str  # famous_animal | meme | person | coin | event | concept | place | other
    desc: str | None
    source: str
    score: float
    categories: list[str] = field(default_factory=list)
    surface: str | None = None  # the words that matched, when the match came from text


@dataclass
class Marker:
    code: str  # e.g. "version:2"
    kind: str | None  # sequel | copycat | template_family | None
    weight: float
    text: str


@dataclass
class Normalized:
    name_raw: str
    symbol_raw: str
    description_raw: str
    name_clean: str  # folded, lowercase, punctuation to spaces
    name_tokens: list[str]  # best segmentation
    name_compact: str  # letters+digits only
    ticker: str  # cleaned symbol, upper, $ stripped
    ticker_base: str  # affixes stripped
    ticker_affixes: list[str]
    markers: list[Marker]
    emoji: list[str]
    emoji_keywords: list[str]
    obfuscation: list[str]  # homoglyph, zero_width, fullwidth, leet, small_caps, repeated_letters
    scripts: list[str]  # non-Latin scripts present, e.g. ["Han"]
    desc_clean: str
    desc_tokens: list[str]
    dollar_mentions: list[str]  # $TICKER mentions in the description
    leet_decoded: str | None = None
    cjk_gloss: list[tuple[str, str]] = field(default_factory=list)  # (Han word, English)
    name_pinyin: list[str] = field(default_factory=list)  # set only when Han was translated
