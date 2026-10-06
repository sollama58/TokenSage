"""The open-world entity gazetteer (guide §4.4): tens of thousands of Wikidata people,
famous animals, memes, AI chatbots and pop-culture items, matched like the seed entities.

Entries come from the packaged snapshot (data/gazetteer_wikidata.json.gz, built by
scripts/build_gazetteer.py) or, once the monthly cron has run, from the `entity` table.
The hand-curated seed in data/ always wins: a surface the seed (or the slang, coin or
WordNet lists) already knows is never claimed by a Wikidata item.

Wikidata labels and aliases are noisy as match surfaces, so each one is filtered:
- one-word dictionary words never match ("Vine", "Office", "Drake");
- a person matches by a full name, or a one-word stage name that is their label (Rihanna),
  never by a bare given or family name alias;
- a one-word personal name ("Charlie" the dog) matches only for a very famous item;
- an alias made only of dictionary words ("Dark Lord") never matches;
- one-word surfaces and labels made only of dictionary words ("The Office") are
  name-only: they count in the coin's name, not in a description or a post, where they are
  most likely ordinary words.
"""

from __future__ import annotations

import gzip
import json
import math
import re
import threading
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import ahocorasick
from anyascii import anyascii

from tokensage.engine import wikiclass
from tokensage.engine.knowledge import DATA_DIR, Entity, Knowledge, load_knowledge

SNAPSHOT = "gazetteer_wikidata.json.gz"
COMMON_WORDS = "common_words.txt.gz"
MIN_CHARS = 4  # letters and digits in a surface
MAX_WORDS = 6
POPULARITY_CAP = 0.8  # a Wikidata item never outranks a curated seed entity of equal fame
SINGLE_NAME_MIN_LINKS = 40  # a one-word personal name matches only for a famous item

# Function words: a phrase made only of these and dictionary words is an ordinary phrase.
_STOP = {
    "the", "a", "an", "of", "and", "or", "in", "on", "at", "to", "for", "with", "by", "from",
    "is", "it", "my", "your", "our", "his", "her", "their", "this", "that", "i", "you", "we",
    "me", "us", "be", "no", "not", "all", "up", "out", "so", "as", "de", "la", "le", "el",
}  # fmt: skip
_PAREN = re.compile(r"\s*\([^)]*\)\s*$")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class GazEntry:
    """One item as stored: label, aliases and description as Wikidata gives them."""

    id: str  # Q-id
    label: str
    aliases: tuple[str, ...]
    desc: str
    kind: str
    categories: tuple[str, ...]
    sitelinks: int

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> GazEntry:
        return cls(
            id=str(d["id"]),
            label=str(d["label"]),
            aliases=tuple(str(a) for a in d.get("aliases") or []),
            desc=str(d.get("desc") or ""),
            kind=str(d.get("kind") or "other"),
            categories=tuple(str(c) for c in d.get("categories") or []),
            sitelinks=int(d.get("sitelinks") or 0),
        )


def surface_form(raw: str) -> str:
    """The lowercase, ASCII, space-separated form the lexicon matches against."""
    s = _PAREN.sub("", anyascii(raw or "")).lower().replace("'", "")
    return " ".join(_NON_ALNUM.sub(" ", s).split())


def popularity(sitelinks: int) -> float:
    """10 Wikipedias -> 0.4, 40 -> 0.64, 100+ -> 0.8 (the cap)."""
    if sitelinks <= 1:
        return 0.2
    return round(max(0.2, min(POPULARITY_CAP, math.log10(sitelinks) / 2.5)), 3)


@lru_cache
def common_words() -> frozenset[str]:
    """English dictionary words (WordNet lemmas with a common-noun, verb or adjective
    sense), built offline by scripts/build_gazetteer.py --words."""
    path = DATA_DIR / COMMON_WORDS
    if not path.exists():
        return frozenset()
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return frozenset(w.strip() for w in f if w.strip())


def is_common(word: str, common: frozenset[str] | None = None) -> bool:
    """A dictionary word, plurals included ("jeans", "cats")."""
    c = common if common is not None else common_words()
    return word in c or (len(word) > 3 and word.endswith("s") and word[:-1] in c)


def known_surfaces(k: Knowledge) -> set[str]:
    """Everything the curated knowledge already matches."""
    out: set[str] = set(k.slang)
    for c in k.coins:
        out.update(c.surfaces)
        out.add(c.symbol.lower())
    for e in k.entities:
        out.update(e.surfaces)
    for words in k.wordnet.values():
        out.update(words)
    return {surface_form(s) for s in out}


def person_name_words(entries: list[GazEntry]) -> set[str]:
    """First and last words of every person's full name ("charlie", "kirk"): a one-word
    surface that is also a personal name is too ambiguous unless the item is very famous
    (a dog named "Charlie" must not claim every Charlie)."""
    out: set[str] = set()
    for e in entries:
        if e.kind == "person":
            words = surface_form(e.label).split()
            if len(words) >= 2:
                out.update((words[0], words[-1]))
    return out


def surfaces_for(
    e: GazEntry,
    common: frozenset[str],
    taken: set[str],
    names: set[str] | frozenset[str] = frozenset(),
) -> list[tuple[str, bool]]:
    """(surface, name_only) pairs an entry may match by."""
    out: dict[str, bool] = {}
    label_form = surface_form(e.label)
    for raw in (e.label, *e.aliases):
        s = surface_form(raw)
        words = s.split()
        if not words or len(words) > MAX_WORDS or s in taken or s in out:
            continue
        if len(s.replace(" ", "")) < MIN_CHARS or all(w.isdigit() for w in words):
            continue
        if all(w in _STOP for w in words) or not any(len(w) >= 3 and w.isalpha() for w in words):
            continue  # "the one", "x ae a 12"
        if len(words) == 1:
            if is_common(s, common):
                continue  # "vine", "office", "drake": an ordinary word first
            if e.kind == "person" and s != label_form:
                continue  # "kissinger", "sweeney": a bare surname or first name
            if s in names and e.sitelinks < SINGLE_NAME_MIN_LINKS:
                continue  # "charlie" the dog, "claude" the singer
            out[s] = True
            continue
        if len(words) == 2 and e.kind == "person" and len(words[0]) == 1:
            continue  # "j smith"
        ordinary = all(is_common(w, common) or w in _STOP for w in words)
        if ordinary and s != label_form:
            continue  # a nickname of ordinary words ("dark lord", "king crow") misleads
        out[s] = ordinary
    return list(out.items())


@dataclass(frozen=True)
class GazHit:
    surface: str
    entity: Entity
    name_only: bool
    start: int
    end: int


class Gazetteer:
    """An Aho-Corasick automaton over the filtered surfaces of every entry."""

    def __init__(self, entries: list[GazEntry], k: Knowledge, version: str):
        self.version = version
        self.size = 0
        self._auto = ahocorasick.Automaton()
        common = common_words()
        taken = known_surfaces(k)
        names = person_name_words(entries)
        best: dict[str, tuple[int, Entity, bool]] = {}
        for g in entries:
            ent: Entity | None = None
            for s, name_only in surfaces_for(g, common, taken, names):
                have = best.get(s)
                if have is not None and have[0] >= g.sitelinks:
                    continue  # two items share a surface: the better-known one keeps it
                if ent is None:
                    cats = g.categories
                    if g.kind == "person":
                        # the description often says more than the occupation query did
                        # ("American political activist" found as a podcaster)
                        kind, more = wikiclass.classify(g.desc)
                        if kind == "person":
                            cats = tuple(dict.fromkeys([*more, *cats]))
                    ent = Entity(
                        label=g.label,
                        kind=g.kind,
                        aliases=(),
                        categories=cats,
                        desc=g.desc,
                        popularity=popularity(g.sitelinks),
                        source="wikidata",
                        ref_id=g.id,
                    )
                best[s] = (g.sitelinks, ent, name_only)
        for s, (_links, ent, name_only) in best.items():
            self._auto.add_word(" " + s + " ", (s, ent, name_only))
        self.size = len(best)
        if self.size:
            self._auto.make_automaton()

    def find(self, padded: str) -> list[GazHit]:
        """Matches in a text already lowercased and padded with single spaces."""
        if not self.size:
            return []
        out: list[GazHit] = []
        for end, (s, ent, name_only) in self._auto.iter(padded):
            out.append(GazHit(s, ent, name_only, end - len(s), end))
        return out


def load_snapshot() -> tuple[list[GazEntry], str]:
    path = DATA_DIR / SNAPSHOT
    if not path.exists():
        return [], "none"
    with gzip.open(path, "rt", encoding="utf-8") as f:
        doc = json.load(f)
    return [GazEntry.from_json(d) for d in doc.get("entities") or []], str(
        doc.get("version") or "snapshot"
    )


_lock = threading.Lock()
_packaged: Gazetteer | None = None


def packaged() -> Gazetteer:
    """The gazetteer built from the packaged snapshot (built once per process)."""
    global _packaged
    if _packaged is None:
        with _lock:
            if _packaged is None:
                entries, version = load_snapshot()
                _packaged = Gazetteer(entries, load_knowledge(), version)
    return _packaged
