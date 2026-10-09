"""The engine pipeline at basic depth. Pure and synchronous: all I/O results come in through
EngineInput, so golden tests run without a database or network."""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from tokensage.engine import (
    embed,
    gazetteer,
    known_coins,
    lexicon,
    meta,
    ocr,
    pairing,
    segment,
    ticker,
    trends,
    wikilookup,
    xcred,
    xmatch,
    xsignals,
)
from tokensage.engine import image as image_stage
from tokensage.engine import lineage as lineage_stage
from tokensage.engine import vision as vision_stage
from tokensage.engine.aggregate import (
    ACCOUNT_SIZE_KINDS,
    NO_PARENT,
    THEME_ORDER,
    TREND_SOURCES,
    Aggregated,
    aggregate,
    channel,
    is_theme,
    main_category,
)
from tokensage.engine.context import Ev, Normalized, ReferentCandidate
from tokensage.engine.gazetteer import Gazetteer
from tokensage.engine.knowledge import Entity, Knowledge, KnownCoin, SlangTerm, load_knowledge
from tokensage.engine.normalize import normalize
from tokensage.engine.render_summary import category_phrase, summarize
from tokensage.sources.x import ProfileData, TweetData
from tokensage.versions import RULES_VERSION as RULES_VERSION

_WORDNET_LABEL = {
    "food": "food_object_abstract",
    "vehicle": "food_object_abstract",
    "body_part": "food_object_abstract",
    "emotion": "food_object_abstract",
}
_WORDNET_WEIGHT = {"food": 0.45, "vehicle": 0.35, "body_part": 0.3, "emotion": 0.3}


@dataclass
class SameNameToken:
    mint: str
    name: str | None
    symbol: str | None
    created_at: datetime | None
    source: str  # db | pumpfun_search | dexscreener


@dataclass
class PriorRead:
    """TokenSage's stored read of another coin (the one this coin copies)."""

    categories: list[tuple[str, float]] = field(default_factory=list)
    referent: ReferentCandidate | None = None


@dataclass
class DbContext:
    x_reuse_count: int = 0
    # this coin's place among the coins linking the same post/profile, by launch time
    # (1 = the first), and when the first of them launched
    x_reuse_rank: int | None = None
    x_reuse_first_at: datetime | None = None
    creator_token_count: int = 0
    same_name: list[SameNameToken] = field(default_factory=list)
    image_candidates: list[image_stage.Candidate] = field(default_factory=list)
    extra_coins: list[KnownCoin] = field(default_factory=list)
    copycat_window_days: int = 30
    # the Wikidata gazetteer; None = the packaged snapshot (the cron's table, when loaded)
    gazetteer: Gazetteer | None = None
    # name-word counts around the launch, for the current-meta signal (engine/meta.py)
    meta_counts: meta.MetaCounts | None = None
    # the day's most-traded pump.fun tokens (top_volume table)
    top_volume: list[meta.TopVolume] = field(default_factory=list)
    # stored reads of the coins this one may copy (lineage.candidate_mints), by mint
    originals: dict[str, PriorRead] = field(default_factory=dict)


@dataclass
class EngineInput:
    mint: str
    name: str | None
    symbol: str | None
    description: str | None
    image_bytes: bytes | None
    created_at: datetime | None
    x_kind: str | None = None  # from XRef.kind
    x_object_time: datetime | None = None
    ctx: DbContext = field(default_factory=DbContext)
    pair: pairing.PairInput | None = None  # the bonding curve's quote token
    # full depth only
    x_url_handle: str | None = None
    tweet: TweetData | None = None
    profile: ProfileData | None = None
    ocr_lines: list[ocr.OcrLine] | None = None  # pre-computed (cached) OCR; None = run it
    ocr_error: str | None = None  # the error of an OCR the analyzer ran before the engine
    run_ocr: bool = False
    trend_index: trends.TrendIndex | None = None
    x_media: list[xmatch.MediaHash] | None = None  # hashed post images / profile avatar
    # the logo's hashes when they come from cache instead of image_bytes
    logo_features: image_stage.ImageFeatures | None = None
    # Wikipedia articles found for names nothing in the gazetteer knows (full depth)
    wiki_refs: list[wikilookup.WikiRef] | None = None
    # the coin's name found in recent news headlines (trends.news_hit), full depth
    news_hits: list[trends.TrendHit] | None = None
    # optional sentence encoder (ENABLE_EMBED); None = no embedding guesses
    encoder: embed.Encoder | None = None
    # logo labels from the local vision model (ENABLE_CLIP, full depth); None = not run
    vision: vision_stage.VisionResult | None = None


@dataclass
class FlagOut:
    code: str
    severity: str
    detail: str


@dataclass
class EngineOutput:
    normalized: Normalized
    agg: Aggregated
    ticker_explanation: str
    copy_of: list[dict]
    image: image_stage.ImageResult
    flags: list[FlagOut]
    summary: str
    caveats: list[str]
    evidence: list[Ev]
    depth: str = "basic"
    ocr_lines: list[ocr.OcrLine] = field(default_factory=list)
    ocr_error: str | None = None
    x: xsignals.XAssessment | None = None
    trend_hits: list[trends.TrendHit] = field(default_factory=list)
    # per-source status of the trend lookups (filled in by the analyzer, which ran them)
    trend_sources: list[trends.SourceStatus] = field(default_factory=list)
    x_match: xmatch.XMatch | None = None
    pair: pairing.PairAssessment | None = None
    lineage: lineage_stage.Lineage | None = None
    x_account: xcred.AccountFacts | None = None
    x_credibility: float | None = None
    # the referent as reported: kind, banded confidence and the inputs behind it
    referent_read: ReferentRead | None = None
    vision: vision_stage.VisionResult | None = None


# ----------------------------------------------------------------- evidence producers


def _normalization_evidence(n: Normalized, k: Knowledge) -> list[Ev]:
    evs: list[Ev] = []
    for m in n.markers:
        if m.kind:
            evs.append(
                Ev(
                    kind="marker",
                    label=f"derivative/{m.kind}",
                    weight=m.weight,
                    detail=f"name carries the marker '{m.text}' ({m.code})",
                    source=f"templates:{m.code}",
                )
            )
    # the description's emoji are the description's evidence, not the name's
    emoji = [(kw, "name") for kw in n.emoji_keywords]
    emoji += [(kw, "description") for kw in n.desc_emoji_keywords]
    for kw, where in emoji:
        for cls in lexicon.wordnet_classes_for(kw, k):
            label = _WORDNET_LABEL.get(cls, cls)
            evs.append(
                Ev(
                    kind="emoji",
                    label=label,
                    weight=0.35,
                    detail=f"emoji keyword '{kw}' is a {cls.split('/')[-1]}",
                    source="cldr",
                    where=where,  # type: ignore[arg-type]
                    surface=kw,
                )
            )
    if n.scripts:
        evs.append(
            Ev(
                kind="script",
                label="regional_language",
                weight=0.6,
                detail=f"name uses the {', '.join(n.scripts)} script"
                + "".join(f"; {h} means '{e}'" for h, e in n.cjk_gloss[:4]),
                source="unicode",
            )
        )
    return evs


# An extra lexicon pass: (where, text, factor) or (where, text, factor, note, parts). A note
# marks a pass over an account's names: its details say so, its referents are scaled by the
# factor, and `parts` ("elon musk|elonmusk") are the display name and handle as words.
Pass = tuple[str, str, float] | tuple[str, str, float, str, str]


def _kept_coin_words(matches: list[known_coins.CopyMatch], n: Normalized) -> set[str]:
    """Name words the known-coin stage reads as a coin ("hawk" of "Hawk" $HAWK: Hawk Tuah),
    so the lexicon does not also read them as dictionary words."""
    out: set[str] = set()
    for m in matches:
        if m.surface:
            out.add(m.surface.lower())
        coin_words = {w for s in m.coin.surfaces for w in s.lower().split()}
        out.update(t for t in n.name_tokens if t in coin_words)
    return out


def _lexicon_evidence(
    n: Normalized,
    k: Knowledge,
    extra_passes: list[Pass] | None = None,
    gaz: Gazetteer | None = None,
    coin_surfaces: frozenset[str] = frozenset(),
) -> list[Ev]:
    evs: list[Ev] = []
    name_text = " ".join(n.name_tokens)
    # The compact form catches brand names the camelCase split breaks apart ("DeepSeek")
    # and multi-word slang stored compact ("diamondhands"); the ticker is a signal too.
    singular = _singular(n.name_tokens, k)
    passes: list[Pass] = [
        ("name", name_text, 1.0),
        ("name", n.name_compact if n.name_compact != name_text else "", 1.0),
        ("name", singular if singular != name_text else "", 1.0),
        ("symbol", n.ticker.lower() if len(n.ticker) >= 3 else "", 0.8),
        ("description", n.desc_clean, 1.0),
        *(extra_passes or []),
    ]
    seen_hits: set[tuple[str, str, str]] = set()
    # words the name was actually written with (before compound splitting): a 1-2 letter
    # match such as "xi" only counts when it was its own word, not a piece of "robotaxi"
    written = set(n.name_clean.split())
    named_words: set[str] = set()  # words of entity/slang matches in the name
    head = _head_word(n)
    head_named = False  # an entity in the name covers its head word
    for where, text, factor, *rest in passes:
        if not text:
            continue
        note = rest[0] if rest else None
        parts = rest[1] if len(rest) > 1 else text
        first_new = len(evs)
        # the Wikidata gazetteer never reads a ticker: short punny tickers ($KIRK, $SPEED)
        # would match surnames and stage names far more often than they mean them
        g = gaz if where != "symbol" else None
        hits = lexicon.find(text, k, g, name_pass=where == "name")
        if note:
            # An account's name is evidence only when the account *is* a known entity
            # (@elonmusk, "Donald Trump Jr"). Dictionary words and short aliases inside a
            # brand name ("American Eagle": a bird, "America") say nothing about the coin.
            hits = [h for h in hits if h.kind == "entity" and _names_account(h.surface, parts)]
        for h in hits:
            if where == "name" and len(h.surface) <= 2 and h.surface not in written:
                continue
            if where == "name" and h.kind != "wordnet" and _tail_of_word(h.surface, written):
                continue  # "iggy" in "Niggy": a name read out of the end of another word
            if where == "name" and (
                h.kind not in ("wordnet", "coin")
                or (h.kind == "coin" and h.surface.lower() in coin_surfaces)
            ):
                # a coin surface ("goat", "kitty") emits nothing here; when the known-coin
                # stage keeps the match ("Hawk" $HAWK is Hawk Tuah) the word is named and
                # its dictionary sense (a bird) is not the coin's; when that stage drops it
                # ("Goat Farm", a dictionary word as a modifier) the dictionary sense stays
                named_words.update(h.surface.split())
                if head and h.kind == "entity" and head in h.surface.replace(" ", ""):
                    head_named = True
            if h.kind == "wordnet" and set(h.surface.split()) <= named_words:
                continue  # a dictionary word inside a named match ("HAWK" of Hawk Tuah)
            if where == "symbol" and h.kind == "wordnet" and h.surface in n.name_tokens:
                continue  # the ticker repeats a name word: the same dictionary sense twice
            if where == "symbol" and h.kind == "wordnet" and _ticker_is_initials(n):
                continue  # $COD for "Call Of Duty" spells initials, not the fish
            if where == "symbol" and h.kind in ("entity", "slang") and head_named:
                continue  # the name says what it is; a punning ticker ($DOGE) is secondary
            hk = (where, h.surface, h.kind)
            if hk in seen_hits:
                continue
            seen_hits.add(hk)
            if h.kind == "slang":
                t = h.payload
                assert isinstance(t, SlangTerm)
                for cat in t.categories:
                    evs.append(
                        Ev(
                            kind="lexicon",
                            label=cat,
                            weight=t.weight * factor,
                            detail=f"'{h.surface}' = {t.meaning}",
                            source=f"slang:{t.term}",
                            where=where,  # type: ignore[arg-type]
                            surface=h.surface,
                            generic=_generic(h.surface, cat),
                        )
                    )
            elif h.kind == "entity":
                e = h.payload
                assert isinstance(e, Entity)
                ref = ReferentCandidate(
                    label=e.label,
                    kind=e.kind,
                    desc=e.desc,
                    source=e.evidence_source,
                    score=round(0.45 + 0.45 * e.popularity, 3),
                    categories=list(e.categories),
                    surface=h.surface,
                )
                for cat in e.categories:
                    if cat.startswith("tradfi/") and where not in ("name", "symbol"):
                        continue  # "NFTs on Robinhood Chain", "an NVIDIA GPU": not a stock coin
                    evs.append(
                        Ev(
                            kind="entity",
                            label=cat,
                            weight=round((0.5 + 0.35 * e.popularity) * factor, 3),
                            detail=f"'{h.surface}' refers to {e.label} ({e.desc})",
                            source=e.evidence_source,
                            where=where,  # type: ignore[arg-type]
                            referent=ref,
                            surface=h.surface,
                            generic=e.popularity < 0.8 and _generic(h.surface, cat),
                        )
                    )
                evs.append(
                    Ev(
                        kind="entity",
                        label="referent",
                        weight=ref.score,
                        detail=f"{e.label}: {e.desc}",
                        source=e.evidence_source,
                        where=where,  # type: ignore[arg-type]
                        referent=ref,
                    )
                )
            elif h.kind == "wordnet":
                cls = h.payload
                assert isinstance(cls, str)
                label = _WORDNET_LABEL.get(cls, cls)
                w = _WORDNET_WEIGHT.get(cls, 0.55 if cls != "animal/other" else 0.4)
                what = cls.replace("animal/", "").replace("_", " ")
                detail = f"'{h.surface}' is a {what}"
                if h.surface in k.name_words:
                    # "Ani", "Drake", "Kirk": more often someone's name than the animal
                    w *= k.scoring.get("name_word_factor", 0.5)
                    detail += " (or, as often, a given name)"
                evs.append(
                    Ev(
                        kind="wordnet",
                        label=label,
                        weight=round(w * factor, 3),
                        detail=detail,
                        source=f"wordnet:{cls}",
                        where=where,  # type: ignore[arg-type]
                        surface=h.surface,
                    )
                )
            # coin hits are handled by the known-coin stage (fuzzy + ticker aware)
        if note:
            scaled: set[int] = set()  # one entity's evidence rows share one candidate
            for ev in evs[first_new:]:
                ev.detail = f"{note}: {ev.detail}"
                if ev.referent is not None and id(ev.referent) not in scaled:
                    scaled.add(id(ev.referent))
                    ev.referent.score = round(ev.referent.score * factor, 3)
                if ev.referent is not None and ev.label == "referent":
                    ev.weight = ev.referent.score
    return evs


_WORD_SETS: dict[int, dict[str, set[str]]] = {}


def _word_sets(k: Knowledge) -> dict[str, set[str]]:
    """Word sets derived from the knowledge, built once per Knowledge object."""
    got = _WORD_SETS.get(id(k))
    if got is None:
        animals = {w for c, ws in k.wordnet.items() if c.startswith("animal/") for w in ws}
        got = {
            "animals": animals,
            "slang": {t for t, v in k.slang.items() if v.categories and v.kind != "marker"},
            "known": k.vocabulary() | animals,
        }
        _WORD_SETS.clear()  # a reloaded Knowledge replaces the old one
        _WORD_SETS[id(k)] = got
    return got


# Plurals whose stem is a different word ("does" is not deer).
_NOT_PLURAL = {"does", "goes", "news", "this", "plus", "yes", "has", "was", "its", "series"}


def _singular(tokens: list[str], k: Knowledge) -> str:
    """The name with plural words made singular when the singular is something the lexicon
    knows ("Ninja Pepes" -> pepe, "Sentients" -> sentient, "Bags" -> bag)."""
    known = _word_sets(k)["known"]
    out: list[str] = []
    for t in tokens:
        stem = t[:-1]
        if (
            len(t) >= 4
            and t.endswith("s")
            and not t.endswith("ss")
            and t not in _NOT_PLURAL
            and t not in known
            and stem in known
        ):
            t = stem
        out.append(t)
    return " ".join(out)


# Labels a single everyday word cannot carry alone: "GAME" is not The Game the rapper, "BOOT"
# not an AI agent, unless something else about the coin says so too.
GENERIC_LABELS = ("celebrity", "ai_agent", "political", "pop_culture")
# Leftovers in front of a name that still leave the name itself ("iTrump", "MrBeast").
_NAME_PREFIXES = {"i", "e", "x", "a", "mr", "my", "dr", "st", "lil"}


def _ticker_is_initials(n: Normalized) -> bool:
    """The ticker is the initials of a multi-word name (with or without its stop words)."""
    tokens = [t for t in n.name_tokens if t]
    if len(tokens) < 2:
        return False
    t = n.ticker.lower()
    main = [tok for tok in tokens if tok not in ("the", "a", "of", "in", "and")]
    return t in ("".join(tok[0] for tok in tokens), "".join(tok[0] for tok in main))


def _generic(surface: str, label: str) -> bool:
    """A one-word match on an everyday word (the 20k most frequent), for a label that word
    alone cannot carry (see GENERIC_LABELS)."""
    w = surface.strip()
    return (
        label.split("/")[0] in GENERIC_LABELS
        and " " not in w
        and len(w) >= 3
        and w in segment.common_words(load_knowledge())
    )


def _tail_of_word(surface: str, written: set[str]) -> bool:
    """The match is the end of a longer written word with only a stray letter or two in front
    ("iggy" of "niggy", "rump" of "frump"): that word is something else, not the name."""
    if " " in surface or surface in written:
        return False
    for w in written:
        if w != surface and w.endswith(surface):
            lead = w[: -len(surface)]
            if len(lead) <= 2 and lead not in _NAME_PREFIXES:
                return True
    return False


def _require_second_signal(evidence: list[Ev], k: Knowledge, symbol_is_name: bool) -> None:
    """A generic one-word match (Ev.generic) keeps its weight only when another signal agrees:
    the same top-level label from another word ("AI" beside "agents"), the same word in
    another input (a name "Agent" described as "your portfolio agent"), or a non-text source
    (logo, known coin, trend). Alone it drops below the reporting floor."""
    factor = k.scoring.get("generic_single_word_factor", 0.25)
    weak: dict[int, ReferentCandidate] = {}  # referents only weakened rows support
    kept: set[int] = set()
    weakened: list[Ev] = []
    for ev in evidence:
        if not ev.generic:
            if ev.referent is not None and ev.label != "referent":
                kept.add(id(ev.referent))
            continue
        top = ev.label.split("/")[0]
        agrees = any(
            o is not ev
            and o.label.split("/")[0] == top
            and o.kind not in ("wordnet", "emoji")
            and o.kind not in ACCOUNT_SIZE_KINDS
            and (
                o.surface is None
                or o.surface != ev.surface
                or channel(o, symbol_is_name) != channel(ev, symbol_is_name)
            )
            for o in evidence
        )
        if not agrees:
            ev.weight = round(ev.weight * factor, 3)
            ev.detail += " (one everyday word, nothing else agrees: weak)"
            weakened.append(ev)
            if ev.referent is not None:
                weak[id(ev.referent)] = ev.referent
        elif ev.referent is not None:
            kept.add(id(ev.referent))
    # "GAME" refers to The Game (rapper) no more than it is a celebrity coin
    for rid, ref in weak.items():
        if rid not in kept:
            ref.score = round(ref.score * factor, 3)
    for ev in evidence:
        if ev.label == "referent" and ev.referent is not None and id(ev.referent) in weak:
            ev.weight = ev.referent.score
    if symbol_is_name:
        # a ticker that spells the name repeats its everyday word ("Speed Demon" $SPEED): two
        # weakened rows for one word would add up inside the name channel and clear the floor
        name_rows = {(ev.surface, ev.label) for ev in weakened if ev.where == "name"}
        drop = {
            id(ev)
            for ev in weakened
            if ev.where == "symbol" and (ev.surface, ev.label) in name_rows
        }
        if drop:
            left = [ev for ev in evidence if id(ev) not in drop]
            backed = {
                id(ev.referent) for ev in left if ev.referent is not None and ev.label != "referent"
            }
            evidence[:] = [
                ev
                for ev in left
                if not (
                    ev.label == "referent"
                    and ev.where == "symbol"
                    and ev.referent is not None
                    and id(ev.referent) in weak
                    and id(ev.referent) not in backed
                )
            ]


# Three-letter animals that stand for a mascot inside a fused word ("Catler", "Pigcoin").
_SHORT_MASCOTS = {"cat", "dog", "ape", "pig", "cow", "bee", "owl", "fox"}


def compound_parts(n: Normalized, k: Knowledge, gaz: Gazetteer | None) -> list[tuple[str, str]]:
    """Animal and slang words fused into a name word the segmenter kept whole: "lambull" ->
    lamb, "nintendoge" -> doge, "catler" -> cat, "frogman" -> frog. (piece, word) pairs.

    A word nothing knows may hide any such piece at its start or end. A dictionary word
    ("cowboy", "category") only splits into a four-letter-plus piece and an everyday word of
    three letters or more ("frog" + "man"; never "fren" + "ch"), since most dictionary
    compounds are not about the animal."""
    animals, slang = _word_sets(k)["animals"], _word_sets(k)["slang"]
    everyday = segment.common_words(k)
    out: list[tuple[str, str]] = []
    for t in n.name_tokens:
        if len(t) < 6 or not t.isalpha() or lexicon.find(t, k, gaz):
            continue
        common = gazetteer.is_common(t)
        best: str | None = None
        for i in range(3, len(t) - 1):
            for piece, rest in ((t[:i], t[i:]), (t[i:], t[:i])):
                if len(piece) < 4 and piece not in _SHORT_MASCOTS:
                    continue
                if piece in lexicon.WORDNET_STOP or (piece not in animals and piece not in slang):
                    continue
                if (
                    len(rest) < 2
                    or common
                    and (len(piece) < 4 or len(rest) < 3 or rest not in everyday)
                ):
                    continue  # "french" is not "fren" + "ch": a 2-letter rest is no word
                if best is None or len(piece) > len(best):
                    best = piece
        if best:
            out.append((best, t))
    return out


def _compound_evidence(parts: list[tuple[str, str]], n: Normalized, k: Knowledge) -> list[Ev]:
    """Lexicon evidence for compound pieces, a little weaker than a word written alone."""
    if not parts:
        return []
    factor = k.scoring.get("compound_factor", 0.75)
    evs = _lexicon_evidence(
        dataclasses.replace(n, name_tokens=[], name_compact="", ticker="", desc_clean=""),
        k,
        [("name", " ".join(p for p, _ in parts), factor)],
    )
    whole = dict(parts)
    for ev in evs:
        if ev.surface in whole:
            ev.detail += f" (inside '{whole[ev.surface]}')"
    return evs


_DOMAIN = re.compile(
    r"^\s*[a-z0-9-]{2,}\.(?:fun|bid|io|xyz|app|ai|tech|gg|so|com|net|org|lol|wtf|pro|me|sh)\s*$",
    re.I,
)


def _domain_evidence(n: Normalized) -> list[Ev]:
    """A coin named like a web address ("netrun.fun", "quants.bid") is a product or site
    coin: crypto-native by its own content."""
    if not _DOMAIN.match(n.name_raw or ""):
        return []
    return [
        Ev(
            kind="lexicon",
            label="crypto_native/utility_claim",
            weight=0.5,
            detail=f"the name '{n.name_raw.strip()}' is a web address: a site or product coin",
            source="rule:domain_name",
        )
    ]


def _news_needs_a_date(evidence: list[Ev], k: Knowledge, created_at: datetime | None) -> list[Ev]:
    """news_event from the lexicon ("Halloween", "election", "Squid Game") or inherited from
    a known coin (PNUT, HAWK: 2024 stories) only when the coin is in the news (a trend or
    headline hit), or the lexicon dates the event and the coin launched near that date. A
    seasonal or year-old story is a theme, not news."""
    if any(ev.kind == "trend" for ev in evidence):
        return evidence
    when = _aware(created_at) if created_at else datetime.now(UTC)
    days = k.scoring.get("news_event_window_days", 14)
    dated = {f"entities:{e.label}": e.event_date for e in k.entities if e.event_date is not None}

    def fresh(ev: Ev) -> bool:
        d = dated.get(ev.source)
        return d is not None and abs((when.date() - d).days) <= days

    return [
        ev
        for ev in evidence
        if not (
            ev.label == "news_event"
            and ev.source.startswith(("entities:", "slang:", "wikidata:", "known_coins:"))
            and not fresh(ev)
        )
    ]


@dataclass
class RecentCopy:
    """A token launched within the copycat window before this one that it copies."""

    what: str  # e.g. "$PNUT (Peanut)" or a mint prefix
    via: str  # name/ticker | logo
    age_s: float  # how long before this token it launched


def _within_window(inp: EngineInput, other: datetime | None) -> float | None:
    """Seconds `other` launched before this token, when that is inside the copycat window
    (and more than 5 min, so a near-simultaneous launch is not a copy); else None."""
    if other is None or inp.created_at is None:
        return None
    gap = (_aware(inp.created_at) - _aware(other)).total_seconds()
    if 300 < gap <= inp.ctx.copycat_window_days * 86400:
        return gap
    return None


def _same_name_evidence(
    inp: EngineInput, n: Normalized, is_famous: bool
) -> tuple[list[Ev], list[dict], list[RecentCopy]]:
    """Earlier tokens with this name or ticker. Only those launched within the copycat
    window count: a namesake from months ago is not the live coin this one copies."""
    evs: list[Ev] = []
    copies: list[dict] = []
    recent: list[RecentCopy] = []
    if not inp.ctx.same_name or not inp.created_at:
        return evs, copies, recent
    in_window = [
        (t, gap)
        for t in inp.ctx.same_name
        if t.mint != inp.mint and (gap := _within_window(inp, t.created_at)) is not None
    ]
    if not in_window:
        return evs, copies, recent
    in_window.sort(key=lambda tg: -tg[1])  # earliest launch first
    first, gap = in_window[0]
    days = inp.ctx.copycat_window_days
    if not is_famous:
        evs.append(
            Ev(
                kind="same_name",
                label="derivative/copycat",
                weight=min(0.75, 0.45 + 0.05 * len(in_window)),
                detail=(
                    f"{len(in_window)} token(s) with this name/ticker launched in the {days} d "
                    f"before it; the earliest (${first.symbol or '?'}, {first.source}) "
                    f"{_dur(gap)} earlier"
                ),
                source=f"same_name:{first.source}",
                where="db",
            )
        )
        recent.append(RecentCopy(f"${first.symbol or '?'}", "name/ticker", gap))
    copies.append(
        {
            "ticker": first.symbol,
            "name": first.name,
            "mint": first.mint,
            "signals": ["same_name_recent", f"recent_count:{len(in_window)}"],
            "created_at": first.created_at,
            "recent": True,
        }
    )
    return evs, copies, recent


def _image_evidence(
    res: image_stage.ImageResult, k: Knowledge, inp: EngineInput
) -> tuple[list[Ev], list[RecentCopy]]:
    """Logo near-duplicates. A token's logo counts as copied only when that token launched
    before this one (and, by the candidate query, within the copycat window); a famous
    coin's logo is a reference to it."""
    evs: list[Ev] = []
    recent: list[RecentCopy] = []
    same = int(k.scoring.get("logo_phash_same", 8))
    for nd in res.near:
        c = nd.candidate
        if c.mint:
            gap = _within_window(inp, c.created_at)
            if gap is None and inp.created_at is not None:
                continue  # launched after this token, or outside the window: not its source
            if gap is not None:
                recent.append(RecentCopy(c.mint[:6] + "…", "logo", gap))
        strength = 0.85 if nd.distance <= same else 0.6
        who = c.known_coin or c.template or c.mint or c.content_key
        label = "derivative/logo_reuse"
        ref = None
        if c.known_coin:
            coin = next((x for x in k.coins if x.symbol == c.known_coin), None)
            if coin:
                ref = ReferentCandidate(
                    label=coin.referent_label,
                    kind=coin.referent_kind,
                    desc=coin.referent_desc,
                    source=f"logo:{coin.symbol}",
                    score=strength * 0.9,
                    categories=list(coin.categories),
                )
        evs.append(
            Ev(
                kind="image_hash",
                label=label,
                weight=strength,
                detail=f"logo is a near-duplicate of {who} (pHash distance {nd.distance}"
                + (", mirrored" if nd.mirrored else "")
                + ")",
                source=f"image:{c.content_key}",
                where="image",
                referent=ref,
            )
        )
    return evs, recent


def _dur(s: float) -> str:
    if s < 120:
        return f"{int(s)} s"
    if s < 7200:
        return f"{int(s // 60)} min"
    if s < 172800:
        return f"{s / 3600:.1f} h"
    return f"{int(s // 86400)} d"


# ----------------------------------------------------------------- flags


def _flags(
    inp: EngineInput,
    n: Normalized,
    agg: Aggregated,
    is_famous: bool,
    recent_copies: list[RecentCopy] | None = None,
    known: list[known_coins.CopyMatch] | None = None,
) -> list[FlagOut]:
    flags: list[FlagOut] = []
    if "homoglyph" in n.obfuscation:
        flags.append(
            FlagOut(
                "homoglyph_ticker",
                "warn",
                "name/ticker uses look-alike characters from another script",
            )
        )
    other_obf = [o for o in n.obfuscation if o != "homoglyph"]
    if other_obf:
        flags.append(FlagOut("obfuscated_text", "info", "name uses " + ", ".join(other_obf)))
    if n.scripts:
        flags.append(FlagOut("regional_script", "info", f"name written in {', '.join(n.scripts)}"))
    days = inp.ctx.copycat_window_days
    if recent_copies and not is_famous:
        # copies of coins launched in the last `days` days: riding a live coin
        best = min(recent_copies, key=lambda c: c.age_s)
        flags.append(
            FlagOut(
                "copycat",
                "warn",
                f"copies {best.what} by {best.via}, launched {_dur(best.age_s)} before it "
                f"(within {days} d)",
            )
        )
    names = [c for c in recent_copies or [] if c.via == "name/ticker"]
    if names and not is_famous:
        flags.append(
            FlagOut(
                "earlier_same_name",
                "warn",
                f"{names[0].what} with this name/ticker launched {_dur(names[0].age_s)} "
                f"earlier (within {days} d)",
            )
        )
    established = [m for m in known or [] if not known_coins.is_self(m, n)]
    if established and not is_famous:
        top = established[0].coin
        flags.append(
            FlagOut(
                "references_known_coin",
                "info",
                f"builds on the established coin ${top.symbol} ({top.name}); "
                "a reference, not a recent copy",
            )
        )
    if inp.ctx.x_reuse_count >= 5:
        flags.append(
            FlagOut(
                "x_link_reused",
                "warn",
                f"the same X link is attached to {inp.ctx.x_reuse_count} other tokens"
                + (
                    f"; this coin is #{inp.ctx.x_reuse_rank} to link it"
                    if inp.ctx.x_reuse_rank
                    else ""
                ),
            )
        )
    if inp.x_kind == "search":
        flags.append(FlagOut("search_link_only", "info", "the X link is a search, not an account"))
    if inp.x_kind == "community" and inp.x_object_time and inp.created_at:
        gap = (_aware(inp.created_at) - _aware(inp.x_object_time)).total_seconds()
        if 0 <= gap <= 3600:
            flags.append(
                FlagOut(
                    "shell_community",
                    "info",
                    f"the X community was created only {_dur(gap)} before the token",
                )
            )
    if inp.ctx.creator_token_count >= 10:
        flags.append(
            FlagOut(
                "serial_creator",
                "info",
                f"the creator wallet has launched {inp.ctx.creator_token_count} tokens",
            )
        )
    return flags


# ----------------------------------------------------------------- entry point


def _ocr_pass(
    inp: EngineInput, n: Normalized, k: Knowledge
) -> tuple[list[ocr.OcrLine], str | None, list[Ev], str]:
    """Run (or reuse) OCR; return lines, error, evidence and the cleaned OCR text."""
    lines: list[ocr.OcrLine] = []
    err: str | None = None
    if inp.ocr_lines is not None:
        lines, err = inp.ocr_lines, inp.ocr_error
    elif inp.run_ocr and inp.image_bytes:
        lines, err = ocr.read(inp.image_bytes)
    if not lines:
        return lines, err, [], ""
    from tokensage.engine.normalize import normalize as _norm

    text = " ".join(line.text for line in lines)
    ocr_n = _norm(text, "", None)
    ocr_text = " ".join(ocr_n.name_tokens)
    evs: list[Ev] = []
    compact = ocr_n.name_compact
    t = n.ticker_base.lower()
    if t and (t in compact or n.ticker.lower() in compact):
        evs.append(
            Ev(
                "ocr",
                "logo_text",
                0.15,
                f"the logo text reads '{text[:60]}', matching the ticker",
                "ocr",
                "image",
            )
        )
    elif n.name_compact and n.name_compact in compact:
        evs.append(
            Ev(
                "ocr",
                "logo_text",
                0.1,
                f"the logo text reads '{text[:60]}', matching the name",
                "ocr",
                "image",
            )
        )
    # a *different* known ticker written on the logo is a strong copycat tell
    by_sym = k.coin_by_symbol()
    for tok in ocr_n.name_tokens:
        up = tok.upper().lstrip("$")
        if len(up) >= 3 and up in by_sym and up != n.ticker_base.upper() and up != n.ticker.upper():
            c = by_sym[up][0]
            evs.append(
                Ev(
                    "ocr_other_ticker",
                    "derivative/logo_reuse",
                    0.6,
                    f"the logo text says '${up}' ({c.name}) but the token is ${n.ticker}",
                    f"known_coins:{c.symbol}",
                    "image",
                    referent=ReferentCandidate(
                        c.referent_label,
                        c.referent_kind,
                        c.referent_desc,
                        f"ocr:{c.symbol}",
                        0.5,
                        list(c.categories),
                    ),
                )
            )
    return lines, err, evs, ocr_text


def _run(inp: EngineInput, depth: str) -> EngineOutput:
    k = load_knowledge()
    n = normalize(inp.name, inp.symbol, inp.description)
    evidence: list[Ev] = []
    evidence += _normalization_evidence(n, k)

    extra_passes: list[Pass] = []
    ocr_lines: list[ocr.OcrLine] = []
    ocr_err: str | None = None
    ocr_text = ""
    xa: xsignals.XAssessment | None = None
    if depth == "full":
        ocr_lines, ocr_err, ocr_evs, ocr_text = _ocr_pass(inp, n, k)
        evidence += ocr_evs
        if ocr_text:
            extra_passes.append(("image", ocr_text, k.scoring.get("ocr_factor", 0.7)))
        if inp.x_kind in ("tweet", "profile", "community", "search"):
            xa = xsignals.assess(
                inp.x_kind,
                inp.x_url_handle,
                inp.tweet,
                inp.profile,
                inp.created_at,
                n.ticker or None,
                inp.mint,
                n.name_tokens,
            )
            evidence += xa.evidence
            if xa.text:
                extra_passes.append(("x", xa.text, 0.8))
            for related in (xa.quoted, xa.replied_to):
                if related and related.text:
                    extra_passes.append(("x", related.text, 0.7))
            extra_passes += _account_passes(xa)
    gaz = inp.ctx.gazetteer if inp.ctx.gazetteer is not None else gazetteer.packaged()
    matches = known_coins.match_known(n, k, extra=inp.ctx.extra_coins)
    coin_words = frozenset(_kept_coin_words(matches, n))
    evidence += _lexicon_evidence(n, k, extra_passes, gaz, coin_words)
    evidence += _compound_evidence(compound_parts(n, k, gaz), n, k)
    evidence += _domain_evidence(n)
    if depth == "full" and inp.wiki_refs:
        evidence += wikilookup.evidence(inp.wiki_refs)

    is_famous = any(known_coins.is_self(m, n) for m in matches)
    evidence += known_coins.evidence_for(matches, n, k)
    self_symbols = {m.coin.symbol for m in matches if known_coins.is_self(m, n)}
    if self_symbols:
        # the famous coin itself (exact name and ticker): its own words ("wif" in Dogwifhat,
        # "inu" in Shiba Inu) do not make it a derivative of anything
        evidence = [
            e
            for e in evidence
            if not (e.kind in ("marker", "lexicon") and e.label.startswith("derivative/"))
        ]
    for ev in known_coins.family_evidence(n, k):
        # the template's own parent ("Dogwifhat" for the wif-hat family) is not derivative
        if ev.referent is not None and ev.source.startswith("templates:") and self_symbols:
            parent = next(
                (f.parent for f in k.families if ev.source == f"templates:{f.name}"), None
            )
            if parent in self_symbols:
                continue
        evidence.append(ev)
    evidence = _prune_baby_markers(evidence, n, matches, k)
    evidence = _prune_lone_markers(evidence, n, matches, inp)
    evidence = _prune_chain_alias(evidence)
    evidence = _prune_ticker_only_inherit(evidence, matches, n)
    head = _head_word(n)
    _weight_by_position(evidence, head)

    tk = ticker.explain(n, k)

    max_dist = int(k.scoring.get("logo_phash_edited", 14))
    if inp.logo_features is not None:
        # the logo's hashes came from cache (or the analyzer already hashed it)
        img = image_stage.ImageResult(features=inp.logo_features)
    else:
        img = image_stage.analyze(inp.image_bytes, [], max_dist)
    logo_near = (
        image_stage.all_near(img.features, inp.ctx.image_candidates, max_dist)
        if img.features is not None
        else []
    )
    img.near = logo_near[:10]
    img_evs, recent_copies = _image_evidence(img, k, inp)
    evidence += img_evs

    pair = _pair(inp, n, k)
    if pair is not None:
        evidence += pair.evidence

    same_evs, copies, recent_names = _same_name_evidence(inp, n, is_famous)
    evidence += same_evs
    recent_copies = (recent_names + recent_copies) if not is_famous else []
    for m in matches:
        if known_coins.is_self(m, n):
            continue
        copies.insert(
            0,
            {
                "ticker": m.coin.symbol,
                "name": m.coin.name,
                "mint": m.coin.mint,
                "signals": m.signals,
                "recent": False,  # an established coin: a reference, not a live copy
            },
        )

    trend_hits: list[trends.TrendHit] = []
    if depth == "full" and inp.trend_index is not None:
        texts = [("name", " ".join(n.name_tokens)), ("description", n.desc_clean)]
        if xa and xa.text:
            texts.append(("x", xa.text))
        for related in (xa.quoted, xa.replied_to) if xa else ():
            if related and related.text:
                texts.append(("x", related.text))
        seen_terms: set[str] = set()
        for where, text in texts:
            for h in inp.trend_index.match(text, where):
                if h.term.term not in seen_terms:
                    seen_terms.add(h.term.term)
                    trend_hits.append(h)
        # a one-word name may be one word of what trends ("Leoncio" of "Leoncio Gomez"), and
        # a ticker that is not the name may be the whole of it; gate() below weeds out the
        # everyday words
        support = " ".join(text for _, text in texts) + " " + ocr_text
        # a whole-label name hit that gate() will drop ("West" trending as itself) must not
        # stop the word-of-a-label lookup ("West" of "Kanye West")
        if (
            len(n.name_tokens) == 1
            and len(n.name_tokens[0]) >= trends.MIN_WORD_LEN
            and not any(h.where == "name" for h in trends.gate(list(trend_hits), support, k))
        ):
            word_hit = inp.trend_index.match_word(n.name_tokens[0], "name")
            if word_hit is not None and word_hit.term.term not in seen_terms:
                seen_terms.add(word_hit.term.term)
                trend_hits.append(word_hit)
        tick = (n.ticker or "").lower()
        if (
            len(tick) >= trends.MIN_TERM_LEN
            and tick.isalpha()
            and tick != n.name_compact
            and tick not in n.name_tokens
        ):
            tick_hit = inp.trend_index.match_exact(tick, "symbol")
            if tick_hit is not None and tick_hit.term.term not in seen_terms:
                seen_terms.add(tick_hit.term.term)
                trend_hits.append(tick_hit)
        # the name searches (Google News, Bluesky) both search the name: each source keeps
        # its own hit, but not one for a label the index already matched
        index_terms = {t.lower() for t in seen_terms}
        named: set[tuple[str, str]] = set()
        for h in inp.news_hits or ():
            key = (h.term.source, h.term.term.lower())
            if key[1] not in index_terms and key not in named:
                named.add(key)
                seen_terms.add(h.term.term)
                trend_hits.append(h)
        # the coin's text may not spell the trend ("Elon", $PNUT): its referent can
        shadowed: list[trends.TrendHit] = []
        for h in trends.referent_hits(evidence, inp.trend_index, k):
            if h.term.term not in seen_terms:
                seen_terms.add(h.term.term)
                trend_hits.append(h)
            elif any(p.partial and p.term.term == h.term.term for p in trend_hits):
                shadowed.append(h)
        trend_hits = trends.gate(trend_hits, support, k)
        # a one-word name's partial hit that gate() dropped ("Trump" of "Donald Trump": an
        # everyday word with nothing to support it) gives way to the referent-backed hit on
        # the same term, which "Trump Dog" and $TRUMP get; a partial hit that survived stays
        # partial
        kept = {h.term.term for h in trend_hits}
        trend_hits += [h for h in shadowed if h.term.term not in kept]
        for h in trend_hits:
            h.score = trends.score(h, inp.trend_index)
        evidence += trends.evidence(trend_hits, inp.trend_index)

    mt = meta.assess(
        inp.mint,
        inp.name,
        n.ticker or inp.symbol,
        inp.created_at,
        inp.ctx.same_name,
        inp.ctx.meta_counts,
        k,
        is_famous=is_famous,
        has_referent=any(e.referent is not None for e in evidence),
        words=meta.candidate_words(n, k),
        compact=n.name_compact,
        top=inp.ctx.top_volume,
    )
    evidence += mt.evidence
    if mt.rank is not None and mt.rank.rank > 1:
        for c in copies:
            if "same_name_recent" in c["signals"]:
                c["rank"], c["rank_of"] = mt.rank.rank, mt.rank.of
                c["rank_window_hours"] = mt.rank.window_hours
                c["signals"].append(f"copycat_rank:{mt.rank.rank}/{mt.rank.of}")
    lin = lineage_stage.assess(
        inp.mint,
        inp.created_at,
        n,
        inp.ctx.same_name,
        logo_near,
        k,
        window_days=inp.ctx.copycat_window_days,
        rank=(mt.rank.rank, mt.rank.of, mt.rank.window_hours) if mt.rank else None,
        self_coins=_self_coins(matches, n, inp.ctx.extra_coins),
        references=[m.coin for m in matches if not known_coins.is_self(m, n)],
        has_logo=img.features is not None,
        top=inp.ctx.top_volume,
    )
    if not is_famous:
        _enrich_copies(copies, lin, n, inp, logo_near)

    evidence = _news_needs_a_date(evidence, k, inp.created_at)
    symbol_is_name = _symbol_is_name(n)
    _require_second_signal(evidence, k, symbol_is_name)
    agg = aggregate(evidence, k, symbol_is_name=symbol_is_name)
    inherited = _inherit(agg, lin, inp.ctx.originals, k)
    if inherited:
        evidence += inherited
        agg = aggregate(evidence, k, symbol_is_name=symbol_is_name)
    if depth == "full" and inp.vision is not None:
        # the logo guess counts when the words agree with it or found no topic at all
        vis = vision_stage.gate(vision_stage.evidence(inp.vision), agg.categories)
        if vis:
            evidence += vis
            agg = aggregate(evidence, k, symbol_is_name=symbol_is_name)
    if inp.encoder is not None:
        emb = embed.guesses(
            inp.encoder,
            agg.categories,
            " ".join(n.name_tokens) or n.name_raw,
            n.description_raw,
            xa.text if xa else None,
        )
        if emb:
            evidence += emb
            agg = aggregate(evidence, k, symbol_is_name=_symbol_is_name(n))
    _demote_description_only_referent(agg, head)
    referent_read = read_referent(agg, symbol_is_name, k)
    flags = _flags(inp, n, agg, is_famous, recent_copies, matches)
    flags += _lineage_flags(lin, k)
    if pair is not None and pair.meaningful:
        flags.append(
            FlagOut(
                "non_sol_pair",
                "info",
                f"trades against {pair.label()} instead of SOL"
                + (f" (tokenized ${pair.underlying} stock)" if pair.underlying else "")
                + (f"; the name builds on it ({pair.builds_on_detail})" if pair.builds_on else ""),
            )
        )
    if xa:
        for code, sev, detail in xa.flags:
            flags.append(FlagOut(code, sev, detail))
    x_account = (
        xcred.account_facts(xa, inp.profile, inp.created_at, n.name_compact, n.ticker or None)
        if xa
        else None
    )
    x_credibility = xcred.credibility(x_account, xa.relation if xa else None, inp.ctx.x_reuse_rank)
    if x_account is not None and x_account.made_for_coin:
        flags.append(
            FlagOut(
                "x_account_made_for_coin",
                "info",
                f"@{x_account.handle} is named after the coin and was created "
                f"{_dur(abs(x_account.age_at_launch_s or 0))} "
                f"{'before' if (x_account.age_at_launch_s or 0) >= 0 else 'after'} it",
            )
        )
    x_match: xmatch.XMatch | None = None
    if depth == "full" and inp.x_kind in ("tweet", "profile"):
        x_match = _x_match(inp, n, img, k, x_account.age_at_launch_s if x_account else None)
        if x_match.content_fetched and x_match.fit < xmatch.FIT_RELATED:
            flags.append(
                FlagOut(
                    "x_content_mismatch",
                    "warn",
                    f"the linked X {inp.x_kind} does not match the token "
                    f"(fit {x_match.fit:.2f}: {x_match.name.detail}; {x_match.ticker.detail})",
                )
            )
        if xmatch.is_image_match(x_match.image, k):
            flags.append(FlagOut("x_image_match", "info", x_match.image.detail))
    extra_caveats: list[str] = []
    if img.error and inp.image_bytes:
        extra_caveats.append(f"image could not be analysed: {img.error}")
    if not n.name_compact and not n.emoji_keywords:
        extra_caveats.append("name is empty or has no readable text")
    if len(agg.evidence) <= 1:
        extra_caveats.append("very little evidence; the token name is generic or unknown")
    if xa and xa.status == "failed":
        extra_caveats.append("the linked X content could not be fetched")
    if xa and xa.status == "deleted":
        extra_caveats.append("the linked tweet or account no longer exists")
    if depth == "full" and ocr_err and inp.image_bytes:
        extra_caveats.append(f"OCR unavailable: {ocr_err}")
    context = _context(pair, recent_copies, trend_hits) + mt.context
    summary, caveats = summarize(
        inp.name,
        n.ticker or inp.symbol,
        agg,
        tk.text,
        extra_caveats,
        referent_confidence=(
            referent_read.confidence
            if referent_read is not None
            and not referent_read.generic
            and agg.referent is not None
            and referent_read.label == agg.referent.label
            else None
        ),
        context=context,
        framing=_framing(n, head, agg, k),
        narrative=_narrative(inp, xa),
        pair_who=_pair_who(pair, agg),
        pair_builds_on=pair is not None and pair.builds_on,
        rival=any(m.code == "marker:rival" for m in n.markers),
    )
    return EngineOutput(
        normalized=n,
        agg=agg,
        ticker_explanation=tk.text,
        copy_of=copies[:5],
        image=img,
        flags=flags,
        summary=summary,
        caveats=caveats,
        evidence=agg.evidence,
        depth=depth,
        ocr_lines=ocr_lines,
        x_match=x_match,
        x_account=x_account,
        x_credibility=x_credibility,
        ocr_error=ocr_err,
        x=xa,
        trend_hits=trend_hits,
        pair=pair,
        lineage=lin,
        referent_read=referent_read,
        vision=inp.vision if depth == "full" else None,
    )


def _self_coins(
    matches: list[known_coins.CopyMatch], n: Normalized, extra: list[KnownCoin]
) -> list[KnownCoin]:
    """The established coins this token is by name and ticker, plus the database's rows of
    them (a seed coin's row there may know its mint)."""
    selfs = [m.coin for m in matches if known_coins.is_self(m, n)]
    keys = {(c.symbol, c.name) for c in selfs}
    return selfs + [c for c in extra if c.mint and (c.symbol, c.name) in keys and c not in selfs]


def _enrich_copies(
    copies: list[dict],
    lin: lineage_stage.Lineage,
    n: Normalized,
    inp: EngineInput,
    logo_near: list[image_stage.NearDup],
) -> None:
    """Each recent copy relation carries its lineage facts (age of the original, which inputs
    match, logo distance); a coin copied by logo alone gets its own copy_of entry."""
    dist: dict[str, int] = {}
    for nd in logo_near:
        if nd.candidate.mint and nd.candidate.mint not in dist:
            dist[nd.candidate.mint] = nd.distance
    for c in copies:
        if not c.get("recent") or not c.get("mint"):
            continue
        when = c.get("created_at")
        if when is not None and inp.created_at is not None:
            c["original_age_s"] = int((_aware(inp.created_at) - _aware(when)).total_seconds())
        c["match"] = lineage_stage.match_inputs(n, c.get("name"), c.get("ticker"))
        if c["mint"] in dist:
            c["match"].append("image")
            c["image_distance"] = dist[c["mint"]]
    have = {c.get("mint") for c in copies}
    for o in (lin.original, lin.logo_original):
        if o is None or o.mint in have or "image" not in o.match:
            continue
        have.add(o.mint)
        copies.append(
            {
                "ticker": o.ticker,
                "name": o.name,
                "mint": o.mint,
                "signals": ["logo_recent", f"phash_distance:{o.image_distance}"],
                "created_at": o.created_at,
                "recent": True,
                "original_age_s": o.age_s,
                "match": list(o.match),
                "image_distance": o.image_distance,
            }
        )


def _lineage_flags(lin: lineage_stage.Lineage, k: Knowledge) -> list[FlagOut]:
    out: list[FlagOut] = []
    o = lin.original
    if lin.kind == "late_copy" and o is not None:
        what = f"${o.ticker}" if o.ticker else (o.name or o.mint[:6] + "…")
        rank = f"#{lin.rank} of {lin.rank_of} within {lin.window_hours} h, " if lin.rank else ""
        out.append(
            FlagOut(
                "late_copy",
                "warn",
                f"a late copy of {what}: {rank}launched {_dur(o.age_s or 0)} after it",
            )
        )
    floor = int(lineage_stage.config(k).get("logo_reused_min", 3))
    if lin.logo_reuse_24h is not None and lin.logo_reuse_24h >= floor:
        out.append(
            FlagOut(
                "logo_reused",
                "info",
                f"{lin.logo_reuse_24h} other coins used a near-identical logo in the 24 h "
                "before it",
            )
        )
    return out


def _inherit(
    agg: Aggregated,
    lin: lineage_stage.Lineage,
    originals: dict[str, PriorRead],
    k: Knowledge,
) -> list[Ev]:
    """A copy is about what its original is about (a copy of a dog coin is a dog coin). When
    the coin's own inputs give no theme, it inherits the original's categories; when they
    name no referent (or only a weak guess), the original's referent. Both at a lowered
    confidence, with where="copy_of" so supported_by says so."""
    o = lin.original
    prior = originals.get(o.mint) if o is not None else None
    if o is None or prior is None:
        return []
    factor = float(lineage_stage.config(k).get("inherit_factor", 0.8))
    who = f"${o.ticker}" if o.ticker else (o.name or o.mint[:6] + "…")
    evs: list[Ev] = []
    # The copy's own theme: not the relation, not its setting (script, pair ecosystem), and
    # not what it only borrows from the token it trades against (db-only labels)
    own_theme = [
        lbl
        for lbl, _ in agg.categories
        if is_theme(lbl) and lbl != meta.LABEL and agg.inputs.get(lbl) != ["db"]
    ]
    if not own_theme:
        labels = [lbl for lbl, _ in prior.categories]
        for lbl, conf in prior.categories:
            if not is_theme(lbl) or lbl == meta.LABEL:
                continue  # the relation, old corpus-rule labels, the script, the pair's ecosystem
            if any(other.startswith(lbl + "/") for other in labels):
                continue  # the parent is lifted again by its child; adding both inflates it
            evs.append(
                Ev(
                    kind="copy_inherit",
                    label=lbl,
                    weight=round(conf * factor, 3),
                    detail=f"inherited from {who}, the coin it copies ({lbl} {conf:.2f})",
                    source=f"copy_of:{o.mint}",
                    where="copy_of",
                )
            )
    r = prior.referent
    own = agg.referent
    weak = float(k.scoring.get("weak_referent", 0.45))
    if (
        r is not None
        and not r.label.startswith(meta.REFERENT_PREFIX)
        and (own is None or own.score < weak)
        # a kind-only read ("frog") says less than the copy's own theme ("cat")
        and not (r.generic and own_theme)
    ):
        ref = ReferentCandidate(
            label=r.label,
            kind=r.kind,
            desc=r.desc,
            source=f"copy_of:{o.mint}",
            score=round(r.score * factor, 3),
            categories=list(r.categories),
            generic=r.generic,
        )
        evs.append(
            Ev(
                kind="referent",
                label="referent",
                weight=ref.score,
                detail=f"{r.label}: inherited from {who}, the coin it copies",
                source=f"copy_of:{o.mint}",
                where="copy_of",
                referent=ref,
            )
        )
    return evs


# What a referent is when only its kind is known: the top-level theme category -> the
# reported kind. A child can say more than its parent (crypto slang is a concept, not a
# project). Relation and context labels (derivative, a regional script, the pair's ecosystem)
# say nothing about what the coin is.
_CATEGORY_KIND = {
    "animal": "animal",
    "meme_template": "meme",
    "celebrity": "person",
    "political": "concept",
    "pop_culture": "media",
    "news_event": "event",
    "food_object_abstract": "object",
    "crypto_native": "project",
    "crypto_native/slang": "concept",
    "crypto_native/cto": "concept",
    "crypto_native/person": "person",
    "crypto_native/chain_or_coin": "coin",
    "crypto_native/company": "organization",
    "crypto_native/launchpad": "project",
    "crypto_native/trading": "concept",
    "crypto_native/tech": "concept",
    "ai_agent": "project",
    "tradfi": "organization",
    "humor_crude_offensive": "concept",
}
# The generic label when no matched word can stand for the kind.
_KIND_LABEL = {
    "animal": "animal",
    "meme_template": "meme",
    "celebrity": "celebrity",
    "political": "politics",
    "pop_culture": "pop culture",
    "news_event": "news event",
    "food_object_abstract": "object",
    "crypto_native": "crypto project",
    "crypto_native/slang": "crypto slang",
    "crypto_native/cto": "community takeover",
    "crypto_native/person": "crypto figure",
    "crypto_native/chain_or_coin": "crypto coin",
    "crypto_native/company": "crypto company",
    "crypto_native/launchpad": "launchpad",
    "crypto_native/trading": "crypto trading",
    "crypto_native/tech": "crypto tech",
    "ai_agent": "AI agent",
    "tradfi": "company",
    "humor_crude_offensive": "crude humour",
}
# Themes whose matched word is itself a fair generic label ("frog", "beer", "brainrot").
_WORD_LABELS = ("animal", "food_object_abstract", "meme_template")
# How the generic referent's desc names a channel that is not one of the coin's own texts.
_CHANNEL_WORD = {"db": "pair token", "copy_of": "original", "x": "X post", "trend": "trend hit"}
# Engine and knowledge kinds as reported.
REFERENT_KINDS = {
    "famous_animal", "meme", "person", "coin", "event", "concept", "place", "other",
    "animal", "media", "project", "object", "organization",
}  # fmt: skip


@dataclass
class ReferentRead:
    """The referent as reported. `generic`: only the kind is known, the label is a generic
    word ("frog") and the confidence stays in the kind-only band."""

    label: str
    kind: str
    desc: str | None
    source: str
    confidence: float
    supported_by: list[str]
    generic: bool = False


def own_categories(agg: Aggregated) -> list[tuple[str, float]]:
    """The categories the coin's own inputs give it. What it only borrows from the token it
    trades against (db-only labels) is context, not its theme, unless its name builds on
    that token (derivative/pair_family), as _inherit already treats a copy's theme."""
    if any(lbl == "derivative/pair_family" for lbl, _ in agg.categories):
        return agg.categories
    return [(lbl, c) for lbl, c in agg.categories if agg.inputs.get(lbl) != ["db"]]


def _theme(agg: Aggregated, floor: float) -> tuple[str, str, float] | None:
    """The coin's strongest theme that names a kind: (top-level label, best label under it,
    top-level confidence)."""
    best: dict[str, tuple[str, float]] = {}
    for lbl, conf in own_categories(agg):
        top = lbl.split("/")[0]
        if top not in _CATEGORY_KIND or lbl in NO_PARENT or conf < floor:
            continue
        if top not in best:
            best[top] = (lbl, conf)
        elif lbl != top and best[top][0] == top:
            best[top] = (lbl, best[top][1])  # the child says which (animal -> animal/frog)
    if not best:
        return None
    # on a tie the subject wins over the setting: a "golden bull" is a bull before it is
    # crypto slang
    top = max(best, key=lambda t: (best[t][1], -THEME_ORDER.index(t)))
    return top, best[top][0], best[top][1]


def _trend_backs(label: str, evidence: list[Ev]) -> bool:
    """A trend hit names this referent: a Wikipedia article whose title is the label, as
    referent_hits() matches them ("Peanut (squirrel)", not the "Peanut" article), or an X,
    Google, news or Bluesky label that is its name ("Moo Deng" for "Moo Deng (hippo)": those
    labels carry no disambiguation)."""
    full = trends._clean(label)
    base = trends._clean(re.sub(r"\s*\([^)]*\)\s*$", "", label))
    for ev in evidence:
        src, _, term = ev.source.partition(":")
        if ev.kind != "trend" or src not in TREND_SOURCES or not term:
            continue
        if src == "wikipedia":
            if full in trends.surfaces_for(term):
                return True
        elif trends._clean(term) in (full, base):
            return True
    return False


def _theme_kind(labels: list[str]) -> str | None:
    for lbl in labels:
        kind = _CATEGORY_KIND.get(lbl) or _CATEGORY_KIND.get(lbl.split("/")[0])
        if kind:
            return kind
    return None


# The bottom of the documented weak-guess band (docs/API.md: 0.3-0.49 a weak guess or a kind).
WEAK_GUESS_MIN = 0.3


def read_referent(agg: Aggregated, symbol_is_name: bool, k: Knowledge) -> ReferentRead | None:
    """The reported referent. A named one keeps its label and gets a confidence by how many
    independent inputs point at it: below 0.45 a weak guess as before, 0.5-0.69 from one
    input, 0.7+ from two or more. When nothing is named (or only a weak guess under 0.45)
    but the coin has a theme, the kind alone is reported with a generic label ("frog") at
    0.3-0.49. Null when the coin has no readable theme (and no named guess at 0.3+)."""
    floor = float(k.scoring.get("generic_referent_min_category", 0.2))
    r = agg.referent
    theme = _theme(agg, floor)
    weakest = float(k.scoring.get("named_referent_min", 0.45))
    if r is not None and not r.generic and r.score < WEAK_GUESS_MIN and theme is None:
        # squashed below the weak-guess band ("Speed" -> iShowSpeed at 0.19): the summary
        # says no clear reference was found, so the referent says so too (and gets no wave)
        agg.caveats = [c for c in agg.caveats if c != "referent is a weak guess"]
        return None
    if r is not None and not r.generic and (r.score >= weakest or theme is None):
        chans = [
            channel(ev, symbol_is_name)
            for ev in agg.evidence
            if ev.referent is not None and ev.referent.label == r.label
        ]
        if "trend" not in chans and _trend_backs(r.label, agg.evidence):
            chans.append("trend")  # a live trend names this referent too
        inputs = list(dict.fromkeys(chans))
        raw = r.score
        if raw >= 0.45:
            lift = min(1.0, (raw - 0.45) / 0.5)
            raw = 0.7 + 0.27 * lift if len(inputs) >= 2 else 0.5 + 0.19 * lift
        kind = r.kind if r.kind in REFERENT_KINDS else "other"
        if kind == "other":
            kind = _theme_kind(r.categories) or (_CATEGORY_KIND[theme[0]] if theme else "other")
        elif kind == "concept" and _theme_kind(r.categories) in ("media", "organization"):
            # a franchise, game or show (Pokémon, Fortnite); a company or exchange (Binance)
            kind = _theme_kind(r.categories) or kind
        return ReferentRead(r.label, kind, r.desc, r.source, round(raw, 3), inputs)
    if r is not None and r.generic:
        # inherited from the coin this one copies, itself only a kind
        inputs = list(
            dict.fromkeys(
                channel(ev, symbol_is_name)
                for ev in agg.evidence
                if ev.referent is not None and ev.referent.label == r.label
            )
        )
        kind = r.kind if r.kind in REFERENT_KINDS else "other"
        return ReferentRead(
            r.label, kind, r.desc, r.source, round(min(0.49, r.score), 3), inputs, True
        )
    if theme is None:
        return None
    top, leaf, conf = theme
    key = leaf if leaf in _CATEGORY_KIND else top
    label = _KIND_LABEL.get(key, top)
    if top == "animal" and leaf != top and not leaf.endswith("/other"):
        label = leaf.split("/")[1].replace("_", "/")  # animal/bear_bull -> bear/bull
    if top in _WORD_LABELS:
        rows = [
            ev
            for ev in agg.evidence
            if ev.surface
            and (ev.label == leaf or ev.label.split("/")[0] == top)
            and ev.where in ("name", "symbol", "image", "description")
        ]
        # the word behind the chosen label, from the name when it has one
        rows.sort(key=lambda ev: (ev.label != leaf, ev.where != "name", -ev.weight))
        if rows and rows[0].surface:
            label = rows[0].surface
    inputs = agg.inputs.get(leaf) or agg.inputs.get(top) or []
    what = _KIND_LABEL.get(key, top)
    article = "an" if what[0].lower() in "aeio" else "a"
    by = " and ".join(_CHANNEL_WORD.get(ch, ch) for ch in inputs) or "own text"
    return ReferentRead(
        label=label,
        kind=_CATEGORY_KIND[key],
        desc=f"{article} {what} coin by its {by}; nothing more specific identified",
        source=f"category:{leaf}",
        confidence=round(min(0.49, 0.3 + 0.2 * conf), 3),
        supported_by=list(inputs),
        generic=True,
    )


# The quoted / replied-to author is usually the narrative; the posting account is often the
# deployer's own; a mention is the weakest tie.
_ACCOUNT_FACTOR = {"author": 0.4, "quoted_author": 0.5, "replied_to_author": 0.5, "mentioned": 0.3}
_ACCOUNT_ROLE = {
    "author": "the posting account",
    "quoted_author": "the quoted account",
    "replied_to_author": "the replied-to account",
    "mentioned": "a mentioned account",
}


def account_text(name: str | None, handle: str | None) -> str:
    """An account's display name and handle as words: "Elon Musk elon musk" for
    (Elon Musk, @elonmusk). Handles are segmented ("elonmusk" -> "elon musk")."""
    parts: list[str] = []
    for raw in (name, handle):
        if raw:
            parts += normalize(raw, None, None).name_tokens
    return " ".join(dict.fromkeys(parts))


def account_parts(name: str | None, handle: str | None) -> str:
    """The display name and the handle as words, separately: "elon musk|elonmusk"."""
    return "|".join(
        " ".join(normalize(raw, None, None).name_tokens) for raw in (name, handle) if raw
    )


def _names_account(surface: str, account_words: str) -> bool:
    """An entity hit names the account when it covers the account's display name or handle
    ("elon musk" in "Elon Musk elon musk") or is itself a multi-word name ("donald trump"
    in "Donald Trump Jr"). A one-word alias inside a longer brand ("american" in
    "American Eagle") does not."""
    s = surface.replace(" ", "")
    if " " in surface.strip():
        return True
    for part in account_words.split("|"):
        if part and part.replace(" ", "") == s:
            return True
    return False


# Marker sources that say "Baby X" is a derivative of X. They only mean that when there is
# an X: a known coin or a named entity the rest of the name refers to. "Baby Shark" is a
# song, not a derivative of a shark coin.
_BABY_SOURCES = ("templates:marker:baby", "templates:baby_x", "slang:baby", "slang:lil",
                 "slang:mini", "slang:smol")  # fmt: skip


def _prune_baby_markers(
    evidence: list[Ev], n: Normalized, matches: list[known_coins.CopyMatch], k: Knowledge
) -> list[Ev]:
    if not any(m.code == "marker:baby" for m in n.markers):
        return evidence
    marker_words = {w for m in n.markers if m.code == "marker:baby" for w in m.text.lower().split()}
    parents = [m for m in matches if not known_coins.is_self(m, n)]
    # an entity in the name whose surface is not the marker itself and does not swallow it
    # ("Baby Shark" the song covers the whole name: nothing is derived from anything)
    named = any(
        ev.referent is not None
        and ev.where == "name"
        and ev.kind == "entity"
        and ev.referent.surface
        and not (set(ev.referent.surface.split()) & marker_words)
        for ev in evidence
    )
    if parents or named:
        return evidence
    return [
        ev
        for ev in evidence
        if not (ev.label.startswith("derivative/") and ev.source in _BABY_SOURCES)
    ]


# Markers that say "a version of X" with nothing else: "Real Madrid" and "World War II" copy
# no coin. Kept when a known coin, a recent namesake, a named entity or another derivative
# row (a template family, the pair token) says what the name derives from.
_LONE_MARKER_SOURCES = ("templates:marker:original-claim", "templates:version:roman",
                        "templates:marker:ai-suffix", "slang:real", "slang:classic", "slang:og",
                        "slang:original")  # fmt: skip


def _prune_lone_markers(
    evidence: list[Ev], n: Normalized, matches: list[known_coins.CopyMatch], inp: EngineInput
) -> list[Ev]:
    lone = [
        ev
        for ev in evidence
        if ev.label.startswith("derivative/") and ev.source in _LONE_MARKER_SOURCES
    ]
    if not lone:
        return evidence
    if any(not known_coins.is_self(m, n) for m in matches):
        return evidence
    if any(
        ev.referent is not None and ev.where == "name" and ev.kind == "entity" for ev in evidence
    ):
        return evidence
    if any(
        ev.label.startswith("derivative/") and ev.source not in _LONE_MARKER_SOURCES
        for ev in evidence
    ):
        return evidence
    if inp.ctx.same_name and any(
        t.mint != inp.mint and _within_window(inp, t.created_at) is not None
        for t in inp.ctx.same_name
    ):
        return evidence  # "Real Blorbo" after a recent Blorbo: the marker says which copy
    gone = {id(ev) for ev in lone}
    return [ev for ev in evidence if id(ev) not in gone]


def _prune_chain_alias(evidence: list[Ev]) -> list[Ev]:
    """The bare "sol" of "dog on sol" names the chain the coin is on, not what it is about:
    the Solana referent it matched is dropped when the name has another named candidate or
    a theme of its own, and never counts from the logo's text alone."""
    sol = [
        ev
        for ev in evidence
        if ev.kind == "entity"
        and ev.referent is not None
        and (ev.referent.surface or "").lower() == "sol"
    ]
    if not sol:
        return evidence
    gone = {id(ev) for ev in sol if ev.where == "image"}
    in_name = [ev for ev in sol if ev.where == "name"]
    if in_name:
        labels = {ev.referent.label for ev in in_name if ev.referent is not None}
        other_named = any(
            ev.referent is not None
            and ev.referent.label not in labels
            and ev.kind in ("entity", "known_coin", "referent", "template_family")
            and ev.where in ("name", "symbol")
            for ev in evidence
        )
        own_theme = any(
            is_theme(ev.label)
            and ev.label.split("/")[0] != "crypto_native"
            and ev.where in ("name", "symbol")
            and ev.kind not in ("marker", "script")
            for ev in evidence
            if id(ev) not in {id(e) for e in sol}
        )
        if other_named or own_theme:
            gone |= {id(ev) for ev in in_name}
    if not gone:
        return evidence
    return [ev for ev in evidence if id(ev) not in gone]


def _prune_ticker_only_inherit(
    evidence: list[Ev], matches: list[known_coins.CopyMatch], n: Normalized
) -> list[Ev]:
    """A coin whose only tie to a famous coin is its ticker, while its name names something
    else ("Department of Government Efficiency $DOGE"), is a pun on that coin, not about its
    subject: keep the reference, drop the inherited categories (the dog)."""
    name_refs = {
        ev.referent.label
        for ev in evidence
        if ev.referent is not None and ev.where == "name" and ev.kind == "entity"
    }
    if not name_refs:
        return evidence
    drop: set[str] = set()
    for m in matches:
        if known_coins.is_self(m, n):
            continue
        if set(m.signals) <= {"ticker", "ticker_base"} and m.coin.referent_label not in name_refs:
            drop.add(f"known_coins:{m.coin.symbol}")
    if not drop:
        return evidence
    return [ev for ev in evidence if not (ev.kind == "known_coin_inherit" and ev.source in drop)]


def _demote_description_only_referent(agg: Aggregated, head: str | None) -> None:
    """A referent seen only in the description, while the name's own subject is unresolved
    ("Gork", described as "elons dumb ai"), is a theme, not what the coin is: report it as
    a weak guess rather than "may refer to Elon Musk"."""
    r = agg.referent
    if r is None or not head or r.score < 0.45:
        return
    wheres = {
        ev.where for ev in agg.evidence if ev.referent is not None and ev.referent.label == r.label
    }
    if wheres and wheres <= {"description"} and not _covers(r, head):
        r.score = round(min(r.score, 0.44), 3)
        agg.caveats.append(
            f"'{r.label}' appears only in the description; the name itself is unresolved"
        )
        ru = agg.referent_runner_up
        weakest = float(load_knowledge().scoring.get("named_referent_min", 0.45))
        if (
            ru is not None
            and ru.score >= weakest
            and any(
                ev.referent is not None and ev.referent.label == ru.label and ev.where == "name"
                for ev in agg.evidence
            )
        ):
            # the name's own entity ("Ohio" in "Ohio Dog") was the runner-up only because
            # the description name-dropped someone more famous: it is the referent
            agg.referent, agg.referent_runner_up = ru, r
            agg.caveats = [c for c in agg.caveats if not c.startswith("referent is ambiguous")]


def _account_passes(xa: xsignals.XAssessment) -> list[Pass]:
    """The names of the accounts involved are evidence too: a reply to @elonmusk, a quote of
    a famous dog's account, a launch post from an account named like the coin."""
    out: list[Pass] = []
    for acc in xa.accounts:
        text = account_text(acc.name, acc.handle)
        if text:
            who = f"@{acc.handle}" if acc.handle else (acc.name or "?")
            out.append(
                (
                    "x",
                    text,
                    _ACCOUNT_FACTOR[acc.role],
                    f"{_ACCOUNT_ROLE[acc.role]} {who}",
                    account_parts(acc.name, acc.handle),
                )
            )
    return out


# Words that never carry a coin's subject on their own (one list with the known-coin stage).
_HEAD_STOP = known_coins.HEAD_STOP


def _head_word(n: Normalized) -> str | None:
    """The head of the name: in "Elon's Cat" it is "cat" (the coin is a cat), in "Trump Dog"
    "dog". The last content word, by English compound order."""
    for t in reversed(n.name_tokens):
        if len(t) >= 3 and t not in _HEAD_STOP and t.isalpha():
            return t
    return None


def _covers(ref: ReferentCandidate, head: str) -> bool:
    s = (ref.surface or "").lower()
    return head in s.split() or (len(head) >= 4 and head in s.replace(" ", ""))


def _weight_by_position(evidence: list[Ev], head: str | None) -> None:
    """A referent that covers the head word is what the coin is ("Elon Pepe" is a Pepe);
    one found only in a modifier is its theme ("Elon's Cat" is a cat tied to Elon)."""
    if not head:
        return
    done: set[int] = set()
    for ev in evidence:
        r = ev.referent
        if r is None or r.surface is None or ev.where != "name" or id(r) in done:
            continue
        done.add(id(r))
        factor = 1.1 if _covers(r, head) else 0.85
        r.score = round(min(0.98, r.score * factor), 3)
    for ev in evidence:
        if ev.referent is not None and ev.label == "referent" and ev.where == "name":
            ev.weight = ev.referent.score


_HEAD_PHRASE = {
    "animal/dog": "a dog coin", "animal/cat": "a cat coin", "animal/frog": "a frog coin",
    "animal/monkey": "a monkey coin", "animal/hippo": "a hippo coin",
    "animal/squirrel": "a squirrel coin", "animal/bird": "a bird coin",
    "animal/bear_bull": "a bear/bull coin", "animal/fish": "a fish coin",
    "animal/other": "an animal coin", "food": "a food coin", "vehicle": "a vehicle coin",
    "body_part": "a body-part coin",
}  # fmt: skip


def _framing(n: Normalized, head: str | None, agg: Aggregated, k: Knowledge) -> str | None:
    """ "a cat coin" when the referent is only the modifier of the name, so the summary
    says "a cat coin tied to Elon Musk" rather than "refers to Elon Musk"."""
    r = agg.referent
    if not head or r is None or r.surface is None or _covers(r, head):
        return None
    scores = dict(agg.categories)
    floor = k.scoring.get("framing_min_confidence", 0.5)
    for cls in lexicon.wordnet_classes_for(head, k):
        phrase = _HEAD_PHRASE.get(cls)
        # the head word's class must have held up in scoring: "Ani" halved to a weak bird
        # does not make "Grok Companion Ani" a bird coin
        if phrase and scores.get(_WORDNET_LABEL.get(cls, cls), 0.0) >= floor:
            return phrase
    return None


def _narrative(inp: EngineInput, xa: xsignals.XAssessment | None) -> str | None:
    """The post a coin was launched on, when its X link points at someone else's earlier
    post (directly, or by replying to or quoting it). For many coins this *is* the story."""
    if xa is None or xa.status != "ok" or inp.created_at is None:
        return None
    own = (xa.author_handle or "").lower()
    cands: list[tuple[str, str | None, str | None, datetime | None, str]] = []
    if xa.relation == "narrative_reference" and inp.tweet is not None:
        t = inp.tweet
        cands.append(("posted", t.author_handle, t.author_name, t.created_at, t.text or ""))
    for verb, r in (("replied to a post by", xa.replied_to), ("quoted a post by", xa.quoted)):
        if r is not None and r.status == "ok" and (r.author_handle or "").lower() != own:
            cands.append((verb, r.author_handle, r.author_name, r.created_at, r.text or ""))
    for verb, handle, name, when, text in cands:
        gap = (_aware(inp.created_at) - _aware(when)).total_seconds() if when else None
        if gap is not None and gap <= 0:
            continue
        who = f"@{handle}" + (
            f" ({name})" if name and name.lower() != (handle or "").lower() else ""
        )
        gist = " ".join(text.split())[:140]
        if verb == "posted":
            lead = (
                f"launched {_dur(gap)} after {who} posted" if gap else f"its X link is {who}'s post"
            )
        else:
            lead = f"its X post {verb} {who}" + (f" from {_dur(gap)} before launch" if gap else "")
        return f'{lead}: "{gist}"' if gist else lead
    return None


def _context(
    pair: pairing.PairAssessment | None,
    recent_copies: list[RecentCopy],
    trend_hits: list[trends.TrendHit],
) -> list[str]:
    """The launch context in a few clauses: what it trades against, what it copies, what is
    trending. (The X post it rides is its own sentence, see _narrative.)"""
    out: list[str] = []
    if pair is not None and pair.underlying:  # a token pair is its own sentence (_pair_who)
        out.append(
            f"trades against {pair.label()} (the tokenized ${pair.underlying} stock)"
            + (", and its name builds on it" if pair.builds_on else "")
        )
    if recent_copies:
        c = min(recent_copies, key=lambda r: r.age_s)
        out.append(f"copies {c.what} ({c.via}) launched {_dur(c.age_s)} earlier")
    if trend_hits:
        h = trend_hits[0]
        t = h.term
        if t.source == "news":
            out.append(f"its name is in the news ('{t.term}')")
        elif t.source == "bluesky":
            out.append(f"its name is being posted about on Bluesky ('{t.term}')")
        elif h.via is not None:
            out.append(f"what it refers to is trending ('{t.term}')")
        elif t.source == "google_trends":
            out.append(f"matches the trending search '{t.term}'")
        elif t.source == "x_trends":
            out.append(f"matches '{t.term}', trending on X")
        else:
            out.append(f"matches the trending topic '{t.term}'")
    return out


def _pair_who(
    pair: pairing.PairAssessment | None, agg: Aggregated
) -> tuple[str, str | None] | None:
    """The token a coin trades against, for the summary: its ticker and name ("$OGC (OG
    Callers)") and what it is ("itself a pump.fun coin about Ansem (...)"), or None for SOL,
    stablecoins, majors and tokenized stocks (a stock pair is a context clause)."""
    if pair is None or pair.kind != "token":
        return None
    kind = "itself a pump.fun coin" if pair.pumpfun else "a token"
    ref = pair.referent
    # a kind-only read of the pair token ("dog") is what it reads as, not what it is about
    if ref is not None and ref.score >= 0.45 and not ref.generic:
        if agg.referent is not None and agg.referent.label == ref.label:
            # the coin's own referent is the pair's: the lead already says what it is
            return pair.label(), (kind if pair.pumpfun else None)
        if ref.label.casefold() in ((pair.name or "").casefold(), (pair.symbol or "").casefold()):
            # Fartcoin refers to Fartcoin: say what it is instead
            return pair.label(), f"{kind}: {ref.desc}" if ref.desc else kind
        return pair.label(), f"{kind} about {ref.label}" + (f" ({ref.desc})" if ref.desc else "")
    tp = main_category(
        [(lbl, c) for lbl, c in pair.categories if not lbl.startswith(pairing.SKIP_CATEGORIES)]
    )
    if tp is not None:
        return pair.label(), f"{kind} that reads as {category_phrase(tp[0], pair.categories)}"
    return pair.label(), (kind if pair.pumpfun else None)


def _pair(inp: EngineInput, n: Normalized, k: Knowledge) -> pairing.PairAssessment | None:
    """Read the pair token: a known coin, a stored analysis, or (failing both) the engine's
    basic read of the pair token's own name and ticker."""
    if inp.pair is None:
        return None
    coin = None
    pair_meaning = None
    stock = pairing.xstock_ticker(inp.pair.symbol, inp.pair.name, inp.pair.mint)
    if inp.pair.kind == "token" and not stock:
        coin = pairing.known_coin_for(inp.pair.mint, [*k.coins, *inp.ctx.extra_coins])
        if coin is None and inp.pair.referent is None and (inp.pair.name or inp.pair.symbol):
            pair_only = EngineInput(
                mint=inp.pair.mint,
                name=inp.pair.name,
                symbol=inp.pair.symbol,
                description=None,
                image_bytes=None,
                created_at=None,
                ctx=DbContext(extra_coins=inp.ctx.extra_coins, gazetteer=inp.ctx.gazetteer),
            )
            pair_meaning = _run(pair_only, "basic").agg
    return pairing.assess(inp.pair, n, k, pair_meaning, coin)


def _x_match(
    inp: EngineInput,
    n: Normalized,
    img: image_stage.ImageResult,
    k: Knowledge,
    account_age_s: int | None = None,
) -> xmatch.XMatch:
    """Compare the linked post with the token, keeping the two apart: what the name, ticker
    and logo mean on their own vs what the post alone is about."""
    token_only = EngineInput(
        mint=inp.mint,
        name=inp.name,
        symbol=inp.symbol,
        description=None,
        image_bytes=inp.image_bytes,
        created_at=inp.created_at,
        ctx=dataclasses.replace(inp.ctx, originals={}),  # its own read, nothing inherited
        logo_features=inp.logo_features,  # already hashed: do not decode the logo again
    )
    text = xmatch.post_text(inp.tweet, inp.profile)
    post_meaning = None
    if text:
        post_only = EngineInput(
            mint=inp.mint,
            name=None,
            symbol=None,
            description=text,
            image_bytes=None,
            created_at=inp.created_at,
            ctx=DbContext(extra_coins=inp.ctx.extra_coins, gazetteer=inp.ctx.gazetteer),
        )
        post_meaning = _run(post_only, "basic").agg
    token_meaning = _run(token_only, "basic").agg
    return xmatch.assess(
        n,
        inp.tweet,
        inp.profile,
        img.features or inp.logo_features,
        inp.x_media or [],
        token_meaning,
        post_meaning,
        k,
        account_age_s,
        mint=inp.mint,
    )


def _symbol_is_name(n: Normalized) -> bool:
    """The ticker spells the name ($UNCCAT for Unc Cat, $PEPE for Pepe): one input, not two."""
    t = n.ticker.lower()
    c = n.name_compact
    if not t or not c:
        return False
    return t == c or (min(len(t), len(c)) >= 3 and (c.startswith(t) or t.startswith(c)))


def _aware(d: datetime) -> datetime:
    """Naive datetimes are UTC (old cache rows); never let a subtraction raise."""
    return d if d.tzinfo is not None else d.replace(tzinfo=UTC)


def run_basic(inp: EngineInput) -> EngineOutput:
    return _run(inp, "basic")


def run_full(inp: EngineInput) -> EngineOutput:
    return _run(inp, "full")
