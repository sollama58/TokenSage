"""S4 Ticker <-> name explanation (guide §5.4)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz, utils

from tokensage.engine.context import Normalized
from tokensage.engine.knowledge import Knowledge

_VOWELS = re.compile(r"[aeiou]")
_BABY_TALK = [("fw", "fr"), ("wif", "with"), ("smol", "small"), ("wen", "when"), ("ser", "sir")]


@dataclass
class TickerExplanation:
    # known_coin | equals_token | acronym | slang | subsequence | vowel_drop | fuzzy |
    # baby_talk | unrelated | empty
    method: str
    text: str
    confidence: float


def _is_subsequence(short: str, long: str) -> bool:
    it = iter(long)
    return all(ch in it for ch in short)


def _lev1(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1:
        return False
    return fuzz.ratio(a, b) >= 100 * (1 - 1 / max(len(a), len(b), 1)) - 1e-9


def explain(n: Normalized, k: Knowledge) -> TickerExplanation:
    t = n.ticker_base.lower()
    full = n.ticker.lower()
    tokens = [x for x in n.name_tokens if x.isalnum()]
    compact = n.name_compact
    if not t:
        return TickerExplanation("empty", "no ticker", 0.0)
    if not compact:
        return TickerExplanation("unrelated", f"${n.ticker}: name is empty or non-Latin", 0.2)

    suffix_note = ""
    if n.ticker_affixes:
        suffix_note = " (affixes " + ", ".join(a.split(":")[1] for a in n.ticker_affixes) + ")"

    # 1. known coin ticker (the copycat stage says more; here we only explain)
    by_sym = k.coin_by_symbol()
    if t.upper() in by_sym:
        c = by_sym[t.upper()][0]
        return TickerExplanation(
            "known_coin", f"${n.ticker} = ticker of {c.name} ({c.referent_label}){suffix_note}", 0.9
        )
    # 2. equals a token or the compact name
    if t == compact or t in tokens:
        return TickerExplanation(
            "equals_token", f"${n.ticker} is the name itself{suffix_note}", 0.95
        )
    # 3. acronym (before subsequence: 'SBS' is in 'superbowlsunday' too)
    if len(tokens) >= 2:
        acro = "".join(tok[0] for tok in tokens if tok)
        if acro == t:
            return TickerExplanation(
                "acronym", f"${n.ticker} = initials of '{' '.join(tokens)}'{suffix_note}", 0.85
            )
        acro_no_stop = "".join(
            tok[0] for tok in tokens if tok not in ("the", "a", "of", "in", "and")
        )
        if acro_no_stop == t:
            return TickerExplanation(
                "acronym", f"${n.ticker} = initials of the main words in '{' '.join(tokens)}'", 0.8
            )
    # 3b. known slang ("CTO", "WAGMI")
    if t in k.slang and k.slang[t].kind != "marker":
        return TickerExplanation("slang", f"${n.ticker} = {k.slang[t].meaning}{suffix_note}", 0.85)
    # 4. subsequence with matching first letter
    if len(t) >= 3 and t[0] == compact[0] and _is_subsequence(t, compact):
        # vowel-drop special case: compact without vowels ~ ticker
        nov = _VOWELS.sub("", compact)
        if nov and (nov == t or _lev1(nov, t) or t.startswith(nov[: len(t)])):
            return TickerExplanation(
                "vowel_drop", f"${n.ticker} = '{compact}' with vowels dropped{suffix_note}", 0.9
            )
        return TickerExplanation(
            "subsequence", f"${n.ticker} is spelled out inside '{compact}'{suffix_note}", 0.8
        )
    # 4. vowel drop against individual tokens
    for tok in tokens:
        nov = _VOWELS.sub("", tok)
        if len(nov) >= 3 and (nov == t or _lev1(nov, t)):
            return TickerExplanation(
                "vowel_drop", f"${n.ticker} = '{tok}' with vowels dropped{suffix_note}", 0.85
            )
    # 6. baby talk
    for a, b in _BABY_TALK:
        if a in t and t.replace(a, b) in compact:
            return TickerExplanation(
                "baby_talk", f"${n.ticker}: baby-talk spelling ({a}->{b})", 0.6
            )
    # 7. fuzzy
    pr = fuzz.partial_ratio(t, compact, processor=utils.default_process)
    if pr >= 85 and len(t) >= 3:
        return TickerExplanation(
            "fuzzy", f"${n.ticker} resembles part of '{compact}' ({pr:.0f}%)", 0.6
        )
    if full != t and (full == compact or full in tokens):
        return TickerExplanation("equals_token", f"${n.ticker} is the name itself", 0.9)
    return TickerExplanation(
        "unrelated", f"${n.ticker} does not come from the name '{n.name_clean}'", 0.4
    )
