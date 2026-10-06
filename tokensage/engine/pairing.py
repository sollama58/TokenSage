"""S7c: the token a coin is paired against (its bonding-curve quote mint).

Almost every pump.fun coin trades against SOL; a few trade against a stablecoin. Those say
nothing about meaning. A coin paired against another token (a memecoin, a community
token) is launched into that token's community, and often builds on it by name ("Baby
BONK" paired with BONK). This stage turns the pairing into evidence:

- always: the coin is in the pair token's ecosystem (crypto_native/paired_ecosystem)
- the name or ticker builds on the pair token: derivative/pair_family, and the pair
  token's referent becomes a strong referent candidate for the coin
- otherwise only the pair token's categories count, weakly: the coin's own name, ticker
  and logo decide what it is about (the pair token's referent is reported, not voted)

Weights are hand-set, not yet fitted (Phase 6 calibration).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tokensage.engine.aggregate import Aggregated
from tokensage.engine.context import Ev, Normalized, ReferentCandidate
from tokensage.engine.knowledge import Knowledge, KnownCoin, Stock
from tokensage.engine.normalize import normalize

# Pair tokens that carry no meaning: the coin is simply priced in SOL (or staked SOL),
# dollars, or wrapped BTC/ETH. Matched by mint only: a fake "USDC" is just a token.
NEUTRAL = {
    "So11111111111111111111111111111111111111112": ("SOL", "sol"),
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": ("USDC", "stablecoin"),
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": ("USDT", "stablecoin"),
    "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB": ("USD1", "stablecoin"),
    "2b1kV6DkPAnxd5ixfnxCpjxmKwqjjaYmCZfHsFu24GXo": ("PYUSD", "stablecoin"),
    "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn": ("JitoSOL", "lst"),
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So": ("mSOL", "lst"),
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1": ("bSOL", "lst"),
    "jupSoLaHXQiZZTSfEWMTRRgpnyFm8f6sZdosWBjx93v": ("JupSOL", "lst"),
    "cbbtcf3aa214zXHbiAZQwf4122FBYbraNdFqgw4iMij": ("cbBTC", "major"),
    "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh": ("WBTC", "major"),
    "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs": ("WETH", "major"),
}
# Kinds that feed the analysis; the rest are only reported.
MEANINGFUL = ("token", "tokenized_stock")
# Backed xStocks: the stock ticker plus "x" (TSLAx, NVDAx, SPYx, BRK.Bx), named
# "<Company> xStock", minted at vanity addresses starting "Xs".
XSTOCK_SYMBOL = re.compile(r"^([A-Za-z]{1,5}(?:\.[A-Za-z])?)([xX])$")
W_TOKENIZED_STOCK = 0.6
SOL_MINT = "So11111111111111111111111111111111111111112"

W_ECOSYSTEM = 0.55
W_BUILDS_ON = 0.75
REFERENT_BUILDS_ON = 0.65  # the pair token's referent, when the name builds on it
CATEGORY_BUILDS_ON = 0.6  # scale on the pair token's own category confidences
CATEGORY_PAIRED = 0.25
# Categories that describe the pair token's status rather than its subject.
SKIP_CATEGORIES = ("derivative", "crypto_native")
# Words too common to say a name builds on the pair token's name.
NAME_STOP = {
    "the", "a", "an", "of", "on", "in", "and", "to", "for", "is", "it", "my", "your",
    "coin", "token", "inu", "official", "sol", "solana", "pump", "fun", "ai", "x",
    "baby", "mini", "mega", "super", "based", "real",
}  # fmt: skip


@dataclass
class PairInput:
    """What the analyzer knows about the pair token before the engine runs."""

    mint: str
    symbol: str | None = None
    name: str | None = None
    kind: str = "token"  # sol | stablecoin | lst | major | token | tokenized_stock
    source: str | None = None  # neutral | known_coin | analysis | db | onchain | none
    # from a stored TokenSage analysis of the pair token, when there is one
    referent: ReferentCandidate | None = None
    categories: list[tuple[str, float]] = field(default_factory=list)


@dataclass
class PairAssessment:
    mint: str
    symbol: str | None
    name: str | None
    kind: str
    source: str | None
    builds_on: bool = False
    builds_on_detail: str | None = None
    referent: ReferentCandidate | None = None
    categories: list[tuple[str, float]] = field(default_factory=list)
    evidence: list[Ev] = field(default_factory=list)
    underlying: str | None = None  # a tokenized stock's ticker (TSLA for TSLAx)

    @property
    def meaningful(self) -> bool:
        return self.kind in MEANINGFUL

    def label(self) -> str:
        sym = f"${self.symbol}" if self.symbol else self.mint[:8] + "…"
        return f"{sym} ({self.name})" if self.name and self.name != self.symbol else sym


def neutral(mint: str) -> PairInput | None:
    if mint == "SOL" or mint == SOL_MINT:
        return PairInput(mint=SOL_MINT, symbol="SOL", name="Solana", kind="sol", source="neutral")
    hit = NEUTRAL.get(mint)
    if hit:
        return PairInput(mint=mint, symbol=hit[0], name=hit[0], kind=hit[1], source="neutral")
    return None


def xstock_ticker(symbol: str | None, name: str | None, mint: str) -> str | None:
    """The stock ticker behind a Backed xStock pair token (TSLA for TSLAx), else None.
    The symbol shape alone is not enough ("MAX", "SEX"): the name must say xStock or the
    mint must be one of Backed's "Xs..." addresses."""
    m = XSTOCK_SYMBOL.match((symbol or "").strip())
    if not m:
        return None
    named = "xstock" in (name or "").casefold().replace(" ", "")
    if named or (m.group(2) == "x" and mint.startswith("Xs")):
        return m.group(1).replace(".", "").upper()
    return None


def known_coin_for(mint: str, coins: list[KnownCoin]) -> KnownCoin | None:
    return next((c for c in coins if c.mint and c.mint == mint), None)


def builds_on(
    pair_n: Normalized, n: Normalized, stock: Stock | None = None, underlying: str | None = None
) -> str | None:
    """Does the coin's name or ticker build on the pair token's (or, for a tokenized stock,
    on the company's name or stock ticker)? Returns why, or None."""
    ticker = n.ticker.upper()
    if underlying:
        why = _builds_on_stock(n, ticker, stock, underlying)
        if why:
            return why
    pt = pair_n.ticker_base or pair_n.ticker
    if pt and len(pt) >= 3:
        pt = pt.upper()
        if pt in (n.ticker_base.upper(), ticker):
            return f"same ticker base as ${pt}"
        if ticker != pt and pt in ticker:
            return f"ticker ${n.ticker} contains ${pt}"
        if pt.lower() in n.name_tokens:
            return f"name contains '{pt.lower()}'"
    words = [t for t in pair_n.name_tokens if len(t) >= 4 and t not in NAME_STOP]
    for w in words:
        if w in n.name_tokens:
            return f"name contains '{w}' from the pair token's name"
    pc = pair_n.name_compact
    if len(pc) >= 5 and pc in n.name_compact and pc != n.name_compact:
        return f"name contains '{pc}'"
    return None


def _builds_on_stock(n: Normalized, ticker: str, stock: Stock | None, sym: str) -> str | None:
    if len(sym) >= 2 and (ticker == sym or n.ticker_base.upper() == sym):
        return f"same ticker as the stock ${sym}"
    if len(sym) >= 3 and sym in ticker:
        return f"ticker ${n.ticker} contains the stock ticker ${sym}"
    if len(sym) >= 3 and sym.lower() in n.name_tokens:
        return f"name contains the stock ticker '{sym.lower()}'"
    if stock is not None:
        company = normalize(stock.name, None, None)
        for w in company.name_tokens:
            if len(w) >= 4 and w not in NAME_STOP and w in n.name_tokens:
                return f"name contains '{w}' ({stock.name})"
    return None


def assess(
    pair: PairInput,
    n: Normalized,
    k: Knowledge,
    pair_meaning: Aggregated | None,
    coin: KnownCoin | None,
) -> PairAssessment:
    """pair_meaning: the engine's basic read of the pair token's name/ticker, used when no
    stored analysis or known-coin entry says what the pair token is."""
    a = PairAssessment(
        mint=pair.mint, symbol=pair.symbol, name=pair.name, kind=pair.kind, source=pair.source
    )
    if not a.meaningful:
        return a
    if a.kind == "token":
        underlying = xstock_ticker(pair.symbol, pair.name, pair.mint)
        if underlying:
            return _assess_tokenized_stock(a, underlying, n, k)

    referent = pair.referent
    categories = list(pair.categories)
    if coin is not None:
        referent = ReferentCandidate(
            label=coin.referent_label,
            kind=coin.referent_kind,
            desc=coin.referent_desc,
            source=f"known_coins:{coin.symbol}",
            score=0.9,
            categories=list(coin.categories),
        )
        categories = categories or [(c, 0.8) for c in coin.categories]
        a.name = a.name or coin.name
        a.symbol = a.symbol or coin.symbol
    if referent is None and pair_meaning is not None and pair_meaning.referent is not None:
        if pair_meaning.referent.score >= 0.45:
            referent = pair_meaning.referent
    if not categories and pair_meaning is not None:
        categories = list(pair_meaning.categories)
    a.referent = referent
    a.categories = [(lbl, round(c, 3)) for lbl, c in categories][:6]

    pair_n = normalize(a.name, a.symbol, None)
    why = builds_on(pair_n, n)
    a.builds_on = why is not None
    a.builds_on_detail = why
    who = a.label()
    src = f"pair:{a.symbol or a.mint}"

    a.evidence.append(
        Ev(
            kind="pair",
            label="crypto_native/paired_ecosystem",
            weight=W_ECOSYSTEM,
            detail=f"trades against {who} instead of SOL: launched into that token's community",
            source=src,
            where="chain",
        )
    )
    if a.builds_on:
        a.evidence.append(
            Ev(
                kind="pair",
                label="derivative/pair_family",
                weight=W_BUILDS_ON,
                detail=f"the name builds on {who}, the token it trades against ({why})",
                source=src,
                where="chain",
            )
        )
    if referent is not None and a.builds_on:
        ref = ReferentCandidate(
            label=referent.label,
            kind=referent.kind,
            desc=referent.desc,
            source=src,
            score=round(min(REFERENT_BUILDS_ON, referent.score), 3),
            categories=list(referent.categories),
        )
        a.evidence.append(
            Ev(
                kind="referent",
                label="referent",
                weight=ref.score,
                detail=f"builds on the pair token {who}, which refers to {referent.label}",
                source=src,
                where="chain",
                referent=ref,
            )
        )
    scale = CATEGORY_BUILDS_ON if a.builds_on else CATEGORY_PAIRED
    for lbl, conf in a.categories:
        if lbl.startswith(SKIP_CATEGORIES):
            continue
        if any(o.startswith(lbl + "/") for o, _ in a.categories):
            continue  # a parent: its sub-label carries it
        a.evidence.append(
            Ev(
                kind="pair",
                label=lbl,
                weight=round(conf * scale, 3),
                detail=f"the pair token {who} is {lbl}",
                source=src,
                where="chain",
            )
        )
    return a


def _assess_tokenized_stock(
    a: PairAssessment, underlying: str, n: Normalized, k: Knowledge
) -> PairAssessment:
    """A coin paired against a tokenized stock (xStock) is launched for that stock's
    crowd: Tesla/Elon fans for TSLAx, AI traders for NVDAx. Its meaning comes from the
    company, and the coin often builds on the company name or ticker ("Tesla Moon")."""
    a.kind = "tokenized_stock"
    a.underlying = underlying
    stock = k.stocks.get(underlying)
    who = a.label()
    src = f"pair:{a.symbol or a.mint}"
    company = stock.name if stock else (a.name or "").replace("xStock", "").strip() or underlying
    if stock is not None:
        a.referent = ReferentCandidate(
            label=stock.entity_label,
            kind="other",
            desc=stock.desc,
            source=f"stocks:{underlying}",
            score=0.9,
            categories=list(stock.categories),
        )
        a.categories = [(c, 0.9) for c in stock.categories]
    else:
        a.categories = [("tradfi/stock", 0.8)]
    why = builds_on(normalize(a.name, a.symbol, None), n, stock, underlying)
    a.builds_on = why is not None
    a.builds_on_detail = why
    a.evidence.append(
        Ev(
            kind="pair",
            label="tradfi/tokenized_stock",
            weight=W_TOKENIZED_STOCK,
            detail=f"trades against {who}, the tokenized {company} stock (xStock, "
            f"${underlying}): launched for that stock's crowd",
            source=src,
            where="chain",
        )
    )
    if a.builds_on:
        a.evidence.append(
            Ev(
                kind="pair",
                label="derivative/pair_family",
                weight=W_BUILDS_ON,
                detail=f"the name builds on {company} (${underlying}), the stock it trades "
                f"against ({why})",
                source=src,
                where="chain",
            )
        )
        if a.referent is not None:
            ref = ReferentCandidate(
                label=a.referent.label,
                kind=a.referent.kind,
                desc=a.referent.desc,
                source=src,
                score=REFERENT_BUILDS_ON,
                categories=list(a.referent.categories),
            )
            a.evidence.append(
                Ev(
                    kind="referent",
                    label="referent",
                    weight=ref.score,
                    detail=f"builds on the paired stock {a.referent.label}: {a.referent.desc}",
                    source=src,
                    where="chain",
                    referent=ref,
                )
            )
    scale = CATEGORY_BUILDS_ON if a.builds_on else CATEGORY_PAIRED
    for lbl, conf in a.categories:
        if lbl.startswith(SKIP_CATEGORIES) or lbl == "tradfi/stock":
            continue  # tradfi/tokenized_stock above already says it
        a.evidence.append(
            Ev(
                kind="pair",
                label=lbl,
                weight=round(conf * scale, 3),
                detail=f"the paired stock {company} is {lbl}",
                source=src,
                where="chain",
            )
        )
    return a
