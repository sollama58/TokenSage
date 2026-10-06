"""The request-time Wikipedia fallback (full depth): the parts of a coin's name, and the
capitalised names in its linked post, that no gazetteer entry covers are looked up on
Wikipedia, and a result whose title is that name becomes a referent with its short
description ("Sydney Sweeney Jeans" -> Sydney Sweeney, American actress).

Pure: the analyzer asks `spans()` what to look up, does the (cached) searches, and hands
`pick()`'s results back to the engine in EngineInput.wiki_refs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from tokensage.engine import lexicon, wikiclass
from tokensage.engine.context import Ev, Normalized, ReferentCandidate
from tokensage.engine.gazetteer import Gazetteer, common_words, is_common, packaged, surface_form
from tokensage.engine.knowledge import Knowledge, SlangTerm
from tokensage.sources.wikipedia import WikiPage

MAX_LOOKUPS = 4
MAX_SPAN_WORDS = 5

# Words that are never part of the name of what a coin is about: they split a name into
# runs ("Peanut Coin" -> "peanut").
_BREAK = {
    "coin", "token", "sol", "solana", "inu", "official", "x", "ai", "dao", "cto", "wif",
    "meme", "pump", "fun", "edition", "v2", "og", "classic",
}  # fmt: skip
# Words that can belong to a name ("Baby Shark", "The Office", "Lord of the Rings") but do
# not start or end one on their own; never looked up alone.
_SOFT = {
    "the", "a", "an", "of", "on", "and", "with", "in", "to", "for", "is", "my", "by", "at",
    "or", "from", "this", "that", "it", "its", "our", "your", "we", "be", "just", "new",
    "first", "real", "baby", "mini", "super", "mr", "mrs", "lil", "big", "little", "army",
    "gang", "club",
}  # fmt: skip
_EDGE_KEEP = {"the", "baby", "big", "little", "super", "mr", "mrs", "lil", "real"}
_CAP_SPAN = re.compile(
    r"\b[A-Z][\w'’.-]*[a-z][\w'’.-]*(?:\s+(?:(?:of|the|de|da|van|von|la|del)\s+)?"
    r"[A-Z][\w'’.-]*[a-z][\w'’.-]*){1,4}"
)
_DISAMBIG = ("disambiguation", "topics referred to by the same term", "list of")


@dataclass(frozen=True)
class Span:
    text: str  # the words to look up, as written in the normalized name
    where: str  # name | x
    common: bool  # every word is a dictionary word ("baby shark"): a weaker match


@dataclass(frozen=True)
class WikiRef:
    span: Span
    title: str
    desc: str
    qid: str | None


def _covered(text: str, k: Knowledge, gaz: Gazetteer | None, name_pass: bool) -> set[int]:
    """Indexes of the words a named lexicon match covers."""
    words = text.split()
    starts: list[int] = []
    pos = 1  # find() pads with one leading space
    for w in words:
        starts.append(pos)
        pos += len(w) + 1
    out: set[int] = set()
    for h in lexicon.find(text, k, gaz, name_pass=name_pass):
        if h.kind == "wordnet" or (isinstance(h.payload, SlangTerm) and h.payload.kind == "marker"):
            continue  # a dictionary sense or a "baby"/"mini" marker names nothing
        for i, s in enumerate(starts):
            if h.start <= s < h.end:
                out.add(i)
    return out


def _runs(words: list[str], covered: set[int]) -> list[list[str]]:
    runs: list[list[str]] = []
    cur: list[str] = []
    for i, w in enumerate(words):
        if i in covered or w in _BREAK or not w.isalpha():
            if cur:
                runs.append(cur)
            cur = []
            continue
        cur.append(w)
    if cur:
        runs.append(cur)
    out: list[list[str]] = []
    for run in runs:
        # "baby shark" and "the office" keep their first word; "shark of" does not
        while run and run[0] in _SOFT and run[0] not in _EDGE_KEEP:
            run = run[1:]
        while run and run[-1] in _SOFT:
            run = run[:-1]
        if run:
            out.append(run)
    return out


def spans(n: Normalized, post_texts: list[str], k: Knowledge, gaz: Gazetteer | None) -> list[Span]:
    """What to look up, most promising first: the name's uncovered runs of words (and,
    for a long run, the run without its last or first word: "sydney sweeney jeans" ->
    "sydney sweeney"), then capitalised names in the post that nothing matched."""
    common = common_words()
    gaz = gaz if gaz is not None else packaged()
    out: list[Span] = []
    seen: set[str] = set()

    def add(words: list[str], where: str) -> None:
        text = " ".join(words)
        if not words or text in seen or len(words) > MAX_SPAN_WORDS:
            return
        if words[0] in _SOFT and words[0] not in _EDGE_KEEP or words[-1] in _SOFT:
            return
        all_common = all(is_common(w, common) or w in _SOFT for w in words)
        if len(words) == 1 and (all_common or len(text) < 4):
            return  # one dictionary word ("jeans") names nothing on its own
        seen.add(text)
        out.append(Span(text, where, all_common))

    name_text = " ".join(n.name_tokens)
    words = name_text.split()
    for run in _runs(words, _covered(name_text, k, gaz, True)):
        add(run, "name")
        if len(run) >= 2:
            add(run[:-1], "name")
            if len(run) >= 3:
                add(run[1:], "name")
    for raw in post_texts:
        for m in _CAP_SPAN.finditer(raw or ""):
            phrase = surface_form(m.group(0))
            if not phrase or _covered(phrase, k, gaz, False):
                continue
            pw = [w for w in phrase.split() if w not in ("the",)]
            if len(pw) >= 2 and not all(is_common(w, common) for w in pw):
                add(pw, "x")
    return out[:MAX_LOOKUPS]


def _title_matches(span: str, title: str) -> bool:
    t = surface_form(title)  # drops a trailing "(TV series)"
    if not t:
        return False
    if t == span or (" " in span and t.removeprefix("the ") == span):
        return True
    return fuzz.ratio(span, t) >= 92 and abs(len(span) - len(t)) <= 2


def pick(span: Span, pages: list[WikiPage]) -> WikiRef | None:
    """The search result that *is* this name: among the top three, the first whose title
    matches the span, that is an article (not a disambiguation page) with a description."""
    for p in sorted(pages, key=lambda x: x.rank)[:3]:
        d = p.desc.lower()
        if p.disambiguation or not p.desc or any(x in d for x in _DISAMBIG):
            continue
        if p.title.lower().startswith("list of"):
            continue
        if _title_matches(span.text, p.title):
            return WikiRef(span, p.title, p.desc, p.qid)
    return None


def evidence(refs: list[WikiRef]) -> list[Ev]:
    evs: list[Ev] = []
    for r in refs:
        kind, cats = wikiclass.classify(r.desc)
        if r.span.where == "name":
            score = 0.45 if r.span.common else 0.6
        else:
            score = 0.4
        src = f"wikipedia_search:{r.title}"
        ref = ReferentCandidate(
            label=r.title,
            kind=kind,
            desc=r.desc,
            source=src,
            score=score,
            categories=cats,
            surface=r.span.text,
        )
        for cat in cats:
            evs.append(
                Ev(
                    kind="entity",
                    label=cat,
                    weight=round(score * 0.8, 3),
                    detail=f"'{r.span.text}' is the Wikipedia article {r.title} ({r.desc})",
                    source=src,
                    where=r.span.where,  # type: ignore[arg-type]
                    referent=ref,
                )
            )
        evs.append(
            Ev(
                kind="entity",
                label="referent",
                weight=score,
                detail=f"{r.title}: {r.desc} (Wikipedia search for '{r.span.text}')",
                source=src,
                where=r.span.where,  # type: ignore[arg-type]
                referent=ref,
            )
        )
    return evs
