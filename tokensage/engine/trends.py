"""S8 Trend matching (guide §5.8): match token text, and the referent it resolves to, against
spiking Wikipedia articles, Google Trends searches, X's trending topics, Google News headlines
and Bluesky posts."""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Literal

import ahocorasick

from tokensage.engine import gazetteer, segment
from tokensage.engine.context import Ev, ReferentCandidate
from tokensage.engine.knowledge import Knowledge

GENERIC_PREFIXES = ("list of", "deaths in", "main page", "special:", "wikipedia:", "portal:")
MIN_TERM_LEN = 4
# a one-word name is looked up among the words of longer trending labels ("Leoncio" in
# "Leoncio Gomez") only when it is at least this long, and only in labels of 2-4 words
MIN_WORD_LEN = 4
MAX_LABEL_WORDS = 4


@dataclass(frozen=True)
class TrendTerm:
    term: str  # article title, spaces not underscores
    spike: float  # views / prior median (0 for every source but Wikipedia)
    # wikipedia: daily views; news: the number of matching headlines; google_trends: Google's
    # approximate search count; x_trends: the hourly lists it was on in the last day;
    # bluesky: matching posts in the last 24 hours
    views: int
    # "wikipedia" | "news" (the coin's name is in recent headlines) | "google_trends" |
    # "x_trends" (on X's trending list) | "bluesky" (the coin's name is in recent posts)
    source: str = "wikipedia"
    # how fresh it is: the UTC day of the Wikipedia spike, the newest matching headline or post,
    # or when Google Trends / X first listed it
    seen_at: datetime | None = None
    # the story behind a Google Trends search, or the most liked matching Bluesky post
    headline: str | None = None
    rank: int | None = None  # x_trends: its best position on X's list (1-50)


@dataclass
class TrendHit:
    term: TrendTerm
    surface: str
    where: str
    headline: str | None = None  # a recent news headline about it, when one was found
    # None: the coin's own text matched; "referent" / "alias": the referent it resolves to
    # (or one of that entity's aliases) is the trending term
    via: str | None = None
    score: float | None = None  # trend strength 0-1, see score(); set by the engine
    # the coin's one-word name is one word of a longer trending label ("Leoncio" of "Leoncio
    # Gomez"), not the whole label
    partial: bool = False
    # a word of the trending label or its story that the coin's other text also has: what
    # let an everyday word ("West") match, see gate()
    support: str | None = None

    @property
    def matched_on(self) -> str:
        return self.via or self.where


SourceState = Literal["ok", "stale", "failed", "skipped", "unavailable"]


@dataclass
class SourceStatus:
    """Whether one trend source had data for this read (trend.sources)."""

    source: str
    status: SourceState
    as_of: datetime | None = None  # the newest data the source gave
    terms: int | None = None  # trending terms (or headlines) it contributed
    detail: str | None = None


class TrendIndex:
    """Aho-Corasick over trend surfaces; rebuilt only when the term set changes."""

    def __init__(
        self, terms: list[TrendTerm], k: Knowledge, sources: list[SourceStatus] | None = None
    ):
        self.terms = terms
        self.sources: list[SourceStatus] = list(sources or [])
        self._auto = ahocorasick.Automaton()
        self._generic = {e.label.lower() for e in k.entities if e.popularity >= 0.95}
        n = 0
        best: dict[str, TrendTerm] = {}
        for t in terms:
            for s in surfaces_for(t.term):
                # two terms (or sources) can share a surface: keep the stronger one by its
                # own source's scale (spike means nothing for a Google Trends term)
                if s not in best or _strength(t) > _strength(best[s]):
                    best[s] = t
        self._best = best
        # each word of a 2-4 word label -> the strongest such label: a one-word name can be
        # one word of what is trending ("Leoncio" while "Leoncio Gomez" trends)
        words: dict[str, TrendTerm] = {}
        for t in terms:
            base = _clean(re.sub(r"\s*\([^)]*\)\s*$", "", t.term.replace("_", " ")))
            ws = base.split()
            if not 2 <= len(ws) <= MAX_LABEL_WORDS or not surfaces_for(t.term):
                continue
            for w in ws:
                if len(w) < MIN_WORD_LEN or not w.isalpha() or w in gazetteer._STOP:
                    continue
                if w not in words or _strength(t) > _strength(words[w]):
                    words[w] = t
        self._words = words
        for s, t in best.items():
            self._auto.add_word(" " + s + " ", (s, t))
            n += 1
        if n:
            self._auto.make_automaton()
        self._n = n

    def match(self, text: str, where: str) -> list[TrendHit]:
        if not self._n or not text:
            return []
        padded = " " + " ".join(_fold(text).lower().split()) + " "
        out: dict[str, TrendHit] = {}
        for _end, (s, t) in self._auto.iter(padded):
            if t.term not in out or len(s) > len(out[t.term].surface):
                out[t.term] = TrendHit(t, s, where)
        return list(out.values())

    def match_exact(self, phrase: str, where: str, via: str | None = None) -> TrendHit | None:
        """A trending term whose surface is this whole phrase (a referent label or alias, or
        the ticker): "Donald Trump" must not match a trending "Trump" article, only "Donald
        Trump"."""
        key = _clean(phrase)
        t = self._best.get(key) if key else None
        return TrendHit(t, key, where, via=via) if t is not None else None

    def match_word(self, word: str, where: str) -> TrendHit | None:
        """The strongest 2-4 word trending label that has this word as one of its words
        ("leoncio" -> "Leoncio Gomez"). gate() decides whether the word is specific enough."""
        key = _clean(word)
        t = self._words.get(key) if key and " " not in key else None
        return TrendHit(t, key, where, partial=True) if t is not None else None

    def is_generic(self, term: str) -> bool:
        return term.lower() in self._generic


def _fold(s: str) -> str:
    """Latin letters without their accents ("Leôncio" -> "Leoncio"), so a trending label and
    a coin name spelled with and without them meet. Other scripts are left alone."""
    if s.isascii():
        return s
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def _clean(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", _fold(s).lower()).split())


def surfaces_for(title: str) -> list[str]:
    """'Peanut (squirrel)' -> ['peanut (squirrel)', 'peanut squirrel', 'peanut']"""
    t = _fold(title).replace("_", " ").strip()
    low = t.lower()
    if low.startswith(GENERIC_PREFIXES) or ":" in low:
        return []
    if any(c.isalpha() and not c.isascii() for c in low):
        # another script ("Aぇヤンタン"): cleaning would leave a stray Latin letter or two
        # ("a") that matches every coin text; coin names are compared in Latin letters
        return []
    out: list[str] = []
    base = re.sub(r"\s*\([^)]*\)\s*$", "", t).strip().lower()
    paren = re.search(r"\(([^)]*)\)\s*$", t)
    if paren and base:
        out.append(f"{base} {paren.group(1).lower()}")
    if len(low) >= MIN_TERM_LEN and low not in out:
        out.append(low)
    if (
        base
        and len(base) >= MIN_TERM_LEN
        and base not in out
        and " " in base
        or (base and len(base) >= 6 and base not in out)
    ):
        out.append(base)
    # one space between words, as TrendIndex.match and _clean write their text ("Dr. Dre"
    # must index as "dr dre", not "dr  dre")
    cleaned = [" ".join(re.sub(r"[^a-z0-9 ]+", " ", s).split()) for s in out if s]
    return list(dict.fromkeys(c for c in cleaned if len(c) >= MIN_TERM_LEN))


def _forms(w: str) -> list[str]:
    """The word and the stems its English endings may hide: "dumbest" -> "dumb", "stories"
    -> "story", "shards" -> "shard"."""
    out = [w]
    if len(w) > 4:
        for end, put in (("ies", "y"), ("est", ""), ("ing", ""), ("ed", ""), ("er", ""), ("s", "")):
            if w.endswith(end):
                stem = w[: -len(end)] + put
                out.append(stem)
                if len(stem) > 2 and stem[-1] == stem[-2]:
                    out.append(stem[:-1])  # "biggest" -> "bigg" -> "big"
                if not put and end != "s":
                    out.append(stem + "e")  # "bravest" -> "brave"
    return out


def everyday(word: str, k: Knowledge) -> bool:
    """A dictionary word that is also among the 20,000 most frequent English words ("west",
    "moon", "juniper"): it trends, or sits in a coin's name, for a thousand unrelated reasons."""
    freq = segment.common_words(k)
    return any(gazetteer.is_common(f) and f in freq for f in _forms(word.lower()))


def rare(word: str, k: Knowledge) -> bool:
    """Neither a dictionary word nor a frequent one, nor an inflection of one ("Leoncio",
    "Glasnow", "Ozzie"; not "Dumbest"): rare enough to name one thing when it is part of
    what is trending. Frequent given names ("Tyler") and places ("Quebec") are not rare."""
    freq = segment.common_words(k)
    return not any(gazetteer.is_common(f) or f in freq for f in _forms(word.lower()))


def ordinary(phrase: str, k: Knowledge) -> bool:
    """Every word of the phrase is an everyday or function word ("My Shoe", "Plague
    Doctor", "Life Is Good"): people post it every day, whatever is trending."""
    words = _clean(phrase).split()
    return bool(words) and all(w in gazetteer._STOP or len(w) <= 2 or everyday(w, k) for w in words)


def gate(hits: list[TrendHit], support_text: str, k: Knowledge) -> list[TrendHit]:
    """Drop one-word matches that are too ordinary to mean the coin is about the trend.

    - The whole label is one word: a Wikipedia article or Google Trends search ("Halloween",
      "Juniper") is kept as before (an unqualified article title is the word's main sense,
      and a search is what people type); an X topic ("Tory", "Chow") or the ticker is kept
      unless the word is an everyday word (see everyday()).
    - One word of a longer label (partial: "Leoncio" of "Leoncio Gomez"): kept only when the
      word is rare (see rare()).
    - Either way, a match the word alone does not carry is kept when the coin's other text
      (name, description, X post, logo text) has another word of the label or of the story
      behind it: "West" with "Kanye" in the description.
    Matches through the referent, multi-word matches, and news and Bluesky hits (searched
    for the coin's own name, with their own rules) pass untouched."""
    words = set(_clean(support_text).split())
    out: list[TrendHit] = []
    for h in hits:
        if h.via is not None or " " in h.surface or h.term.source in ("news", "bluesky"):
            out.append(h)
            continue
        if h.surface.isdigit():
            continue  # "2026", "1000": a number names nothing
        if not h.partial and h.where != "symbol" and h.term.source != "x_trends":
            out.append(h)
            continue
        ok = rare(h.surface, k) if h.partial else not everyday(h.surface, k)
        if not ok:
            h.support = _agreeing(h, words, k)
            ok = h.support is not None
        if ok:
            out.append(h)
    return out


def _agreeing(h: TrendHit, words: set[str], k: Knowledge) -> str | None:
    """Another word of the trending label (any content word) or of its story (not an
    everyday one: headlines are long) that the coin's text also has."""
    label = _clean(h.term.term).split()
    story = _clean(h.headline or h.term.headline or "").split()
    for w in label:
        if w != h.surface and len(w) >= MIN_WORD_LEN and w not in gazetteer._STOP and w in words:
            return w
    for w in story:
        if (
            w != h.surface
            and len(w) >= MIN_WORD_LEN
            and w.isalpha()
            and w not in gazetteer._STOP
            and not everyday(w, k)
            and w in words
        ):
            return w
    return None


def _strength(t: TrendTerm) -> float:
    return score(TrendHit(t, "", "name"))


def score(h: TrendHit, index: TrendIndex | None = None) -> float:
    """How strong the trend is, 0-1 (trend.score, trend.terms[].score).
    wikipedia: log of the spike, 30x its usual views = 1; google_trends: log of the search
    count, 100 = 0, 100,000 = 1; news: the number of outlets' headlines, 8 = 1; x_trends: half
    its best rank (1 = 0.5, 50 = 0.01), half the hours listed (12 = 0.5); bluesky: posts in the
    last 24 hours, 25 = 1. A perennial entity (one of the best-known articles) counts half: it
    trends every day."""
    t = h.term
    if t.source == "news":
        s = t.views / 8
    elif t.source == "bluesky":
        s = t.views / 25
    elif t.source == "x_trends":
        rank = min(max(t.rank or 50, 1), 50)
        s = 0.5 * (51 - rank) / 50 + 0.5 * min(t.views, 12) / 12
    elif t.source == "google_trends":
        s = math.log10(max(t.views, 100) / 100) / 3
    else:
        s = math.log(max(t.spike, 1.0)) / math.log(30)
    s = max(0.05, min(1.0, s))
    if index is not None and t.source not in ("news", "bluesky") and index.is_generic(t.term):
        s *= 0.5
    return round(s, 3)


def referent_hits(
    evidence: list[Ev], index: TrendIndex, k: Knowledge, max_referents: int = 6
) -> list[TrendHit]:
    """Match the referents the coin resolves to (and those entities' aliases) against the
    trending terms: $PNUT resolves to "Peanut the Squirrel" whatever its name says, and a coin
    named "Elon" is about "Elon Musk" when that article spikes."""
    aliases = {e.evidence_source: e.aliases for e in k.entities}
    seen: set[str] = set()
    out: list[TrendHit] = []
    refs = sorted(
        (ev for ev in evidence if ev.referent is not None and ev.kind != "trend"),
        key=lambda ev: -(ev.referent.score if ev.referent else 0),
    )
    for ev in refs:
        ref = ev.referent
        assert ref is not None
        if ref.label in seen or ref.source.startswith(
            ("wikipedia:", "news:", "gtrends:", "xtrends:", "bsky:")
        ):
            continue
        seen.add(ref.label)
        if len(seen) > max_referents:
            break
        h = index.match_exact(ref.label, ev.where, "referent")
        if h is None:
            for a in aliases.get(ref.source, ()):
                if len(a) >= MIN_TERM_LEN:
                    h = index.match_exact(a, ev.where, "alias")
                    if h is not None:
                        break
        if h is not None:
            out.append(h)
    return out


PARTIAL_REFERENT_PENALTY = 0.1  # "Leoncio" names "Leoncio Gomez" less surely than the label


def evidence(hits: list[TrendHit], index: TrendIndex) -> list[Ev]:
    evs: list[Ev] = []
    for h in hits:
        got = _hit_evidence(h, index)
        if h.partial:
            for ev in got:
                if ev.referent is not None:
                    ev.referent.score = round(ev.referent.score - PARTIAL_REFERENT_PENALTY, 3)
                    ev.weight = ev.referent.score
        evs += got
    return evs


def _hit_evidence(h: TrendHit, index: TrendIndex) -> list[Ev]:
    t = h.term
    if t.source == "news":
        return news_evidence(h)
    if t.source == "google_trends":
        return gtrends_evidence(h, index)
    if t.source == "x_trends":
        return xtrends_evidence(h, index)
    if t.source == "bluesky":
        return bluesky_evidence(h)
    evs: list[Ev] = []
    if t.spike >= 10:
        w = 0.6
    elif t.spike >= 3:
        w = 0.45
    else:
        w = 0.3
    if index.is_generic(t.term):
        w *= 0.5  # "Donald Trump" trends every day; weak signal of a *new* event
    evs.append(
        Ev(
            kind="trend",
            label="news_event",
            weight=round(w, 3),
            detail=(
                f"{_matched(h)} matches the trending Wikipedia article '{t.term}' "
                f"({t.views:,} views, {t.spike:.1f}x its usual)"
            ),
            source=f"wikipedia:{t.term}",
            where=h.where,  # type: ignore[arg-type]
        )
    )
    # A clear spike names what the coin is about even when no gazetteer entry does
    # (a person or event that broke this week).
    if (
        t.spike >= 3
        and not index.is_generic(t.term)
        and h.where in ("name", "x")
        and h.via is None  # matched through a referent: that referent is the answer
    ):
        ref = ReferentCandidate(
            label=t.term,
            kind="event",
            desc=f"in the news: Wikipedia article trending at {t.spike:.0f}x its usual views",
            source=f"wikipedia:{t.term}",
            score=0.6 if t.spike >= 10 else 0.45,
            categories=["news_event"],
            surface=h.surface,
        )
        evs.append(
            Ev(
                kind="referent",
                label="referent",
                weight=ref.score,
                detail=f"{t.term}: {ref.desc}",
                source=f"wikipedia:{t.term}",
                where=h.where,  # type: ignore[arg-type]
                referent=ref,
            )
        )
    return evs


def _matched(h: TrendHit) -> str:
    if h.via == "referent":
        return f"its referent '{h.surface}'"
    if h.via == "alias":
        return f"its referent (as '{h.surface}')"
    what = f"the ticker '{h.surface}'" if h.where == "symbol" else f"'{h.surface}'"
    if h.partial:
        what += f" (one word of '{h.term.term}')"
    if h.support:
        what += f" (its text also says '{h.support}')"
    return what


def gtrends_evidence(h: TrendHit, index: TrendIndex) -> list[Ev]:
    """A Google Trends search: news_event evidence weighted by the search count, and (from the
    coin's own name or post) a referent for a story nothing else knows yet."""
    t = h.term
    w = 0.55 if t.views >= 10_000 else (0.45 if t.views >= 1_000 else 0.3)
    generic = index.is_generic(t.term)
    if generic:
        w *= 0.5
    story = f', e.g. "{t.headline[:90]}"' if t.headline else ""
    evs = [
        Ev(
            kind="trend",
            label="news_event",
            weight=round(w, 3),
            detail=(
                f"{_matched(h)} matches the Google Trends search '{t.term}' "
                f"({t.views:,}+ searches){story}"
            ),
            source=f"gtrends:{t.term.lower()}",
            where=h.where,  # type: ignore[arg-type]
        )
    ]
    if t.views >= 1_000 and not generic and h.where in ("name", "x") and h.via is None:
        ref = ReferentCandidate(
            label=t.term,
            kind="event",
            desc=f"trending on Google ({t.views:,}+ searches){story}",
            source=f"gtrends:{t.term.lower()}",
            score=0.5 if t.views >= 10_000 else 0.4,
            categories=["news_event"],
            surface=h.surface,
        )
        evs.append(
            Ev(
                kind="referent",
                label="referent",
                weight=ref.score,
                detail=f"{t.term}: {ref.desc}",
                source=ref.source,
                where=h.where,  # type: ignore[arg-type]
                referent=ref,
            )
        )
    return evs


def xtrends_evidence(h: TrendHit, index: TrendIndex) -> list[Ev]:
    """On X's trending list: news_event evidence weighted by rank, and (from the coin's own
    name or post) a referent for a topic nothing else knows yet."""
    t = h.term
    rank = t.rank or 50
    w = 0.5 if rank <= 10 else (0.4 if rank <= 25 else 0.3)
    generic = index.is_generic(t.term)
    if generic:
        w *= 0.5
    hours = f", {t.views} hour(s) on the list" if t.views > 1 else ""
    evs = [
        Ev(
            kind="trend",
            label="news_event",
            weight=round(w, 3),
            detail=(f"{_matched(h)} matches '{t.term}', trending on X (best rank #{rank}{hours})"),
            source=f"xtrends:{t.term.lower()}",
            where=h.where,  # type: ignore[arg-type]
        )
    ]
    if rank <= 25 and not generic and h.where in ("name", "x") and h.via is None:
        ref = ReferentCandidate(
            label=t.term,
            kind="event",
            desc=f"trending on X (best rank #{rank}{hours})",
            source=f"xtrends:{t.term.lower()}",
            score=0.45 if rank <= 10 else 0.4,
            categories=["news_event"],
            surface=h.surface,
        )
        evs.append(
            Ev(
                kind="referent",
                label="referent",
                weight=ref.score,
                detail=f"{t.term}: {ref.desc}",
                source=ref.source,
                where=h.where,  # type: ignore[arg-type]
                referent=ref,
            )
        )
    return evs


MIN_BLUESKY_POSTS = 3  # from at least three accounts: one person posting is not a trend
BLUESKY_WINDOW = timedelta(hours=24)


def bluesky_hit(phrase: str, posts: list[dict], now: datetime | None = None) -> TrendHit | None:
    """A trend hit for a coin name people are posting about on Bluesky, or None when fewer
    than MIN_BLUESKY_POSTS accounts did in the last day. posts: bluesky.relevant(...)."""
    from tokensage.sources.bluesky import created

    now = now or datetime.now(UTC)
    recent = [(p, d) for p in posts if (d := created(p)) is not None and now - d < BLUESKY_WINDOW]
    authors = {p.get("author") for p, _ in recent}
    if len(recent) < MIN_BLUESKY_POSTS or len(authors) < MIN_BLUESKY_POSTS:
        return None
    top = max(recent, key=lambda pd: int(pd[0].get("likes") or 0) + int(pd[0].get("reposts") or 0))
    return TrendHit(
        TrendTerm(phrase, 0.0, len(recent), source="bluesky", seen_at=max(d for _, d in recent)),
        phrase.lower(),
        "name",
        headline=" ".join(str(top[0].get("text") or "").split())[:200] or None,
    )


def bluesky_evidence(h: TrendHit) -> list[Ev]:
    """The coin's name is being posted about on Bluesky: weaker than the news (anyone can
    post), so news_event evidence, and a referent only when many posts carry it."""
    n = h.term.views
    eg = f', e.g. "{h.headline[:90]}"' if h.headline else ""
    evs = [
        Ev(
            kind="trend",
            label="news_event",
            weight=0.45 if n >= 10 else 0.3,
            detail=f"'{h.term.term}' is in {n} Bluesky post(s) from the last 24 hours{eg}",
            source=f"bsky:{h.term.term.lower()}",
            where=h.where,  # type: ignore[arg-type]
        )
    ]
    if n >= 10:
        ref = ReferentCandidate(
            label=h.term.term,
            kind="event",
            desc=f"being posted about: {n} Bluesky posts in a day{eg}",
            source=f"bsky:{h.term.term.lower()}",
            score=0.35,
            categories=["news_event"],
            surface=h.surface,
        )
        evs.append(
            Ev(
                kind="referent",
                label="referent",
                weight=ref.score,
                detail=f"{h.term.term}: {ref.desc}",
                source=ref.source,
                where=h.where,  # type: ignore[arg-type]
                referent=ref,
            )
        )
    return evs


MIN_NEWS_HEADLINES = 2  # from at least two outlets: one story is not "in the news"
# a one-word name is easier to find by chance ("Leoncio" is somebody in a local story): it
# takes three outlets
MIN_ONE_WORD_OUTLETS = 3
MIN_NEWS_WORD_LEN = 5


def news_word(name: str | None, k: Knowledge) -> str | None:
    """A one-word coin name rare enough to search the news for on its own ("Leoncio",
    "Glasnow"), or None. Everyday and frequent words ("Juniper", "West", "Tyler") match
    unrelated stories every day."""
    from tokensage.sources.gnews import _FILLER

    if not name:
        return None
    words = re.sub(r"[^\w' ]+", " ", name).split()
    while words and words[-1].isdigit():
        words.pop()
    content = [w for w in words if w.lower() not in _FILLER]
    if len(content) != 1 or len(words) > 3:
        return None
    w = content[0]
    if len(w) < MIN_NEWS_WORD_LEN or not w.isalpha() or not rare(w, k):
        return None
    return w


def news_hit(
    phrase: str, heads: list[dict], min_outlets: int = MIN_NEWS_HEADLINES
) -> TrendHit | None:
    """A trend hit for a coin name found in recent headlines, or None when too few outlets
    carry it (min_outlets: MIN_ONE_WORD_OUTLETS for a one-word name). heads: the relevant
    headlines (gnews.relevant) as dicts."""
    outlets = {(h.get("source") or h.get("title") or "").lower() for h in heads}
    if len(heads) < min_outlets or len(outlets) < min_outlets:
        return None
    newest = [d for d in (_published(h) for h in heads) if d is not None]
    return TrendHit(
        TrendTerm(phrase, 0.0, len(heads), source="news", seen_at=max(newest) if newest else None),
        phrase.lower(),
        "name",
        headline=str(heads[0].get("title") or "")[:200] or None,
    )


def _published(h: dict) -> datetime | None:
    """A cached headline's RSS pubDate as an aware UTC datetime."""
    raw = str(h.get("published") or "").strip()
    if not raw:
        return None
    try:
        d = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        return None
    return (d if d.tzinfo else d.replace(tzinfo=UTC)).astimezone(UTC)


def news_evidence(h: TrendHit) -> list[Ev]:
    """The coin's name is in the news: news_event evidence, and a referent for the story when
    several outlets carry it (a model launch, a viral animal, a court case that broke today)."""
    n = h.term.views
    eg = f', e.g. "{h.headline[:90]}"' if h.headline else ""
    evs = [
        Ev(
            kind="trend",
            label="news_event",
            weight=0.5 if n >= 5 else 0.35,
            detail=f"'{h.term.term}' is in {n} news headline(s) from the last 2 days{eg}",
            source=f"news:{h.term.term.lower()}",
            where=h.where,  # type: ignore[arg-type]
        )
    ]
    if n >= 3:
        ref = ReferentCandidate(
            label=h.term.term,
            kind="event",
            desc=f"in the news: {n} recent headlines{eg}",
            source=f"news:{h.term.term.lower()}",
            score=0.45 if n >= 5 else 0.35,
            categories=["news_event"],
            surface=h.surface,
        )
        evs.append(
            Ev(
                kind="referent",
                label="referent",
                weight=ref.score,
                detail=f"{h.term.term}: {ref.desc}",
                source=ref.source,
                where=h.where,  # type: ignore[arg-type]
                referent=ref,
            )
        )
    return evs
