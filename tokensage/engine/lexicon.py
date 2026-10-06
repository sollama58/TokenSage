"""S3 Lexicon and gazetteer matching with one Aho-Corasick automaton (guide §5.3).

Surfaces: slang terms, known-coin names/aliases/symbols, entity labels/aliases, WordNet
class words. Matches are word-bounded. Returns hits with the payload needed for scoring.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import ahocorasick

from tokensage.engine.knowledge import Entity, Knowledge, KnownCoin, SlangTerm

_lock = threading.Lock()
_auto: ahocorasick.Automaton | None = None


@dataclass(frozen=True)
class Hit:
    surface: str
    kind: str  # slang | coin | entity | wordnet
    payload: SlangTerm | KnownCoin | Entity | str  # wordnet: the class label
    start: int
    end: int


# WordNet words whose everyday sense in coin names is not the animal/food/vehicle one.
WORDNET_STOP = {
    "cycle",
    "bat",
    "fly",
    "seal",
    "bass",
    "jack",
    "chip",
    "chips",
    "date",
    "dash",
    "mint",
    "swallow",
    "buck",
    "ram",
    "crane",
    "drake",
    "pump",
    "fan",
    "cap",
    "hat",
    "coin",
    "king",
    "queen",
    "ace",
    "star",
    "sun",
    "moon",
    "fire",
    "ice",
    "rock",
    "roll",
    "jam",
    "pop",
    "rocket",
    "gas",
    "bull",
    "bear",
    "whale",
    "ape",
    "degen",
    "bag",
    "cup",
    "dish",
    "pot",
    "mother",
    "father",
    "baby",
    "boss",
    "chief",
    "nut",
    "nuts",
    "cookie",
    "tea",
    "toast",
    "pie",
    "ham",
    "spam",
    "bob",
    "bill",
    "pat",
    "sol",
    "max",
    "mac",
    "jet",
    "van",
    "sub",
}


def _build(k: Knowledge) -> ahocorasick.Automaton:
    a = ahocorasick.Automaton()
    entries: dict[str, list[tuple[str, object]]] = {}

    def add(surface: str, kind: str, payload: object) -> None:
        s = surface.lower().strip()
        if len(s) < 2:
            return
        entries.setdefault(s, []).append((kind, payload))

    for term in k.slang.values():
        add(term.term, "slang", term)
    for c in k.coins:
        for s in c.surfaces:
            add(s, "coin", c)
        if len(c.symbol) >= 3:
            add(c.symbol.lower(), "coin", c)
    for e in k.entities:
        for s in e.surfaces:
            add(s, "entity", e)
    for cls, words in k.wordnet.items():
        for w in words:
            if w not in WORDNET_STOP:
                add(w, "wordnet", cls)
    for s, payloads in entries.items():
        a.add_word(" " + s + " ", (s, payloads))
    a.make_automaton()
    return a


def automaton(k: Knowledge) -> ahocorasick.Automaton:
    global _auto
    if _auto is None:
        with _lock:
            if _auto is None:
                _auto = _build(k)
    return _auto


def find(text: str, k: Knowledge) -> list[Hit]:
    """Word-bounded matches in a lowercased, space-separated text."""
    if not text:
        return []
    padded = " " + " ".join(text.lower().split()) + " "
    hits: list[Hit] = []
    for end_idx, (surface, payloads) in automaton(k).iter(padded):
        end = end_idx  # index of the trailing space
        start = end - len(surface) - 1 + 1
        for kind, payload in payloads:
            hits.append(Hit(surface, kind, payload, start, end))
    # prefer longer surfaces when they overlap (e.g. "just a chill guy" over "chill guy")
    hits.sort(key=lambda h: (h.start, -(h.end - h.start)))
    kept: list[Hit] = []
    covered_until = -1
    last_start = -1
    for h in hits:
        if h.start == last_start:
            kept.append(h)  # same span, different payloads: keep all
            continue
        if h.start < covered_until:
            continue
        kept.append(h)
        covered_until = h.end
        last_start = h.start
    return kept


def wordnet_classes_for(word: str, k: Knowledge) -> list[str]:
    w = word.lower()
    return [cls for cls, words in k.wordnet.items() if w in words]
