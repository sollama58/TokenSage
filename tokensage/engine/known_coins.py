"""S5 Known-coin / copycat / derivative detection (guide §5.5)."""

from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

from tokensage.engine.context import Ev, Normalized, ReferentCandidate
from tokensage.engine.knowledge import Knowledge, KnownCoin


@dataclass
class CopyMatch:
    coin: KnownCoin
    signals: list[str] = field(default_factory=list)
    score: float = 0.0  # 0..1
    via_template_surface: bool = False


def _compact(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


# Surfaces that are naming *templates*, not the coin's subject: "Trump wif Hat" borrows the
# WIF template but is about Trump, so the parent's animal categories must not carry over.
TEMPLATE_SURFACES = {"wif hat", "wif", "inu", "dogwifhat", "catwifhat"}


def match_known(
    n: Normalized, k: Knowledge, extra: list[KnownCoin] | None = None
) -> list[CopyMatch]:
    coins = list(k.coins) + list(extra or [])
    th = k.scoring.get("fuzzy_threshold", 86)
    out: dict[str, CopyMatch] = {}

    def bump(c: KnownCoin, signal: str, score: float) -> None:
        m = out.setdefault(c.symbol + "|" + c.name, CopyMatch(c))
        if signal not in m.signals:
            m.signals.append(signal)
        m.score = 1 - (1 - m.score) * (1 - score)

    tb = n.ticker_base.upper()
    tfull = n.ticker.upper()
    compact = n.name_compact
    for c in coins:
        sym = c.symbol.upper()
        if tfull and tfull == sym:
            bump(c, "ticker", 0.75)
        elif tb and tb == sym and tb != tfull:
            bump(c, "ticker_base", 0.7)
        # best name signal across ALL surfaces: exact > contains > fuzzy
        best: tuple[int, str, float, str] | None = None  # (rank, signal, score, surface)
        for s in c.surfaces:
            sc = _compact(s)
            if len(sc) < 3 or not compact:
                continue
            cand: tuple[int, str, float, str] | None = None
            if compact == sc:
                cand = (3, "name", 0.85, s)
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
            if best[1] == "name_contains" and best[3].lower() in TEMPLATE_SURFACES:
                out[c.symbol + "|" + c.name].via_template_surface = True
    matches = sorted(out.values(), key=lambda m: -m.score)
    has_marker = any(mk.kind for mk in n.markers)
    strong: list[CopyMatch] = []
    for m in matches:
        if (
            m.score >= 0.7
            or len(m.signals) >= 2
            or "name" in m.signals
            or (has_marker and "name_contains" in m.signals)
        ):
            strong.append(m)
    return strong[:5]


def is_self(match: CopyMatch, n: Normalized) -> bool:
    """The token *is* the famous coin (same ticker and same name), not a copy of it.
    Markers that are part of the coin's own name ("inu" in Shiba Inu) don't count."""
    surfaces = {_compact(s) for s in match.coin.surfaces}
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
        )
        verb = "matches" if self_coin else "builds on"
        detail = f"{verb} ${c.symbol} ({c.name}) via {', '.join(m.signals)}"
        if not self_coin:
            evs.append(
                Ev(
                    kind="known_coin",
                    label=f"derivative/{sub}",
                    weight=min(k.scoring.get("known_coin_exact_weight", 0.9), m.score),
                    detail=detail,
                    source=f"known_coins:{c.symbol}",
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
        if fam.pattern.search(spaced) or fam.pattern.search(n.name_clean):
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
