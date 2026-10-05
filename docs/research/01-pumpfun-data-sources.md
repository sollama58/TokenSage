# How to get pump.fun token data (research for TokenSage)

Compiled 2026-10-05.

## 0. How this was researched, and what could not be tested

- **Live endpoint tests were blocked from this sandbox.** The egress proxy returned `403 CONNECT tunnel failed` for every crypto/data host tried: `frontend-api.pump.fun`, `frontend-api-v2/-v3.pump.fun`, `pumpportal.fun`, `api.dexscreener.com`, `api.geckoterminal.com`, `ipfs.io`, `gateway.pinata.cloud`, `dweb.link`, `api.mainnet-beta.solana.com`, `rpc.ankr.com`, Alchemy, and the docs sites for Helius, Moralis, Bitquery, Birdeye, Shyft, Solana Tracker and DexScreener. WebFetch was blocked for the same hosts. **This is a policy block in this sandbox, not Cloudflare.** None of the curl results below come from pump.fun, so nothing here proves how pump.fun or Cloudflare treats Render IPs.
- Because of that, the most reliable evidence comes from **GitHub, which was reachable**:
  - the official `pump-fun/pump-public-docs` repo, including its IDLs (last commit 2026-09-29);
  - the official `pump-fun/pump-fun-skills` repo (2026-04-23);
  - the official npm package `@pump-fun/pump-sdk` 2.0.0 (published 2026-09-13);
  - recent third-party code and captured real payloads.
- Web search results were used for pricing and statistics. Items marked **[UNVERIFIED]** come only from search-engine summaries or secondary sites. Confirm them on the vendor's own page before you rely on them.

---

## 1. pump.fun's unofficial frontend APIs

### 1.1 Hosts and status

| Host | Status (as of 2026) | Source |
|---|---|---|
| `https://frontend-api-v3.pump.fun` | **Current.** Used by pump.fun's own skills repo and by tools still maintained in Sept 2026 | [pump-fun-skills coin-fees/SKILL.md](https://github.com/pump-fun/pump-fun-skills/blob/main/coin-fees/SKILL.md), [CookrAI/pumpfun-collector](https://github.com/CookrAI/pumpfun-collector) (commit 2026-09-28) |
| `https://frontend-api-v2.pump.fun` | Marked deprecated | [BankkRoll/pumpfun-apis README](https://github.com/BankkRoll/pumpfun-apis) |
| `https://frontend-api.pump.fun` (v1) | Marked deprecated | same |
| `https://advanced-api-v2.pump.fun` | Current. Used for feeds and "advanced" lists (`/coins`, `/coins-v3/{mint}`, `/coins/mayhem-mode` …) | [BankkRoll captures 2026-06-17](https://github.com/BankkRoll/pumpfun-apis/tree/main/captures/2026-06-17) |
| Others | `profile-api`, `swap-api`, `livestream-api`, `market-api`, `volatility-api-v2`, `clips-api` (`.pump.fun`), and `pump-fe.helius-rpc.com` (pump.fun's RPC frontend) | same |

### 1.2 Useful endpoints on frontend-api-v3

These come from the reverse-engineered OpenAPI spec `frontend-api-v3.json` in [BankkRoll/pumpfun-apis](https://github.com/BankkRoll/pumpfun-apis) (updated 2026-06-17) and from the official skills repo.

| Endpoint | Notes |
|---|---|
| `GET /coins-v2/{mint}` | **The only endpoint pump.fun documents itself** ([pump-fun-skills](https://github.com/pump-fun/pump-fun-skills/blob/main/coin-fees/SKILL.md)). Use it for single-coin lookup. |
| `GET /coins/{mint}?sync=true` | Older single-coin lookup that still appears in the spec |
| `GET /coins/latest` | The single newest coin |
| `GET /coins?offset=&limit=&sort=&order=&includeNsfw=&complete=&creator=&searchTerm=&meta=` | List and sort. Sorts used in practice: `created_timestamp`, `market_cap`, `last_trade_timestamp`, `reply_count`, `last_reply`. **Pagination stops at about 1,000 rows per query; there is no cursor** ([pumpfun-collector api.py](https://github.com/CookrAI/pumpfun-collector)). |
| `GET /coins/search?searchTerm=&type=&…` | Search. A `search-unrestricted` variant was also captured. |
| `GET /coins/king-of-the-hill?includeNsfw=` | Current "king of the hill" coin |
| `GET /coins/currently-live`, `/coins/featured/{timeWindow}`, `/coins/for-you`, `/coins/similar`, `/coins/user-created-coins/{userId}` | Feeds |
| `GET /replies/{mint}?limit=&offset=&reverseOrder=` | Comments/replies on a coin |
| `GET /trades/all/{mint}`, `/trades/latest`, `/trades/count/{mint}` | Trades |
| `GET /metas/search` | Trending "metas" (narratives) |
| `POST /ipfs/token-metadata` | Upload used by the pump.fun UI. External uploads to the old `pump.fun/api/ipfs` are **no longer supported** ([search summary of pumpportal.fun/creation](https://pumpportal.fun/creation/)) [UNVERIFIED] |

### 1.3 Real response object

This is a real coin object captured from `frontend-api-v3` (mint `3arUrp…pump`, created 2025-09-02). It is stored in the repo [callmedraxx/pump-stream-sniper `latest_tokens_sample.json`](https://github.com/callmedraxx/pump-stream-sniper):

```json
{
 "mint": "3arUrpH3nzaRJbbpVgY42dcqSq9A5BFgUxKozZ4npump",
 "name": "StreamerCoin", "symbol": "STREAMER", "description": "",
 "image_uri": "https://ipfs.io/ipfs/bafkreignns4pa47e6yy3jiw7ua34gl3tagb4k2rmuxgly32zeku4abiukm",
 "metadata_uri": "https://ipfs.io/ipfs/bafkreig5wtk2ui6yti4zaczp2u4x27rkbnyzf7n7ontszeedlicqcc2mxe",
 "twitter": "https://x.com/biznez_/status/1962983562833264871",
 "telegram": null, "website": "https://streamercoin.live/", "show_name": true,
 "bonding_curve": "45YS7EqqWbhug1w5p2iAyVJb4JrqtS3T6mRpjb6Nz3fS",
 "associated_bonding_curve": "HP6yEA4ZYV8MoRpqgCEgPqqpByAXtcmw4d8yDVjfcQSr",
 "creator": "B8wtc55J62sZ9reiyWLCkJ46b9YnQeMqSbmeyZUg95vR",
 "created_timestamp": 1756846785184,
 "raydium_pool": null, "complete": true,
 "virtual_sol_reserves": 115005359213, "virtual_token_reserves": 279900000000000,
 "real_sol_reserves": 85005359213, "real_token_reserves": 0,
 "total_supply": 1000000000000000, "hidden": null,
 "last_trade_timestamp": 1756846875000, "king_of_the_hill_timestamp": 1756846818000,
 "market_cap": 51426.46285769623, "usd_market_cap": 11578153.847781727,
 "ath_market_cap": 12304905.87, "ath_market_cap_timestamp": 1756999787713,
 "nsfw": false, "is_banned": false, "market_id": null, "inverted": true,
 "last_reply": 1757565781000, "reply_count": 830,
 "is_currently_live": true, "initialized": true, "video_uri": null, "updated_at": null,
 "pump_swap_pool": "B1EzTqQTkAWMBZBkEyKGb5dxebYfB74nJcmSAR14cXJt",
 "banner_uri": null, "hide_banner": false, "program": null,
 "thumbnail": "https://prod-livestream-thumbnails-…s3.us-east-1.amazonaws.com/499286/1757535968101.jpeg",
 "num_participants": 18, "downrank_score": 0, "livestream_ban_expiry": 0
}
```

How to read these fields:

- `created_timestamp` is in **milliseconds**.
- `market_cap` is in **SOL**. `usd_market_cap` is in USD.
- Reserves are in lamports or raw token units. Tokens have 6 decimals and a total supply of 1e9.
- `twitter` is often a **tweet URL, not a profile**. In practice `telegram` and `website` are frequently `null`.

The newer schema, captured 2026-06-17 from `POST /coins-v2/mints` ([BankkRoll captured-api.json](https://github.com/BankkRoll/pumpfun-apis/tree/main/captures/2026-06-17)), adds these fields:

- `ath_market_cap`, `banner_uri`, `base_decimals`, `chain_id`, `cto_address`, `hidden`, `indexed_by_pump`, `is_cashback_enabled`, `is_charity`
- `mayhem`, `mayhem_state`, `market_cap_quote`, `multichain_family`, `platform`, `pool_address`
- `program` (values `pump` or `non_launchpad`), `protocol`, `quote_decimals`, `quote_mint`, `real_quote_reserves`
- `token_program`, `tokenized_agent`, `total_supply_str`

pump.fun's own docs also add these warnings ([coin-fees/SKILL.md](https://github.com/pump-fun/pump-fun-skills/blob/main/coin-fees/SKILL.md)):

- **`frontend-api-v3` is CORS-protected.** Call it from your backend, not from the browser.
- **"NEVER trust `token_program` from the HTTP API"**, because it can be stale. Read the owner of the mint account on-chain instead.

### 1.4 Auth, rate limits, Cloudflare, stability

- **Auth:** read endpoints work **without auth**. The pumpfun-collector, maintained in Sept 2026, sends only a User-Agent and hits `/coins` at about 4 requests per second, backing off on `429`. BankkRoll recommends `Origin: https://pump.fun` and an optional `Authorization: Bearer <JWT>` for "complete data". Personalized and bookmark endpoints need the JWT.
- **Rate limits:** there are no published numbers. Expect `429`. The spec mentions `x-ratelimit-*` headers and ETag/`If-None-Match` caching. **[UNVERIFIED]** The hosts sit behind Cloudflare. Datacenter IPs, which includes Render's, can get 403 challenge pages. This could not be tested from here.
- **Stability:** these APIs are undocumented and versioned without notice. v1 and v2 were already deprecated, field names have changed (for example `real_sol_reserves` became `real_quote_reserves`), and new endpoints appear often. **Do not make them your only source.** Use them for enrichment such as `nsfw`, `is_banned`, `reply_count`, `usd_market_cap`, and the `twitter`/`telegram`/`website` links that are already parsed out.

---

## 2. Real-time new-token feeds

### 2.1 PumpPortal (third party, not pump.fun) — `wss://pumpportal.fun/api/data`

- Subscribe with `{"method":"subscribeNewToken"}`. Other methods are `subscribeMigration`, `subscribeTokenTrade` (`keys:[mints]`), `subscribeAccountTrade` (`keys:[wallets]`), and the matching `unsubscribe*`.
- **Pricing [UNVERIFIED; the docs page was blocked]:**
  - `subscribeNewToken` and `subscribeMigration` are reportedly **free with no key**.
  - Trade streams cost **0.01 SOL per 10,000 messages**, need an API key, and require a minimum balance of 0.02 SOL.
  - Use **one websocket for all subscriptions**. Opening too many connections gets a temporary ban of about one hour.
  - Sources: [pumpportal.fun/data-api/real-time](https://pumpportal.fun/data-api/real-time/) via search summary, and [pumpportal.fun/fees](https://pumpportal.fun/fees/).
- **Real create message, Oct 2026:** a capture dated 2026-10-03 ([macdarenz-droid/Meme-snipe pp_events.json](https://github.com/macdarenz-droid/Meme-snipe)) lists these keys:
  `signature, mint, traderPublicKey, txType("create"), initialBuy, solAmount, bondingCurveKey, vTokensInBondingCurve, vSolInBondingCurve, marketCapSol, name, symbol, uri, is_mayhem_mode, pool("pump")`
  - The same feed also carries `pool:"bonk"` (letsbonk) creates, which have a different key set, and `txType:"migrate"` with `pool:"pump-amm"`. **Filter on `pool=="pump"`.**
- Example payload, from [mileshua/berkeley-tech-week-hackathon data.json](https://github.com/mileshua/berkeley-tech-week-hackathon):
  ```json
  {"signature":"3L7j…","mint":"5MRDTuFBNdx3jfjWFco9PPLaz82na59baFGXrbnCpump","traderPublicKey":"GVY3…",
   "txType":"create","initialBuy":12222945.437093,"solAmount":0.345679011,
   "bondingCurveKey":"3LY4…","vTokensInBondingCurve":1060777054.56,"vSolInBondingCurve":30.3457,
   "marketCapSol":28.607,"name":"Fireball 1000°C","symbol":"FB",
   "uri":"https://ipfs.io/ipfs/bafkreifurk23dj6shpnv2gk6misri5ldgtu4lcbxzcmh27bngf5iw7xyzm","pool":"pump"}
  ```
  The message does **not** include the description, image or socials. You still have to fetch `uri`.
- **Caveat:** Chainstack retired its PumpPortal listener on 2026-09-26 because "it sampled rather than covered the chain" ([chainstacklabs/pumpfun-bonkfun-bot commit #232](https://github.com/chainstacklabs/pumpfun-bonkfun-bot)). That suggests PumpPortal can miss some creates. **[Their claim, not measured here.]**

### 2.2 pump.fun's own NATS feed (unofficial, undocumented)

- Endpoint: `wss://prod-v2.nats.realtime.pump.fun/`, using the NATS-over-websocket protocol.
  1. Send `CONNECT {"user":"subscriber","pass":"lW5a9y20NceF6AE9","lang":"nats.ws","headers":true,…}`.
  2. Then send `SUB newCoinCreated.prod 1`.
- Other subjects:
  - replies: `newReplyCreated.{mint}.prod`
  - livestream: `pump.fun.livestream`
  - AMM trades: `wss://amm-prod.nats.realtime.pump.fun/` with `ammTradeEvent.{pool}` (different password)
- Sources: [TanPingZhi/pumpfunpy api.py](https://github.com/TanPingZhi/pumpfunpy) (May 2025), [umair9747/pumpWatch](https://github.com/umair9747/pumpWatch), and [callmedraxx/pump-stream-sniper stream.py](https://github.com/callmedraxx/pump-stream-sniper) (Sept 2025).
- **The credentials are scraped from pump.fun's web bundle and can rotate at any time.** I could not confirm the feed still works in Oct 2026. Treat it as an opportunistic extra only.

### 2.3 Standard Solana RPC (`logsSubscribe`): the vendor-neutral path

See section 3. This works on **any** RPC that supports websockets, including Helius's free plan. It was confirmed working in Chainstack's live listener tests in Sept 2026 ([chainstack docs](https://docs.chainstack.com/docs/solana-listening-to-pumpfun-token-mint-using-only-logssubscribe), [bot repo](https://github.com/chainstacklabs/pumpfun-bonkfun-bot)).

### 2.4 Commercial providers (pricing from search summaries, **all [UNVERIFIED]**)

| Provider | What it gives | Free tier / price |
|---|---|---|
| **Helius** ([plans](https://www.helius.dev/docs/billing/plans), [pricing](https://www.helius.dev/pricing)) | RPC plus standard WS (`logsSubscribe` on any plan). Enhanced WS (`transactionSubscribe`) from Developer upward. Webhooks. DAS `getAsset` (Token-2022 and Metaplex metadata). "Parsed Events" guide for pump.fun mints including `create_v2` ([guide](https://www.helius.dev/docs/parsed-events/guides/fetch-pumpfun-mints)). LaserStream gRPC. | Free: 1M credits/mo, 10 RPC rps, 1 webhook. Developer $49/mo (10M credits). Business $499 (LaserStream gRPC since 2026-04-07). Professional $999. Streaming billed at 20 credits/MB since Apr 2026. ([blog](https://www.helius.dev/blog/laserstream-websockets)) |
| **Shyft** ([guide](https://docs.shyft.to/dev-guides/grpc-case-studies/pumpfun-grpc-streaming-examples/detecting-new-token-launches)) | Yellowstone gRPC streaming with an IDL parser | Free: RPC only, no gRPC. gRPC from $199/mo. |
| **Bitquery** ([docs](https://docs.bitquery.io/docs/blockchain/Solana/Pumpfun/Pump-Fun-API/), [pricing](https://bitquery.io/products/pumpfun-api)) | GraphQL plus subscriptions. Tracks `create`/`create_v2` and Mayhem. | 7-day trial only (1,000 points). About $39–$239/mo. Streams on higher tiers. |
| **Moralis** ([docs](https://docs.moralis.com/data-api/solana/token/search-and-discovery/pump-fun-new-tokens)) | `GET https://solana-gateway.moralis.io/token/mainnet/exchange/pumpfun/new?limit=` (tokens under 24h old), plus `/bonding` and `/graduated` | Free API key exists. Compute-unit cost per call not found. |
| **Solana Tracker** ([pricing](https://docs.solanatracker.io/pricing)) | `/tokens/latest` and Datastream WS | Free: 2,500 requests/mo. WS from about €397/mo. |
| **Birdeye** ([pricing](https://birdeye.so/data-api/pricing)) | Token lists and new listings, compute-unit metered | Free "Standard" tier is evaluation-grade. $39–$499/mo. WS from Premium. |
| **DexScreener** ([API ref](https://docs.dexscreener.com/api/reference)) | `GET https://api.dexscreener.com/token-profiles/latest/v1` (60 rpm) returns `url, chainId, tokenAddress, icon, header, description, links[{type,label,url}]`. These are only tokens whose creators **paid** for a profile, which is a small subset. `/tokens/v1/solana/{addrs,≤30}` and `/token-pairs/v1/solana/{addr}` allow 300 rpm. Keyless WS at `wss://api.dexscreener.com` for profiles, boosts and CTOs. | Free, no key ([rate-limit summary](https://coinpaprika.com/education/dexscreener-api-rate-limits-explained/)) |
| **GeckoTerminal** ([docs](https://api.geckoterminal.com/docs/index.html)) | `/networks/solana/new_pools`, token info. Mostly pools, so mostly graduated coins. | Free about 30 calls/min, sometimes cited as 10/min. Shared per IP. |
| Others seen | QuickNode "Pump Fun API" add-on, NoLimitNodes, pumpdev.io, pumpapi.io, Codex | Not evaluated |

---

## 3. On-chain path (authoritative, no third party)

All facts in this section come from the official [pump-fun/pump-public-docs](https://github.com/pump-fun/pump-public-docs) IDLs (`idl/pump.json`, `idl/pump_amm.json`) and docs, read at commit 2026-09-29.

### 3.1 Program IDs

| Program | Address |
|---|---|
| Pump (bonding curve) | `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` |
| PumpSwap AMM (graduation target since about March 2025, replacing Raydium) | `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA` |
| Mayhem program (account in `create_v2`) | `MAyhSmzXzV1pTf7LsNkrNwkWKTo4ougAJ1PPg47MD4e` |
| Token-2022 | `TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb` |

### 3.2 Create instructions

**`create`** (legacy)
- Discriminator `[24,30,200,40,5,28,7,119]`.
- Args: `name, symbol, uri, creator`.
- Creates an **SPL Token** mint plus a **Metaplex** metadata account (accounts include `mpl_token_metadata` and `metadata`).

**`create_v2`**
- Introduced 2025-11-11/12 together with Mayhem mode. Docs commit "Idl and type files for mayhem mode and create v2" is dated 2025-11-07.
- Discriminator `[214,144,76,236,95,139,49,180]`.
- Creates a **Token-2022 mint**: `decimals=6`, metadata pointer set to the mint itself, so **name, symbol and uri live in the Token-2022 TokenMetadata extension on the mint account. There is no Metaplex PDA.**
- Args, from [COIN_CREATION.md](https://github.com/pump-fun/pump-public-docs/blob/main/docs/instructions/COIN_CREATION.md):
  - `name` (≤32 chars)
  - `symbol` (≤13)
  - `uri` (≤200)
  - `creator`
  - `is_mayhem_mode: bool`
  - `is_cashback_enabled: OptionBool` (deprecated, must be false)
  - `creator_fee_bps: OptionU64`
  - `is_holder_reward: OptionBool` (added 2026-09-12)
- Both instructions are still in the IDL. The legacy `create` was described as "to be deprecated later" ([Chainstack](https://chainstack.com/trading-bot-update-full-mayhem-mode-support-for-pump-fun/)). **Handle both.** The current share of `create` versus `create_v2` is unknown.

### 3.3 CreateEvent

Discriminator `[27,114,169,77,222,235,99,118]`, hex `1b72a94ddeeb6376`. Borsh fields in order:

```
name:string, symbol:string, uri:string, mint:pubkey, bonding_curve:pubkey, user:pubkey,
creator:pubkey, timestamp:i64, virtual_token_reserves:u64, virtual_sol_reserves:u64,
real_token_reserves:u64, token_total_supply:u64, token_program:pubkey, is_mayhem_mode:bool,
is_cashback_enabled:bool, quote_mint:pubkey, virtual_quote_reserves:u64, creator_fee_bps:u64,
is_holder_reward:bool
```

- **Fields are appended over time. Older events are shorter.** The official SDK pads missing trailing bytes with 0, 1 or 9 zero bytes and retries ([`@pump-fun/pump-sdk` 2.0.0 `decodeCreateEventBc`](https://www.npmjs.com/package/@pump-fun/pump-sdk)). Decode the leading fields you need and tolerate whatever comes after.
- Strings are Borsh-encoded: a u32 little-endian length followed by UTF-8 bytes.
- Other relevant events:
  - `CompleteEvent` (`user, mint, bonding_curve, timestamp, quote_mint`)
  - `CompletePumpAmmMigrationEvent` (`… pool, quote_mint`)

### 3.4 Subscribing with plain `logsSubscribe`

```json
{"jsonrpc":"2.0","id":1,"method":"logsSubscribe",
 "params":[{"mentions":["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"]},{"commitment":"confirmed"}]}
```

For each notification:

1. Skip it if `value.err != null`. Failed transactions still deliver logs, and that mint does not exist.
2. Look for a log line that is exactly `Program log: Instruction: Create` or `Program log: Instruction: CreateV2`. **Match the whole line.** Chainstack found that substring matching also hit `CreateTokenAccount`, `CreatePool` and similar instructions from other programs ([commit #231](https://github.com/chainstacklabs/pumpfun-bonkfun-bot)).
3. Base64-decode each `Program data: …` line. Keep the one whose first 8 bytes equal `1b72a94ddeeb6376` and Borsh-decode it.
4. Only trust `Program data:` lines emitted while the pump program is executing. `Program data:` does not say which program emitted it.

The event also arrives as an Anchor `emit_cpi` self-CPI: the accounts `event_authority` and `program` are present. Geyser and gRPC consumers can parse the inner instruction instead.

Throughput is roughly 0.3–0.5 creates per second. Note that `logsSubscribe` on the pump program also delivers **every buy and sell**, which is far more traffic, so make sure your plan's websocket bandwidth or credits can absorb it. Commercial alternatives are Helius `transactionSubscribe` and Yellowstone gRPC.

### 3.5 Metadata on-chain versus off-chain

- Legacy mints: read the Metaplex Metadata PDA, or use Helius DAS `getAsset`.
- `create_v2` mints: read the mint account's TokenMetadata extension, via `getAccountInfo` with `jsonParsed`, `@solana/spl-token`'s `getTokenMetadata`, or DAS.
- On-chain storage holds only name, symbol and uri. **Description, image and socials are only in the off-chain JSON at `uri`.**
- **Mutability [mostly UNVERIFIED]:** pump.fun guides say metadata is immutable after launch and that socials cannot be edited ([pump.fun create guide](https://intercom.help/pumpfun-web/en/articles/11002205-create-a-coin-on-pump-fun), [blofin](https://blofin.com/academy/education/pumpfun/how-coins-are-created-on-pump-fun)). However:
  - the frontend schema now has `updated_at`, `banner_uri` and `cto_address`, so **the frontend API's view can change** (banners, CTO/community takeover, moderation flags `hidden`/`is_banned`/`nsfw`);
  - DexScreener profiles are a separate, paid, editable layer.

### 3.6 Mint suffix "pump"

- The pump.fun UI grinds vanity mint keypairs that end in `pump`, for example `…Bpump`.
- **This is not enforced on-chain.** In the PumpPortal capture of 2026-10-03, 9 of 29 `pool:"pump"` creates had mints **without** the suffix (for example `2kXgYzc6MNKKpk4tUqf3wNifJUrnWrWpoBKXF7GjF3Xk`). These are most likely coins from third-party launchers, bots or SDK users.
- **Never filter on the suffix.** Detect coins by program events.

### 3.7 Graduation and "complete"

- `BondingCurve.complete` becomes `true` at the end of the buy that brings `real_token_reserves` to 0 ([PUMP_PROGRAM_README](https://github.com/pump-fun/pump-public-docs/blob/main/docs/PUMP_PROGRAM_README.md)).
- That happens at about **85 SOL** of real reserves. The real example above shows `real_sol_reserves` of 85,005,359,213 lamports, which is about 85 SOL.
- After that, anyone can call `migrate`, which is permissionless and idempotent, to create the canonical PumpSwap pool. In the API this shows as `pump_swap_pool`.
- Coins graduated before about March 2025 went to Raydium instead (`raydium_pool`).
- Fewer than 2% of coins graduate ([solanacompass](https://solanacompass.com/news/pumpfun-launched-42000-tokens-in-one-day-fewer-than-2-will-ever-reach-a-dex)).

### 3.8 Changes in 2025–2026 that affect parsers

| When | Change |
|---|---|
| ~Mar 2025 | PumpSwap replaces Raydium as the graduation target |
| May 2025 | Creator fees |
| Nov 2025 | `create_v2` / Token-2022 and **Mayhem mode**: an opt-in AI agent trades the coin randomly for 24h, supply is temporarily 2B, and unsold tokens are burned ([KuCoin](https://www.kucoin.com/news/flash/pump-fun-launches-mayhem-mode-to-boost-early-stage-solana-memecoins), [Chainstack](https://chainstack.com/trading-bot-update-full-mayhem-mode-support-for-pump-fun/)) |
| 2026 | New trade instructions `buy_v2`, `sell_v2`, `buy_exact_quote_in_v2`. **Quote mints other than SOL, starting with USDC** (`BondingCurve.quote_mint`). The `*_sol_reserves` fields are renamed `*_quote_reserves`. |
| 2026 | **Holder-rewards coins** (`is_holder_reward`, 2026-09-12). **Cashback deprecated.** "Tokenized agents". |
| 2026-09-30 | PumpSwap pools can have negative `virtual_quote_reserves` |

**Implication:** `marketCapSol` and reserves are not always in SOL. Check `quote_mint`, where the default pubkey means SOL.

---

## 4. Metadata JSON at `uri`

### 4.1 Format

The shape below comes from pump.fun's own docs ([pump-fun-skills create-coin/references/METADATA.md](https://github.com/pump-fun/pump-fun-skills/blob/main/create-coin/references/METADATA.md)).

| Field | Notes |
|---|---|
| `name` | |
| `symbol` | |
| `description` | |
| `image` | `https://ipfs.io/ipfs/{cid}` or another HTTPS URL |
| `showName` | bool |
| `createdOn` | Usually `"https://pump.fun"`. Third-party launchers put their own value or omit it. |
| `twitter`, `telegram`, `website` | Optional. Sometimes present as `""`. |
| `video` | Optional. The app may use an S3 URL. |

Example in the canonical shape. This is from a bundler repo's upload file ([Rabnail-SOL/Solana-PumpFun-Bundler upload/metadata.json](https://github.com/Rabnail-SOL/Solana-PumpFun-Bundler)), not a live token; a live fetch was blocked here:

```json
{"name":"Bolt token","symbol":"Bolt2","description":"Brave Veer & Bolt",
 "image":"https://cf-ipfs.com/ipfs/bafkreie7pjegykwmpduhmnqh4joe6dqlcqy6w2bwkykvnzh7qknmhswcee",
 "showName":true,"createdOn":"https://pump.fun",
 "twitter":"https://x.com/pepa_inu","telegram":"https://t.me/pepaonsols","website":"https://www.pepa-inu.com"}
```

Real metadata URIs seen in recent create events:

- `https://ipfs.io/ipfs/bafkrei…` (the dominant form; CIDv1 raw)
- `https://ipfs.io/ipfs/Qm…`
- `https://ipfs.filebase.io/ipfs/Qm…`
- older ones on `https://cf-ipfs.com/ipfs/…` and `https://cloudflare-ipfs.com/ipfs/…`
- arbitrary HTTPS (Arweave, S3, custom domains) from third-party launchers

**Validate the JSON defensively.** Fields can be missing, `null`, wrong type, huge, or malicious, such as `javascript:` URLs or tracking links.

### 4.2 Gateways and fallback strategy

- **`ipfs.io` rate-limits heavily.** A Sept-2026 collector reports `429` responses ("switching to a service worker gateway") and resolves through `pump.mypinata.cloud` → `gateway.pinata.cloud` → `ipfs.io` ([pumpfun-collector fetch.py](https://github.com/CookrAI/pumpfun-collector)).
  - `pump.mypinata.cloud` is pump.fun's dedicated Pinata gateway. Third-party use is tolerated today but could be restricted at any time. **[UNVERIFIED policy]**
- **`cf-ipfs.com` and `cloudflare-ipfs.com` were decommissioned on 2024-08-14** ([Cloudflare blog](https://blog.cloudflare.com/cloudflares-public-ipfs-gateways-and-supporting-interplanetary-shipyard)). Rewrite them.
- Recommended approach:
  1. Extract the CID and path from any `/ipfs/` URL, `ipfs://` URL, or `*.ipfs.*` subdomain.
  2. Try gateways in order: `pump.mypinata.cloud` → `dweb.link` → `ipfs.io` → `gateway.pinata.cloud`. Add your own Pinata, Filebase or 4EVERLAND dedicated gateway if you have one.
  3. Use about 5 s connect / 10 s total per attempt, and race two gateways in parallel after about 1.5 s.
  4. Cap responses at about 64 KB for JSON and about 5 MB for images.
  5. Cache by CID forever, because content is immutable.
  6. Retry later on 429 or 504. Newly pinned content can take seconds to propagate.
- The frontend API's `image_uri` is usually the same IPFS URL. Livestream coins also have `thumbnail` (S3) and `video_uri`.
- **Images:** whatever the creator uploaded, mostly PNG, JPEG, GIF and WebP. Sizes range from tiny to multi-MB, and animated GIFs and transparent PNGs are common. Sniff the content type from magic bytes, not the extension.
  - The collector normalises images to RGB, caps them at 1024 px, drops anything under 256 px, and dedupes by perceptual hash. It found **about 18% perceptual near-duplicates among pump.fun images** ([README](https://github.com/CookrAI/pumpfun-collector)).
  - On the pump.fun frontend, `twitter` is sometimes a `pbs.twimg.com`-style image source too.

---

## 5. Practical recommendations for a low-budget Render deployment

### 5.1 Volume

| Measurement | Value | Source |
|---|---|---|
| 2026 average | About 30k new pump.fun coins per day | search summaries |
| Peak, June 2026 | About 42k in one day | [solanacompass](https://solanacompass.com/news/pumpfun-launched-42000-tokens-in-one-day-fewer-than-2-will-ever-reach-a-dex) |
| Dec 2025 | Over 20–25k per day | [yellow.com](https://yellow.com/news/pumpfun-token-creation-reaches-25000-in-single-day-as-meme-coin-activity-returns) |
| PumpPortal capture, 2026-10-03 | 29 creates in 85 s ≈ 29k/day (tiny sample) | own calculation |
| pumpfun-collector README | "~25/min, 20–40k a day" | [README](https://github.com/CookrAI/pumpfun-collector) |
| Launch-day survival | 68.67% stop trading on launch day | [CoinGecko via KuCoin](https://www.kucoin.com/news/flash/coingecko-analysis-68-67-of-pump-fun-tokens-stop-trading-on-launch-day) |

**Plan for about 0.5 creates per second sustained, with bursts of 2–5 per second.** That means about 1 metadata JSON plus 1 image fetch per second, which is 30–40k of each per day.

### 5.2 Suggested architecture

**Primary feed (free): Solana `logsSubscribe` on the pump program, decoding `CreateEvent`.**
- Use Helius free (1M credits per month) or another free RPC websocket.
- This is authoritative and complete. It gives name, symbol, uri, mint, creator, timestamp, `is_mayhem_mode` and `quote_mint` with no third party in between.
- **Risk:** the pump program's buy and sell logs are high-volume, so watch websocket bandwidth and credit use.
- If that gets too heavy, fall back to PumpPortal.

**Secondary / bootstrap feed (free): PumpPortal `subscribeNewToken`.**
- One persistent connection, reconnect with backoff, filter on `pool=="pump"`.
- Easiest to start with. It may miss some creates and it is an unofficial dependency.
- **Run both feeds and dedupe on `mint`.** That covers outages in either.

**Enrichment (free, best-effort): `frontend-api-v3.pump.fun/coins-v2/{mint}`.**
- Call it server-side, after a delay of about 30–120 s for new coins, and cache.
- It gives parsed `twitter`/`telegram`/`website`, `nsfw`, `is_banned`, `hidden`, `usd_market_cap`, `complete`, `reply_count`.
- Throttle to at most 2–4 requests per second. Handle 403/429 by dropping to on-chain plus IPFS only.
- **Verify from Render early.** Cloudflare may challenge Render's egress IPs; this could not be tested here.

**Metadata and images:** IPFS with multi-gateway fallback and a CID cache, as in section 4.2. Process lazily: for example only for coins that survive more than N minutes, or that someone actually queries. About 70% die on launch day, so this saves most of the bandwidth.

**Market data for graduated coins:** DexScreener `/tokens/v1/solana/{≤30 mints}` (300 rpm, free) and GeckoTerminal (about 30 rpm). Use DexScreener token profiles, which are paid and creator-curated, as an extra social-links signal.

**Avoid as a primary source on a low budget:** Bitquery, Solana Tracker WS, Birdeye WS and Shyft gRPC all cost roughly $39–$400+ per month. Moralis's `/exchange/pumpfun/new` is a reasonable paid polling fallback.

**Render-specific notes:**
- Use a **Background Worker** (always-on) for the websocket listeners.
- Free web services spin down when idle and would drop the websocket.
- Persist to Postgres or Redis with a unique constraint on `mint`.
- Expect websocket disconnects every few minutes to hours. Implement ping/pong (about 20 s), reconnect with jitter, and, after a reconnect, backfill the gap with RPC `getSignaturesForAddress` on the pump program or `/coins?sort=created_timestamp`.

### 5.3 Pitfalls

1. **Spam and duplicates.**
   - Many coins copy the name and ticker of whatever is trending, often within seconds, and there are mass bundler launches.
   - About 18% of images are near-duplicates, and the same IPFS image CID is reused across many mints.
   - Dedupe on mint. Cluster by normalised name/symbol, image CID or perceptual hash, and creator wallet.
2. **Bots and serial creators.** One wallet may launch dozens of coins a day. Track creator history. Metadata may also be fake or placeholder: the bundler template above, with its `pepa_inu` socials, is reused across many repos.
3. **Social links are unverified and often misleading.** `twitter` is frequently a link to *someone else's* viral tweet or account, not the project's. Links can be phishing or malware. Never auto-follow them, and sanitise every URL to `https`, `x.com`, `twitter.com` and `t.me` only.
4. **Metadata changes.**
   - On-chain name, symbol and uri are effectively immutable, and IPFS content cannot change.
   - But the `uri` may point to mutable HTTPS hosting from third-party launchers.
   - The frontend's `banner_uri`, `cto_address`, `nsfw`, `hidden` and `is_banned` can change later, so re-poll occasionally.
5. **NSFW and illegal images.**
   - pump.fun flags `nsfw` and runs moderation (`/moderation/ban-image-terms`, `ban-regex-patterns`), but on-chain or PumpPortal-sourced coins arrive **unmoderated**.
   - Without external AI APIs, use a local NSFW classifier (for example an ONNX NSFW model), honour the frontend `nsfw`/`is_banned`/`hidden` flags when available, and hide images until they are checked.
   - Never re-host flagged images.
6. **Schema drift.** New `CreateEvent` fields have been appended (`quote_mint`, `creator_fee_bps`, `is_holder_reward`). The frontend API renames fields. Non-SOL quote mints make "SOL" market caps wrong. Write tolerant parsers and log unknown shapes.
7. **Non-"pump" mints, letsbonk and other launchpads** appear in mixed feeds. Filter by program ID or the `pool` field, not by suffix.
8. **Failed transactions** deliver logs too. Check `err`.
9. **Gateway 429s and CIDs that never resolve.** Some metadata never propagates. Mark it unresolved after N retries over about 1 h.
10. **Legal and ToS.** frontend-api is not a public API, and pump.fun's ToS may forbid scraping. Keep it optional.

---

## Key sources

- Official: [pump-fun/pump-public-docs](https://github.com/pump-fun/pump-public-docs) (IDLs and docs, 2026-09-29), [pump-fun/pump-fun-skills](https://github.com/pump-fun/pump-fun-skills), [@pump-fun/pump-sdk 2.0.0 on npm](https://www.npmjs.com/package/@pump-fun/pump-sdk)
- API specs and captures: [BankkRoll/pumpfun-apis](https://github.com/BankkRoll/pumpfun-apis)
- Working tools and real payloads: [CookrAI/pumpfun-collector](https://github.com/CookrAI/pumpfun-collector), [chainstacklabs/pumpfun-bonkfun-bot](https://github.com/chainstacklabs/pumpfun-bonkfun-bot), [TanPingZhi/pumpfunpy](https://github.com/TanPingZhi/pumpfunpy), [callmedraxx/pump-stream-sniper](https://github.com/callmedraxx/pump-stream-sniper), [macdarenz-droid/Meme-snipe](https://github.com/macdarenz-droid/Meme-snipe)
- Vendors: [PumpPortal real-time](https://pumpportal.fun/data-api/real-time/), [Helius plans](https://www.helius.dev/docs/billing/plans), [DexScreener API](https://docs.dexscreener.com/api/reference), [GeckoTerminal](https://api.geckoterminal.com/docs/index.html), [Moralis pump.fun](https://docs.moralis.com/data-api/solana/token/search-and-discovery/pump-fun-new-tokens), [Bitquery](https://docs.bitquery.io/docs/blockchain/Solana/Pumpfun/Pump-Fun-API/), [Solana Tracker](https://docs.solanatracker.io/pricing), [Birdeye](https://birdeye.so/data-api/pricing), [Shyft](https://docs.shyft.to/dev-guides/grpc-case-studies/pumpfun-grpc-streaming-examples/detecting-new-token-launches)
