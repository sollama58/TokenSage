"""S1 Normalization (guide §5.1). Order matters; every step is a documented research finding."""

from __future__ import annotations

import re
import unicodedata

import emoji as emoji_lib
from anyascii import anyascii

from tokensage.engine import gazetteer, segment
from tokensage.engine.context import Marker, Normalized
from tokensage.engine.knowledge import Knowledge, load_knowledge

_ZW = re.compile(r"[​‌‍‎‏⁠﻿­⁡-⁤]")
_PUNCT_TO_SPACE = re.compile(r"[^\w$#@&'+.\-]+")
_DOLLAR = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,12})\b")
# An all-caps run ending in a lone lowercase "x" (TSLAx, NVDAx: tokenized-stock tickers) is
# one word, not "TSL" + "Ax".
# A lone capital is a word of its own: "DogeX" is Doge + X, "PepeV2" is Pepe + V + 2.
_CAMEL = re.compile(
    r"[A-Z]{2,}x(?![a-z])|[A-Z]{2,}(?=[A-Z][a-z])|[A-Z](?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]{2,}"
    r"|\d+(?:\.\d+)?|[A-Z]"
)
_REPEAT3 = re.compile(r"([a-z])\1{2,}")  # letters only: "1000x" is a number, not "looong"
_LEET = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"}
)
_SMALLCAPS = set("ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡʏᴢ")
# Cyrillic/Greek look-alikes -> Latin. Applied only to MIXED-script names (a spoof pattern);
# a fully Cyrillic name is real Cyrillic and goes through anyascii instead.
_CONFUSABLE = str.maketrans(
    {
        "А": "A",
        "В": "B",
        "Е": "E",
        "К": "K",
        "М": "M",
        "Н": "H",
        "О": "O",
        "Р": "P",
        "С": "C",
        "Т": "T",
        "Х": "X",
        "У": "Y",
        "І": "I",
        "Ј": "J",
        "Ѕ": "S",
        "а": "a",
        "е": "e",
        "о": "o",
        "р": "p",
        "с": "c",
        "у": "y",
        "х": "x",
        "і": "i",
        "ј": "j",
        "ѕ": "s",
        "ԁ": "d",
        "ɡ": "g",
        "Α": "A",
        "Β": "B",
        "Ε": "E",
        "Ζ": "Z",
        "Η": "H",
        "Ι": "I",
        "Κ": "K",
        "Μ": "M",
        "Ν": "N",
        "Ο": "O",
        "Ρ": "P",
        "Τ": "T",
        "Υ": "Y",
        "Χ": "X",
        "ο": "o",
        "ρ": "p",
        "ν": "v",
        "ι": "i",
        "κ": "k",
        "α": "a",
        "τ": "t",
        "υ": "u",
    }
)
_SCRIPT_RANGES = {
    "Han": (0x4E00, 0x9FFF),
    "Hiragana": (0x3040, 0x309F),
    "Katakana": (0x30A0, 0x30FF),
    "Hangul": (0xAC00, 0xD7AF),
    "Cyrillic": (0x0400, 0x04FF),
    "Arabic": (0x0600, 0x06FF),
    "Thai": (0x0E00, 0x0E7F),
    "Devanagari": (0x0900, 0x097F),
    "Greek": (0x0370, 0x03FF),
    "Hebrew": (0x0590, 0x05FF),
}


_LOOKALIKE = re.compile(r"[\u0400-\u04ff\u0370-\u03ff]")
_LATIN = re.compile(r"[A-Za-z]")


def _mixed_word(s: str) -> bool:
    """A word that mixes Latin letters with Cyrillic or Greek ones ("P\u0435pe" with a Cyrillic
    \u0435): the look-alike spoof pattern. Whole words in another script next to Latin ones
    ("Pepe \u0421\u043e\u0431\u0430\u043a\u0430", "\u67f4\u72ac\u30b3\u30a4\u30f3") are
    bilingual names, not spoofs."""
    return any(_LATIN.search(w) and _LOOKALIKE.search(w) for w in s.split())


def _scripts(s: str) -> list[str]:
    found: list[str] = []
    for ch in s:
        o = ord(ch)
        for name, (lo, hi) in _SCRIPT_RANGES.items():
            if lo <= o <= hi and name not in found:
                found.append(name)
    return found


def _emoji_keywords(s: str, k: Knowledge) -> tuple[list[str], list[str], str]:
    """Return (emoji list, keyword list, string with emoji removed)."""
    found: list[str] = []
    kws: list[str] = []
    for m in emoji_lib.emoji_list(s):
        e = m["emoji"]
        found.append(e)
        bare = "".join(ch for ch in e if ch not in ("️", "︎"))
        for key in (bare, e, bare[:1]):
            if key in k.emoji:
                for w in k.emoji[key]:
                    if w not in kws:
                        kws.append(w)
                break
    stripped = emoji_lib.replace_emoji(s, replace=" ")
    return found, kws, stripped


def _detect_markers(text: str, k: Knowledge) -> list[Marker]:
    out: list[Marker] = []
    for rule in k.markers:
        m = rule.pattern.search(text)
        if m:
            out.append(Marker(rule.code, rule.kind, rule.weight, m.group(0)))
    return out


def _fold(s: str) -> str:
    return anyascii(s)


_HAN = re.compile(r"[\u4e00-\u9fff]+")
_CJK_MAXLEN: dict[int, int] = {}


def translate_han(s: str, k: Knowledge) -> tuple[str, list[tuple[str, str]]]:
    """Translate Han runs with data/cjk_words.yaml, longest entry first at each position:
    '中国龙' -> ' china  dragon '. Characters with no entry stay as they are (anyascii folds
    them to pinyin later). Returns the new string and the (Han, English) pairs used."""
    if not k.cjk or not _HAN.search(s):
        return s, []
    maxlen = _CJK_MAXLEN.get(id(k))
    if maxlen is None:
        maxlen = _CJK_MAXLEN[id(k)] = max(len(w) for w in k.cjk)
    glosses: list[tuple[str, str]] = []

    def run(m: re.Match[str]) -> str:
        text, i, out = m.group(0), 0, []
        while i < len(text):
            for n in range(min(maxlen, len(text) - i), 0, -1):
                word = text[i : i + n]
                if word in k.cjk:
                    eng = k.cjk[word]
                    out.append(f" {eng} " if eng else " ")
                    if eng:
                        glosses.append((word, eng))
                    i += n
                    break
            else:
                out.append(text[i])
                i += 1
        return "".join(out)

    return _HAN.sub(run, s), glosses


def _split_camel(s: str) -> str:
    """'AIAgentSupercycle' -> 'AI Agent Supercycle'. Leaves lowercase words alone."""
    out: list[str] = []
    for tok in s.split():
        if any(c.islower() for c in tok) and any(c.isupper() for c in tok[1:]):
            parts = _CAMEL.findall(tok)
            out.append(" ".join(parts) if parts else tok)
        else:
            out.append(tok)
    return " ".join(out)


def _squeeze(s: str) -> tuple[str, bool]:
    squeezed = _REPEAT3.sub(r"\1\1", s)
    return squeezed, squeezed != s


def _deleet(token: str) -> str | None:
    """Undo leetspeak only for tokens mixing letters and digits (keeps 420, 69, 2.0)."""
    if not (any(c.isalpha() for c in token) and any(c.isdigit() or c in "@$" for c in token)):
        return None
    if re.fullmatch(r"[a-z]+\d{1,4}", token) or re.fullmatch(r"\d{1,4}[a-z]+", token):
        return None  # "web3", "3am": digits as digits, not letters
    cand = token.translate(_LEET)
    return cand if cand.isalpha() and cand != token else None


_KNOWN_SYMBOLS: dict[int, frozenset[str]] = {}


def _known_symbols(k: Knowledge) -> frozenset[str]:
    syms = _KNOWN_SYMBOLS.get(id(k))
    if syms is None:
        syms = frozenset(k.coin_by_symbol())
        _KNOWN_SYMBOLS[id(k)] = syms
    return syms


def ticker_base(ticker: str, k: Knowledge) -> tuple[str, list[str]]:
    """Strip known affixes: BPNUT -> PNUT, PNUT2 -> PNUT, BABYDOGEINU -> DOGE. A ticker that
    is an English word (BEAR, BLINK, APEX) is left whole."""
    t = ticker.upper()
    affixes: list[str] = []
    known = _known_symbols(k)
    if t not in known and gazetteer.is_common(t.lower()):
        # an English word is a whole ticker: BEAR is not B + EAR, APEX not APE + X
        return t, affixes
    changed = True
    # stop as soon as the ticker is itself a known coin's symbol: BONK is not B + ONK,
    # and BBONK is B + BONK (not BB + ONK)
    while changed and len(t) > k.ticker_min_base and t not in known:
        changed = False
        # one affix per step, suffix first (BRETT2 -> BRETT, not RETT2), re-checking for a
        # known symbol after every strip
        for s in sorted(k.ticker_suffixes, key=len, reverse=True):
            if t.endswith(s) and len(t) - len(s) >= k.ticker_min_base:
                t, changed = t[: -len(s)], True
                affixes.append("suffix:" + s)
                break
        if changed:
            continue
        for p in sorted(k.ticker_prefixes, key=len, reverse=True):
            if t.startswith(p) and len(t) - len(p) >= k.ticker_min_base:
                t, changed = t[len(p) :], True
                affixes.append("prefix:" + p)
                break
    return t, affixes


def clean_ticker(symbol: str) -> str:
    s = unicodedata.normalize("NFKC", symbol or "")
    s = _ZW.sub("", s)
    s = emoji_lib.replace_emoji(s, replace="")
    s = _fold(s).strip().lstrip("$#").strip()
    return re.sub(r"\s+", "", s).upper()[:20]


MAX_NAME_CHARS = 200


def normalize(
    name: str | None,
    symbol: str | None,
    description: str | None,
    name_limit: int = MAX_NAME_CHARS,
) -> Normalized:
    k = load_knowledge()
    # names are short on pump.fun (32 chars); cap hostile ones so one request can't hold
    # a worker thread (and the GIL) for minutes in segmentation
    name = (name or "")[:name_limit]
    symbol = (symbol or "")[:64]
    description = description or ""
    obf: list[str] = []

    # 1. NFKC
    n1 = unicodedata.normalize("NFKC", name)
    if n1 != name and any(0xFF01 <= ord(c) <= 0xFF5E for c in name):
        obf.append("fullwidth")
    if any(c in _SMALLCAPS for c in name):
        obf.append("small_caps")
    # 2. emoji out (before stripping Cf, which would break ZWJ sequences)
    emojis, emoji_kws, n2 = _emoji_keywords(n1, k)
    # 3. zero-width
    n3 = _ZW.sub("", n2)
    if n3 != n2:
        obf.append("zero_width")
    # 4. homoglyphs (look-alikes mixed into a Latin word), then fold to ASCII; the scripts
    # are read after the look-alikes are put back, so a spoofed Latin name is not "Cyrillic"
    if _mixed_word(n3):
        obf.append("homoglyph")
        n3 = n3.translate(_CONFUSABLE)
    scripts = _scripts(n3)
    # 4b. translate, not just transliterate, Han words (猫 -> cat, not mao)
    translated, cjk_gloss = translate_han(n3, k)
    # the pinyin reading stays available to the ticker step ($MAO for 猫)
    name_pinyin = (
        _PUNCT_TO_SPACE.sub(" ", _split_camel(_fold(n3)).lower()).split() if cjk_gloss else []
    )
    n3 = translated
    folded_name = _fold(n3)
    # 5. markers BEFORE stripping punctuation; the camelCase split ("PepeV2" -> "Pepe V 2")
    # can expose a marker the written form hides
    camel = _split_camel(folded_name)
    markers = _detect_markers(folded_name, k)
    if camel != folded_name:
        seen_codes = {m.code for m in markers}
        markers += [m for m in _detect_markers(camel, k) if m.code not in seen_codes]
    # 6. camelCase split before lowercasing (done above)
    # 7. punctuation to spaces, keep version dots handled by markers already
    lowered = camel.lower()
    cleaned = _PUNCT_TO_SPACE.sub(" ", lowered)
    cleaned = re.sub(r"(?<!\d)[.'](?!\d)", " ", cleaned)
    cleaned = cleaned.replace("$", " ").replace("#", " ").replace("@", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # 8. repeated letters
    squeezed, did_sq = _squeeze(cleaned)
    if did_sq:
        obf.append("repeated_letters")
    # 9. leet per token
    toks = squeezed.split()
    leet_tokens: list[str] = []
    leet_any = False
    for t in toks:
        d = _deleet(t)
        if d:
            leet_tokens.append(d)
            leet_any = True
        else:
            leet_tokens.append(t)
    if leet_any:
        obf.append("leet")
    leet_decoded = " ".join(leet_tokens) if leet_any else None
    base_text = leet_decoded or squeezed
    compact = re.sub(r"[^a-z0-9]", "", base_text)
    # segmentation: choose between the given spacing and a re-segmentation of the compact form
    name_tokens = segment.best_tokens(base_text, compact, k)

    # ticker
    ticker = clean_ticker(symbol)
    tbase, affixes = ticker_base(ticker, k)

    # description
    d1 = unicodedata.normalize("NFKC", description)
    _, d_emoji_kws, d2 = _emoji_keywords(d1, k)
    d3 = _ZW.sub("", d2)
    dollars = [m.upper() for m in _DOLLAR.findall(d3)]
    d3, _ = translate_han(d3, k)
    d_fold = _fold(d3).lower()
    # a sentence's full stop is not part of its last word ("your portfolio agent."); dots
    # inside a word (pump.fun, v2.0) stay
    d_words = re.sub(r"\.+(?=\s|$)", " ", _PUNCT_TO_SPACE.sub(" ", d_fold))
    desc_clean = re.sub(r"\s+", " ", d_words).strip()
    desc_tokens = desc_clean.split()[:400]

    return Normalized(
        name_raw=name,
        symbol_raw=symbol,
        description_raw=description[:4000],
        name_clean=base_text,
        name_tokens=name_tokens,
        name_compact=compact,
        ticker=ticker,
        ticker_base=tbase,
        ticker_affixes=affixes,
        markers=markers,
        emoji=emojis,
        emoji_keywords=emoji_kws,
        obfuscation=obf,
        scripts=scripts,
        desc_clean=desc_clean,
        desc_tokens=desc_tokens,
        dollar_mentions=dollars,
        leet_decoded=leet_decoded,
        cjk_gloss=cjk_gloss,
        name_pinyin=name_pinyin,
        desc_emoji_keywords=d_emoji_kws,
    )
