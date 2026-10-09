"""S5 Known-coin / copycat / derivative detection (guide §5.5)."""

from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

from tokensage.engine import segment
from tokensage.engine.context import Ev, Normalized, ReferentCandidate
from tokensage.engine.knowledge import Knowledge, KnownCoin


@dataclass
class CopyMatch:
    coin: KnownCoin
    signals: list[str] = field(default_factory=list)
    score: float = 0.0  # 0..1
    via_template_surface: bool = False
    surface: str | None = None  # the name surface that matched, if any


def _compact(s: str) -> str:
    return "".join(filter(str.isalnum, s.lower()))


# Surfaces that are naming *templates*, not the coin's subject: "Trump wif Hat" borrows the
# WIF template but is about Trump, so the parent's animal categories must not carry over.
TEMPLATE_SURFACES = {"wif hat", "wif", "inu", "dogwifhat", "catwifhat"}


def _distinctive(surface: str, k: Knowledge) -> bool:
    """A single word that names the coin's subject on its own ("peanut", "moodeng"), not a
    dictionary word ("cat") or a short/common one."""
    w = surface.lower().strip()
    if " " in w or len(w) < 4 or not w.isalpha():
        return False
    return not any(w in words for words in k.wordnet.values())


# Words that never carry a coin's subject on their own (shared with the pipeline's head word).
HEAD_STOP = {
    "coin", "token", "sol", "solana", "inu", "hat", "cap", "beanie", "edition", "fun", "pump",
    "meme", "official", "the", "a", "an", "of", "on", "x", "ai", "dao", "v2", "2", "cto", "army",
    "gang", "club", "wif", "killer", "slayer", "season", "szn", "mode", "moon", "rocket",
    "mania", "fever", "summer", "winter", "era", "vibes", "energy", "maxi", "king", "queen",
    "god", "lord", "boss", "time", "life", "world", "nation", "party", "money", "cash", "bag",
}  # fmt: skip


def head_word(n: Normalized) -> str | None:
    """The head of the name: in "Elon's Cat" it is "cat" (the coin is a cat), in "Trump Dog"
    "dog". The last content word, by English compound order."""
    for t in reversed(n.name_tokens):
        if len(t) >= 3 and t not in HEAD_STOP and t.isalpha():
            return t
    return None


def _everyday_modifier(surface: str, n: Normalized, k: Knowledge) -> bool:
    """A one-word surface that is an everyday word ("mother", "house", "pump", "andy": among
    the 20k most frequent) used as a modifier of something else ("Mother Earth", "House
    Cat", "Andy Warhol"): there it keeps its everyday sense and does not name the coin. As
    the name's head word ("Justice for Peanut", "Peanut Army", "Not Peanut") it does."""
    w = surface.lower().strip()
    if " " in w or w not in segment.common_words(k):
        return False
    return head_word(n) != w


def match_known(
    n: Normalized, k: Knowledge, extra: list[KnownCoin] | None = None
) -> list[CopyMatch]:
    coins = list(k.coins) + list(extra or [])
    th = k.scoring.get("fuzzy_threshold", 86)
    out: dict[str, CopyMatch] = {}
    words = set(n.name_tokens)

    def bump(c: KnownCoin, signal: str, score: float) -> None:
        m = out.setdefault(c.symbol + "|" + c.name, CopyMatch(c))
        if signal not in m.signals:
            m.signals.append(signal)
        m.score = 1 - (1 - m.score) * (1 - score)

    tb = n.ticker_base.upper()
    tfull = n.ticker.upper()
    compact = n.name_compact
    needs_support: set[str] = set()  # an everyday-word name match: not strong on its own
    for c in coins:
        sym = c.symbol.upper()
        if tfull and tfull == sym:
            bump(c, "ticker", 0.75)
        elif tb and tb == sym and tb != tfull:
            bump(c, "ticker_base", 0.7)
        # best name signal across ALL surfaces: exact > contains > fuzzy
        best: tuple[int, str, float, str] | None = None  # (rank, signal, score, surface)
        for s, sc in c.compact_surfaces:
            if len(sc) < 3 or not compact:
                continue
            cand: tuple[int, str, float, str] | None = None
            if compact == sc:
                cand = (3, "name", 0.85, s)
            elif s.lower() in words and _distinctive(s, k):
                cand = (2, "name_word", 0.55, s)  # "Not Peanut", "Peanut Army"
            elif len(sc) >= 4 and sc in compact and len(compact) <= len(sc) + 12:
                cand = (2, "name_contains", 0.55, s)
            else:
                r = fuzz.ratio(compact, sc)
                if r >= th and len(compact) >= 4:
                    cand = (1, f"name_fuzzy:{int(r)}", 0.5, s)
            if cand and (best is None or cand[0] > best[0]):
                best = cand
        if best:
            bump(c, best[1], best[2])
            out[c.symbol + "|" + c.name].surface = best[3]
            if best[1] == "name_contains" and best[3].lower() in TEMPLATE_SURFACES:
                out[c.symbol + "|" + c.name].via_template_surface = True
            if best[1] in ("name_word", "name_contains") and _everyday_modifier(best[3], n, k):
                needs_support.add(c.symbol + "|" + c.name)
    matches = sorted(out.values(), key=lambda m: -m.score)
    has_marker = any(mk.kind for mk in n.markers)
    strong: list[CopyMatch] = []
    for m in matches:
        key = m.coin.symbol + "|" + m.coin.name
        if key in needs_support and not ({"ticker", "ticker_base"} & set(m.signals)):
            continue  # "House Cat" is a cat, not Housecoin, unless the ticker says so too
        if m.signals == ["ticker_base"]:
            continue  # an affix-stripped ticker alone ($BPNUT on "Zorp") is a hint, not a copy
        if (
            m.score >= 0.7
            or len(m.signals) >= 2
            or "name" in m.signals
            or "name_word" in m.signals
            or (has_marker and "name_contains" in m.signals)
        ):
            strong.append(m)
    return strong[:5]


def is_self(match: CopyMatch, n: Normalized) -> bool:
    """The token *is* the famous coin (same ticker and same name), not a copy of it.
    Markers that are part of the coin's own name ("inu" in Shiba Inu) don't count."""
    surfaces = {sc for _, sc in match.coin.compact_surfaces}
    own = " ".join(match.coin.surfaces).casefold()
    foreign_markers = [m for m in n.markers if m.text.casefold().strip() not in own]
    return (
        n.ticker.upper() == match.coin.symbol.upper()
        and _compact(n.name_clean) in surfaces
        and not foreign_markers
        and "homoglyph" not in n.obfuscation
    )


def evidence_for(matches: list[CopyMatch], n: Normalized, k: Knowledge) -> list[Ev]:
    evs: list[Ev] = []
    for m in matches:
        c = m.coin
        self_coin = is_self(m, n)
        derivative_markers = [mk for mk in n.markers if mk.kind]
        # an established coin's name/ticker is a reference; "copycat" is kept for copies
        # of coins launched recently (see pipeline._same_name_evidence)
        sub = "reference"
        if derivative_markers:
            kinds = {mk.kind for mk in derivative_markers}
            sub = (
                "sequel"
                if "sequel" in kinds
                else ("template_family" if "template_family" in kinds else "reference")
            )
        if "homoglyph" in n.obfuscation:
            sub = "homoglyph_spoof"
        ref = ReferentCandidate(
            label=c.referent_label,
            kind=c.referent_kind,
            desc=c.referent_desc or c.lore,
            source=f"known_coins:{c.symbol}",
            score=0.3
            if m.via_template_surface
            else min(0.95, m.score + (0.1 if self_coin else 0.0)),
            categories=list(c.categories),
            surface=m.surface,
        )
        verb = "matches" if self_coin else "builds on"
        detail = f"{verb} ${c.symbol} ({c.name}) via {', '.join(m.signals)}"
        # which input matched: a ticker-only match is the symbol's, not the name's
        where = "symbol" if m.signals and set(m.signals) <= {"ticker", "ticker_base"} else "name"
        if not self_coin:
            evs.append(
                Ev(
                    kind="known_coin",
                    label=f"derivative/{sub}",
                    weight=min(k.scoring.get("known_coin_exact_weight", 0.9), m.score),
                    detail=detail,
                    source=f"known_coins:{c.symbol}",
                    where=where,  # type: ignore[arg-type]
                    referent=ref,
                )
            )
        # inherit the parent's categories at reduced weight; a template-only match ("X wif
        # hat") carries the meme template but not the parent's subject (its animal)
        for cat in c.categories:
            if m.via_template_surface and not cat.startswith(("meme_template", "derivative")):
                continue
            evs.append(
                Ev(
                    kind="known_coin_inherit",
                    label=cat,
                    weight=round(m.score * (0.8 if self_coin else 0.6), 3),
                    detail=f"inherited from ${c.symbol}: {c.lore}",
                    source=f"known_coins:{c.symbol}",
                    where=where,  # type: ignore[arg-type]
                    referent=ref,
                )
            )
        evs.append(
            Ev(
                kind="referent",
                label="referent",
                weight=ref.score,
                detail=f"{c.referent_label}: {c.referent_desc or c.lore}",
                source=f"known_coins:{c.symbol}",
                where=where,  # type: ignore[arg-type]
                referent=ref,
            )
        )
    return evs


def family_evidence(n: Normalized, k: Knowledge) -> list[Ev]:
    evs: list[Ev] = []
    spaced = " ".join(n.name_tokens)
    by_sym = k.coin_by_symbol()
    for fam in k.families:
        if fam.weight <= 0:
            continue
        hit = fam.pattern.search(spaced) or fam.pattern.search(n.name_clean)
        if hit:
            parent = by_sym.get(fam.parent or "", [None])[0] if fam.parent else None
            ref = None
            if parent:
                ref = ReferentCandidate(
                    label=parent.referent_label,
                    kind=parent.referent_kind,
                    desc=parent.referent_desc,
                    source=f"family:{fam.name}",
                    score=fam.weight * 0.7,
                    categories=list(parent.categories),
                    surface=hit.group(0).strip(),
                )
            for cat in fam.categories:
                evs.append(
                    Ev(
                        kind="template_family",
                        label=cat,
                        weight=fam.weight,
                        detail=f"name follows the '{fam.name}' template"
                        + (f" derived from ${parent.symbol}" if parent else ""),
                        source=f"templates:{fam.name}",
                        referent=ref,
                    )
                )
    return evs
