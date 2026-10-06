"""The current pump.fun meta (guide §5.5, §5.8): our own launch corpus as a meaning signal.

A name launched 300 times today *is* the current meta even before any gazetteer knows it.
Two counts feed this, both gathered by the analyzer and passed in (this module is pure):

- the same-name tokens around this one (DbContext.same_name: our token table plus the
  pump.fun and DexScreener searches): how many launched within the window, and this
  token's place among them ("3rd of 41 $PNUT coins launched within 24 h of it");
- per word of the name, how many token names in our table carry it within the window,
  against its usual share over the longer history (an IDF-style lift, so "trump", which is
  in thousands of names every week, says little, while "sahur" spiking today says a lot).

Either one emits crypto_native/pumpfun_meta evidence; when nothing else resolves a
referent, the name (or word) becomes the referent "current pump.fun meta: <name>".
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from tokensage.engine.context import Ev, Normalized, ReferentCandidate
from tokensage.engine.knowledge import Knowledge

LABEL = "crypto_native/pumpfun_meta"


class _Launch(Protocol):  # pipeline.SameNameToken, without importing the pipeline
    mint: str
    symbol: str | None
    created_at: datetime | None


@dataclass
class MetaWord:
    word: str
    recent: int  # token names carrying the word, launched within the window
    total: int  # token names carrying the word over the history (window included)


@dataclass
class MetaCounts:
    """Word counts from our token table around this token's launch (see analyzer)."""

    recent_total: int = 0  # all tokens launched within the window
    history_total: int = 0  # all tokens over the history (window included)
    words: list[MetaWord] = field(default_factory=list)


@dataclass
class CopyRank:
    rank: int  # 1 = the earliest
    of: int  # same-name launches within the window, this token included
    window_hours: int

    def phrase(self, what: str) -> str:
        return (
            f"{_ordinal(self.rank)} of {self.of} {what} coins launched within "
            f"{self.window_hours} h of it"
        )


@dataclass
class MetaResult:
    evidence: list[Ev] = field(default_factory=list)
    rank: CopyRank | None = None
    context: list[str] = field(default_factory=list)  # summary clauses


def config(k: Knowledge) -> dict[str, Any]:
    return k.meta


def candidate_words(n: Normalized, k: Knowledge) -> list[str]:
    """The words of the name worth counting across other names: plain lowercase
    letters/digits (they go into a regex), long enough, not stop words."""
    cfg = config(k)
    stop = {str(w).lower() for w in cfg.get("stop_words") or []}
    min_len = int(cfg.get("min_word_len", 3))
    out: list[str] = []
    for tok in n.name_tokens:
        w = tok.lower()
        if len(w) >= min_len and w.isascii() and w.isalnum() and not w.isdigit() and w not in stop:
            if w not in out:
                out.append(w)
    return out[: int(cfg.get("max_words", 6))]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def copy_rank(
    mint: str, created_at: datetime | None, same_name: Sequence[_Launch], window_hours: int
) -> CopyRank | None:
    """This token's place by launch time among the same-name tokens launched within
    `window_hours` either side of it. None when it has no namesake in the window."""
    if created_at is None:
        return None
    me = _aware(created_at)
    seen: set[str] = {mint}
    earlier = later = 0
    for t in same_name:
        if t.mint in seen or t.created_at is None:
            continue
        gap = (me - _aware(t.created_at)).total_seconds()
        if abs(gap) > window_hours * 3600:
            continue
        seen.add(t.mint)
        if gap > 0:
            earlier += 1
        else:
            later += 1
    if earlier + later == 0:
        return None
    return CopyRank(rank=earlier + 1, of=earlier + later + 1, window_hours=window_hours)


def _weight(count: int, minimum: int, cfg: dict[str, Any]) -> float:
    w = float(cfg.get("weight_base", 0.35)) + float(cfg.get("weight_step", 0.1)) * math.log2(
        count / max(1, minimum) + 1
    )
    return round(min(float(cfg.get("weight_max", 0.75)), w), 3)


def _lift(w: MetaWord, counts: MetaCounts) -> float:
    """The word's share of names launched in the window over its share of the rest of the
    history, add-one smoothed (no history yet = no lift)."""
    recent_share = w.recent / max(1, counts.recent_total)
    base_n = max(0, w.total - w.recent)
    base_total = max(0, counts.history_total - counts.recent_total)
    return recent_share / ((base_n + 1) / (base_total + 1))


def hot_word(counts: MetaCounts | None, k: Knowledge) -> tuple[MetaWord, float] | None:
    """The name's word most over-represented in today's launches, if any clears the bar."""
    if counts is None:
        return None
    cfg = config(k)
    min_count = int(cfg.get("min_word_count", 5))
    min_lift = float(cfg.get("min_word_lift", 3.0))
    best: tuple[MetaWord, float] | None = None
    for w in counts.words:
        if w.recent < min_count:
            continue
        lift = _lift(w, counts)
        if lift >= min_lift and (best is None or lift > best[1]):
            best = (w, lift)
    return best


def assess(
    mint: str,
    name: str | None,
    ticker: str | None,
    created_at: datetime | None,
    same_name: Sequence[_Launch],
    counts: MetaCounts | None,
    k: Knowledge,
    *,
    is_famous: bool,
    has_referent: bool,
) -> MetaResult:
    """The meta evidence, copycat rank and summary clause for one token.

    is_famous: the token is an established coin itself; its clones are not a new meta.
    has_referent: some other stage already names what the coin is about; the meta then
    adds its category but not a referent of its own."""
    cfg = config(k)
    window = int(cfg.get("window_hours", 24))
    out = MetaResult()
    out.rank = copy_rank(mint, created_at, same_name, window)
    if is_famous:
        return out
    label_name = (name or "").strip() or (f"${ticker}" if ticker else "")
    what = f"${ticker}" if ticker else f"'{label_name}'"

    min_name = int(cfg.get("min_name_count", 5))
    if out.rank is not None and out.rank.of >= min_name and label_name:
        r = out.rank
        weight = _weight(r.of, min_name, cfg)
        detail = (
            f"{r.of} coins with this name or ticker launched within {window} h of it; "
            f"this is the {_ordinal(r.rank)}"
        )
        desc = f"launched {r.of} times within {window} h"
        out.evidence.append(_ev(label_name, weight, detail, desc, "name", has_referent, cfg))
        out.context.append(f"{r.phrase(what)} (a current meta)")
        return out

    hot = hot_word(counts, k)
    if hot is not None:
        w, lift = hot
        weight = round(
            _weight(w.recent, int(cfg.get("min_word_count", 5)), cfg)
            * float(cfg.get("word_factor", 0.8)),
            3,
        )
        detail = (
            f"'{w.word}' is in {w.recent} coin names launched within {window} h of it, "
            f"{'over 100' if lift > 100 else f'{lift:.0f}'}x its usual share over the last "
            f"{int(cfg.get('history_days', 90))} d"
        )
        # the word as the name writes it ("Sahur", not "sahur"), when it is spelled out there
        m = re.search(rf"(?<![^\W_]){re.escape(w.word)}(?![^\W_])", name or "", re.IGNORECASE)
        word = m.group(0) if m else w.word
        desc = f"in {w.recent} coin names launched within {window} h"
        out.evidence.append(_ev(word, weight, detail, desc, "word", has_referent, cfg))
        out.context.append(f"'{word}' is a current meta ({w.recent} coins in {window} h)")
    return out


def _ev(
    subject: str,
    weight: float,
    detail: str,
    desc: str,
    scope: str,
    has_referent: bool,
    cfg: dict[str, Any],
) -> Ev:
    if has_referent:
        return Ev("meta", LABEL, weight, detail, f"meta:{scope}:{subject.lower()}", "db")
    score = float(cfg.get(f"referent_score_{scope}", 0.4))
    ref = ReferentCandidate(
        label=f"current pump.fun meta: {subject}",
        kind="meme",
        desc=desc,
        source=f"meta:{scope}:{subject.lower()}",
        score=score,
        categories=[LABEL],
        surface=subject,
    )
    # kind "referent" so the aggregator takes the candidate; its label is the category
    return Ev("referent", LABEL, weight, detail, ref.source, "db", referent=ref)


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"
