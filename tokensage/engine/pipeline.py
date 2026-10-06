"""The engine pipeline at basic depth. Pure and synchronous: all I/O results come in through
EngineInput, so golden tests run without a database or network."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from tokensage.engine import image as image_stage
from tokensage.engine import known_coins, lexicon, ticker
from tokensage.engine.aggregate import Aggregated, aggregate
from tokensage.engine.context import Ev, Normalized, ReferentCandidate
from tokensage.engine.knowledge import Entity, Knowledge, KnownCoin, SlangTerm, load_knowledge
from tokensage.engine.normalize import normalize
from tokensage.engine.render_summary import summarize

RULES_VERSION = "0.3.0-basic"

_WORDNET_LABEL = {
    "food": "food_object_abstract",
    "vehicle": "food_object_abstract",
    "body_part": "food_object_abstract",
    "emotion": "food_object_abstract",
}
_WORDNET_WEIGHT = {"food": 0.45, "vehicle": 0.35, "body_part": 0.3, "emotion": 0.3}


@dataclass
class SameNameToken:
    mint: str
    name: str | None
    symbol: str | None
    created_at: datetime | None
    source: str  # db | pumpfun_search | dexscreener


@dataclass
class DbContext:
    x_reuse_count: int = 0
    creator_token_count: int = 0
    same_name: list[SameNameToken] = field(default_factory=list)
    image_candidates: list[image_stage.Candidate] = field(default_factory=list)
    extra_coins: list[KnownCoin] = field(default_factory=list)


@dataclass
class EngineInput:
    mint: str
    name: str | None
    symbol: str | None
    description: str | None
    image_bytes: bytes | None
    created_at: datetime | None
    x_kind: str | None = None  # from XRef.kind
    x_object_time: datetime | None = None
    ctx: DbContext = field(default_factory=DbContext)


@dataclass
class FlagOut:
    code: str
    severity: str
    detail: str


@dataclass
class EngineOutput:
    normalized: Normalized
    agg: Aggregated
    ticker_explanation: str
    copy_of: list[dict]
    image: image_stage.ImageResult
    flags: list[FlagOut]
    summary: str
    caveats: list[str]
    evidence: list[Ev]


# ----------------------------------------------------------------- evidence producers


def _normalization_evidence(n: Normalized, k: Knowledge) -> list[Ev]:
    evs: list[Ev] = []
    for m in n.markers:
        if m.kind:
            evs.append(
                Ev(
                    kind="marker",
                    label=f"derivative/{m.kind}",
                    weight=m.weight,
                    detail=f"name carries the marker '{m.text}' ({m.code})",
                    source=f"templates:{m.code}",
                )
            )
    for kw in n.emoji_keywords:
        for cls in lexicon.wordnet_classes_for(kw, k):
            label = _WORDNET_LABEL.get(cls, cls)
            evs.append(
                Ev(
                    kind="emoji",
                    label=label,
                    weight=0.35,
                    detail=f"emoji keyword '{kw}' is a {cls.split('/')[-1]}",
                    source="cldr",
                )
            )
    if n.scripts:
        evs.append(
            Ev(
                kind="script",
                label="regional_language",
                weight=0.6,
                detail=f"name uses the {', '.join(n.scripts)} script",
                source="unicode",
            )
        )
    return evs


def _lexicon_evidence(n: Normalized, k: Knowledge) -> list[Ev]:
    evs: list[Ev] = []
    name_text = " ".join(n.name_tokens)
    # The compact form catches brand names the camelCase split breaks apart ("DeepSeek")
    # and multi-word slang stored compact ("diamondhands"); the ticker is a signal too.
    passes = [
        ("name", name_text, 1.0),
        ("name", n.name_compact if n.name_compact != name_text else "", 1.0),
        ("symbol", n.ticker.lower() if len(n.ticker) >= 3 else "", 0.8),
        ("description", n.desc_clean, 1.0),
    ]
    seen_hits: set[tuple[str, str, str]] = set()
    for where, text, factor in passes:
        if not text:
            continue
        for h in lexicon.find(text, k):
            hk = (where, h.surface, h.kind)
            if hk in seen_hits:
                continue
            seen_hits.add(hk)
            if h.kind == "slang":
                t = h.payload
                assert isinstance(t, SlangTerm)
                for cat in t.categories:
                    evs.append(
                        Ev(
                            kind="lexicon",
                            label=cat,
                            weight=t.weight * factor,
                            detail=f"'{h.surface}' = {t.meaning}",
                            source=f"slang:{t.term}",
                            where=where,  # type: ignore[arg-type]
                        )
                    )
            elif h.kind == "entity":
                e = h.payload
                assert isinstance(e, Entity)
                ref = ReferentCandidate(
                    label=e.label,
                    kind=e.kind,
                    desc=e.desc,
                    source=f"entities:{e.label}",
                    score=round(0.45 + 0.45 * e.popularity, 3),
                    categories=list(e.categories),
                )
                for cat in e.categories:
                    evs.append(
                        Ev(
                            kind="entity",
                            label=cat,
                            weight=round((0.5 + 0.35 * e.popularity) * factor, 3),
                            detail=f"'{h.surface}' refers to {e.label} ({e.desc})",
                            source=f"entities:{e.label}",
                            where=where,  # type: ignore[arg-type]
                            referent=ref,
                        )
                    )
                evs.append(
                    Ev(
                        kind="entity",
                        label="referent",
                        weight=ref.score,
                        detail=f"{e.label}: {e.desc}",
                        source=f"entities:{e.label}",
                        where=where,  # type: ignore[arg-type]
                        referent=ref,
                    )
                )
            elif h.kind == "wordnet":
                cls = h.payload
                assert isinstance(cls, str)
                label = _WORDNET_LABEL.get(cls, cls)
                w = _WORDNET_WEIGHT.get(cls, 0.55 if cls != "animal/other" else 0.4)
                evs.append(
                    Ev(
                        kind="wordnet",
                        label=label,
                        weight=w * factor,
                        detail=f"'{h.surface}' is a {cls.replace('animal/', '').replace('_', ' ')}",
                        source=f"wordnet:{cls}",
                        where=where,  # type: ignore[arg-type]
                    )
                )
            # coin hits are handled by the known-coin stage (fuzzy + ticker aware)
    return evs


def _same_name_evidence(
    inp: EngineInput, n: Normalized, is_famous: bool
) -> tuple[list[Ev], list[dict]]:
    evs: list[Ev] = []
    copies: list[dict] = []
    if not inp.ctx.same_name or not inp.created_at:
        return evs, copies
    earlier = [
        t
        for t in inp.ctx.same_name
        if t.mint != inp.mint
        and t.created_at
        and (inp.created_at - t.created_at).total_seconds() > 300
    ]
    if not earlier:
        return evs, copies
    earlier.sort(key=lambda t: t.created_at or inp.created_at)  # type: ignore[arg-type,return-value]
    first = earlier[0]
    if not is_famous:
        evs.append(
            Ev(
                kind="same_name",
                label="derivative/copycat",
                weight=min(0.75, 0.45 + 0.05 * len(earlier)),
                detail=(
                    f"{len(earlier)} earlier token(s) share this name/ticker; the earliest "
                    f"({first.symbol or '?'}, {first.source}) predates it by "
                    f"{_dur((inp.created_at - first.created_at).total_seconds())}"  # type: ignore[operator]
                ),
                source=f"same_name:{first.source}",
                where="db",
            )
        )
    copies.append(
        {
            "ticker": first.symbol,
            "name": first.name,
            "mint": first.mint,
            "signals": ["same_name_earlier", f"earlier_count:{len(earlier)}"],
        }
    )
    return evs, copies


def _image_evidence(res: image_stage.ImageResult, k: Knowledge) -> list[Ev]:
    evs: list[Ev] = []
    same = int(k.scoring.get("logo_phash_same", 8))
    for nd in res.near:
        c = nd.candidate
        strength = 0.85 if nd.distance <= same else 0.6
        who = c.known_coin or c.template or c.mint or c.content_key
        label = "derivative/logo_reuse"
        ref = None
        if c.known_coin:
            coin = next((x for x in k.coins if x.symbol == c.known_coin), None)
            if coin:
                ref = ReferentCandidate(
                    label=coin.referent_label,
                    kind=coin.referent_kind,
                    desc=coin.referent_desc,
                    source=f"logo:{coin.symbol}",
                    score=strength * 0.9,
                    categories=list(coin.categories),
                )
        evs.append(
            Ev(
                kind="image_hash",
                label=label,
                weight=strength,
                detail=f"logo is a near-duplicate of {who} (pHash distance {nd.distance}"
                + (", mirrored" if nd.mirrored else "")
                + ")",
                source=f"image:{c.content_key}",
                where="image",
                referent=ref,
            )
        )
    return evs


def _dur(s: float) -> str:
    if s < 120:
        return f"{int(s)} s"
    if s < 7200:
        return f"{int(s // 60)} min"
    if s < 172800:
        return f"{s / 3600:.1f} h"
    return f"{int(s // 86400)} d"


# ----------------------------------------------------------------- flags


def _flags(inp: EngineInput, n: Normalized, agg: Aggregated, is_famous: bool) -> list[FlagOut]:
    flags: list[FlagOut] = []
    cats = dict(agg.categories)
    if "homoglyph" in n.obfuscation:
        flags.append(
            FlagOut(
                "homoglyph_ticker",
                "warn",
                "name/ticker uses look-alike characters from another script",
            )
        )
    other_obf = [o for o in n.obfuscation if o != "homoglyph"]
    if other_obf:
        flags.append(FlagOut("obfuscated_text", "info", "name uses " + ", ".join(other_obf)))
    if n.scripts:
        flags.append(FlagOut("regional_script", "info", f"name written in {', '.join(n.scripts)}"))
    derivative_hit = any(
        e.kind in ("known_coin", "same_name", "image_hash") and e.label.startswith("derivative/")
        for e in agg.evidence
    )
    if derivative_hit and cats.get("derivative", 0) >= 0.5 and not is_famous:
        flags.append(
            FlagOut("copycat", "warn", "same ticker base, name or logo as an existing coin")
        )
    if inp.ctx.same_name and inp.created_at:
        earlier = [
            t
            for t in inp.ctx.same_name
            if t.mint != inp.mint
            and t.created_at
            and (inp.created_at - t.created_at).total_seconds() > 300
        ]
        if earlier and not is_famous:
            flags.append(
                FlagOut(
                    "earlier_same_name",
                    "warn",
                    f"{len(earlier)} token(s) with this name/ticker were created earlier",
                )
            )
    if inp.ctx.x_reuse_count >= 5:
        flags.append(
            FlagOut(
                "x_link_reused",
                "warn",
                f"the same X link is attached to {inp.ctx.x_reuse_count} other tokens",
            )
        )
    if inp.x_kind == "search":
        flags.append(FlagOut("search_link_only", "info", "the X link is a search, not an account"))
    if inp.x_kind == "community" and inp.x_object_time and inp.created_at:
        gap = (inp.created_at - inp.x_object_time).total_seconds()
        if 0 <= gap <= 3600:
            flags.append(
                FlagOut(
                    "shell_community",
                    "info",
                    f"the X community was created only {_dur(gap)} before the token",
                )
            )
    if inp.ctx.creator_token_count >= 10:
        flags.append(
            FlagOut(
                "serial_creator",
                "info",
                f"the creator wallet has launched {inp.ctx.creator_token_count} tokens",
            )
        )
    return flags


# ----------------------------------------------------------------- entry point


def run_basic(inp: EngineInput) -> EngineOutput:
    k = load_knowledge()
    n = normalize(inp.name, inp.symbol, inp.description)
    evidence: list[Ev] = []
    evidence += _normalization_evidence(n, k)
    evidence += _lexicon_evidence(n, k)

    matches = known_coins.match_known(n, k, extra=inp.ctx.extra_coins)
    is_famous = any(known_coins.is_self(m, n) for m in matches)
    evidence += known_coins.evidence_for(matches, n, k)
    evidence += known_coins.family_evidence(n, k)

    tk = ticker.explain(n, k)

    img = image_stage.analyze(
        inp.image_bytes, inp.ctx.image_candidates, int(k.scoring.get("logo_phash_edited", 14))
    )
    evidence += _image_evidence(img, k)

    same_evs, copies = _same_name_evidence(inp, n, is_famous)
    evidence += same_evs
    for m in matches:
        if known_coins.is_self(m, n):
            continue
        copies.insert(
            0,
            {
                "ticker": m.coin.symbol,
                "name": m.coin.name,
                "mint": m.coin.mint,
                "signals": m.signals,
            },
        )

    agg = aggregate(evidence, k)
    flags = _flags(inp, n, agg, is_famous)
    extra_caveats: list[str] = []
    if img.error and inp.image_bytes:
        extra_caveats.append(f"image could not be analysed: {img.error}")
    if not n.name_compact and not n.emoji_keywords:
        extra_caveats.append("name is empty or has no readable text")
    if len(agg.evidence) <= 1:
        extra_caveats.append("very little evidence; the token name is generic or unknown")
    summary, caveats = summarize(inp.name, n.ticker or inp.symbol, agg, tk.text, extra_caveats)
    return EngineOutput(
        normalized=n,
        agg=agg,
        ticker_explanation=tk.text,
        copy_of=copies[:5],
        image=img,
        flags=flags,
        summary=summary,
        caveats=caveats,
        evidence=agg.evidence,
    )
