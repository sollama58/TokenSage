"""S8 Trend matching (guide §5.8): match token text against spiking Wikipedia articles."""

from __future__ import annotations

import re
from dataclasses import dataclass

import ahocorasick

from tokensage.engine.context import Ev, ReferentCandidate
from tokensage.engine.knowledge import Knowledge

GENERIC_PREFIXES = ("list of", "deaths in", "main page", "special:", "wikipedia:", "portal:")
MIN_TERM_LEN = 4


@dataclass(frozen=True)
class TrendTerm:
    term: str  # article title, spaces not underscores
    spike: float  # views / prior median
    views: int
    source: str = "wikipedia"


@dataclass
class TrendHit:
    term: TrendTerm
    surface: str
    where: str


class TrendIndex:
    """Aho-Corasick over trend surfaces; rebuilt only when the term set changes."""

    def __init__(self, terms: list[TrendTerm], k: Knowledge):
        self.terms = terms
        self._auto = ahocorasick.Automaton()
        self._generic = {e.label.lower() for e in k.entities if e.popularity >= 0.95}
        n = 0
        best: dict[str, TrendTerm] = {}
        for t in terms:
            for s in surfaces_for(t.term):
                # two terms can share a surface: keep the stronger spike, not the last added
                if s not in best or t.spike > best[s].spike:
                    best[s] = t
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

    def is_generic(self, term: str) -> bool:
        return term.lower() in self._generic


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


def evidence(hits: list[TrendHit], index: TrendIndex) -> list[Ev]:
    evs: list[Ev] = []
    for h in hits:
        t = h.term
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
                    f"'{h.surface}' matches the trending Wikipedia article '{t.term}' "
                    f"({t.views:,} views, {t.spike:.1f}x its usual)"
                ),
                source=f"wikipedia:{t.term}",
                where=h.where,  # type: ignore[arg-type]
            )
        )
        # A clear spike names what the coin is about even when no gazetteer entry does
        # (a person or event that broke this week).
        if t.spike >= 3 and not index.is_generic(t.term) and h.where in ("name", "x"):
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
