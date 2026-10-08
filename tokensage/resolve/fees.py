"""Where a pump.fun coin's creator fee goes (rules 0.19.0).

pump.fun charges a creator fee on every trade. Since 2025-2026 that fee can be redirected,
and the redirect is visible on-chain:

- **holder rewards** (`BondingCurve.is_holder_reward`): the fee is set aside for holders and
  pump.fun pays it out. The curve's `creator` is then the per-mint PDA
  `["holder-rewards", mint]` under the pump program, not a wallet.
- **cashback** (`BondingCurve.is_cashback_coin`, deprecated): the fee goes back to traders.
- **fee sharing**: the creator vault is migrated to the Pump Fees program
  (`pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ`). The curve's `creator` becomes the
  `SharingConfig` PDA `["sharing-config", mint]`, whose `shareholders` (address, share_bps,
  summing to 10,000) say who is paid. A shareholder address is one of:
    - a plain wallet (system-owned, or not yet funded): the creator or any other wallet;
    - a `SocialFeePda` of the Pump Fees program (`["social-fee-pda", user_id, platform]`):
      fees claimable by the linked social account. Platform 2 is GitHub (the "pay a GitHub
      account" feature, Feb 2026); 1 is X; 0 is pump.fun;
    - a `DonationFeePda` (`["donation-fee-pda", mint, config_id]`): a "charity coin"
      (Apr 2026). The fee is escrowed per (mint, donate.gg config) and cranked into
      donate.gg's relay program for the chosen charity.
- otherwise the fee goes straight to the wallet in `BondingCurve.creator` (the launch
  wallet, or whoever a community takeover re-pointed it to).

Cost: the sharing-config PDA rides in the resolver's one getMultipleAccounts call (no extra
credit). Only a fee-shared coin whose shareholders are not just its own admin costs one
more getMultipleAccounts (1 Helius credit) to classify the recipient accounts; wallets,
social and donation PDAs are then cached in `fee_recipient`.

Verified on mainnet 2026-10-08 against real coins of every kind (tests/fixtures/
fee_accounts.json holds their account bytes).
"""

from __future__ import annotations

import base64
import re
import struct
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import httpx
import structlog

from tokensage.resolve.pump_ca import PUMP_PROGRAM, _on_curve, b58decode, find_program_address
from tokensage.resolve.pump_event import b58encode
from tokensage.resolve.rpc import RpcError, SolanaRpc

log = structlog.get_logger("resolve.fees")

PUMP_FEES_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
DONATION_RELAY_PROGRAM = "RLAYHr9TRFcKB2ubYQhspcnXiaGpaVzNQvHytt47RZu"

# Anchor account discriminators from idl/pump_fees.json (pump-fun/pump-public-docs)
SHARING_CONFIG_DISC = bytes([216, 74, 9, 0, 56, 140, 93, 75])
SOCIAL_FEE_PDA_DISC = bytes([139, 96, 53, 17, 42, 169, 206, 150])
DONATION_FEE_PDA_DISC = bytes([246, 197, 96, 9, 193, 30, 93, 115])

# SocialFeePda.platform (@pump-fun/pump-sdk `Platform`)
PLATFORMS = {0: "pump", 1: "x", 2: "github"}
ZERO_KEY = "1" * 32

# creator | wallet | split | holder_rewards | charity | github | social | other | cashback | unknown
Destination = str


# ----------------------------------------------------------------- PDAs


def sharing_config_pda(mint: str) -> str:
    return find_program_address([b"sharing-config", b58decode(mint)], PUMP_FEES_PROGRAM)[0]


def holder_rewards_pda(mint: str) -> str:
    return find_program_address([b"holder-rewards", b58decode(mint)], PUMP_PROGRAM)[0]


def social_fee_pda(user_id: str, platform: int) -> str:
    return find_program_address(
        [b"social-fee-pda", user_id.encode(), bytes([platform])], PUMP_FEES_PROGRAM
    )[0]


def donation_fee_pda(mint: str, config_id: str) -> str:
    return find_program_address(
        [b"donation-fee-pda", b58decode(mint), b58decode(config_id)], PUMP_FEES_PROGRAM
    )[0]


# ----------------------------------------------------------------- decoders


def _pk(b: bytes, o: int) -> tuple[str, int]:
    if o + 32 > len(b):
        raise ValueError("truncated pubkey")
    return b58encode(b[o : o + 32]), o + 32


def _u(b: bytes, o: int, fmt: str) -> tuple[int, int]:
    size = struct.calcsize(fmt)
    if o + size > len(b):
        raise ValueError("truncated int")
    return struct.unpack_from(fmt, b, o)[0], o + size


def _string(b: bytes, o: int, limit: int = 256) -> tuple[str, int]:
    n, o = _u(b, o, "<I")
    if n > limit or o + n > len(b):
        raise ValueError("bad string length")
    return b[o : o + n].decode("utf-8", "replace"), o + n


def decode_sharing_config(data: bytes) -> dict[str, Any]:
    """SharingConfig {bump, version, status, mint, admin, admin_revoked, shareholders[]}."""
    if data[:8] != SHARING_CONFIG_DISC:
        raise ValueError("not a pump_fees SharingConfig account")
    o = 8
    bump, o = _u(data, o, "<B")
    version, o = _u(data, o, "<B")
    status, o = _u(data, o, "<B")  # 0 Paused, 1 Active
    mint, o = _pk(data, o)
    admin, o = _pk(data, o)
    revoked, o = _u(data, o, "<B")
    n, o = _u(data, o, "<I")
    if n > 32:
        raise ValueError("too many shareholders")
    shareholders = []
    for _ in range(n):
        addr, o = _pk(data, o)
        bps, o = _u(data, o, "<H")
        shareholders.append({"address": addr, "share_bps": bps})
    # the program enforces a 10,000 bps total; anything else is not a config we can read
    if sum(sh["share_bps"] for sh in shareholders) > 10_000:
        raise ValueError("shareholder shares exceed 10,000 bps")
    return {
        "version": version,
        "status": "active" if status == 1 else "paused",
        "mint": mint,
        "admin": admin,
        "admin_revoked": bool(revoked),
        "shareholders": shareholders,
    }


def decode_social_fee_pda(data: bytes) -> dict[str, Any]:
    """SocialFeePda {bump, version, user_id, platform, total_claimed, last_claimed,
    total_stable_claimed}."""
    if data[:8] != SOCIAL_FEE_PDA_DISC:
        raise ValueError("not a pump_fees SocialFeePda account")
    o = 10  # disc, bump, version
    user_id, o = _string(data, o, limit=64)
    platform, o = _u(data, o, "<B")
    total_claimed, o = _u(data, o, "<Q")
    last_claimed, o = _u(data, o, "<Q")
    total_stable = 0
    if o + 8 <= len(data):
        total_stable, o = _u(data, o, "<Q")
    return {
        "user_id": user_id,
        "platform": platform,
        "total_claimed": total_claimed,
        "last_claimed": last_claimed,
        "total_stable_claimed": total_stable,
    }


def decode_donation_fee_pda(data: bytes) -> dict[str, Any]:
    """DonationFeePda {bump, version, config_id, base_mint, quote_mint, creator,
    total_donated, last_crank_ts}."""
    if data[:8] != DONATION_FEE_PDA_DISC:
        raise ValueError("not a pump_fees DonationFeePda account")
    o = 10
    config_id, o = _pk(data, o)
    base_mint, o = _pk(data, o)
    quote_mint, o = _pk(data, o)
    creator, o = _pk(data, o)
    total_donated, o = _u(data, o, "<Q")
    last_crank = 0
    if o + 8 <= len(data):
        last_crank, o = _u(data, o, "<q")
    return {
        "config_id": config_id,
        "base_mint": base_mint,
        "quote_mint": quote_mint,
        "creator": creator,
        "total_donated": total_donated,
        "last_crank_ts": last_crank,
    }


# ----------------------------------------------------------------- model


@dataclass
class FeeRecipient:
    address: str
    share_bps: int
    kind: str  # creator | wallet | github | x | pump | charity | program | unresolved
    is_creator: bool = False
    platform: str | None = None
    user_id: str | None = None
    github_login: str | None = None
    url: str | None = None
    charity_config_id: str | None = None
    lifetime_received: float | None = None  # SOL (or quote units) claimed/donated so far

    @property
    def share(self) -> float:
        return round(self.share_bps / 10_000, 4)


@dataclass
class CreatorFee:
    destination: Destination
    mechanism: str  # direct | sharing_config | holder_rewards | cashback
    creator_fee_bps: int | None = None  # a custom rate (custom pairs only); 0 = standard
    admin: str | None = None  # the wallet that controls the split (fee sharing)
    sharing_config: str | None = None
    sharing_version: int | None = None
    sharing_status: str | None = None
    mutable: bool | None = None  # the split can still be changed by its admin
    split: bool = False  # more than one recipient
    recipients: list[FeeRecipient] = field(default_factory=list)
    shares: dict[str, float] = field(default_factory=dict)  # kind -> share of the fee
    caveats: list[str] = field(default_factory=list)
    rpc_calls: int = 0

    @property
    def creator_wallet(self) -> str | None:
        """The human behind the coin, when the curve's creator is a PDA: the sharing
        config's admin."""
        return self.admin

    def describe(self) -> str:
        """One plain sentence for summaries and flags."""
        d = self.destination
        if d == "holder_rewards":
            return "creator fees go to the coin's holders (holder rewards coin)"
        if d == "cashback":
            return "creator fees go back to traders as cashback (deprecated cashback coin)"
        if d == "creator":
            return "creator fees go to the creator wallet" + (
                " through a fee-sharing config" if self.mechanism == "sharing_config" else ""
            )
        if d == "unknown":
            return "where the creator fees go could not be determined"
        parts = []
        for r in sorted(self.recipients, key=lambda r: -r.share_bps):
            pct = f"{r.share_bps / 100:g}%"
            if r.kind == "github":
                handle = f"@{r.github_login}" if r.github_login else f"#{r.user_id or '?'}"
                who = f"GitHub account {handle}"
            elif r.kind == "charity":
                who = "a charity via donate.gg"
            elif r.kind == "creator":
                who = "the creator wallet"
            elif r.kind == "wallet":
                who = f"wallet {r.address[:4]}…{r.address[-4:]}"
            elif r.kind in ("x", "pump"):
                who = f"{r.kind} account {r.user_id or '?'}"
            elif r.kind == "social":
                who = f"a linked social account ({r.platform or 'unknown platform'})"
            elif r.kind == "unresolved":
                who = f"an account that could not be classified ({r.address[:4]}…{r.address[-4:]})"
            elif r.kind == "program":
                who = f"an account of another program ({r.address[:4]}…{r.address[-4:]})"
            else:
                who = f"{r.kind} {r.address[:4]}…{r.address[-4:]}"
            parts.append(f"{pct} to {who}")
        head = {
            "charity": "creator fees go to charity",
            "github": "creator fees go to a GitHub account",
            "social": "creator fees go to a linked social account",
            "wallet": "creator fees go to a wallet other than the creator",
            "split": "creator fees are split",
            "other": "creator fees go to an account of another program",
        }.get(d, "creator fees are redirected")
        return f"{head}: {', '.join(parts)}" if parts else head


# ----------------------------------------------------------------- classification


# A user id as GitHub and X issue them. SocialFeePda.user_id is written by whoever creates
# the PDA (the instruction is permissionless), so anything else is never echoed.
_USER_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def _clean_user_id(user_id: Any) -> str | None:
    return user_id if isinstance(user_id, str) and _USER_ID.fullmatch(user_id) else None


def _account_bytes(acc: dict) -> bytes:
    """The account's data as bytes; b"" for any shape other than [base64, "base64"]."""
    raw = acc.get("data")
    if not (isinstance(raw, list) and raw and isinstance(raw[0], str)):
        return b""
    try:
        return base64.b64decode(raw[0])
    except ValueError:
        return b""


def _is_off_curve(address: str) -> bool:
    try:
        key = b58decode(address)
    except ValueError:
        return False
    return len(key) == 32 and not _on_curve(key)


def classify_recipient_account(address: str, acc: dict | None) -> dict[str, Any]:
    """What a shareholder address is, from its account (None when it does not exist)."""
    if acc is None:
        if _is_off_curve(address):
            # a program address no one has created yet (e.g. a GitHub user's fee PDA before
            # its first claim): no private key can sign for it, so it is not a wallet
            return {"kind": "unresolved", "uncreated": True}
        return {"kind": "wallet"}  # a system wallet that has never been funded
    if not isinstance(acc, dict):
        return {"kind": "unresolved"}
    owner = acc.get("owner")
    if owner == SYSTEM_PROGRAM:
        return {"kind": "wallet"}
    data = _account_bytes(acc)
    if owner == PUMP_FEES_PROGRAM:
        try:
            if data[:8] == SOCIAL_FEE_PDA_DISC:
                s = decode_social_fee_pda(data)
                return {
                    "kind": PLATFORMS.get(s["platform"], "social"),
                    "platform": PLATFORMS.get(s["platform"], f"platform:{s['platform']}"),
                    "user_id": _clean_user_id(s["user_id"]),
                    "lifetime_lamports": s["total_claimed"],
                }
            if data[:8] == DONATION_FEE_PDA_DISC:
                d = decode_donation_fee_pda(data)
                return {
                    "kind": "charity",
                    "charity_config_id": d["config_id"],
                    "lifetime_lamports": d["total_donated"],
                    "quote_mint": d["quote_mint"],
                }
        except (ValueError, struct.error) as e:
            log.info("fees.recipient_decode_failed", address=address, error=str(e))
    return {"kind": "program", "owner": owner}


def _destination(recipients: list[FeeRecipient]) -> tuple[Destination, dict[str, float], bool]:
    live = [r for r in recipients if r.share_bps > 0] or recipients
    shares: dict[str, float] = {}
    for r in live:
        shares[r.kind] = round(shares.get(r.kind, 0.0) + r.share_bps / 10_000, 4)
    split = len(live) > 1
    if not live:
        return "unknown", shares, False
    if not split:
        return _KIND_DESTINATION.get(live[0].kind, "other"), shares, False
    if any(r.kind == "unresolved" for r in live):
        # a share we could not classify (a failed read) can be the dominant one: no verdict
        return "unknown", shares, True
    top_kind, top_share = max(shares.items(), key=lambda kv: kv[1])
    if top_share >= 0.5 and top_kind in ("charity", "github", "x", "pump", "social"):
        return _KIND_DESTINATION[top_kind], shares, True
    return "split", shares, True


# Recipient kinds whose classification cannot change: their cached row is reused as is.
_STATIC_KINDS = {"wallet", "program"}
# Bytes of a recipient account the classifier reads (SocialFeePda and DonationFeePda are
# under 256 bytes); an admin can list any account, so never download all of it.
_RECIPIENT_BYTES = 256

# a sole recipient's kind -> destination; x / pump.fun-linked accounts are "social"
_KIND_DESTINATION = {
    "creator": "creator",
    "wallet": "wallet",
    "github": "github",
    "charity": "charity",
    "x": "social",
    "pump": "social",
    "social": "social",
    "program": "other",
    "unresolved": "unknown",
}


def _lamports_to_quote(lamports: int, quote_mint: str | None) -> float:
    # SOL-paired coins (the default) count in lamports; a USDC-paired coin's fee is in USDC
    # base units (6 decimals). Anything else is reported in the quote's base units / 1e9.
    if quote_mint and quote_mint not in (ZERO_KEY, "So11111111111111111111111111111111111111112"):
        return round(lamports / 1e6, 6) if quote_mint.startswith("EPjFWdd5") else lamports / 1e9
    return round(lamports / 1e9, 9)


# ----------------------------------------------------------------- GitHub login (optional)


async def github_login(
    http: httpx.AsyncClient, user_id: str, token: str = ""
) -> tuple[str | None, str | None]:
    """(login, account type) for a numeric GitHub user id via api.github.com, or (None, None).
    Unauthenticated calls are limited to 60/h per IP; GITHUB_TOKEN raises that to 5,000/h."""
    if not (user_id.isascii() and user_id.isdigit()):
        return None, None
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "tokensage"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = await http.get(f"https://api.github.com/user/{user_id}", headers=headers, timeout=4.0)
    except httpx.HTTPError as e:
        log.info("fees.github_lookup_failed", user_id=user_id, error=type(e).__name__)
        return None, None
    if r.status_code != 200:
        log.info("fees.github_lookup_status", user_id=user_id, status=r.status_code)
        return None, None
    try:
        j = r.json()
    except ValueError:
        return None, None
    login = j.get("login") if isinstance(j, dict) else None
    return (str(login)[:64] if login else None), (
        str(j.get("type"))[:32] if isinstance(j, dict) and j.get("type") else None
    )


# ----------------------------------------------------------------- resolve


async def _cached_recipients(
    conn: asyncpg.Connection | None, addrs: list[str], max_age: timedelta
) -> dict[str, dict[str, Any]]:
    if conn is None or not addrs:
        return {}
    try:
        rows = await conn.fetch(
            """select address, kind, platform, user_id, github_login, charity_config_id,
                      lifetime_lamports, quote_mint, resolved_at
               from fee_recipient where address = any($1::text[])""",
            addrs,
        )
    except asyncpg.PostgresError as e:  # table missing on an un-migrated dev DB: no cache
        log.info("fees.cache_read_failed", error=str(e)[:120])
        return {}
    now = datetime.now(UTC)
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["resolved_at"] and now - row["resolved_at"] <= max_age:
            out[row["address"]] = dict(row)
    return out


async def _store_recipient(conn: asyncpg.Connection | None, r: FeeRecipient, info: dict) -> None:
    if conn is None:
        return
    try:
        await conn.execute(
            """insert into fee_recipient (address, kind, platform, user_id, github_login,
                                          charity_config_id, lifetime_lamports, quote_mint,
                                          resolved_at)
               values ($1,$2,$3,$4,$5,$6,$7,$8, now())
               on conflict (address) do update set kind=excluded.kind,
                 platform=excluded.platform, user_id=excluded.user_id,
                 github_login=coalesce(excluded.github_login, fee_recipient.github_login),
                 charity_config_id=excluded.charity_config_id,
                 lifetime_lamports=excluded.lifetime_lamports, quote_mint=excluded.quote_mint,
                 resolved_at=now()""",
            r.address,
            "wallet" if r.kind == "creator" else r.kind,
            r.platform,
            r.user_id,
            r.github_login,
            r.charity_config_id,
            info.get("lifetime_lamports"),
            info.get("quote_mint"),
        )
    except asyncpg.PostgresError as e:
        log.info("fees.cache_write_failed", error=str(e)[:120])


async def resolve_creator_fee(
    mint: str,
    curve: dict[str, Any],
    sharing_acc: dict | None,
    *,
    rpc: SolanaRpc | None,
    conn: asyncpg.Connection | None = None,
    http: httpx.AsyncClient | None = None,
    github_token: str = "",
    lookup_github: bool = True,
    cache_max_age: timedelta = timedelta(days=7),
) -> CreatorFee:
    """Work out where this coin's creator fee goes. `curve` is the decoded BondingCurve and
    `sharing_acc` the sharing-config PDA's account from the same batched read (None when
    it does not exist). Never raises: an RPC failure leaves `destination: unknown` with a
    caveat."""
    creator = curve.get("creator")
    if creator and set(creator) == {"1"}:
        creator = None
    fee_bps = curve.get("creator_fee_bps")
    quote_mint = curve.get("quote_mint")

    if curve.get("is_holder_reward") or (creator and creator == holder_rewards_pda(mint)):
        cf = CreatorFee("holder_rewards", "holder_rewards", creator_fee_bps=fee_bps)
        if creator and creator != holder_rewards_pda(mint):
            cf.caveats.append("is_holder_reward is set but the curve creator is not the PDA")
        return cf
    if curve.get("is_cashback_coin"):
        return CreatorFee("cashback", "cashback", creator_fee_bps=fee_bps)

    sc_addr = sharing_config_pda(mint)
    sharing: dict[str, Any] | None = None
    if sharing_acc is not None and sharing_acc.get("owner") == PUMP_FEES_PROGRAM:
        try:
            sharing = decode_sharing_config(_account_bytes(sharing_acc))
        except (ValueError, struct.error, TypeError, KeyError) as e:
            log.info("fees.sharing_decode_failed", mint=mint, error=str(e))
            sharing = None

    if creator != sc_addr or sharing is None:
        if creator == sc_addr and sharing is None:
            cf = CreatorFee("unknown", "sharing_config", creator_fee_bps=fee_bps)
            cf.sharing_config = sc_addr
            cf.caveats.append("the fee-sharing config could not be read")
            return cf
        if creator is None:
            cf = CreatorFee("unknown", "direct", creator_fee_bps=fee_bps)
            cf.caveats.append("the bonding curve has no creator set")
            return cf
        cf = CreatorFee("creator", "direct", creator_fee_bps=fee_bps)
        cf.recipients = [FeeRecipient(creator, 10_000, "creator", is_creator=True)]
        cf.shares = {"creator": 1.0}
        if sharing is not None:
            cf.caveats.append("a fee-sharing config exists but the curve does not point at it")
        return cf

    cf = CreatorFee(
        "unknown",
        "sharing_config",
        creator_fee_bps=fee_bps,
        admin=sharing["admin"],
        sharing_config=sc_addr,
        sharing_version=sharing["version"],
        sharing_status=sharing["status"],
        mutable=not sharing["admin_revoked"],
    )
    holders = sharing["shareholders"]
    if not holders:
        cf.caveats.append("the fee-sharing config has no shareholders")
        return cf
    recipients = [
        FeeRecipient(
            h["address"],
            h["share_bps"],
            "creator" if h["address"] == sharing["admin"] else "unresolved",
            is_creator=h["address"] == sharing["admin"],
        )
        for h in holders
    ]
    # the admin signed the config into existence, so it is a wallet: no read needed
    todo = [r for r in recipients if r.kind == "unresolved"]
    infos: dict[str, dict[str, Any]] = {}
    if todo:
        cached = await _cached_recipients(conn, [r.address for r in todo], cache_max_age)
        # A wallet or another program's account stays what it is, so its cached kind is
        # reused. A fee PDA's running total (claimed / donated so far) changes, so it is
        # re-read every time; its cached row only saves the GitHub login lookup.
        fresh = [
            r
            for r in todo
            if r.address not in cached or cached[r.address]["kind"] not in _STATIC_KINDS
        ]
        infos.update({a: c for a, c in cached.items() if c["kind"] in _STATIC_KINDS})
        if fresh:
            if rpc is None:
                cf.caveats.append("fee recipients not resolved (no RPC)")
            else:
                try:
                    accs = await rpc.get_multiple_accounts(
                        [r.address for r in fresh],
                        encoding="base64",
                        data_slice=(0, _RECIPIENT_BYTES),
                    )
                    cf.rpc_calls += 1
                    for r, acc in zip(fresh, accs, strict=True):
                        try:
                            infos[r.address] = classify_recipient_account(r.address, acc)
                        except (ValueError, struct.error, TypeError, KeyError) as e:
                            log.info(
                                "fees.recipient_decode_failed", address=r.address, error=str(e)
                            )
                            infos[r.address] = {"kind": "unresolved"}
                except RpcError as e:
                    log.warning("fees.recipients_read_failed", mint=mint, error=str(e))
                    cf.caveats.append("fee recipients could not be read from the chain")
        for r in fresh:
            # the login a GitHub PDA resolved to is stable: keep the cached one
            c = cached.get(r.address)
            if c and r.address in infos and infos[r.address]["kind"] == c["kind"]:
                if c.get("github_login") and not infos[r.address].get("github_login"):
                    infos[r.address]["github_login"] = c["github_login"]
        for r in todo:
            info = infos.get(r.address)
            if info is None:
                continue
            r.kind = info["kind"]
            r.platform = info.get("platform")
            r.user_id = info.get("user_id")
            r.charity_config_id = info.get("charity_config_id")
            r.github_login = info.get("github_login")
            lam = info.get("lifetime_lamports")
            if lam is not None:
                # a social PDA accrues SOL whatever the coin's pair; a donation PDA accrues
                # the coin's quote token
                r.lifetime_received = (
                    round(int(lam) / 1e9, 9)
                    if r.kind != "charity"
                    else _lamports_to_quote(int(lam), info.get("quote_mint") or quote_mint)
                )
            if r.kind == "github":
                # looked up once, when the recipient is first seen; a failed lookup is retried
                # when the cached row expires
                if (
                    r.github_login is None
                    and r.address not in cached
                    and lookup_github
                    and http is not None
                    and r.user_id
                ):
                    r.github_login, _ = await github_login(http, r.user_id, github_token)
                r.url = (
                    f"https://github.com/{r.github_login}"
                    if r.github_login
                    else (f"https://api.github.com/user/{r.user_id}" if r.user_id else None)
                )
            if info.get("uncreated") or r.kind == "unresolved":
                continue  # nothing to cache: read it again next time
            if (
                r.address not in cached
                or r.kind not in _STATIC_KINDS
                or (r.github_login and not cached[r.address].get("github_login"))
            ):
                await _store_recipient(conn, r, info)
    cf.recipients = recipients
    cf.destination, cf.shares, cf.split = _destination(recipients)
    if any(r.kind == "unresolved" for r in recipients):
        cf.caveats.append("some fee recipients could not be classified")
    if any(infos.get(r.address, {}).get("uncreated") for r in todo):
        cf.caveats.append("a fee recipient is a program address that does not exist yet")
    if cf.sharing_status != "active":
        cf.caveats.append("the fee-sharing config is paused")
    return cf


def creator_fee_to_dict(cf: CreatorFee | None) -> dict[str, Any] | None:
    """The API shape (Market.creator_fee)."""
    if cf is None:
        return None
    return {
        "destination": cf.destination,
        "mechanism": cf.mechanism,
        "creator_fee_bps": cf.creator_fee_bps,
        "admin": cf.admin,
        "sharing_config": cf.sharing_config,
        "sharing_version": cf.sharing_version,
        "mutable": cf.mutable,
        "split": cf.split,
        "shares": cf.shares,
        "recipients": [
            {
                "address": r.address,
                "share": r.share,
                "share_bps": r.share_bps,
                "kind": r.kind,
                "is_creator": r.is_creator,
                "platform": r.platform,
                "user_id": r.user_id,
                "github_login": r.github_login,
                "url": r.url,
                "charity_config_id": r.charity_config_id,
                "lifetime_received": r.lifetime_received,
            }
            for r in cf.recipients
        ],
        "summary": cf.describe(),
    }
