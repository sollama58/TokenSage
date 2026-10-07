"""Loads packaged knowledge from data/ once per process (all small files)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


@dataclass(frozen=True)
class SlangTerm:
    term: str
    meaning: str
    categories: tuple[str, ...]
    weight: float
    kind: str  # slang | template | marker | character


@dataclass(frozen=True)
class KnownCoin:
    symbol: str
    name: str
    aliases: tuple[str, ...]
    chain: str
    lore: str
    categories: tuple[str, ...]
    referent_label: str
    referent_kind: str
    referent_desc: str
    source: str = "seed"
    mint: str | None = None
    logo_phash: int | None = None

    @property
    def surfaces(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys([self.name.lower(), *(a.lower() for a in self.aliases)]))


@dataclass(frozen=True)
class Entity:
    label: str
    kind: str
    aliases: tuple[str, ...]
    categories: tuple[str, ...]
    desc: str
    popularity: float
    source: str = "seed"  # seed | wikidata
    ref_id: str | None = None  # the Wikidata Q-id of a gazetteer entity
    # when the event happened: its news_event category holds only near this date
    event_date: date | None = None

    @property
    def surfaces(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys([self.label.lower(), *(a.lower() for a in self.aliases)]))

    @property
    def evidence_source(self) -> str:
        return f"wikidata:{self.ref_id}" if self.ref_id else f"entities:{self.label}"


@dataclass(frozen=True)
class MarkerRule:
    pattern: re.Pattern[str]
    code: str
    kind: str | None
    weight: float


@dataclass(frozen=True)
class Family:
    name: str
    pattern: re.Pattern[str]
    parent: str | None
    categories: tuple[str, ...]
    weight: float


@dataclass(frozen=True)
class Stock:
    """A listed company or ETF (data/stocks.yaml)."""

    ticker: str  # e.g. TSLA, BRK.B
    name: str
    kind: str  # stock | etf
    categories: tuple[str, ...]  # tradfi/stock or tradfi/index_etf first, then extras
    desc: str
    entity_label: str  # the lexicon entity it becomes, e.g. "Tesla (TSLA)"

    @property
    def compact_ticker(self) -> str:
        return self.ticker.replace(".", "").upper()


@dataclass
class Knowledge:
    slang: dict[str, SlangTerm]
    coins: list[KnownCoin]
    entities: list[Entity]
    markers: list[MarkerRule]
    families: list[Family]
    ticker_prefixes: list[str]
    ticker_suffixes: list[str]
    ticker_min_base: int
    scoring: dict[str, float]
    wordnet: dict[str, list[str]]
    emoji: dict[str, list[str]]
    versions: dict[str, str] = field(default_factory=dict)
    stocks: dict[str, Stock] = field(default_factory=dict)  # by compact ticker
    meta: dict[str, Any] = field(default_factory=dict)  # data/meta.yaml
    # WordNet words that are also given names / surnames: their dictionary sense is weak
    name_words: frozenset[str] = frozenset()
    cjk: dict[str, str] = field(default_factory=dict)  # Han word -> English (data/cjk_words.yaml)

    # -- derived indexes
    def coin_by_symbol(self) -> dict[str, list[KnownCoin]]:
        out: dict[str, list[KnownCoin]] = {}
        for c in self.coins:
            out.setdefault(c.symbol.upper(), []).append(c)
        return out

    def vocabulary(self) -> set[str]:
        """Words worth boosting in the segmenter: slang, coin/entity surface words."""
        words: set[str] = set()
        for t in self.slang:
            words.update(t.lower().split())
        for c in self.coins:
            for s in c.surfaces:
                words.update(s.split())
            words.add(c.symbol.lower())
        for e in self.entities:
            for s in e.surfaces:
                words.update(s.split())
        return {w for w in words if w.isalpha() and len(w) >= 2}


def _yaml(name: str) -> Any:
    with (DATA_DIR / name).open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _json(name: str) -> Any:
    with (DATA_DIR / name).open(encoding="utf-8") as f:
        return json.load(f)


def _date(v: Any) -> date | None:
    if v is None or isinstance(v, date):
        return v
    return date.fromisoformat(str(v))


def _wordnet(ignore: set[str]) -> dict[str, list[str]]:
    raw = _json("wordnet_classes.json")
    return {cls: [w for w in words if w not in ignore] for cls, words in raw.items()}


def _stocks() -> tuple[dict[str, Stock], list[Entity]]:
    """Stocks and the lexicon entities they become: the company name (unless it is a common
    word), extra aliases, the ticker (unless it is a word) and the xStock ticker (TSLAx)."""
    stocks: dict[str, Stock] = {}
    entities: list[Entity] = []
    for e in _yaml("stocks.yaml")["stocks"]:
        ticker = str(e["ticker"]).upper()
        compact = ticker.replace(".", "")
        name = str(e["name"])
        kind = str(e.get("kind", "stock"))
        base = "tradfi/index_etf" if kind == "etf" else "tradfi/stock"
        cats = tuple(dict.fromkeys([base, *(e.get("categories") or [])]))
        label = f"{name} ({ticker})"
        stocks[compact] = Stock(ticker, name, kind, cats, str(e.get("desc", "")), label)
        aliases = [str(a) for a in e.get("aliases") or []]
        if not e.get("name_is_word"):
            aliases.append(name)
        if not e.get("ticker_is_word") and len(compact) >= 3:
            aliases.append(compact.lower())
        if len(compact) >= 3:
            aliases.append(compact.lower() + "x")  # the xStock (TSLAx); "max" for MA is a word
        entities.append(
            Entity(
                label=label,
                kind="other",
                aliases=tuple(dict.fromkeys(aliases)),
                categories=cats,
                desc=str(e.get("desc", "")),
                popularity=0.5,
            )
        )
    return stocks, entities


@lru_cache
def load_knowledge() -> Knowledge:
    slang_raw = _yaml("slang.yaml")["terms"]
    slang = {
        str(term).lower(): SlangTerm(
            term=str(term).lower(),
            meaning=v.get("meaning", ""),
            categories=tuple(v.get("categories") or []),
            weight=float(v.get("weight", 0.5)),
            kind=v.get("kind", "slang"),
        )
        for term, v in slang_raw.items()
    }
    coins = [
        KnownCoin(
            symbol=str(c["symbol"]).upper(),
            name=str(c["name"]),
            aliases=tuple(str(a) for a in c.get("aliases") or []),
            chain=str(c.get("chain", "")),
            lore=str(c.get("lore", "")),
            categories=tuple(c.get("categories") or []),
            referent_label=str(c["referent"]["label"]),
            referent_kind=str(c["referent"]["kind"]),
            referent_desc=str(c["referent"].get("desc", "")),
        )
        for c in _yaml("known_coins_seed.yaml")["coins"]
    ]
    entities = [
        Entity(
            label=str(e["label"]),
            kind=str(e["kind"]),
            aliases=tuple(str(a) for a in e.get("aliases") or []),
            categories=tuple(e.get("categories") or []),
            desc=str(e.get("desc", "")),
            popularity=float(e.get("popularity", 0.3)),
            event_date=_date(e.get("event_date")),
        )
        for e in _yaml("entities_seed.yaml")["entities"]
    ]
    stocks, stock_entities = _stocks()
    entities += stock_entities
    t = _yaml("templates.yaml")
    markers = [
        MarkerRule(re.compile(m["pattern"]), m["code"], m.get("kind"), float(m.get("weight", 0.3)))
        for m in t["markers"]
    ]
    families = [
        Family(
            f["name"],
            re.compile(f["pattern"]),
            f.get("parent"),
            tuple(f.get("categories") or []),
            float(f.get("weight", 0.3)),
        )
        for f in t["families"]
    ]
    return Knowledge(
        slang=slang,
        coins=coins,
        entities=entities,
        stocks=stocks,
        markers=markers,
        families=families,
        ticker_prefixes=[str(p).upper() for p in t["ticker"]["prefixes"]],
        ticker_suffixes=[str(s).upper() for s in t["ticker"]["suffixes"]],
        ticker_min_base=int(t["ticker"].get("min_base_len", 3)),
        scoring={k: float(v) for k, v in t["scoring"].items()},
        wordnet=_wordnet(set(str(w).lower() for w in t.get("wordnet_ignore") or [])),
        emoji=_json("cldr_emoji_en.json"),
        meta=_yaml("meta.yaml"),
        name_words=frozenset(str(w).lower() for w in t.get("wordnet_name_words") or []),
        cjk={str(w): str(v or "").lower() for w, v in _yaml("cjk_words.yaml")["words"].items()},
        versions={"lexicon": "2026-10-07.2", "known_coins": "seed-2026-10-06"},
    )
