"""Lineage: is this coin the original, an early copy or a late copy, and of what.

Pure: the same-name tokens and the logo near-duplicates come in from the database context
(pipeline.DbContext), so golden tests run it without a database.

- The original is the earliest coin, launched within the copycat window before this one,
  with the same name or ticker (same_name) or a near-identical logo (logo near-duplicates).
  A coin sharing both wins over one sharing only one of them.
- rank / rank_of are the existing same-name copy rank (meta.copy_rank, either side of the
  launch). A logo-only copy is ranked among the earlier coins with a near-identical logo.
- siblings_*h count the coins with the same name, ticker or a near-identical logo launched
  in the N hours up to this coin's launch, this one included, so a re-read later does not
  change them (no look-ahead).
- Kinds (thresholds in data/meta.yaml, `lineage`): early_copy = rank <= 3 and the original
  launched less than 6 h earlier; late_copy = rank > 10 or the original launched more than
  24 h earlier; copy = any other copy; reference = builds on an established coin only;
  original = none of the above; unknown = no launch time.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from tokensage.engine.context import Normalized
from tokensage.engine.image import NearDup
from tokensage.engine.knowledge import Knowledge, KnownCoin
from tokensage.engine.normalize import clean_ticker


class _Launch(Protocol):  # pipeline.SameNameToken, without importing the pipeline
    mint: str
    name: str | None
    symbol: str | None
    created_at: datetime | None


@dataclass
class Original:
    mint: str
    name: str | None
    ticker: str | None
    created_at: datetime | None
    age_s: int | None  # seconds from its launch to this coin's
    match: list[str] = field(default_factory=list)  # name | ticker | image
    image_distance: int | None = None


@dataclass
class Lineage:
    kind: str  # original | early_copy | copy | late_copy | reference | unknown
    original: Original | None = None
    logo_original: Original | None = None  # the earliest logo source, when another coin
    rank: int | None = None
    rank_of: int | None = None
    window_hours: int | None = None
    siblings_1h: int | None = None
    siblings_6h: int | None = None
    siblings_24h: int | None = None
    logo_reuse_24h: int | None = None
    logo_first_seen_at: datetime | None = None
    reference: KnownCoin | None = None  # the established coin, for kind "reference"


MIN_GAP_S = 300


def config(k: Knowledge) -> dict[str, Any]:
    return dict(k.meta.get("lineage") or {})


def _aware(d: datetime) -> datetime:
    return d if d.tzinfo is not None else d.replace(tzinfo=UTC)


def _compact(s: str | None) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _gap(me: datetime, other: datetime | None) -> float | None:
    return None if other is None else (me - _aware(other)).total_seconds()


def match_inputs(n: Normalized, name: str | None, symbol: str | None) -> list[str]:
    out: list[str] = []
    if n.name_compact and _compact(name) == n.name_compact:
        out.append("name")
    if n.ticker and clean_ticker(symbol or "").upper() == n.ticker.upper():
        out.append("ticker")
    return out


def assess(
    mint: str,
    created_at: datetime | None,
    n: Normalized,
    same_name: Sequence[_Launch],
    logo_near: Sequence[NearDup],
    k: Knowledge,
    *,
    window_days: int,
    rank: tuple[int, int, int] | None,
    self_coins: Sequence[KnownCoin],
    references: Sequence[KnownCoin],
    has_logo: bool,
    top: Sequence[Any] = (),
) -> Lineage:
    """rank: (rank, rank_of, window_hours) from meta.copy_rank. self_coins: the established
    coins this token is by name and ticker (known_coins.is_self). references: the
    established coins it builds on. has_logo: its logo was hashed (logo counts apply)."""
    cfg = config(k)
    early_rank = int(cfg.get("early_max_rank", 3))
    early_age = float(cfg.get("early_max_original_age_h", 6)) * 3600
    late_rank = int(cfg.get("late_min_rank", 11))
    late_age = float(cfg.get("late_min_original_age_h", 24)) * 3600

    if created_at is None:
        ref = self_coins[0] if self_coins else (references[0] if references else None)
        return Lineage(kind="reference" if ref else "unknown", reference=ref)
    me = _aware(created_at)
    window = window_days * 86400
    top_match = next((t for t in top if t.mint != mint and match_inputs(n, t.name, t.symbol)), None)

    # every coin sharing a name/ticker or logo, launched before this one (and the gap)
    names: dict[str, tuple[_Launch, float]] = {}
    for t in same_name:
        g = _gap(me, t.created_at)
        if t.mint != mint and g is not None and 0 <= g <= window:
            names.setdefault(t.mint, (t, g))
    logos: dict[str, tuple[NearDup, float]] = {}
    first_logo: datetime | None = me if has_logo else None
    for nd in logo_near:
        c = nd.candidate
        g = _gap(me, c.created_at)
        if not c.mint or c.mint == mint or g is None or g < 0:
            continue
        if c.created_at is not None and first_logo is not None:
            first_logo = min(first_logo, _aware(c.created_at))
        if g <= window and (c.mint not in logos or nd.distance < logos[c.mint][0].distance):
            logos[c.mint] = (nd, g)

    out = Lineage(kind="original")
    if has_logo:
        out.logo_reuse_24h = sum(1 for _, g in logos.values() if g <= 86400)
        out.logo_first_seen_at = first_logo
    for hours in (1, 6, 24):
        sib = {m for m, (_, g) in names.items() if g <= hours * 3600}
        sib |= {m for m, (_, g) in logos.items() if g <= hours * 3600}
        setattr(out, f"siblings_{hours}h", len(sib) + 1)

    if self_coins:
        # a launch with a famous coin's exact name and ticker is that coin only when the
        # mint agrees (or no mint is known for it); otherwise it is a clone of it
        known = [c for c in self_coins if c.mint]
        if known and not any(c.mint == mint for c in known):
            out.kind, out.reference = "reference", known[0]
        return out

    def original(mint_: str) -> Original:
        t = names.get(mint_)
        nd = logos.get(mint_)
        src_name = t[0].name if t else nd[0].candidate.name if nd else None
        src_sym = t[0].symbol if t else nd[0].candidate.symbol if nd else None
        when = t[0].created_at if t else nd[0].candidate.created_at if nd else None
        gap = t[1] if t else nd[1] if nd else None
        o = Original(
            mint=mint_,
            name=src_name,
            ticker=src_sym,
            created_at=when,
            age_s=int(gap) if gap is not None else None,
            match=match_inputs(n, src_name, src_sym),
        )
        if nd:
            o.match.append("image")
            o.image_distance = nd[0].distance
        return o

    # strictly earlier than this coin: a same-second launch is a sibling, not a source
    # launched more than 5 min earlier (as the copycat rule): a near-simultaneous launch is
    # a sibling on the same idea, not its source
    earlier_names = sorted((g, m) for m, (_, g) in names.items() if g > MIN_GAP_S)
    earlier_logos = sorted((g, m) for m, (_, g) in logos.items() if g > MIN_GAP_S)
    both = sorted((g, m) for g, m in earlier_names if m in logos)
    chosen: str | None = None
    if both:
        chosen = both[-1][1]
    elif earlier_names:
        chosen = earlier_names[-1][1]
    elif earlier_logos:
        chosen = earlier_logos[-1][1]
    if earlier_logos and earlier_logos[-1][1] != chosen:
        out.logo_original = original(earlier_logos[-1][1])
    if chosen is None and top_match is not None and top_match.mint != mint:
        # no earlier namesake in our corpus, but it shares its name or ticker with one of
        # today's most-traded coins: a copy of a running coin of unknown age
        out.original = Original(
            mint=top_match.mint,
            name=top_match.name or None,
            ticker=top_match.symbol or None,
            created_at=None,
            age_s=None,
            match=match_inputs(n, top_match.name, top_match.symbol),
        )
        out.kind = "copy"
        return out
    if chosen is None:
        if references:
            out.kind, out.reference = "reference", references[0]
        return out

    o = original(chosen)
    out.original = o
    if chosen in names and rank is not None:
        out.rank, out.rank_of, out.window_hours = rank
    elif chosen not in names:
        # a logo-only copy: its place among the earlier same-logo coins of the last day
        before = sum(1 for g, _ in earlier_logos if g <= 86400)
        out.rank, out.rank_of, out.window_hours = before + 1, before + 1, 24
    age = o.age_s or 0
    if (out.rank is not None and out.rank >= late_rank) or age > late_age:
        out.kind = "late_copy"
    elif (out.rank is None or out.rank <= early_rank) and age < early_age:
        out.kind = "early_copy"
    else:
        out.kind = "copy"
    return out


def candidate_mints(
    mint: str,
    created_at: datetime | None,
    same_name: Sequence[_Launch],
    logo_mints: Sequence[tuple[str, datetime | None]],
    window_days: int,
    per_kind: int = 5,
    top_mints: Sequence[str] = (),
) -> list[str]:
    """The coins assess() may pick as this one's original: the earliest namesakes, the
    earliest same-logo coins and the earliest coin sharing both, launched within the window
    before it, plus top-volume coins sharing its name or ticker. The analyzer loads their
    stored reads for pipeline._inherit."""
    out: list[str] = [m for m in top_mints if m != mint]
    if created_at is None:
        return out
    me = _aware(created_at)
    window = window_days * 86400

    def earlier(pairs: list[tuple[str, datetime | None]]) -> list[tuple[datetime, str]]:
        return sorted(
            (_aware(when), m)
            for m, when in pairs
            if m != mint
            and when is not None
            and MIN_GAP_S < (me - _aware(when)).total_seconds() <= window
        )

    names = earlier([(t.mint, t.created_at) for t in same_name])
    logos = earlier(list(logo_mints))
    logo_set = {m for _, m in logos}
    both = [x for x in names if x[1] in logo_set][:1]
    for lst in (both, names[:per_kind], logos[:per_kind]):
        for _, m in lst:
            if m not in out:
                out.append(m)
    return out
