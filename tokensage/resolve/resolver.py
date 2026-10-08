"""Turn a CA into on-chain facts (guide §4.1) using standard RPC calls only.

resolve(conn, rpc, http, settings, ca) -> Resolved
  1. mint account -> token program (SPL / Token-2022) or not_a_token_mint
  2. bonding-curve PDA -> is it pump.fun; curve state
  3. name / symbol / uri (Token-2022 extension, Metaplex PDA, or DAS)
  4. creation time + creator (db cache, frontend-api, RPC history)
  5. persist token + token_market
"""

from __future__ import annotations

import asyncio
import base64
import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
import structlog

from tokensage.config import Settings
from tokensage.resolve import metaplex
from tokensage.resolve.fees import CreatorFee, resolve_creator_fee, sharing_config_pda
from tokensage.resolve.pump_ca import (
    PUMP_PROGRAM,
    bonding_curve_pda,
    decode_bonding_curve,
    find_program_address,
    parse_ca,
)
from tokensage.resolve.pump_event import decode_create_event
from tokensage.resolve.rpc import RpcError, SolanaRpc

log = structlog.get_logger("resolve")

SPL_TOKEN = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
TOKEN_PROGRAMS = {SPL_TOKEN: "spl-token", TOKEN_2022: "token-2022"}
SOL_MINT = "So11111111111111111111111111111111111111112"
GLOBAL_DISC = bytes([167, 232, 232, 177, 200, 108, 114, 127])
# pump.fun's long-standing initial_real_token_reserves; used only if the Global read fails.
FALLBACK_INITIAL_REAL_TOKEN_RESERVES = 793_100_000_000_000
HISTORY_MAX_PAGES = 5


class ResolveError(Exception):
    """A definitive answer about the CA that maps to an API error."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code  # token_not_found | not_a_token_mint | not_pumpfun


@dataclass
class Resolved:
    mint: str
    token_program: str  # spl-token | token-2022
    is_pumpfun: bool
    name: str | None
    symbol: str | None
    uri: str | None
    creator: str | None
    bonding_curve: str | None
    complete: bool | None
    curve_progress: float | None
    is_mayhem: bool | None
    quote_mint: str | None
    created_at: datetime | None
    created_at_source: str | None
    onchain_metadata_source: str | None  # token2022 | metaplex | das | none
    # rules 0.19.0: `creator` is the creator *wallet*. The bonding curve's creator field
    # can be a PDA (a fee-sharing config, the holder-rewards PDA); it is kept here as is.
    creator_onchain: str | None = None
    creator_kind: str | None = None  # wallet | sharing_config | holder_rewards_pda | unknown
    creator_fee: CreatorFee | None = None


# ----------------------------------------------------------------- helpers


def _global_pda() -> str:
    return find_program_address([b"global"], PUMP_PROGRAM)[0]


_initial_real_cache: int | None = None


async def initial_real_token_reserves(rpc: SolanaRpc) -> int:
    global _initial_real_cache
    if _initial_real_cache:
        return _initial_real_cache
    try:
        acc = await rpc.get_account_info(_global_pda(), encoding="base64")
        if acc:
            data = base64.b64decode(acc["data"][0])
            if data[:8] == GLOBAL_DISC and len(data) >= 8 + 1 + 64 + 24:
                off = 8 + 1 + 32 + 32 + 8 + 8
                (val,) = struct.unpack_from("<Q", data, off)
                if val > 0:
                    _initial_real_cache = val
                    return val
    except RpcError as e:
        log.warning("resolve.global_read_failed", error=str(e))
    return FALLBACK_INITIAL_REAL_TOKEN_RESERVES


def _token2022_metadata(parsed_info: dict) -> dict[str, str] | None:
    for ext in parsed_info.get("extensions") or []:
        if ext.get("extension") == "tokenMetadata":
            st = ext.get("state") or {}
            return {
                "name": str(st.get("name") or "").strip(),
                "symbol": str(st.get("symbol") or "").strip(),
                "uri": str(st.get("uri") or "").strip(),
            }
    return None


def _quote_label(quote_mint: str | None) -> str | None:
    if quote_mint is None:
        return None
    if quote_mint == "1" * 32 or quote_mint == SOL_MINT or set(quote_mint) == {"1"}:
        return "SOL"
    return quote_mint


def _raw_bytes(acc: dict) -> bytes:
    """Account data as bytes. Accounts with no jsonParsed parser (the bonding curve, Metaplex
    metadata) come back as ["<base64>", "base64"] whichever encoding was asked."""
    data = acc.get("data")
    if not isinstance(data, list) or not data:
        raise ValueError("account data is not base64-encoded")
    return base64.b64decode(data[0])


async def _onchain_metadata(
    rpc: SolanaRpc,
    mint: str,
    token_program: str,
    info: dict,
    metaplex_acc: dict | None = None,
    metaplex_read: bool = False,
) -> tuple[dict[str, str] | None, str]:
    """name / symbol / uri from the Token-2022 extension, the Metaplex PDA, or DAS.
    metaplex_read: the caller already read the Metaplex PDA into metaplex_acc (None when the
    account does not exist); otherwise it is read here if needed."""
    meta: dict[str, str] | None = None
    meta_source = "none"
    if token_program == "token-2022":
        meta = _token2022_metadata(info)
        meta_source = "token2022" if meta else "none"
    if meta is None:
        # a Token-2022 mint without the metadata extension can still carry a Metaplex PDA
        # (non-pump T22 tokens, pair tokens); it came back in the same batched read
        md_acc = (
            await rpc.get_account_info(metaplex.metadata_pda(mint), encoding="base64")
            if not metaplex_read
            else metaplex_acc
        )
        if md_acc:
            try:
                meta = metaplex.decode_metadata(_raw_bytes(md_acc))
                meta_source = "metaplex"
            except (ValueError, struct.error):
                meta = None
    if meta is None or not meta.get("uri"):
        asset = await rpc.get_asset(mint)
        content = (asset or {}).get("content") or {}
        if content.get("json_uri") or content.get("metadata"):
            md = content.get("metadata") or {}
            meta = {
                "name": str(md.get("name") or (meta or {}).get("name") or "").strip(),
                "symbol": str(md.get("symbol") or (meta or {}).get("symbol") or "").strip(),
                "uri": str(content.get("json_uri") or (meta or {}).get("uri") or "").strip(),
            }
            meta_source = "das"
    return meta, meta_source


async def read_mint_metadata(rpc: SolanaRpc, mint: str) -> dict[str, str] | None:
    """Name / symbol / uri of any SPL or Token-2022 mint (e.g. a pair token), or None when
    the address is not a mint or carries no metadata."""
    # one call for the mint and its Metaplex PDA (1 credit instead of 2 for SPL mints)
    acc, md_acc = await rpc.get_multiple_accounts(
        [mint, metaplex.metadata_pda(mint)], encoding="jsonParsed"
    )
    if acc is None:
        return None
    token_program = TOKEN_PROGRAMS.get(acc.get("owner") or "")
    parsed = acc.get("data") if isinstance(acc.get("data"), dict) else None
    if token_program is None or not parsed or parsed.get("parsed", {}).get("type") != "mint":
        return None
    meta, _ = await _onchain_metadata(
        rpc, mint, token_program, parsed["parsed"]["info"], metaplex_acc=md_acc, metaplex_read=True
    )
    return meta


# ----------------------------------------------------------------- creation time


async def _created_from_frontend_api(
    http: httpx.AsyncClient, mint: str
) -> tuple[datetime | None, str | None]:
    try:
        r = await http.get(
            f"https://frontend-api-v3.pump.fun/coins-v2/{mint}",
            headers={"Origin": "https://pump.fun"},
            timeout=6.0,
        )
    except httpx.HTTPError:
        return None, None
    if r.status_code != 200:
        return None, None
    try:
        j = r.json()
    except ValueError:
        return None, None
    if not isinstance(j, dict):
        return None, None
    ts = j.get("created_timestamp")
    created = (
        datetime.fromtimestamp(ts / 1000, tz=UTC)
        if isinstance(ts, int | float) and ts > 0
        else None
    )
    creator = j.get("creator") if isinstance(j.get("creator"), str) else None
    return created, creator


async def _created_from_history(
    rpc: SolanaRpc, bonding_curve: str, mint: str
) -> tuple[datetime | None, str | None, dict | None]:
    """Walk the bonding curve's signatures back to the oldest and decode its CreateEvent."""
    before: str | None = None
    oldest: dict | None = None
    for _ in range(HISTORY_MAX_PAGES):
        page = await rpc.get_signatures(bonding_curve, before=before)
        if not page:
            break
        oldest = page[-1]
        before = oldest["signature"]
        if len(page) < 1000:
            break
    else:
        return None, None, None  # too much history; give up (caller reports null)
    if not oldest:
        return None, None, None
    tx = await rpc.get_transaction(oldest["signature"])
    event = None
    meta = (tx or {}).get("meta") or {}  # "meta": null happens on some providers
    for line in meta.get("logMessages") or []:
        if line.startswith("Program data: "):
            try:
                ev = decode_create_event(line[len("Program data: ") :])
            except Exception:  # noqa: BLE001
                ev = None
            if ev and ev.get("mint") == mint:
                event = ev
                break
    bt = (tx or {}).get("blockTime") or oldest.get("blockTime")
    if event:
        # early CreateEvent layouts have no timestamp field: fall back to the block time
        ts = event.get("timestamp") or bt
        return (datetime.fromtimestamp(ts, tz=UTC) if ts else None), event.get("creator"), event
    return (datetime.fromtimestamp(bt, tz=UTC) if bt else None), None, None


# ----------------------------------------------------------------- main


async def resolve(
    conn: asyncpg.Connection,
    rpc: SolanaRpc,
    http: httpx.AsyncClient,
    settings: Settings,
    raw_ca: str,
    created_hint: datetime | None = None,
) -> Resolved:
    """created_hint: a creation time the API caller supplied. Used instead of the frontend
    API and the (RPC-expensive) signature-history lookup when nothing better is stored."""
    mint = parse_ca(raw_ca)

    # 1. mint, bonding curve, Metaplex PDA and the pump-fees sharing config in one call
    # (1 RPC credit instead of 2-4). Accounts with no jsonParsed parser come back base64.
    curve_addr = bonding_curve_pda(mint)
    acc, curve_acc, md_acc, sharing_acc = await rpc.get_multiple_accounts(
        [mint, curve_addr, metaplex.metadata_pda(mint), sharing_config_pda(mint)],
        encoding="jsonParsed",
    )
    if acc is None:
        raise ResolveError("token_not_found", "no account found on-chain for this address")
    owner = acc.get("owner")
    token_program = TOKEN_PROGRAMS.get(owner or "")
    parsed = acc.get("data") if isinstance(acc.get("data"), dict) else None
    ptype = (parsed or {}).get("parsed", {}).get("type") if parsed else None
    if token_program is None or ptype != "mint":
        raise ResolveError(
            "not_a_token_mint",
            f"account is not an SPL/Token-2022 mint (owner={owner}, type={ptype})",
        )
    info = parsed["parsed"]["info"]  # type: ignore[index]

    # 2. bonding curve
    curve: dict | None = None
    if curve_acc and curve_acc.get("owner") == PUMP_PROGRAM:
        try:
            curve = decode_bonding_curve(_raw_bytes(curve_acc))
        except ValueError:
            curve = None
    is_pumpfun = curve is not None
    if not is_pumpfun and not settings.accept_non_pump:
        raise ResolveError("not_pumpfun", "mint has no pump.fun bonding curve")

    complete = curve_progress = creator = quote_mint = is_mayhem = None
    creator_onchain = creator_kind = None
    creator_fee: CreatorFee | None = None
    if curve:
        complete, curve_progress = await _curve_state(rpc, curve)
        creator = curve.get("creator")
        if creator and set(creator) == {"1"}:
            creator = None
        creator_onchain = creator
        is_mayhem = curve.get("is_mayhem_mode")
        quote_mint = _quote_label(curve.get("quote_mint")) or "SOL"
        # where the creator fee goes; the curve's creator may be a PDA rather than a wallet
        creator_fee = await resolve_creator_fee(
            mint,
            curve,
            sharing_acc if sharing_acc and sharing_acc.get("owner") else None,
            rpc=rpc,
            conn=conn,
            cache_max_age=timedelta(days=settings.fee_recipient_cache_days),
        )
        if creator_fee.mechanism == "sharing_config":
            creator_kind = "sharing_config"
            creator = creator_fee.admin  # the wallet that opted the coin into fee sharing
        elif creator_fee.mechanism == "holder_rewards":
            creator_kind = "holder_rewards_pda"
            creator = None  # a pump.fun-controlled PDA; the launch wallet is found below
        else:
            creator_kind = "wallet" if creator else "unknown"

    # 3. on-chain name / symbol / uri
    meta, meta_source = await _onchain_metadata(
        rpc, mint, token_program, info, metaplex_acc=md_acc, metaplex_read=True
    )

    # 4. creation time + creator
    created: datetime | None = None
    created_src: str | None = None
    row = await conn.fetchrow(
        "select created_at, created_at_source, creator from token where mint=$1", mint
    )
    if row and row["creator"] and row["creator"] != creator_onchain:
        # a row stored by older rules may hold the curve's PDA as creator: never reuse that
        creator = creator or row["creator"]
    if row and row["created_at"] and row["created_at_source"] != "hints":
        created, created_src = row["created_at"], row["created_at_source"]
    if created is None and is_pumpfun:
        c, cr = await _created_from_frontend_api(http, mint)
        if c:
            created, created_src = c, "frontend_api"
            creator = creator or cr
    # A caller's hint (now or stored earlier) only stands in for the expensive signature
    # history, and is replaced as soon as a real source answers (see persist()).
    if created is None and created_hint is not None:
        created, created_src = created_hint, "hints"
    if created is None and row and row["created_at"]:
        created, created_src = row["created_at"], row["created_at_source"]
    if created is None and is_pumpfun:
        try:
            c, cr, _ev = await _created_from_history(rpc, curve_addr, mint)
        except RpcError as e:
            log.warning("resolve.history_failed", mint=mint, error=str(e))
            c, cr = None, None
        if c:
            created, created_src = c, "rpc_history"
            creator = creator or cr

    res = Resolved(
        mint=mint,
        token_program=token_program,
        is_pumpfun=is_pumpfun,
        name=(meta or {}).get("name") or None,
        symbol=(meta or {}).get("symbol") or None,
        uri=(meta or {}).get("uri") or None,
        creator=creator,
        bonding_curve=curve_addr if is_pumpfun else None,
        complete=complete,
        curve_progress=curve_progress,
        is_mayhem=is_mayhem,
        quote_mint=quote_mint,
        created_at=created,
        created_at_source=created_src,
        onchain_metadata_source=meta_source,
        creator_onchain=creator_onchain,
        creator_kind=creator_kind,
        creator_fee=creator_fee,
    )
    await persist(conn, res)
    return res


async def _curve_state(rpc: SolanaRpc, curve: dict) -> tuple[bool, float | None]:
    complete = bool(curve.get("complete"))
    progress: float | None = None
    real = curve.get("real_token_reserves")
    if real is not None:
        init = await initial_real_token_reserves(rpc)
        progress = max(0.0, min(1.0, 1 - real / init)) if init else None
        if complete:
            progress = 1.0
    return complete, progress


async def curves_now(
    conn: asyncpg.Connection,
    rpc: SolanaRpc | None,
    mints: list[str],
    max_age_s: int = 300,
    rpc_timeout_s: float | None = None,
) -> dict[str, tuple[bool | None, float | None, datetime | None]]:
    """Other coins' bonding curves, mint -> (complete, curve_progress, as_of): the stored
    state when fresher than max_age_s, else read on-chain (and stored back), all the stale
    ones in one getMultipleAccounts (1 RPC credit for up to 100 instead of one each). A
    failed read falls back to the stored state; one that times out (rpc_timeout_s) leaves
    the coins it was for out. Coins with no answer are absent."""
    mints = list(dict.fromkeys(mints))
    if not mints:
        return {}
    rows = {
        r["mint"]: r
        for r in await conn.fetch(
            "select mint, complete, curve_progress, updated_at from token_market"
            " where mint = any($1::text[])",
            mints,
        )
    }
    now = datetime.now(UTC)
    out: dict[str, tuple[bool | None, float | None, datetime | None]] = {}
    stale: list[str] = []
    for mint in mints:
        row = rows.get(mint)
        if row and row["updated_at"] and (now - row["updated_at"]).total_seconds() <= max_age_s:
            out[mint] = (row["complete"], row["curve_progress"], row["updated_at"])
        else:
            stale.append(mint)
    if not stale:
        return out
    fresh: dict[str, tuple[bool, float | None]] = {}

    async def read(rpc: SolanaRpc) -> None:
        accs: list = []
        try:
            accs = await rpc.get_multiple_accounts([bonding_curve_pda(m) for m in stale])
        except (RpcError, ValueError) as e:
            log.info("curve_now.failed", mints=stale, error=str(e)[:120])
        for mint, acc in zip(stale, accs, strict=False):
            try:
                if acc and acc.get("owner") == PUMP_PROGRAM:
                    curve = decode_bonding_curve(base64.b64decode(acc["data"][0]))
                    # may read pump.fun's Global account: inside the same time bound
                    fresh[mint] = await _curve_state(rpc, curve)
            except (RpcError, ValueError, KeyError, IndexError, TypeError) as e:
                log.info("curve_now.failed", mint=mint, error=str(e)[:120])

    if rpc is not None:
        try:
            await (asyncio.wait_for(read(rpc), rpc_timeout_s) if rpc_timeout_s else read(rpc))
        except TimeoutError:
            log.info("curve_now.timeout", mints=stale)
            return out
    if fresh:
        # store back only coins TokenSage knows (token_market rows reference token)
        unknown = [m for m in fresh if m not in rows]
        known = set(rows)
        if unknown:
            known |= {
                r["mint"]
                for r in await conn.fetch(
                    "select mint from token where mint = any($1::text[])", unknown
                )
            }
        keep = [m for m in fresh if m in known]
        if keep:
            await conn.execute(
                """insert into token_market (mint, complete, curve_progress, updated_at)
                   select m, c, p, $4 from unnest($1::text[], $2::bool[], $3::float8[])
                     as u(m, c, p)
                   on conflict (mint) do update set complete=excluded.complete,
                     curve_progress=excluded.curve_progress,
                     updated_at=excluded.updated_at""",
                keep,
                [fresh[m][0] for m in keep],
                [fresh[m][1] for m in keep],
                now,
            )
    for mint in stale:
        if mint in fresh:
            out[mint] = (fresh[mint][0], fresh[mint][1], now)
        elif (row := rows.get(mint)) is not None:
            out[mint] = (row["complete"], row["curve_progress"], row["updated_at"])
    return out


async def persist(conn: asyncpg.Connection, r: Resolved) -> None:
    await conn.execute(
        """
        insert into token (mint, name, symbol, uri, creator, bonding_curve, token_program,
                           quote_mint, is_pumpfun, is_mayhem, created_at,
                           created_at_source, seen_by)
        values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,'{request}')
        on conflict (mint) do update set
          name = coalesce(token.name, excluded.name),
          symbol = coalesce(token.symbol, excluded.symbol),
          uri = coalesce(token.uri, excluded.uri),
          creator = coalesce(excluded.creator,
                             case when token.creator = $13 then null else token.creator end),
          bonding_curve = coalesce(token.bonding_curve, excluded.bonding_curve),
          token_program = excluded.token_program,
          quote_mint = coalesce(excluded.quote_mint, token.quote_mint),
          is_pumpfun = excluded.is_pumpfun,
          is_mayhem = coalesce(excluded.is_mayhem, token.is_mayhem),
          created_at = case
            when token.created_at is null
              or (token.created_at_source = 'hints'
                  and coalesce(excluded.created_at_source, 'hints') <> 'hints')
            then excluded.created_at else token.created_at end,
          created_at_source = case
            when token.created_at is null
              or (token.created_at_source = 'hints'
                  and coalesce(excluded.created_at_source, 'hints') <> 'hints')
            then excluded.created_at_source else token.created_at_source end,
          seen_by = case when 'request' = any(token.seen_by) then token.seen_by
                         else token.seen_by || '{request}' end
        """,
        r.mint,
        r.name,
        r.symbol,
        r.uri,
        r.creator,
        r.bonding_curve,
        r.token_program,
        r.quote_mint,
        r.is_pumpfun,
        r.is_mayhem,
        r.created_at,
        r.created_at_source,
        r.creator_onchain,
    )
    await conn.execute(
        """
        insert into token_market (mint, complete, curve_progress, updated_at)
        values ($1,$2,$3, now())
        on conflict (mint) do update set complete=excluded.complete,
          curve_progress=excluded.curve_progress, updated_at=now()
        """,
        r.mint,
        r.complete,
        r.curve_progress,
    )
