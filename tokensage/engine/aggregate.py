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
# Labels that say how the coin relates to another coin (a copy, a "Baby X"), not what it is
# about: kept in categories[] as the relation, never the coin's main category.
RELATION_PREFIXES = ("derivative",)
# Top-level labels about the coin's setting, not its subject: a name in Han or Cyrillic says
# which community it is for, not what it is about. The main category only when nothing else is.
CONTEXT_LABELS = {"regional_language"}
# On a tie between two themes the subject wins over the setting: a "golden bull" is a bull
# before it is crypto slang. Shared by main_category and the generic referent's kind.
THEME_ORDER = [
    "animal", "celebrity", "meme_template", "pop_culture", "news_event", "political",
    "ai_agent", "tradfi", "food_object_abstract", "humor_crude_offensive", "crypto_native",
]  # fmt: skip
# Evidence source prefixes of the trend stage (trends.py): the world's input
TREND_SOURCES = {"wikipedia", "gtrends", "xtrends", "news", "bsky"}
# Evidence kinds that only say what a word usually means, not what this coin refers to.
DICTIONARY_KINDS = {"wordnet", "emoji"}
# Labels a dictionary noun gives any text that has one: only the name, ticker or image can
# start them.
NOUN_ONLY = {"food_object_abstract"}


@dataclass
class Aggregated:
    categories: list[tuple[str, float]]  # sorted desc
    referent: ReferentCandidate | None
    referent_runner_up: ReferentCandidate | None
    evidence: list[Ev]
    caveats: list[str] = field(default_factory=list)
    # per category label: the independent inputs that agree on it, strongest first
    inputs: dict[str, list[str]] = field(default_factory=dict)

    @property
    def main(self) -> tuple[str, float] | None:
        return main_category(self.categories)


def is_relation(label: str) -> bool:
    return label.split("/")[0] in RELATION_PREFIXES


def is_theme(label: str) -> bool:
    """A label about what the coin is about: not the copy relation, not its setting (the
    script of its name, the pair it trades against)."""
    return (
        not is_relation(label)
        and label.split("/")[0] not in CONTEXT_LABELS
        and (label not in NO_PARENT)
    )


def theme_rank(label: str) -> int:
    top = label.split("/")[0]
    return THEME_ORDER.index(top) if top in THEME_ORDER else len(THEME_ORDER)


def main_category(categories: list[tuple[str, float]]) -> tuple[str, float] | None:
    """The coin's main category: its strongest top-level theme, the subject before the
    setting on a tie. A relation (derivative) or context label (regional_language) is the
    main category only when the coin has no theme at all."""
    tops = [(lbl, s) for lbl, s in categories if "/" not in lbl]
    themes = [(lbl, s) for lbl, s in tops if is_theme(lbl)]
    if themes:
        return max(themes, key=lambda t: (t[1], -theme_rank(t[0])))
    return tops[0] if tops else None


def _effective_weight(ev: Ev, k: Knowledge) -> float:
    w = ev.weight
    if ev.where == "description":
        w *= k.scoring.get("description_factor", 0.6)
    return max(0.0, min(1.0, w))


def _parent(label: str) -> str | None:
    return label.split("/")[0] if "/" in label else None


def channel(ev: Ev, symbol_is_name: bool = False) -> str:
    """The independent input a piece of evidence comes from: name, symbol, description,
    image, x, trend or db. A ticker that spells the name is the name again, and a trend or
    headline hit is the world's input whatever text it was found in."""
    if ev.kind == "trend" or (ev.referent is not None and ev.source.split(":")[0] in TREND_SOURCES):
        # a trending article, search, topic, headline or post naming the referent
        return "trend"
    if ev.where == "symbol" and symbol_is_name:
        return "name"
    if ev.where == "chain":
        return "db"
    return ev.where


def aggregate(evidence: list[Ev], k: Knowledge, symbol_is_name: bool = False) -> Aggregated:
    """Category confidence from the number and kind of agreeing inputs.

    Within one input, extra matches add only part of their weight (two dictionary words in a
    description are not two witnesses); across independent inputs (name, ticker, image, X,
    description, trend) the strengths combine by noisy-OR, plus a bonus per agreeing input.
    So two independent agreements read higher than one, and the value moves with how
    strong each input is rather than sitting on a few steps."""
    labels = category_labels()
    cap = k.scoring.get("cap", 0.97)
    bonus = k.scoring.get("diversity_bonus", 0.08)
    min_conf = k.scoring.get("min_category_confidence", 0.2)
    within = k.scoring.get("within_input_factor", 0.5)
    agree_floor = k.scoring.get("agreeing_input_min", 0.15)

    # dedupe identical (label, source, where) so the same rule firing twice doesn't inflate
    seen: set[tuple[str, str, str, str]] = set()
    uniq: list[Ev] = []
    for ev in evidence:
        key = (ev.kind, ev.label, ev.source, ev.where)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(ev)

    def targets(ev: Ev) -> list[str]:
        out = [ev.label]
        p = _parent(ev.label) if ev.label not in NO_PARENT else None
        if p and p in labels:
            out.append(p)
        return out

    # A noun in the description or a post ("a blanket burrito", "kiss and run") only backs
    # up a food/object theme the name, ticker or image already has: every description has
    # nouns. (An animal the description names, "the best dog on earth", still counts.)
    backed: set[str] = set()
    for ev in uniq:
        if ev.label in labels and not (
            ev.kind in DICTIONARY_KINDS and ev.where in ("description", "x")
        ):
            backed.update(t.split("/")[0] for t in targets(ev))

    # per label, per input: the weights of the rows that support it
    per: dict[str, dict[str, list[float]]] = {}
    named: set[str] = set()  # labels with evidence beyond dictionary words
    image_backed: set[str] = set()
    for ev in uniq:
        if ev.label not in labels:
            continue
        if (
            ev.kind in DICTIONARY_KINDS
            and ev.where in ("description", "x")
            and ev.label in NOUN_ONLY
            and ev.label not in backed
        ):
            continue
        w = _effective_weight(ev, k)
        if w <= 0:
            continue
        ch = channel(ev, symbol_is_name)
        for t in targets(ev):
            per.setdefault(t, {}).setdefault(ch, []).append(w)
            if ev.kind not in DICTIONARY_KINDS:
                named.add(t)
            if ev.where == "image":
                image_backed.add(t)
    scores: dict[str, float] = {}
    inputs: dict[str, list[str]] = {}
    dict_cap = k.scoring.get("wordnet_only_cap", 0.6)
    food_cap = k.scoring.get("food_dictionary_cap", 0.45)
    for label, chans in per.items():
        strength: dict[str, float] = {}
        for ch, ws in chans.items():
            top = max(ws)
            rest = 1.0
            for w in ws:
                rest *= 1 - w
            strength[ch] = top + within * ((1 - rest) - top)
        miss = 1.0
        for v in strength.values():
            miss *= 1 - v
        conf = 1 - miss
        agreeing = [ch for ch, v in strength.items() if v >= agree_floor]
        conf = min(cap, conf + bonus * min(max(0, len(agreeing) - 1), 2))
        if label not in named:
            # "ani is a bird" and "🐿" are dictionary senses, not knowledge of this coin: a
            # label they alone support never outranks one a named entity or coin supports
            conf = min(conf, dict_cap)
            if label == "food_object_abstract" and label not in image_backed:
                # "it has a noun" is not a theme: below the 0.5 filter unless the logo agrees
                conf = min(conf, food_cap)
        scores[label] = round(conf, 3)
        inputs[label] = sorted(strength, key=lambda ch: -strength[ch])

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
            generic=rc[top_l].generic,
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
        inputs={lbl: inputs[lbl] for lbl, _ in categories},
    )
