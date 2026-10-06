"""S7b (full depth): does the linked X post / profile actually match the token?

The blended read (pipeline) mixes the post text into the coin's meaning. This stage keeps
them apart and compares: name and ticker against the post text, post images against the
logo, and what the post is about (the engine run on the post alone) against what the token
name, ticker and image say on their own. The result is a 0-1 `fit` and a verdict.

Weights below are hand-set, not yet fitted (Phase 6 calibration).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz
from rapidfuzz.distance import Levenshtein

from tokensage.engine import image as image_stage
from tokensage.engine.aggregate import Aggregated
from tokensage.engine.context import Normalized
from tokensage.engine.knowledge import Knowledge
from tokensage.engine.normalize import normalize
from tokensage.engine.segment import common_words
from tokensage.sources.x import ProfileData, TweetData

MAX_POST_CHARS = 600
REFERENT_MIN = 0.45  # the post's referent must be this confident to count
TOKEN_REFERENT_MIN = 0.3  # the token's own (name/ticker/image) referent is often weaker
# Categories too generic to say two things are about the same subject.
GENERIC_CATEGORIES = ("derivative", "crypto_native", "unknown")
CATEGORY_MIN = 0.3
# Words that carry no identity on their own when matching a name inside a post.
NAME_STOP = {
    "the", "a", "an", "of", "on", "in", "and", "to", "for", "is", "it", "my", "your",
    "coin", "token", "inu", "official", "sol", "solana", "pump", "fun", "ai", "x",
}  # fmt: skip
CASHTAG = re.compile(r"\$([A-Za-z][A-Za-z0-9]{0,14})\b")
HASHTAG = re.compile(r"#([A-Za-z][A-Za-z0-9_]{0,30})\b")
WORD = re.compile(r"[A-Za-z0-9]+")

# Contribution of each agreeing signal to the noisy-OR fit.
W_NAME, W_TICKER_CASH, W_IMAGE, W_REFERENT, W_CATEGORY = 0.85, 0.55, 0.9, 0.7, 0.35
FIT_ABOUT, FIT_RELATED = 0.6, 0.2


@dataclass
class MediaHash:
    url: str
    status: str  # ok | failed
    phash: int | None = None
    phash_mirror: int | None = None
    error: str | None = None


@dataclass
class FieldMatch:
    score: float
    how: str
    detail: str


@dataclass
class ImageMatch:
    score: float
    best_distance: int | None
    media_checked: int
    detail: str
    best_url: str | None = None


@dataclass
class ReferentMatch:
    x_label: str | None
    x_kind: str | None
    agrees: bool | None
    confidence: float


@dataclass
class XMatch:
    name: FieldMatch
    ticker: FieldMatch
    image: ImageMatch
    referent: ReferentMatch
    x_categories: list[tuple[str, float]] = field(default_factory=list)
    fit: float = 0.0
    verdict: str = "unknown"  # about_this_coin | related | unrelated | unknown
    content_fetched: bool = False


# ----------------------------------------------------------------- the post's text


def post_text(tweet: TweetData | None, profile: ProfileData | None) -> str | None:
    """What the link shows: the post text (plus the post it quotes), or for a profile link
    the display name, handle and bio."""
    parts: list[str] = []
    if tweet is not None and tweet.status == "ok":
        parts.append(tweet.text or "")
        if tweet.quoted is not None and tweet.quoted.status == "ok" and tweet.quoted.text:
            parts.append(tweet.quoted.text)
    elif profile is not None and profile.status == "ok":
        parts += [profile.name or "", profile.handle or "", profile.description or ""]
    text = "\n".join(p for p in parts if p).strip()
    return text[:MAX_POST_CHARS] or None


# ----------------------------------------------------------------- name


def match_name(n: Normalized, text: str | None) -> FieldMatch:
    if not text:
        return FieldMatch(0.0, "none", "no post text to compare")
    if not n.name_compact:
        return FieldMatch(0.0, "none", "token has no readable name")
    raw_name = " ".join(n.name_raw.split()).casefold()
    if len(raw_name) >= 3 and raw_name in " ".join(text.split()).casefold():
        return FieldMatch(1.0, "exact", f"the post contains the name '{n.name_raw.strip()}'")
    pn = normalize(text, None, None)  # same folding as names: homoglyphs, leet, emoji, camel
    if len(n.name_compact) >= 4 and n.name_compact in pn.name_compact:
        return FieldMatch(
            0.9, "normalized", f"'{n.name_compact}' appears in the post once normalised"
        )
    informative = [t for t in n.name_tokens if len(t) > 1 and t not in NAME_STOP]
    post_words = set(pn.name_tokens) | set(pn.emoji_keywords) | set(pn.desc_tokens)
    if informative:
        hits = [t for t in informative if t in post_words]
        if hits:
            frac = len(hits) / len(informative)
            score = 0.8 if frac == 1 else round(0.6 * frac, 3)
            return FieldMatch(
                score,
                "segment",
                f"{len(hits)}/{len(informative)} name words in the post: " + ", ".join(hits[:5]),
            )
    if len(n.name_compact) >= 5 and pn.name_compact:
        r = fuzz.partial_ratio(n.name_compact, pn.name_compact)
        if r >= 88:
            return FieldMatch(
                round(0.45 * (r / 100), 3), "fuzzy", f"near-match of the name (similarity {r:.0f})"
            )
    return FieldMatch(0.0, "none", "the post does not mention the token name")


# ----------------------------------------------------------------- ticker


def match_ticker(
    n: Normalized, text: str | None, k: Knowledge, handle: str | None = None
) -> FieldMatch:
    t = (n.ticker or "").upper()
    if not t:
        return FieldMatch(0.0, "none", "token has no ticker")
    if not text and not handle:
        return FieldMatch(0.0, "none", "no post text to compare")
    text = text or ""
    cash = {c.upper() for c in CASHTAG.findall(text)}
    if t in cash:
        return FieldMatch(1.0, "cashtag", f"the post names ${t}")
    if t in {h.upper() for h in HASHTAG.findall(text)}:
        return FieldMatch(0.85, "hashtag", f"the post tags #{t}")
    if len(t) >= 3:
        # A bare word: ALL-CAPS counts anywhere; lowercase only if it is not an everyday word
        # (otherwise $DOG would 'match' every post containing 'dog').
        everyday = t.lower() in common_words(k)
        for w in WORD.findall(text):
            if w.upper() == t and (w.isupper() or not everyday):
                return FieldMatch(0.7, "bare", f"the post uses the ticker word '{w}'")
        if handle and handle.upper() == t:
            return FieldMatch(0.7, "bare", f"the account handle is @{handle}")
    if len(t) >= 4:
        for c in cash:
            if Levenshtein.distance(c, t) == 1:
                return FieldMatch(0.35, "fuzzy", f"the post names ${c}, one letter off ${t}")
    return FieldMatch(0.0, "none", "the post does not mention the ticker")


# ----------------------------------------------------------------- image


def match_image(
    logo: image_stage.ImageFeatures | None, media: list[MediaHash], k: Knowledge
) -> ImageMatch:
    ok = [m for m in media if m.status == "ok" and m.phash is not None]
    if not media:
        return ImageMatch(0.0, None, 0, "the post has no images")
    if not ok:
        return ImageMatch(0.0, None, 0, f"none of {len(media)} post image(s) could be fetched")
    if logo is None:
        return ImageMatch(0.0, None, len(ok), "no token logo to compare")
    same = int(k.scoring.get("logo_phash_same", 8))
    edited = int(k.scoring.get("logo_phash_edited", 14))
    best: tuple[int, str] | None = None
    for m in ok:
        assert m.phash is not None
        d = image_stage.hamming(logo.phash, m.phash)
        if m.phash_mirror is not None:
            d = min(d, image_stage.hamming(logo.phash, m.phash_mirror))
        if best is None or d < best[0]:
            best = (d, m.url)
    assert best is not None
    d, url = best
    if d <= same:
        score, how = 1.0, "the same image"
    elif d <= edited:
        score, how = 0.8, "an edited copy of the logo"
    elif d <= edited + 6:
        score, how = 0.35, "loosely similar to the logo"
    else:
        score, how = 0.0, "not similar to the logo"
    return ImageMatch(
        score, d, len(ok), f"best of {len(ok)} post image(s) is {how} (distance {d})", url
    )


def is_image_match(m: ImageMatch, k: Knowledge) -> bool:
    edited = int(k.scoring.get("logo_phash_edited", 14))
    return m.best_distance is not None and m.best_distance <= edited


# ----------------------------------------------------------------- referent / categories


def _label_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.casefold())


def _same_referent(a: str, b: str) -> bool:
    ka, kb = _label_key(a), _label_key(b)
    return bool(ka and kb) and (ka == kb or ka in kb or kb in ka)


def compare_meaning(
    token: Aggregated | None, post: Aggregated | None
) -> tuple[ReferentMatch, list[tuple[str, float]], float]:
    """(referent agreement, the post's own categories, category agreement 0-1)."""
    xr = post.referent if post and post.referent and post.referent.score >= REFERENT_MIN else None
    tr = (
        token.referent
        if token and token.referent and token.referent.score >= TOKEN_REFERENT_MIN
        else None
    )
    agrees: bool | None = None
    if xr and tr:
        agrees = _same_referent(xr.label, tr.label)
    ref = ReferentMatch(
        x_label=xr.label if xr else None,
        x_kind=xr.kind if xr else None,
        agrees=agrees,
        confidence=round(xr.score, 3) if xr else 0.0,
    )
    x_cats = [(lbl, round(c, 3)) for lbl, c in (post.categories if post else []) if c >= 0.25][:5]

    def specific(lbl: str) -> bool:
        return not lbl.startswith(GENERIC_CATEGORIES)

    t_cats = [lbl for lbl, c in (token.categories if token else []) if c >= CATEGORY_MIN]
    p_cats = [lbl for lbl, c in x_cats if c >= CATEGORY_MIN]
    cat_agree = 0.0
    if agrees is not False:  # two different animals share "animal"; that is not agreement
        for a in filter(specific, t_cats):
            for b in filter(specific, p_cats):
                if a == b:
                    cat_agree = max(cat_agree, 1.0 if "/" in a else 0.4)
                elif a.split("/")[0] == b.split("/")[0]:
                    cat_agree = max(cat_agree, 0.25)
    return ref, x_cats, cat_agree


# ----------------------------------------------------------------- combine


def combine(
    name: FieldMatch,
    ticker: FieldMatch,
    image: ImageMatch,
    referent: ReferentMatch,
    cat_agree: float,
    content_fetched: bool,
) -> tuple[float, str]:
    ticker_w = W_TICKER_CASH if ticker.how in ("cashtag", "hashtag") else W_TICKER_CASH * 0.8
    parts = [
        W_NAME * name.score,
        ticker_w * ticker.score,
        W_IMAGE * image.score,
        W_REFERENT * min(1.0, referent.confidence / 0.6) if referent.agrees else 0.0,
        W_CATEGORY * cat_agree,
    ]
    miss = 1.0
    for p in parts:
        miss *= 1.0 - max(0.0, min(1.0, p))
    fit = 1.0 - miss
    if referent.agrees is False:
        fit *= 0.5  # the post is confidently about something else
    fit = round(max(0.0, min(1.0, fit)), 3)
    if not content_fetched:
        return fit, "unknown"
    if fit >= FIT_ABOUT:
        return fit, "about_this_coin"
    if fit >= FIT_RELATED:
        return fit, "related"
    return fit, "unrelated"


def assess(
    n: Normalized,
    tweet: TweetData | None,
    profile: ProfileData | None,
    logo: image_stage.ImageFeatures | None,
    media: list[MediaHash],
    token_meaning: Aggregated | None,
    post_meaning: Aggregated | None,
    k: Knowledge,
) -> XMatch:
    text = post_text(tweet, profile)
    handle = profile.handle if profile is not None and profile.status == "ok" else None
    name = match_name(n, text)
    ticker = match_ticker(n, text, k, handle)
    image = match_image(logo, media, k)
    ref, x_cats, cat_agree = compare_meaning(token_meaning, post_meaning)
    fetched = bool(text) or image.media_checked > 0
    fit, verdict = combine(name, ticker, image, ref, cat_agree, fetched)
    return XMatch(
        name=name,
        ticker=ticker,
        image=image,
        referent=ref,
        x_categories=x_cats,
        fit=fit,
        verdict=verdict,
        content_fetched=fetched,
    )
