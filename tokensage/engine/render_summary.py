"""S10 Template-generated summary and caveats. Never free text (guide §3)."""

from __future__ import annotations

from tokensage.engine.aggregate import Aggregated, is_relation, is_theme, main_category
from tokensage.engine.context import Ev

_CATEGORY_PHRASE = {
    "animal": "an animal-mascot coin",
    "meme_template": "an internet-meme coin",
    "ai_agent": "an AI / agent-themed coin",
    "political": "a political (PolitiFi) coin",
    "celebrity": "a celebrity-themed coin",
    "news_event": "a coin riding a news or viral event",
    "food_object_abstract": "a coin about an everyday object or concept",
    "regional_language": "a regional / language-community coin",
    "crypto_native": "a crypto-culture in-joke coin",
    "derivative": "a derivative of an existing coin",
    "humor_crude_offensive": "a crude-humour coin",
    "tradfi": "a stock-market / tradfi-themed coin",
    "pop_culture": "a film / TV / game / pop-culture coin",
}
# crypto_native covers people, chains, companies and in-jokes alike: its strongest sub-label
# says which (paired_ecosystem is context, not subject, so it never names the theme).
_CRYPTO_PHRASE = {
    "crypto_native/slang": "a crypto-slang coin",
    "crypto_native/person": "a coin about a crypto figure",
    "crypto_native/chain_or_coin": "a coin about a blockchain or established coin",
    "crypto_native/company": "a coin about a crypto exchange or company",
    "crypto_native/trading": "a crypto-trading coin",
    "crypto_native/tech": "a blockchain-tech coin",
    "crypto_native/launchpad": "a launchpad / launch-platform coin",
    "crypto_native/pumpfun_meta": "a pump.fun-meta joke coin",
    "crypto_native/cto": "a community-takeover coin",
    "crypto_native/utility_claim": "a coin claiming utility (a tool or protocol)",
}


def category_phrase(label: str, categories: list[tuple[str, float]]) -> str:
    if label == "crypto_native":
        for lbl, _ in categories:  # sorted by confidence
            if lbl in _CRYPTO_PHRASE:
                return _CRYPTO_PHRASE[lbl]
    return _CATEGORY_PHRASE.get(label, label)


def _has_theme(agg: Aggregated) -> bool:
    """The coin's own inputs give it a theme. Labels it only borrows from the token it trades
    against (db-only) are context unless its name builds on that token."""
    cats = agg.categories
    if not any(lbl == "derivative/pair_family" for lbl, _ in cats):
        cats = [(lbl, c) for lbl, c in cats if agg.inputs.get(lbl) != ["db"]]
    tp = main_category(cats)
    return tp is not None and is_theme(tp[0])


def _verb(score: float) -> str:
    if score >= 0.8:
        return "refers to"
    if score >= 0.6:
        return "most likely refers to"
    return "may refer to"


def summarize(
    name: str | None,
    ticker: str | None,
    agg: Aggregated,
    ticker_explanation: str | None,
    extra_caveats: list[str],
    max_bullets: int = 5,
    context: list[str] | None = None,
    framing: str | None = None,
    narrative: str | None = None,
    pair_who: tuple[str, str | None] | None = None,
    pair_builds_on: bool = False,
    rival: bool = False,
    referent_confidence: float | None = None,
) -> tuple[str, list[str]]:
    """framing: "a cat coin" when the referent is only the name's modifier. narrative: the
    X post the coin was launched on, if any. pair_who: the token it trades against instead
    of SOL, as (its ticker and name, what it is): ("$OGC (OG Callers)", "itself a pump.fun
    coin"). pair_builds_on: the coin's name builds on that token. rival: the name sets itself
    against the referent ("Doge Killer")."""
    head = f"{name or '(unnamed)'} (${ticker or '?'})"
    parts: list[str] = []
    r = agg.referent
    if r and r.score >= 0.45 and not r.generic:
        # a kind-only referent ("dog", from the pair token or the coin this one copies) is
        # what the coin reads as, not what it refers to: the category sentence says it
        desc = f" ({r.desc})" if r.desc else ""
        # the reported (banded) confidence, as referent.confidence says it
        score = referent_confidence if referent_confidence is not None else r.score
        conf = f" (confidence {score:.2f})"
        if rival:
            parts.append(f"{head} sets itself against {r.label}{desc}{conf}.")
        elif framing:
            parts.append(f"{head} is {framing} tied to {r.label}{desc}{conf}.")
        else:
            parts.append(f"{head} {_verb(score)} {r.label}{desc}{conf}.")
    elif narrative or (pair_who and not _has_theme(agg)):
        # no name anyone knows: the post it was launched on, or (when the name has no theme
        # either) the token it was launched against, is the story
        if narrative:
            joiner = " was " if narrative.startswith("launched") else ": "
            parts.append(f"{head}{joiner}{narrative}.")
            narrative = None
        elif pair_who:
            parts.append(
                f"{head} was launched into the {pair_who[0]} community, trading against it"
                + (" and building on its name." if pair_builds_on else " instead of SOL.")
                + (f" {pair_who[0]} is {pair_who[1]}." if pair_who[1] else "")
            )
            pair_who = None
        tp = main_category(agg.categories)
        guess = f"; weak guess: {r.label}" if r and r.score >= 0.3 and not r.generic else ""
        if tp:
            parts.append(
                f"Beyond that it reads as {category_phrase(tp[0], agg.categories)} "
                f"(confidence {tp[1]:.2f}){guess}."
            )
        elif guess:
            parts.append(f"No clear reference in the name{guess}.")
    else:
        tp = main_category(agg.categories)
        guess = f"; weak guess: {r.label}" if r and r.score >= 0.3 and not r.generic else ""
        if tp:
            phrase = category_phrase(tp[0], agg.categories)
            parts.append(f"{head} reads as {phrase} (confidence {tp[1]:.2f}){guess}.")
        else:
            parts.append(f"{head}: no clear reference found; see evidence and caveats{guess}.")
    if narrative:
        parts.append(narrative[0].upper() + narrative[1:] + ".")
    if pair_who:
        parts.append(
            f"It trades against {pair_who[0]} instead of SOL"
            + ("; its name builds on it." if pair_builds_on else ".")
            + (f" {pair_who[0]} is {pair_who[1]}." if pair_who[1] else "")
        )
    # the theme before the relation: "animal/frog 0.49, derivative/copycat 0.75"
    subs = sorted(
        ((lbl, s) for lbl, s in agg.categories if "/" in lbl), key=lambda x: is_relation(x[0])
    )[:4]
    if subs:
        parts.append("Categories: " + ", ".join(f"{lbl} {s:.2f}" for lbl, s in subs) + ".")
    if context:
        parts.append("Context: " + "; ".join(context) + ".")
    if ticker_explanation:
        parts.append(ticker_explanation.rstrip(".") + ".")
    bullets = _why(agg.evidence, max_bullets)
    if bullets:
        parts.append("Why: " + "; ".join(bullets) + ".")
    caveats = list(dict.fromkeys([*agg.caveats, *extra_caveats]))
    return " ".join(parts), caveats


def _why(evidence: list[Ev], n: int) -> list[str]:
    informative = [e for e in evidence if e.weight > 0 and e.kind not in ("onchain", "metadata")]
    informative.sort(key=lambda e: -e.weight)
    out: list[str] = []
    seen: set[str] = set()
    seen_referents: set[str] = set()
    for e in informative:
        d = e.detail.strip().rstrip(".")
        if d.lower() in seen:
            continue
        if e.referent is not None and e.kind in ("referent", "entity"):
            if e.referent.label in seen_referents:
                continue  # one line per referent, however many rows name it
            seen_referents.add(e.referent.label)
        seen.add(d.lower())
        out.append(d)
        if len(out) >= n:
            break
    return out
