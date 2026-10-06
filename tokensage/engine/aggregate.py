"""S9 Aggregation (guide §5.9): evidence -> per-label confidences, referent, flags."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from tokensage.engine.context import Ev, ReferentCandidate
from tokensage.engine.knowledge import Knowledge
from tokensage.taxonomy import category_labels

_WHERE_FACTOR = {"description": "description_factor", "image": None, "x": None}
# Labels about the coin's context, not its subject: they score on their own but do not
# lift their parent (a coin paired against BONK is not thereby a crypto in-joke coin).
NO_PARENT = {"crypto_native/paired_ecosystem"}


@dataclass
class Aggregated:
    categories: list[tuple[str, float]]  # sorted desc
    referent: ReferentCandidate | None
    referent_runner_up: ReferentCandidate | None
    evidence: list[Ev]
    caveats: list[str] = field(default_factory=list)


def _effective_weight(ev: Ev, k: Knowledge) -> float:
    w = ev.weight
    if ev.where == "description":
        w *= k.scoring.get("description_factor", 0.6)
    return max(0.0, min(1.0, w))


def _parent(label: str) -> str | None:
    return label.split("/")[0] if "/" in label else None


def aggregate(evidence: list[Ev], k: Knowledge) -> Aggregated:
    labels = category_labels()
    cap = k.scoring.get("cap", 0.97)
    bonus = k.scoring.get("diversity_bonus", 0.08)
    min_conf = k.scoring.get("min_category_confidence", 0.2)

    # dedupe identical (label, source, where) so the same rule firing twice doesn't inflate
    seen: set[tuple[str, str, str, str]] = set()
    uniq: list[Ev] = []
    for ev in evidence:
        key = (ev.kind, ev.label, ev.source, ev.where)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(ev)

    # per-label noisy-OR plus source-diversity bonus
    comp: dict[str, float] = {}
    wheres: dict[str, set[str]] = {}
    for ev in uniq:
        if ev.label not in labels:
            continue
        w = _effective_weight(ev, k)
        if w <= 0:
            continue
        targets = [ev.label]
        p = _parent(ev.label) if ev.label not in NO_PARENT else None
        if p and p in labels:
            targets.append(p)
        for t in targets:
            comp[t] = comp.get(t, 1.0) * (1 - w)
            wheres.setdefault(t, set()).add(ev.where)
    scores: dict[str, float] = {}
    for label, c in comp.items():
        conf = 1 - c
        extra = max(0, len(wheres.get(label, set())) - 1)
        conf = min(cap, conf + bonus * min(extra, 2))
        scores[label] = round(conf, 3)

    # conflict: several animal species -> keep the top one strong, soften the rest
    animals = sorted(
        (
            (lbl, s)
            for lbl, s in scores.items()
            if lbl.startswith("animal/") and lbl != "animal/other"
        ),
        key=lambda x: -x[1],
    )
    for lbl, s in animals[1:]:
        if s < animals[0][1]:
            scores[lbl] = round(s * 0.7, 3)

    categories = sorted(
        ((lbl, s) for lbl, s in scores.items() if s >= min_conf), key=lambda x: (-x[1], x[0])
    )

    # referent: noisy-OR over candidates with the same label
    rc: dict[str, ReferentCandidate] = {}
    rcomp: dict[str, float] = {}
    surf: dict[str, str] = {}  # first matched words seen for a label, whichever row
    voted: set[tuple[str, str]] = set()  # one vote per (source, referent) however many rows
    for ev in uniq:
        r = ev.referent
        if r is None or ev.kind not in ("referent", "entity", "image_hash", "template_family"):
            continue  # known_coin / inherit duplicates must not inflate the referent
        if (ev.source, r.label) in voted:
            continue
        voted.add((ev.source, r.label))
        w = max(0.0, min(0.98, r.score))
        if ev.where == "description":
            w *= k.scoring.get("description_factor", 0.6)
        rcomp[r.label] = rcomp.get(r.label, 1.0) * (1 - w)
        if r.label not in rc or r.score > rc[r.label].score:
            rc[r.label] = r
        if r.surface and r.label not in surf:
            surf[r.label] = r.surface
    ranked = sorted(((1 - c, lbl) for lbl, c in rcomp.items()), reverse=True)
    referent = runner = None
    caveats: list[str] = []
    if ranked:
        top_s, top_l = ranked[0]
        referent = ReferentCandidate(
            label=rc[top_l].label,
            kind=rc[top_l].kind,
            desc=rc[top_l].desc,
            source=rc[top_l].source,
            score=round(min(cap, top_s), 3),
            categories=rc[top_l].categories,
            surface=surf.get(top_l),
        )
        if len(ranked) > 1:
            s2, l2 = ranked[1]
            # a copy: rc[l2] is the candidate object shared with the evidence entries
            runner = dataclasses.replace(rc[l2], score=round(s2, 3), surface=surf.get(l2))
            if top_s - s2 < k.scoring.get("referent_ambiguity_gap", 0.1) and s2 >= 0.3:
                caveats.append(
                    f"referent is ambiguous: '{top_l}' ({top_s:.2f}) vs '{l2}' ({s2:.2f})"
                )
        if referent.score < 0.45:
            caveats.append("referent is a weak guess")
    return Aggregated(
        categories=categories,
        referent=referent,
        referent_runner_up=runner,
        evidence=uniq,
        caveats=caveats,
    )
