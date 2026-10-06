"""The engine pipeline at basic depth. Pure and synchronous: all I/O results come in through
EngineInput, so golden tests run without a database or network."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from tokensage.engine import image as image_stage
from tokensage.engine import (
    known_coins,
    lexicon,
    ocr,
    pairing,
    ticker,
    trends,
    xmatch,
    xsignals,
)
from tokensage.engine.aggregate import Aggregated, aggregate
from tokensage.engine.context import Ev, Normalized, ReferentCandidate
from tokensage.engine.knowledge import Entity, Knowledge, KnownCoin, SlangTerm, load_knowledge
from tokensage.engine.normalize import normalize
from tokensage.engine.render_summary import summarize
from tokensage.sources.x import ProfileData, TweetData

RULES_VERSION = "0.8.0-full"

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
    copycat_window_days: int = 30


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
    pair: pairing.PairInput | None = None  # the bonding curve's quote token
    # full depth only
    x_url_handle: str | None = None
    tweet: TweetData | None = None
    profile: ProfileData | None = None
    ocr_lines: list[ocr.OcrLine] | None = None  # pre-computed (cached) OCR; None = run it
    run_ocr: bool = False
    trend_index: trends.TrendIndex | None = None
    x_media: list[xmatch.MediaHash] | None = None  # hashed post images / profile avatar
    # the logo's hashes when they come from cache instead of image_bytes
    logo_features: image_stage.ImageFeatures | None = None


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
    depth: str = "basic"
    ocr_lines: list[ocr.OcrLine] = field(default_factory=list)
    ocr_error: str | None = None
    x: xsignals.XAssessment | None = None
    trend_hits: list[trends.TrendHit] = field(default_factory=list)
    x_match: xmatch.XMatch | None = None
    pair: pairing.PairAssessment | None = None


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


# An extra lexicon pass: (where, text, factor) or (where, text, factor, note). A note marks
# a pass over account names: its details say so and its referents are scaled by the factor.
Pass = tuple[str, str, float] | tuple[str, str, float, str]


def _lexicon_evidence(
    n: Normalized, k: Knowledge, extra_passes: list[Pass] | None = None
) -> list[Ev]:
    evs: list[Ev] = []
    name_text = " ".join(n.name_tokens)
    # The compact form catches brand names the camelCase split breaks apart ("DeepSeek")
    # and multi-word slang stored compact ("diamondhands"); the ticker is a signal too.
    passes: list[Pass] = [
        ("name", name_text, 1.0),
        ("name", n.name_compact if n.name_compact != name_text else "", 1.0),
        ("symbol", n.ticker.lower() if len(n.ticker) >= 3 else "", 0.8),
        ("description", n.desc_clean, 1.0),
        *(extra_passes or []),
    ]
    seen_hits: set[tuple[str, str, str]] = set()
    for where, text, factor, *note in passes:
        if not text:
            continue
        first_new = len(evs)
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
        if note:
            scaled: set[int] = set()  # one entity's evidence rows share one candidate
            for ev in evs[first_new:]:
                ev.detail = f"{note[0]}: {ev.detail}"
                if ev.referent is not None and id(ev.referent) not in scaled:
                    scaled.add(id(ev.referent))
                    ev.referent.score = round(ev.referent.score * factor, 3)
                if ev.referent is not None and ev.label == "referent":
                    ev.weight = ev.referent.score
    return evs


@dataclass
class RecentCopy:
    """A token launched within the copycat window before this one that it copies."""

    what: str  # e.g. "$PNUT (Peanut)" or a mint prefix
    via: str  # name/ticker | logo
    age_s: float  # how long before this token it launched


def _within_window(inp: EngineInput, other: datetime | None) -> float | None:
    """Seconds `other` launched before this token, when that is inside the copycat window
    (and more than 5 min, so a near-simultaneous launch is not a copy); else None."""
    if other is None or inp.created_at is None:
        return None
    gap = (_aware(inp.created_at) - _aware(other)).total_seconds()
    if 300 < gap <= inp.ctx.copycat_window_days * 86400:
        return gap
    return None


def _same_name_evidence(
    inp: EngineInput, n: Normalized, is_famous: bool
) -> tuple[list[Ev], list[dict], list[RecentCopy]]:
    """Earlier tokens with this name or ticker. Only those launched within the copycat
    window count: a namesake from months ago is not the live coin this one copies."""
    evs: list[Ev] = []
    copies: list[dict] = []
    recent: list[RecentCopy] = []
    if not inp.ctx.same_name or not inp.created_at:
        return evs, copies, recent
    in_window = [
        (t, gap)
        for t in inp.ctx.same_name
        if t.mint != inp.mint and (gap := _within_window(inp, t.created_at)) is not None
    ]
    if not in_window:
        return evs, copies, recent
    in_window.sort(key=lambda tg: -tg[1])  # earliest launch first
    first, gap = in_window[0]
    days = inp.ctx.copycat_window_days
    if not is_famous:
        evs.append(
            Ev(
                kind="same_name",
                label="derivative/copycat",
                weight=min(0.75, 0.45 + 0.05 * len(in_window)),
                detail=(
                    f"{len(in_window)} token(s) with this name/ticker launched in the {days} d "
                    f"before it; the earliest (${first.symbol or '?'}, {first.source}) "
                    f"{_dur(gap)} earlier"
                ),
                source=f"same_name:{first.source}",
                where="db",
            )
        )
        recent.append(RecentCopy(f"${first.symbol or '?'}", "name/ticker", gap))
    copies.append(
        {
            "ticker": first.symbol,
            "name": first.name,
            "mint": first.mint,
            "signals": ["same_name_recent", f"recent_count:{len(in_window)}"],
            "created_at": first.created_at,
            "recent": True,
        }
    )
    return evs, copies, recent


def _image_evidence(
    res: image_stage.ImageResult, k: Knowledge, inp: EngineInput
) -> tuple[list[Ev], list[RecentCopy]]:
    """Logo near-duplicates. A token's logo counts as copied only when that token launched
    before this one (and, by the candidate query, within the copycat window); a famous
    coin's logo is a reference to it."""
    evs: list[Ev] = []
    recent: list[RecentCopy] = []
    same = int(k.scoring.get("logo_phash_same", 8))
    for nd in res.near:
        c = nd.candidate
        if c.mint:
            gap = _within_window(inp, c.created_at)
            if gap is None and inp.created_at is not None:
                continue  # launched after this token, or outside the window: not its source
            if gap is not None:
                recent.append(RecentCopy(c.mint[:6] + "…", "logo", gap))
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
    return evs, recent


def _dur(s: float) -> str:
    if s < 120:
        return f"{int(s)} s"
    if s < 7200:
        return f"{int(s // 60)} min"
    if s < 172800:
        return f"{s / 3600:.1f} h"
    return f"{int(s // 86400)} d"


# ----------------------------------------------------------------- flags


def _flags(
    inp: EngineInput,
    n: Normalized,
    agg: Aggregated,
    is_famous: bool,
    recent_copies: list[RecentCopy] | None = None,
    known: list[known_coins.CopyMatch] | None = None,
) -> list[FlagOut]:
    flags: list[FlagOut] = []
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
    days = inp.ctx.copycat_window_days
    if recent_copies and not is_famous:
        # copies of coins launched in the last `days` days: riding a live coin
        best = min(recent_copies, key=lambda c: c.age_s)
        flags.append(
            FlagOut(
                "copycat",
                "warn",
                f"copies {best.what} by {best.via}, launched {_dur(best.age_s)} before it "
                f"(within {days} d)",
            )
        )
    names = [c for c in recent_copies or [] if c.via == "name/ticker"]
    if names and not is_famous:
        flags.append(
            FlagOut(
                "earlier_same_name",
                "warn",
                f"{names[0].what} with this name/ticker launched {_dur(names[0].age_s)} "
                f"earlier (within {days} d)",
            )
        )
    established = [m for m in known or [] if not known_coins.is_self(m, n)]
    if established and not is_famous:
        top = established[0].coin
        flags.append(
            FlagOut(
                "references_known_coin",
                "info",
                f"builds on the established coin ${top.symbol} ({top.name}); "
                "a reference, not a recent copy",
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
        gap = (_aware(inp.created_at) - _aware(inp.x_object_time)).total_seconds()
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


def _ocr_pass(
    inp: EngineInput, n: Normalized, k: Knowledge
) -> tuple[list[ocr.OcrLine], str | None, list[Ev], str]:
    """Run (or reuse) OCR; return lines, error, evidence and the cleaned OCR text."""
    lines: list[ocr.OcrLine] = []
    err: str | None = None
    if inp.ocr_lines is not None:
        lines = inp.ocr_lines
    elif inp.run_ocr and inp.image_bytes:
        lines, err = ocr.read(inp.image_bytes)
    if not lines:
        return lines, err, [], ""
    from tokensage.engine.normalize import normalize as _norm

    text = " ".join(line.text for line in lines)
    ocr_n = _norm(text, "", None)
    ocr_text = " ".join(ocr_n.name_tokens)
    evs: list[Ev] = []
    compact = ocr_n.name_compact
    t = n.ticker_base.lower()
    if t and (t in compact or n.ticker.lower() in compact):
        evs.append(
            Ev(
                "ocr",
                "crypto_native/pumpfun_meta",
                0.15,
                f"the logo text reads '{text[:60]}', matching the ticker",
                "ocr",
                "image",
            )
        )
    elif n.name_compact and n.name_compact in compact:
        evs.append(
            Ev(
                "ocr",
                "crypto_native/pumpfun_meta",
                0.1,
                f"the logo text reads '{text[:60]}', matching the name",
                "ocr",
                "image",
            )
        )
    # a *different* known ticker written on the logo is a strong copycat tell
    by_sym = k.coin_by_symbol()
    for tok in ocr_n.name_tokens:
        up = tok.upper().lstrip("$")
        if len(up) >= 3 and up in by_sym and up != n.ticker_base.upper() and up != n.ticker.upper():
            c = by_sym[up][0]
            evs.append(
                Ev(
                    "ocr_other_ticker",
                    "derivative/logo_reuse",
                    0.6,
                    f"the logo text says '${up}' ({c.name}) but the token is ${n.ticker}",
                    f"known_coins:{c.symbol}",
                    "image",
                    referent=ReferentCandidate(
                        c.referent_label,
                        c.referent_kind,
                        c.referent_desc,
                        f"ocr:{c.symbol}",
                        0.5,
                        list(c.categories),
                    ),
                )
            )
    return lines, err, evs, ocr_text


def _run(inp: EngineInput, depth: str) -> EngineOutput:
    k = load_knowledge()
    n = normalize(inp.name, inp.symbol, inp.description)
    evidence: list[Ev] = []
    evidence += _normalization_evidence(n, k)

    extra_passes: list[Pass] = []
    ocr_lines: list[ocr.OcrLine] = []
    ocr_err: str | None = None
    xa: xsignals.XAssessment | None = None
    if depth == "full":
        ocr_lines, ocr_err, ocr_evs, ocr_text = _ocr_pass(inp, n, k)
        evidence += ocr_evs
        if ocr_text:
            extra_passes.append(("image", ocr_text, k.scoring.get("ocr_factor", 0.7)))
        if inp.x_kind in ("tweet", "profile", "community", "search"):
            xa = xsignals.assess(
                inp.x_kind,
                inp.x_url_handle,
                inp.tweet,
                inp.profile,
                inp.created_at,
                n.ticker or None,
                inp.mint,
                n.name_tokens,
            )
            evidence += xa.evidence
            if xa.text:
                extra_passes.append(("x", xa.text, 0.8))
            for related in (xa.quoted, xa.replied_to):
                if related and related.text:
                    extra_passes.append(("x", related.text, 0.7))
            extra_passes += _account_passes(xa)
    evidence += _lexicon_evidence(n, k, extra_passes)

    matches = known_coins.match_known(n, k, extra=inp.ctx.extra_coins)
    is_famous = any(known_coins.is_self(m, n) for m in matches)
    evidence += known_coins.evidence_for(matches, n, k)
    evidence += known_coins.family_evidence(n, k)

    tk = ticker.explain(n, k)

    max_dist = int(k.scoring.get("logo_phash_edited", 14))
    img = image_stage.analyze(inp.image_bytes, inp.ctx.image_candidates, max_dist)
    if img.features is None and inp.logo_features is not None:
        # the logo's hashes came from cache: compare those instead of skipping the check
        img = image_stage.ImageResult(
            features=inp.logo_features,
            near=image_stage.near_duplicates(inp.logo_features, inp.ctx.image_candidates, max_dist),
        )
    img_evs, recent_copies = _image_evidence(img, k, inp)
    evidence += img_evs

    pair = _pair(inp, n, k)
    if pair is not None:
        evidence += pair.evidence

    same_evs, copies, recent_names = _same_name_evidence(inp, n, is_famous)
    evidence += same_evs
    recent_copies = (recent_names + recent_copies) if not is_famous else []
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
                "recent": False,  # an established coin: a reference, not a live copy
            },
        )

    trend_hits: list[trends.TrendHit] = []
    if depth == "full" and inp.trend_index is not None:
        texts = [("name", " ".join(n.name_tokens)), ("description", n.desc_clean)]
        if xa and xa.text:
            texts.append(("x", xa.text))
        for related in (xa.quoted, xa.replied_to) if xa else ():
            if related and related.text:
                texts.append(("x", related.text))
        seen_terms: set[str] = set()
        for where, text in texts:
            for h in inp.trend_index.match(text, where):
                if h.term.term not in seen_terms:
                    seen_terms.add(h.term.term)
                    trend_hits.append(h)
        evidence += trends.evidence(trend_hits, inp.trend_index)

    agg = aggregate(evidence, k)
    flags = _flags(inp, n, agg, is_famous, recent_copies, matches)
    if pair is not None and pair.meaningful:
        flags.append(
            FlagOut(
                "non_sol_pair",
                "info",
                f"trades against {pair.label()} instead of SOL"
                + (f"; the name builds on it ({pair.builds_on_detail})" if pair.builds_on else ""),
            )
        )
    if xa:
        for code, sev, detail in xa.flags:
            flags.append(FlagOut(code, sev, detail))
    x_match: xmatch.XMatch | None = None
    if depth == "full" and inp.x_kind in ("tweet", "profile"):
        x_match = _x_match(inp, n, img, k)
        if x_match.content_fetched and x_match.fit < xmatch.FIT_RELATED:
            flags.append(
                FlagOut(
                    "x_content_mismatch",
                    "warn",
                    f"the linked X {inp.x_kind} does not match the token "
                    f"(fit {x_match.fit:.2f}: {x_match.name.detail}; {x_match.ticker.detail})",
                )
            )
        if xmatch.is_image_match(x_match.image, k):
            flags.append(FlagOut("x_image_match", "info", x_match.image.detail))
    extra_caveats: list[str] = []
    if img.error and inp.image_bytes:
        extra_caveats.append(f"image could not be analysed: {img.error}")
    if not n.name_compact and not n.emoji_keywords:
        extra_caveats.append("name is empty or has no readable text")
    if len(agg.evidence) <= 1:
        extra_caveats.append("very little evidence; the token name is generic or unknown")
    if xa and xa.status == "failed":
        extra_caveats.append("the linked X content could not be fetched")
    if xa and xa.status == "deleted":
        extra_caveats.append("the linked tweet or account no longer exists")
    if depth == "full" and ocr_err and inp.image_bytes:
        extra_caveats.append(f"OCR unavailable: {ocr_err}")
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
        depth=depth,
        ocr_lines=ocr_lines,
        x_match=x_match,
        ocr_error=ocr_err,
        x=xa,
        trend_hits=trend_hits,
        pair=pair,
    )


# The quoted / replied-to author is usually the narrative; the posting account is often the
# deployer's own; a mention is the weakest tie.
_ACCOUNT_FACTOR = {"author": 0.4, "quoted_author": 0.5, "replied_to_author": 0.5, "mentioned": 0.3}
_ACCOUNT_ROLE = {
    "author": "the posting account",
    "quoted_author": "the quoted account",
    "replied_to_author": "the replied-to account",
    "mentioned": "a mentioned account",
}


def account_text(name: str | None, handle: str | None) -> str:
    """An account's display name and handle as words: "Elon Musk elon musk" for
    (Elon Musk, @elonmusk). Handles are segmented ("elonmusk" -> "elon musk")."""
    parts: list[str] = []
    for raw in (name, handle):
        if raw:
            parts += normalize(raw, None, None).name_tokens
    return " ".join(dict.fromkeys(parts))


def _account_passes(xa: xsignals.XAssessment) -> list[Pass]:
    """The names of the accounts involved are evidence too: a reply to @elonmusk, a quote of
    a famous dog's account, a launch post from an account named like the coin."""
    out: list[Pass] = []
    for acc in xa.accounts:
        text = account_text(acc.name, acc.handle)
        if text:
            who = f"@{acc.handle}" if acc.handle else (acc.name or "?")
            out.append(("x", text, _ACCOUNT_FACTOR[acc.role], f"{_ACCOUNT_ROLE[acc.role]} {who}"))
    return out


def _pair(inp: EngineInput, n: Normalized, k: Knowledge) -> pairing.PairAssessment | None:
    """Read the pair token: a known coin, a stored analysis, or (failing both) the engine's
    basic read of the pair token's own name and ticker."""
    if inp.pair is None:
        return None
    coin = None
    pair_meaning = None
    if inp.pair.kind == "token":
        coin = pairing.known_coin_for(inp.pair.mint, [*k.coins, *inp.ctx.extra_coins])
        if coin is None and inp.pair.referent is None and (inp.pair.name or inp.pair.symbol):
            pair_only = EngineInput(
                mint=inp.pair.mint,
                name=inp.pair.name,
                symbol=inp.pair.symbol,
                description=None,
                image_bytes=None,
                created_at=None,
                ctx=DbContext(extra_coins=inp.ctx.extra_coins),
            )
            pair_meaning = _run(pair_only, "basic").agg
    return pairing.assess(inp.pair, n, k, pair_meaning, coin)


def _x_match(
    inp: EngineInput, n: Normalized, img: image_stage.ImageResult, k: Knowledge
) -> xmatch.XMatch:
    """Compare the linked post with the token, keeping the two apart: what the name, ticker
    and logo mean on their own vs what the post alone is about."""
    token_only = EngineInput(
        mint=inp.mint,
        name=inp.name,
        symbol=inp.symbol,
        description=None,
        image_bytes=inp.image_bytes,
        created_at=inp.created_at,
        ctx=inp.ctx,
    )
    text = xmatch.post_text(inp.tweet, inp.profile)
    post_meaning = None
    if text:
        post_only = EngineInput(
            mint=inp.mint,
            name=None,
            symbol=None,
            description=text,
            image_bytes=None,
            created_at=inp.created_at,
            ctx=DbContext(extra_coins=inp.ctx.extra_coins),
        )
        post_meaning = _run(post_only, "basic").agg
    token_meaning = _run(token_only, "basic").agg
    return xmatch.assess(
        n,
        inp.tweet,
        inp.profile,
        img.features or inp.logo_features,
        inp.x_media or [],
        token_meaning,
        post_meaning,
        k,
    )


def _aware(d: datetime) -> datetime:
    """Naive datetimes are UTC (old cache rows); never let a subtraction raise."""
    return d if d.tzinfo is not None else d.replace(tzinfo=UTC)


def run_basic(inp: EngineInput) -> EngineOutput:
    return _run(inp, "basic")


def run_full(inp: EngineInput) -> EngineOutput:
    return _run(inp, "full")
