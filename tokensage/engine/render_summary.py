"""S10 Template-generated summary and caveats. Never free text (guide §3)."""

from __future__ import annotations

from tokensage.engine.aggregate import Aggregated
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
}


def _top_parent(categories: list[tuple[str, float]]) -> tuple[str, float] | None:
    for lbl, s in categories:
        if "/" not in lbl:
            return lbl, s
    return None


def summarize(
    name: str | None,
    ticker: str | None,
    agg: Aggregated,
    ticker_explanation: str | None,
    extra_caveats: list[str],
    max_bullets: int = 5,
    context: list[str] | None = None,
) -> tuple[str, list[str]]:
    head = f"{name or '(unnamed)'} (${ticker or '?'})"
    parts: list[str] = []
    if agg.referent and agg.referent.score >= 0.45:
        desc = f": {agg.referent.desc}" if agg.referent.desc else ""
        parts.append(
            f"{head} most likely refers to {agg.referent.label}{desc} "
            f"(confidence {agg.referent.score:.2f})."
        )
    else:
        tp = _top_parent(agg.categories)
        if tp:
            parts.append(
                f"{head} reads as {_CATEGORY_PHRASE.get(tp[0], tp[0])} (confidence {tp[1]:.2f})."
            )
        else:
            parts.append(f"{head}: no clear reference found; see evidence and caveats.")
    subs = [(lbl, s) for lbl, s in agg.categories if "/" in lbl][:4]
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
        if e.referent is not None and e.label == "referent":
            if e.referent.label in seen_referents:
                continue  # one line per referent, however many sources agree
            seen_referents.add(e.referent.label)
        seen.add(d.lower())
        out.append(d)
        if len(out) >= n:
            break
    return out
