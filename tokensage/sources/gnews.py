"""Google News RSS search: a cheap 'is this in the news right now' check (full depth only)."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import quote_plus

import httpx
import structlog

from tokensage.net.breaker import breaker

log = structlog.get_logger("gnews")
URL = "https://news.google.com/rss/search?q={q}+when:{when}&hl=en-US&gl=US&ceid=US:en"
_ITEM = re.compile(r"<item>(.*?)</item>", re.S)
_TITLE = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S)
_SOURCE = re.compile(r"<source[^>]*>(.*?)</source>", re.S)
_PUB = re.compile(r"<pubDate>(.*?)</pubDate>", re.S)


# Pages about a coin's price or chart, not news about what the coin is named after
_CRYPTO = re.compile(
    r"\b(price|prices|coin|coins|token|tokens|crypto|cryptocurrency|memecoins?|pump\.?fun|"
    r"solana|usdt?|converter|market cap|chart|airdrop|presale|binance|coinbase|dex|bitcoin|"
    r"btc|ethereum|altcoins?|stablecoins?|dogecoin|blockchain|defi|web3|nfts?)\b",
    re.I,
)
# Words that do not make a coin name specific enough to search the news for
_FILLER = {"the", "of", "a", "an", "on", "and", "coin", "token", "inu", "sol", "wif", "official"}
_APOSTROPHES = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'"})


def straight(text: str) -> str:
    """Curly apostrophes as straight ones: "Trump\u2019s Cat" (an iPhone's default) and
    "Trump's Cat" are the same name in a coin and in a headline."""
    return text.translate(_APOSTROPHES)


@dataclass
class Headline:
    title: str
    source: str | None
    published: str | None


async def search(http: httpx.AsyncClient, query: str, when: str = "2d") -> list[Headline] | None:
    src = "gnews"
    if not query or not breaker.allow(src):
        return None
    try:
        r = await http.get(URL.format(q=quote_plus(query), when=when), timeout=8.0)
    except httpx.HTTPError as e:
        breaker.failure(src)
        # a timeout's str() is empty: keep the type, or the log says nothing
        log.info("gnews.error", error=f"{type(e).__name__}: {e}"[:120])
        return None
    if r.status_code != 200 or "<rss" not in r.text[:500]:
        breaker.failure(src)
        log.info("gnews.bad_response", status=r.status_code)
        return None
    breaker.success(src)
    out: list[Headline] = []
    for m in _ITEM.finditer(r.text):
        block = m.group(1)
        t = _TITLE.search(block)
        if not t:
            continue
        title = html.unescape(t.group(1)).strip()
        s = _SOURCE.search(block)
        p = _PUB.search(block)
        out.append(
            Headline(
                title, html.unescape(s.group(1)).strip() if s else None, p.group(1) if p else None
            )
        )
        if len(out) >= 20:
            break
    return out


def name_query(name: str | None) -> str | None:
    """The coin name as a news phrase, or None when it is too generic to search for: a single
    word ("Claudia", "Einstein") matches unrelated stories, so it takes two content words."""
    if not name:
        return None
    words = re.sub(r"[^\w' ]+", " ", straight(name)).split()
    while words and words[-1].isdigit():  # "Peanut the Squirrel 2.0": the story, not the sequel
        words.pop()
    # "Official Moo Deng", "Moo Deng Inu", "Moo Deng Coin": the story is "Moo Deng" (and a
    # headline with "coin" in it is dropped as crypto, so the phrase could never be found)
    while words and words[0].lower() in _FILLER:
        words.pop(0)
    while words and words[-1].lower() in _FILLER:
        words.pop()
    content = [w for w in words if w.lower() not in _FILLER]
    if len(content) < 2 or sum(len(w) for w in content) < 6 or len(words) > 6:
        return None
    return " ".join(words)


def relevant(heads: list[dict], phrase: str, symbol: str | None = None) -> list[dict]:
    """The headlines that name the phrase itself and are not about a coin: drops price pages,
    crypto stories, and stories naming the ticker (unless the ticker is a word of the phrase).
    heads: headlines as news_for caches them ({title, source, published})."""
    pat = re.compile(r"(?<!\w)" + re.escape(phrase.lower()) + r"(?!\w)")
    sym = (symbol or "").strip().lower()
    # the ticker as a cashtag, or written in capitals ("QTC lists cross-chain"); the same
    # letters as a plain word ("a baby hippo" for $HIPPO) are the story, not the coin
    sym_pat = (
        re.compile(
            r"(?i:\$" + re.escape(sym) + r")(?!\w)|(?<!\w)" + re.escape(sym.upper()) + r"(?!\w)"
        )
        if len(sym) >= 2 and sym not in phrase.lower().split()
        else None
    )
    out = []
    for h in heads:
        title = straight(str(h.get("title") or ""))
        t = title.lower()
        if not pat.search(t) or _CRYPTO.search(t) or (sym_pat and sym_pat.search(title)):
            continue
        out.append(h)
    return out
