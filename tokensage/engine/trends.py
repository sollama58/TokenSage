"""S8 Trend matching (guide §5.8): match token text, and the referent it resolves to, against
spiking Wikipedia articles, Google Trends searches and Google News headlines."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal

import ahocorasick

from tokensage.engine.context import Ev, ReferentCandidate
from tokensage.engine.knowledge import Knowledge

GENERIC_PREFIXES = ("list of", "deaths in", "main page", "special:", "wikipedia:", "portal:")
MIN_TERM_LEN = 4


@dataclass(frozen=True)
class TrendTerm:
    term: str  # article title, spaces not underscores
    spike: float  # views / prior median (0 for news and Google Trends terms)
    # wikipedia: daily views; news: the number of matching headlines; google_trends: Google's
    # approximate search count
    views: int
    # "wikipedia" | "news" (the coin's name is in recent headlines) | "google_trends"
    source: str = "wikipedia"
    # how fresh it is: the UTC day of the Wikipedia spike, the newest matching headline, or when
    # Google Trends listed the search
    seen_at: datetime | None = None
    headline: str | None = None  # the story behind a Google Trends search


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
                # two terms can share a surface: keep the stronger spike, not the last added
                if s not in best or t.spike > best[s].spike:
                    best[s] = t
        self._best = best
        for s, t in best.items():
            self._auto.add_word(" " + s + " ", (s, t))
            n += 1
        if n:
            self._auto.make_automaton()
        self._n = n

    def match(self, text: str, where: str) -> list[TrendHit]:
        if not self._n or not text:
            return []
        padded = " " + " ".join(text.lower().split()) + " "
        out: dict[str, TrendHit] = {}
        for _end, (s, t) in self._auto.iter(padded):
            if t.term not in out or len(s) > len(out[t.term].surface):
                out[t.term] = TrendHit(t, s, where)
        return list(out.values())

    def match_exact(self, phrase: str, where: str, via: str) -> TrendHit | None:
        """A trending term whose surface is this whole phrase (a referent label or alias):
        "Donald Trump" must not match a trending "Trump" article, only "Donald Trump"."""
        key = _clean(phrase)
        t = self._best.get(key) if key else None
        return TrendHit(t, key, where, via=via) if t is not None else None

    def is_generic(self, term: str) -> bool:
        return term.lower() in self._generic


def _clean(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split())


def surfaces_for(title: str) -> list[str]:
    """'Peanut (squirrel)' -> ['peanut (squirrel)', 'peanut squirrel', 'peanut']"""
    t = title.replace("_", " ").strip()
    low = t.lower()
    if low.startswith(GENERIC_PREFIXES) or ":" in low:
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
    return [re.sub(r"[^a-z0-9 ]+", " ", s).strip() for s in out if s]


def score(h: TrendHit, index: TrendIndex | None = None) -> float:
    """How strong the trend is, 0-1 (trend.score, trend.terms[].score).
    wikipedia: log of the spike, 30x its usual views = 1; google_trends: log of the search
    count, 100 = 0, 100,000 = 1; news: the number of outlets' headlines, 8 = 1. A perennial
    entity (one of the best-known articles) counts half: it trends every day."""
    t = h.term
    if t.source == "news":
        s = t.views / 8
    elif t.source == "google_trends":
        s = math.log10(max(t.views, 100) / 100) / 3
    else:
        s = math.log(max(t.spike, 1.0)) / math.log(30)
    s = max(0.05, min(1.0, s))
    if index is not None and t.source != "news" and index.is_generic(t.term):
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
        if ref.label in seen or ref.source.startswith(("wikipedia:", "news:", "gtrends:")):
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


def evidence(hits: list[TrendHit], index: TrendIndex) -> list[Ev]:
    evs: list[Ev] = []
    for h in hits:
        t = h.term
        if t.source == "news":
            evs += news_evidence(h)
            continue
        if t.source == "google_trends":
            evs += gtrends_evidence(h, index)
            continue
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
    return f"'{h.surface}'"


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


MIN_NEWS_HEADLINES = 2  # from at least two outlets: one story is not "in the news"


def news_hit(phrase: str, heads: list[dict]) -> TrendHit | None:
    """A trend hit for a coin name found in recent headlines, or None when too few outlets
    carry it. heads: the relevant headlines (gnews.relevant) as dicts."""
    outlets = {(h.get("source") or h.get("title") or "").lower() for h in heads}
    if len(heads) < MIN_NEWS_HEADLINES or len(outlets) < MIN_NEWS_HEADLINES:
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
