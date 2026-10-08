"""S10 Template-generated summary and caveats. Never free text (guide §3)."""

from __future__ import annotations

from tokensage.engine.aggregate import Aggregated, is_relation, main_category
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


def _phrase(label: str, categories: list[tuple[str, float]]) -> str:
    if label == "crypto_native":
        for lbl, _ in categories:  # sorted by confidence
            if lbl in _CRYPTO_PHRASE:
                return _CRYPTO_PHRASE[lbl]
    return _CATEGORY_PHRASE.get(label, label)


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
    rival: bool = False,
) -> tuple[str, list[str]]:
    """framing: "a cat coin" when the referent is only the name's modifier. narrative: the
    X post the coin was launched on, if any. rival: the name sets itself against the
    referent ("Doge Killer")."""
    head = f"{name or '(unnamed)'} (${ticker or '?'})"
    parts: list[str] = []
    r = agg.referent
    if r and r.score >= 0.45:
        desc = f" ({r.desc})" if r.desc else ""
        conf = f" (confidence {r.score:.2f})"
        if rival:
            parts.append(f"{head} sets itself against {r.label}{desc}{conf}.")
        elif framing:
            parts.append(f"{head} is {framing} tied to {r.label}{desc}{conf}.")
        else:
            parts.append(f"{head} {_verb(r.score)} {r.label}{desc}{conf}.")
    elif narrative:
        # no name anyone knows: the post it was launched on is the story
        joiner = " was " if narrative.startswith("launched") else ": "
        parts.append(f"{head}{joiner}{narrative}.")
        narrative = None
        tp = main_category(agg.categories)
        guess = f"; weak guess: {r.label}" if r and r.score >= 0.3 else ""
        if tp:
            parts.append(
                f"Beyond that it reads as {_phrase(tp[0], agg.categories)} "
                f"(confidence {tp[1]:.2f}){guess}."
            )
        elif guess:
            parts.append(f"No clear reference in the name{guess}.")
    else:
        tp = main_category(agg.categories)
        guess = f"; weak guess: {r.label}" if r and r.score >= 0.3 else ""
        if tp:
            parts.append(
                f"{head} reads as {_phrase(tp[0], agg.categories)} (confidence {tp[1]:.2f}){guess}."
            )
        else:
            parts.append(f"{head}: no clear reference found; see evidence and caveats{guess}.")
    if narrative:
        parts.append(narrative[0].upper() + narrative[1:] + ".")
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
