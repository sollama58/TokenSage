"""The subject of the X post a coin was launched on (since rules 0.30.0).

When a coin's theme comes from its X post and no lexicon or Wikipedia entity names it, the
post usually does: "a white lioness named "Luna"", "Meet PING PING 🐼", "its called Dick
Titties now". This module finds that phrase deterministically, by what the coin's own name or
ticker repeats: a quoted term, a "named X" / "meet X" phrase, a capitalised name, or the
shortest stretch of the post holding the name's words. Failing that, the post's lead clause
stands for it. Pure: the pipeline hands it the posts and the normalized name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from tokensage.engine import segment
from tokensage.engine.context import Normalized
from tokensage.engine.knowledge import Knowledge


@dataclass(frozen=True)
class Post:
    id: str
    author: str | None
    text: str
    # the post is someone else's earlier narrative (not the coin's own launch post): it
    # needs no content beyond the name to say what the coin is
    narrative: bool


@dataclass(frozen=True)
class Subject:
    label: str
    how: str  # quoted | named | proper | window | cashtag | lead
    matched: bool  # the coin's name or ticker repeats it
    post_id: str
    author: str | None
    excerpt: str  # "@author: <first characters of the post>"


_URL = re.compile(r"https?://\S+|\bwww\.\S+", re.IGNORECASE)
_CA = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
_LEAD_MENTIONS = re.compile(r"^(?:\s*@\w{1,15})+\s*")
_QUOTED = re.compile(r"""(?:^|(?<=[\s(\[:,]))["“'‘]([^"“”'‘’\n]{2,40}?)["”'’](?=$|[\s).,!?:;\]])""")
_NAMED = re.compile(
    r"\b(?:named|called|nicknamed|nickname(?:\s+is|\s+could\s+be)?|name\s+is|name's|"
    r"meet|introducing(?:\s+to)?)\s+([^\n.,!?;:]{2,48})",
    re.IGNORECASE,
)
_WORD = r"[A-Z][\w'’-]*"
_PROPER = re.compile(rf"(?<![\w$@#]){_WORD}(?:[ \t]+{_WORD}){{0,3}}")
_CASHTAG = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,15})\b")
_SENTENCE = re.compile(r"[^.!?\n]+")
# words that end a "named X" phrase: "called Dick Titties now", "named Luna and she"
_PHRASE_END = {
    "and", "who", "which", "that", "is", "has", "he", "she", "it", "its", "from", "a", "the",
    "now", "lol", "lmao", "rn", "with", "in", "on", "at", "for", "to", "was", "will",
}  # fmt: skip


def _cfg(k: Knowledge) -> dict:
    return dict(k.rules.get("x_subject") or {})


def _toks(text: str) -> list[str]:
    return [w for w in re.split(r"[^0-9a-z]+", text.casefold()) if w]


def clean(text: str) -> str:
    """The post without links, contract addresses and the leading @replies."""
    t = _URL.sub(" ", text or "")
    t = _CA.sub(" ", t)
    t = _LEAD_MENTIONS.sub("", t)
    return t.strip()


def excerpt(author: str | None, text: str, k: Knowledge) -> str:
    n = int(_cfg(k).get("excerpt_chars", 120))
    body = " ".join(clean(text).split())
    if len(body) > n:
        cut = body[:n].rsplit(" ", 1)[0] if " " in body[:n] else body[:n]
        body = cut.rstrip(" ,;:-") + "…"
    return f"@{author}: {body}" if author else body


def _display(phrase: str) -> str:
    p = " ".join(phrase.strip(" \t'\"“”‘’").split())
    if p.isupper() and len(p.replace(" ", "")) > 4:
        p = p.title()
    return p


class _Name:
    """The coin's name and ticker as the matcher needs them."""

    def __init__(self, n: Normalized, filler: set[str], fuzzy_min: int) -> None:
        self.tokens = [t for t in n.name_tokens if len(t) >= 3 and t not in filler]
        self.compact = n.name_compact
        self.ticker = n.ticker.lower()
        self.fuzzy_min = fuzzy_min

    def covers(self, phrase: str) -> tuple[int, int, bool]:
        """(a strength score, how many name words the phrase holds, whether it is the whole
        name or ticker). A word written as in the name counts double a near spelling."""
        words = _toks(phrase)
        c = "".join(words)
        exact = bool(c) and (
            c == self.compact
            or (len(self.ticker) >= 3 and c == self.ticker)
            or (len(self.ticker) >= 4 and self.ticker in c)
            or (len(c) >= 4 and c in self.compact)
        )
        same = sum(1 for t in self.tokens if t in words)
        near = sum(
            1
            for t in self.tokens
            if t not in words
            and any(len(w) >= 4 and fuzz.ratio(w, t) >= self.fuzzy_min for w in words)
        )
        return 2 * same + near + 2 * int(exact), same + near, exact


def _named(text: str) -> list[str]:
    out: list[str] = []
    for m in _NAMED.finditer(text):
        words = m.group(1).split()
        kept: list[str] = []
        for w in words[:5]:
            bare = w.strip("'\"“”‘’").casefold()
            if kept and bare in _PHRASE_END:
                break
            kept.append(w)
        phrase = " ".join(kept)
        phrase = re.sub(r"[^\w'’\"“”‘\s-]+", " ", phrase).strip()
        if phrase:
            out.append(phrase)
    return out


def _windows(text: str, name: _Name) -> list[str]:
    """The shortest stretches of the post that hold every word of the name."""
    if not name.tokens:
        return []
    spans = [(m.group(0), m.start(), m.end()) for m in re.finditer(r"[\w'’]+", text)]
    want = name.tokens
    limit = len(want) + 3
    best: list[tuple[int, str]] = []
    for i in range(len(spans)):
        for j in range(i, min(len(spans), i + limit)):
            phrase = text[spans[i][1] : spans[j][2]]
            _, hit, _ = name.covers(phrase)
            if hit == len(want):
                best.append((j - i, phrase))
                break
    best.sort(key=lambda x: x[0])
    return [p for _, p in best[:1]]


def _content_words(text: str, name: _Name, filler: set[str]) -> int:
    words = [w for w in _toks(_CASHTAG.sub(" ", text)) if not w.isdigit()]
    return sum(
        1
        for w in words
        if w not in filler and w not in name.tokens and w != name.ticker and len(w) >= 2
    )


def find(posts: list[Post], n: Normalized, k: Knowledge) -> Subject | None:
    """The best phrase a post gives for what the coin is: one its name or ticker repeats
    (matched), else the lead clause of the first post that says anything (not matched)."""
    cfg = _cfg(k)
    filler = {str(w).casefold() for w in cfg.get("filler") or []}
    name = _Name(n, filler, int(cfg.get("fuzzy_min", 80)))
    min_words = int(cfg.get("min_content_words", 3))
    common = segment.common_words(k)
    ranked: list[tuple[tuple[int, int, int, int], Subject]] = []
    lead: Subject | None = None
    for order, post in enumerate(posts):
        text = clean(post.text)
        if not text:
            continue
        if not post.narrative and _content_words(text, name, filler) < min_words:
            continue  # "$ZORB is live, CA in bio": the launch post names nothing
        ex = excerpt(post.author, post.text, k)
        cands: list[tuple[str, str]] = []
        cands += [(q, "quoted") for q in _QUOTED.findall(text)]
        cands += [(q, "named") for q in _named(text)]
        cands += [(m.group(0), "proper") for m in _PROPER.finditer(text)]
        cands += [(w, "window") for w in _windows(text, name)]
        for how_rank, (phrase, how) in enumerate(cands):
            label = _display(phrase)
            if not label or label.casefold() in filler:
                continue
            score, hit, exact = name.covers(label)
            if not hit and not exact:
                continue
            explicit = how in ("quoted", "named")
            if not explicit and _everyday(label, common) and _toks(label) != name.tokens:
                continue  # "Utility" of "I am Now Utility": a word, not a name
            key = (int(explicit), score, -order, -how_rank)
            ranked.append((key, Subject(label, how, True, post.id, post.author, ex)))
        tags = {t.casefold() for t in _CASHTAG.findall(text)}
        if name.ticker and name.ticker in tags and n.name_raw.strip():
            # the post names the coin by its cashtag: the coin's own name, as the post's
            # subject ("Folia": "2,000 AI agents, one tree ... earns $FOLIA")
            label = _display(n.name_raw)
            key = (0, 2, -order, -999)
            ranked.append((key, Subject(label, "cashtag", True, post.id, post.author, ex)))
        if lead is None:
            lead = _lead(text, post, ex, name, filler, cfg)
    if ranked:
        ranked.sort(key=lambda x: x[0], reverse=True)
        return ranked[0][1]
    return lead


def _everyday(label: str, common: frozenset[str]) -> bool:
    """One everyday English word ("same", "core", "animals"), plural or not."""
    words = _toks(label)
    if len(words) != 1:
        return False
    w = words[0]
    return w in common or (w.endswith("s") and w[:-1] in common)


def _lead(
    text: str, post: Post, ex: str, name: _Name, filler: set[str], cfg: dict
) -> Subject | None:
    """The post's lead clause, cut to a label's length: the first clause that names
    something in capitals after its first word ("A Baby Musk Ox from Alaska Wildlife ..."),
    else the first that says something (three content words)."""
    max_words = int(cfg.get("lead_max_words", 9))
    max_chars = int(cfg.get("lead_max_chars", 60))
    clauses = [" ".join(m.group(0).split()).strip(" ,;:-") for m in _SENTENCE.finditer(text)]
    clauses = [c for c in clauses if _content_words(c, name, filler) >= 3]
    if not clauses:
        return None
    named = [c for c in clauses if any(m.start() > 0 for m in _PROPER.finditer(c))]
    s = (named or clauses)[0]
    words = s.split()[:max_words]
    label = " ".join(words)
    while len(label) > max_chars and len(words) > 1:
        words = words[:-1]
        label = " ".join(words)
    label = _display(label.rstrip(" ,;:-"))
    return Subject(label, "lead", False, post.id, post.author, ex) if label else None
