# TokenSage — Project Guide

> **Audience:** the engineer or AI coding agent who will build TokenSage from an empty repo.
> **Status:** Phases 0–5 built on 2026-10-06 (skeleton, v1 contract, queue, worker, CI, on-chain CA resolution, safe metadata/image fetch with retries, the basic-depth engine of §5 with a 67-case golden set, and full depth: OCR, the X fetch chain with caching and relation signals, Wikipedia-pageview trends with news confirmation, and the knowledge cron). Phase 5 (integration hardening: per-key daily quotas and usage counters, `503` back-pressure, signed webhook callbacks, access logs, load-test harness with a local fake chain) is built too; the live load test against Render and the consumer integration remain. Phase 0's smoke test still needs to be *run* from Render. Written 2026-10-05.
> **Companion material:**
> - `docs/research/` holds four detailed research reports with sources. This guide is the synthesis; go to the reports for the evidence behind any claim.
> - `docs/reference/` holds small, **tested** reference implementations: CA validation, the pump.fun bonding-curve address derivation and account decoder, the `CreateEvent` decoder with real captured events, the X URL parser, the tweet-ID date decoder and the syndication token.
>
> **Shape of the product:** TokenSage is a **backend HTTP API**. Another application sends it a token **Contract Address (CA, the mint address)** and gets back a structured JSON analysis of what the token means. There is no end-user UI.

---

## 0. How to use this guide

1. **Read §1–§3 first.** They define what we are building and the domain knowledge it depends on.
2. **§4–§8 are the design:** data sources, understanding engine, architecture, data model, deployment.
3. **§6.4 is the contract with the consumer application.** Build it first (with stubs) so integration can start early.
4. **§9 is the build plan.** Follow the phases in order. **Phase 0 is a live smoke test from Render. Do not skip it.** None of the external endpoints could be tested live during research, because the research sandbox blocked them. Every free data source in this guide is "documented to work" but unconfirmed from Render's IPs.
5. **§13 lists open questions.** Resolve them as you go and update this file.

**Ground rules for the implementer:**
- **No external AI APIs.** No OpenAI, Anthropic, Gemini, Google Cloud Vision, Hugging Face Inference API, or any other hosted model call. Small models running **locally** on CPU inside our own container (ONNX) are allowed, but they are **optional, flag-gated layers** and never required for a useful result. The core engine is deterministic: rules, lexicons, gazetteers, fuzzy matching, perceptual hashing, OCR.
- **Every conclusion must carry evidence.** TokenSage explains *why* it thinks `$PNUT` refers to Peanut the Squirrel. A label with no evidence trail is a bug.
- **Untrusted input everywhere.** Token names, metadata JSON, image bytes, URLs and tweets are attacker-controlled (§10).
- **Tolerant parsers.** pump.fun, its unofficial APIs and the X mirrors change shape without notice. Validate, degrade gracefully, and log unknown shapes. Never crash the pipeline on one bad token.

---

## 1. Product definition

### 1.1 One-sentence goal
Given a **Contract Address (CA)**, work out **what that pump.fun token is about** and return it as stable, versioned JSON that another application can consume: referent, categories, explanations, flags, confidence scores and the evidence behind each. TokenSage looks up everything else from the CA itself: the token's name, ticker, description, image and linked X/Twitter content.

```
consumer app ──GET /v1/tokens/{CA}──▶ TokenSage ──▶ Solana RPC (on-chain name/ticker/uri, bonding curve)
                                                ├─▶ IPFS / metadata host (description, image, socials)
                                                ├─▶ X mirrors (linked tweet / profile)
                                                └─▶ local knowledge (known coins, gazetteers, trends)
            ◀── Analysis JSON (§3) ─────────────┘
```

### 1.2 Questions TokenSage answers per token

| Question | Example answer |
|---|---|
| **What does it refer to?** (the *referent*) | "Peanut (squirrel): Instagram-famous pet squirrel seized by NY officials, Oct 2024" |
| **What kind of narrative is it?** (categories, multi-label) | animal/squirrel 0.80, news-event 0.55, derivative 0.92 |
| **Is it a copy or derivative of an existing coin?** | "Derivative of $PNUT: same ticker plus '2', logo pHash distance 6" |
| **What do the ticker and name mean?** | "PNUT = vowel-dropped 'peanut'"; "WIF = slang 'with' (dog wif hat)" |
| **What is in the image?** | OCR text "$PNUT", dominant colours, near-duplicate of known logo X, optional visual labels |
| **What is the linked X content, and how does it relate?** | "Links a tweet by @elonmusk from 3 h before launch. Borrowed narrative, not an official account" |
| **Is it tied to something trending now?** | "'Peanut (squirrel)' Wikipedia views spiked 40× yesterday" |
| **Red flags** (informational, not financial advice) | homoglyph ticker, recycled X account, spoofed handle in tweet URL, same tweet linked by 37 other tokens |

### 1.3 Non-goals (v1)
- No trading, sniping, price prediction or "buy" signals.
- No wallet or holder forensics beyond creator-wallet history counts. This can be added later.
- No external AI. No reverse image search via Google Lens, TinEye or Cloud Vision.
- No exhaustive X scraping. X is fetched only for tokens someone asks about at `depth=full` (§6.3).
- No end-user UI. A tiny read-only debug page is allowed for development, but the product is the API.
- No discovery feed ("show me new tokens"). The consumer brings the CA. The optional corpus ingester (§6.6) exists only to improve copycat detection, not to push tokens to anyone.

### 1.4 Constraints
- **Hosting:** Render, defined entirely in a Render Blueprint (`render.yaml`).
- **Budget:** low. Target about $15–40/month for v1 (§11).
- **Input:** one CA per request (or a small batch). Any string the consumer sends must be validated as a Solana address before anything else happens (§6.4).
- **Load is request-driven.** Cost and CPU scale with the consumer app's request rate, not with pump.fun's launch rate. The consumer's expected request volume and latency needs are an open question (§13); design for tens of requests per minute on Starter, with caching.
- **Background context:** pump.fun launches about **30,000 coins per day** in 2026 (peak ~42,000). About 69% stop trading on launch day, and fewer than 2% graduate. That matters for copycat detection: any popular name has many clones (§5.5, §6.6).
- **Integration-friendly:** stable versioned paths (`/v1/…`), an OpenAPI schema, API-key auth, predictable errors, and an explicit "partial result" status when some upstream source fails.
- **RAM:** Render Starter instances have 512 MB. The base system must fit there; heavier optional layers move to a 2 GB Standard instance.

---

## 2. Domain primer (read this even if you know crypto)

### 2.1 pump.fun mechanics that matter to us
- Anyone can launch a coin by calling the **pump program** (`6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`) on Solana with a **name (≤32 chars), symbol/ticker (≤13 chars) and a metadata `uri` (≤200 chars)**.
- **On-chain data holds only name, symbol and uri.** Description, image and socials (`twitter`, `telegram`, `website`) live **off-chain** in the JSON at `uri`, usually on IPFS.
- Two create instructions exist and **both must be supported**:
  - `create` (legacy): SPL Token mint plus Metaplex metadata account.
  - `create_v2` (since Nov 2025): Token-2022 mint with the metadata extension on the mint itself.
- Both emit the same **`CreateEvent`** in the program logs, which is what we decode (§4.1).
- A coin trades on a **bonding curve**. When the curve sells out (~85 SOL raised), `complete = true` and the coin **graduates** to the **PumpSwap AMM** (`pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`). Before about March 2025 coins graduated to Raydium instead.
- 2025–2026 additions to tolerate:
  - **Mayhem mode**: an opt-in random-trading agent for 24 h.
  - **Non-SOL quote mints** (USDC). Reserves fields were renamed `*_quote_reserves`, so "market cap in SOL" is not always SOL.
  - Holder-rewards coins, creator fees, and "tokenized agents".
- **Mint addresses often end in `pump`**, but this is a UI vanity convention, not a rule. About 30% of creates in one Oct 2026 sample lacked it. **Never filter on the suffix.**
- Third-party launchers and bots create coins through the same program, often with **non-IPFS metadata hosts**. The real events in `docs/reference/create_events.txt` include `metadata.j7tracker.io`, `meta.uxento.io` and `usepaid.app`.

### 2.2 How memecoins get their "meaning"
Most coins are an attempt to **attach to an attention source**. Recognising which source is the core of the product. Common patterns (multi-label; one coin often hits several):

| Pattern | What it looks like | Famous examples (for the seed knowledge base) |
|---|---|---|
| **Animal / pet mascot** | dog, cat, frog, hippo, squirrel, penguin… often a specific famous animal | `$WIF` dogwifhat (a Shiba Inu wearing a knit hat), `$BONK`, `$POPCAT` (the "pop cat" meme), `$MEW` ("cat in a dogs world"), `$MOODENG` (Thai baby pygmy hippo, 2024), `$PNUT` (Peanut the Squirrel, seized and euthanised in NY, Oct 2024), `$MICHI`, `$FWOG`, `$MYRO` |
| **Meme template / internet culture** | Pepe, Wojak, Chad, NPC, "X wif hat", brainrot slang | `$CHILLGUY` ("Just a chill guy" drawing, 2024), `$GIGA` (Gigachad), `$PONKE`, `$SLERF` |
| **Templated derivative** | Name pattern reused from a winner: "X wif hat", "Baby X", "X 2.0", "X Classic", "X Inu" | "catwifhat", "trumpwifhat", "Baby PNUT", "Moo Deng Classic" |
| **Copycat / relaunch** | Same ticker or name as a trending or famous coin, often with a homoglyph or a re-uploaded logo | dozens of `$PNUT`s within minutes of the original |
| **News / viral event** | Something that happened in the last hours or days | The PNUT news cycle; a viral video; a sports moment |
| **Celebrity / influencer** | Elon Musk posts, rappers, streamers, politicians' quotes | Coins named after something Elon tweeted minutes earlier |
| **Political (PolitiFi)** | Elections, leaders, slogans | Trump- and election-themed coins |
| **AI / agent** | AI-agent lore, "launched by an AI", LLM-related terms | `$GOAT` (Goatseus Maximus, promoted by the "Truth Terminal" AI bot), `$ZEREBRO`, `$AI16Z`, `$ACT` |
| **Crypto-native / self-referential** | Slang, pump.fun jokes, "community takeover" (CTO), utility claims | `$FARTCOIN` (toilet humour plus "AI-generated idea" lore), `$WEN` |
| **Regional / language meta** | CJK names, country tickers | Chinese-character coins are a recurring pump.fun "meta" |
| **Crude humour / offensive** | toilet jokes, slurs, shock content | Recognised as a category from text (and OCR text), like any other narrative. There is no image moderation (owner decision). |

> The famous-coin facts above are well known up to 2024–2025 but must be **verified when seeding** the known-coins table (CoinGecko/Wikipedia), not hard-coded from this guide.

### 2.3 Ticker and name conventions to decode
- **Vowel dropping:** PNUT ← peanut, MSTR ← master. Check whether the ticker is a subsequence of the compacted name and how it compares to the name with vowels removed.
- **Acronyms:** "cat in a dogs world" → CIADW. Note that the real ticker was MEW, which is "lore", so a gazetteer is needed.
- **Baby talk / slang spellings:** wif = with, fwog = frog, smol, chonk, wen, ser, fren.
- **Version and derivative markers:** `2.0`, `v2`, `II`, `classic`, `og`, `real`, `new`, `baby`, `mini`, `inu`, `ai`, `wif`. **Detect these before stripping punctuation**, or "2.0" becomes "20".
- **Concatenation:** `dogwifhat`, `justachillguy`, `peanutthesquirrel`. Needs word segmentation with a meme-aware vocabulary.
- **Obfuscation:** leetspeak (`p3anu7`), full-width (`ＰＮＵＴ`), small caps (`ʙᴀʙʏ ᴘɴᴜᴛ`), Cyrillic look-alikes (`Рepe`), zero-width characters. Their presence is **itself a signal** (spoof or copycat).
- **Emoji:** 🐿 means squirrel and 🥜 means peanut. Map emoji with Unicode CLDR keyword annotations, not only their short names.

### 2.4 How X/Twitter links behave on pump.fun
The metadata `twitter` field can be:
- a **profile** (the project's or someone else's)
- a **single tweet**, very often **someone else's viral tweet** used as the narrative source
- an **X Community** (`x.com/i/communities/<id>`), which is increasingly common, cheap to create and anonymous
- a **search or hashtag link**
- a **t.co shortlink**
- or garbage

Key facts:
- **The handle in a status URL is ignored by X.** `x.com/elonmusk/status/<id-of-a-random-tweet>` resolves to the random tweet. This is a **spoofing technique**: always trust the *fetched* author, never the URL handle.
- **Tweet, user and community IDs are snowflakes.** Their creation time can be decoded **offline**: `ms = (id >> 22) + 1288834974657`. This gives "did the tweet or community exist before the token?" for free, with no network call.
- **A tweet that predates the token** means the coin was made *about* that tweet (narrative source). **A tweet after launch by the token's own account** is a launch announcement.
- **The same tweet or handle reused by many tokens** indicates narrative farming. Count it in our own database; it costs nothing.

### 2.5 Glossary
| Term | Meaning |
|---|---|
| **CA** | contract address (mint) |
| **dev** | token creator wallet |
| **CTO** | community takeover (dev abandoned the coin, the community runs socials) |
| **KOL** | influencer |
| **jeet** | panic seller |
| **rug** | dev dumps or abandons the coin |
| **bundle** | dev buys with many wallets at launch |
| **graduate / migrate** | bonding curve completes and the coin moves to the AMM |
| **KOTH** | pump.fun's "king of the hill" spot |
| **meta** | the narrative currently in fashion (e.g. "AI agents", "Chinese coins") |

A starter slang lexicon is in `docs/research/03-understanding-techniques.md` §A3.

---

## 3. What "understanding" means here: the output contract

Every analysed token produces one **Analysis** document. This is the product: the API returns it inside a small response envelope (§6.4). Build the engine to this schema from day one, version it, and publish it in the OpenAPI schema so the consumer app can generate a typed client.

**Compatibility rules for `schema_version`:** adding fields or new category labels is a minor change (same version; consumers must ignore unknown fields and labels). Renaming, removing or changing the meaning of a field means a new version, served under a new path (`/v2/…`) while `/v1` keeps working.

```jsonc
{
  "schema_version": "1",
  "mint": "…",                        // the CA, canonical base58
  "created_at": "2026-10-05T12:00:00Z", // token creation time, null if it could not be determined
  "launchpad": "pump.fun",           // "pump.fun" | "unknown" (non-pump mints are best-effort, §4.1)
  "market": { "complete": false, "curve_progress": 0.42, "graduated_pool": null,
              "creator": "…", "is_mayhem_mode": false, "quote_mint": "SOL" },
  "raw": { "name": "Peanut the Squirrel 2.0", "symbol": "PNUT2", "description": "…",
           "image_url": "…", "twitter": "…", "telegram": "…", "website": "…" },
  "normalized": { "name_tokens": ["peanut","the","squirrel","2.0"], "ticker": "PNUT2",
                  "ticker_base": "PNUT", "markers": ["version:2"], "emoji_keywords": ["squirrel"],
                  "obfuscation": [] },
  "referent": { "label": "Peanut (squirrel)", "kind": "famous_animal",
                "desc": "Instagram-famous pet squirrel seized by NY officials (Oct 2024)",
                "source": "wikidata:Q…", "confidence": 0.86 },
  "categories": [ {"label": "derivative", "confidence": 0.92},
                  {"label": "animal/squirrel", "confidence": 0.80},
                  {"label": "news_event", "confidence": 0.55} ],
  "ticker_explanation": "PNUT = vowel-dropped 'peanut'; '2' = sequel marker",
  "copy_of": [ {"ticker": "PNUT", "mint": "…", "signals": ["ticker_base", "name", "logo_phash:6"]} ],
  "image": { "status": "ok", "phash": "…", "pdq": "…", "ocr": ["$PNUT"],
             "palette": ["#c87f3a"], "near_duplicates": [ … ], "labels": [],
             "source_url": "…" },              // gateway URL of the image
  "x": { "ref": {"kind": "tweet", "tweet_id": "…", "url_handle": "…"},
         "tweet_time": "…", "predates_token_by_s": 10800,
         "author": {"handle": "…", "verified_type": "…", "followers": 0, "username_changes": 0},
         "text": "…", "relation": "narrative_reference",
         "reuse_count": 37, "fetch_source": "fxtwitter", "status": "ok" },
  "trend": { "matched": true, "terms": [{"term": "Peanut (squirrel)", "spike": 40.2, "source": "wikipedia_pageviews"}] },
  "flags": [ {"code": "homoglyph_ticker", "severity": "warn", "detail": "…"} ],
  "summary": "Most likely refers to Peanut (squirrel)… (template-generated)",
  "evidence": [ {"kind": "known_coin_match", "label": "derivative", "weight": 0.9,
                 "detail": "ticker PNUT2 → base PNUT = known coin $PNUT", "source": "known_coins:pnut"} ],
  "caveats": [ "No current news spike; likely revival/copycat" ],
  "depth": "full",                    // "basic" | "full" (see §6.3)
  "analyzed_at": "2026-10-05T12:00:07Z",
  "versions": { "rules": "0.3.0", "lexicon": "2026-10-05", "known_coins": "2026-10-04", "models": {} }
}
```

**Rules for the summary:** it is **template-generated** from the structured fields, never free-form generated. It lists the referent, the top categories and up to 5 evidence bullets sorted by weight. Caveats are added automatically when:
- there is only a single weak source
- two referents score within 0.1 of each other
- homoglyphs are present
- the image is missing or failed to load
- the tweet was deleted or the fetch failed

---

## 4. Data sources: decisions

Full details, pricing and payload samples are in `docs/research/01-pumpfun-data-sources.md` and `02-x-twitter-access.md`.

### 4.1 Resolving a CA (the entry point of every request)

Everything starts from the CA. The resolver turns it into the token's on-chain facts using **only standard Solana RPC calls** (no third-party API needed), then fetches the off-chain metadata (§4.2). Reference code: `docs/reference/pump_ca.py` (tested; the bonding-curve address derivation matches 7 real mint/curve pairs).

1. **Validate the input.**
   - Strip whitespace, and accept pump.fun, DexScreener or explorer URLs that end in the address.
   - It must be base58 and decode to exactly 32 bytes. Otherwise return `400 invalid_ca` without touching the network.
2. **Fetch the mint account** with `getAccountInfo(ca, {encoding: "jsonParsed"})`.
   - Missing: `404 token_not_found`. (Very new tokens may need a retry at `confirmed` commitment.)
   - Owner is not the SPL Token program (`TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA`) or Token-2022 (`TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb`), or it is not a mint (e.g. someone sent a wallet address): `422 not_a_token_mint`.
   - The owner program also tells you which create path made it: SPL Token means legacy `create`, Token-2022 means `create_v2`. This is the authoritative `token_program`; never trust the frontend API's copy.
3. **Check it is a pump.fun token.**
   - Derive the bonding-curve address: program-derived address with seeds `["bonding-curve", mint]` under the pump program `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`.
   - Fetch it with `getAccountInfo`. If it exists and starts with the `BondingCurve` discriminator, the token is a pump.fun coin. The account stays after graduation, with `complete = true`.
   - Decode it tolerantly (older accounts are shorter): `virtual/real token and quote reserves, token_total_supply, complete, creator, is_mayhem_mode, quote_mint, creator_fee_bps, is_holder_reward`.
   - **Curve progress** = 1 − real_token_reserves ÷ initial_real_token_reserves. Read the initial value from the pump `Global` account once and cache it; do not hard-code it.
   - No bonding curve: the mint did not come from pump.fun (letsbonk, another launchpad, or a plain SPL token). Behaviour is controlled by `ACCEPT_NON_PUMP` (default `true`): analyse it anyway from its metadata with `launchpad: "unknown"`, skipping pump-specific signals. Set it to `false` to return `422 not_pumpfun`.
4. **Read name, symbol and uri on-chain.**
   - **Token-2022 (`create_v2`):** the jsonParsed mint account includes the `tokenMetadata` extension with `name`, `symbol`, `uri`.
   - **Legacy SPL:** fetch the Metaplex metadata account, the program-derived address with seeds `["metadata", metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s, mint]` under the Metaplex program `metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s`. Borsh-decode `name`, `symbol`, `uri` and strip trailing NUL padding.
   - **Shortcut:** on Helius, one DAS `getAsset(ca)` call returns name, symbol, json_uri and token program for both kinds. Use it when available, and keep the raw path as fallback so the RPC provider stays swappable.
5. **Creation time and creator.** These are not stored on the mint, so use the first that works:
   1. our own database (a previous request, or the optional corpus ingester in §6.6, which has the exact `CreateEvent`);
   2. pump.fun `GET frontend-api-v3.pump.fun/coins-v2/{ca}` → `created_timestamp` (milliseconds) and `creator` (optional enrichment, below);
   3. RPC history: page `getSignaturesForAddress(bonding_curve)` backwards to the oldest signature, then `getTransaction` and decode its `CreateEvent` with `pump_event.py`. This is exact, but a heavily traded coin can need many pages, so cap it (e.g. 5 pages / 5,000 signatures) and otherwise report `created_at: null`. The creator also comes from the `BondingCurve` account.
6. **Cache the result.** Name, symbol, uri, creator and creation time never change, so store them forever. Bonding-curve state (`complete`, progress) changes, so give it a short TTL (about 60 s).

Typical cost of a cold resolve: 2–3 RPC calls plus 1 metadata fetch, well inside free RPC tiers at tens of requests per minute.

**Optional: knowing about tokens before anyone asks (corpus ingester).** Copycat detection works best when TokenSage has seen the other coins with the same name. A background feed listener can record every new pump.fun token cheaply (§6.6). The two free feeds:

- **Solana RPC `logsSubscribe`** on the pump program, decoding `CreateEvent`:
  ```json
  {"jsonrpc":"2.0","id":1,"method":"logsSubscribe",
   "params":[{"mentions":["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"]},{"commitment":"confirmed"}]}
  ```
  1. Skip it if `value.err != null`.
  2. Require an **exact** log line `Program log: Instruction: Create` or `Program log: Instruction: CreateV2`. Substring matching wrongly catches `CreateTokenAccount` and `CreatePool`.
  3. Base64-decode the `Program data:` lines and pick the one whose first 8 bytes are `1b72a94ddeeb6376`.
  4. Borsh-decode it with `docs/reference/pump_event.py` (tolerates older and newer layouts; tested on 6 real 2026 events).
  - **Bandwidth risk:** `mentions` also delivers **every buy and sell**, far more traffic than creates. Measure the credit and bandwidth cost in Phase 0.
- **PumpPortal websocket** `wss://pumpportal.fun/api/data` with `{"method":"subscribeNewToken"}`.
  - Free and keyless. Use **one connection only**; several connections earn about a one-hour ban.
  - Filter `pool == "pump"`, because the feed also carries letsbonk creates.
  - Messages include `mint, name, symbol, uri, traderPublicKey, marketCapSol, is_mayhem_mode, pool`.
  - Reportedly samples rather than covers the chain (Chainstack, Sept 2026). Fine for a corpus; gaps only weaken copycat counts.
- pump.fun's own NATS feed (`wss://prod-v2.nats.realtime.pump.fun`, subject `newCoinCreated.prod`) uses credentials scraped from their web bundle that can rotate at any time. Do not build on it.

**On-demand copycat lookups (no corpus needed):** pump.fun `GET frontend-api-v3.pump.fun/coins/search?searchTerm=<ticker or name>` (unofficial, throttled) returns other coins with the same name and their creation times. DexScreener `GET https://api.dexscreener.com/latest/dex/search?q=<ticker>` (free) covers graduated coins. Together with the `known_coin` table and every token TokenSage has already analysed, this answers "is this the original or a clone?" well enough for v1.

### 4.2 Token metadata (description, image, socials)

1. Fetch the `uri` found by the resolver (§4.1).
   - IPFS URLs: extract the CID and path and fetch through a **gateway fallback chain**: `pump.mypinata.cloud` → `dweb.link` → `ipfs.io` → `gateway.pinata.cloud`.
     - After ~1.5 s, race a second gateway.
     - Timeouts: 5 s connect / 10 s total.
     - Cap JSON at 64 KB and images at 5 MB.
     - **Rewrite** `cf-ipfs.com` and `cloudflare-ipfs.com`; they were shut down in Aug 2024.
     - **Cache by CID forever**, because IPFS content is immutable.
   - Non-IPFS URLs (third-party launchers): fetch with the same limits and the SSRF guard (§10), and cache by URL plus content hash.
2. Expected JSON fields: `name, symbol, description, image, showName, createdOn, twitter, telegram, website, video?`. Validate everything:
   - fields may be missing, `""`, the wrong type, huge, or hostile (`javascript:` URLs);
   - `createdOn` tells you the launcher (`https://pump.fun` vs others).
3. If the metadata cannot be fetched within the request budget, return a **partial** analysis built from the on-chain name and symbol, with a caveat. Keep retrying the CID in the background with backoff for about 1 h, then mark it `unresolved`. The next request for that CA picks up the completed result.

**Enrichment (optional, throttled):** `GET https://frontend-api-v3.pump.fun/coins-v2/{mint}`.
- This is the one endpoint pump.fun itself documents.
- It returns already-parsed `twitter/telegram/website`, plus `is_banned`, `hidden`, `usd_market_cap`, `complete`, `reply_count`, `created_timestamp` (ms) and `market_cap` (SOL).
- Call it server-side only (it is CORS-protected), at ≤2–4 requests/second, with backoff on 429.
- It **may be Cloudflare-challenged from Render IPs**; test in Phase 0.
- **Never trust its `token_program` field** (pump.fun's own warning).
- Use it for parsed socials and market signals, not as the primary source.

**Market context (reported in `market`, used only as context, never as advice):**
- Bonding-curve state from the resolver: `complete` and curve progress.
- For graduated coins: DexScreener `/tokens/v1/solana/{≤30 mints}` (free, 300 rpm) or GeckoTerminal (~30 rpm) for the pool address. DexScreener paid token profiles are an extra source of social links.

### 4.3 X/Twitter content: tiered and lazy

**For every request (free, no network):**
1. Parse the `twitter` field with `parse_x_ref()` (`docs/reference/xref.py`, tested on 18 URL shapes).
2. Decode the snowflake time of any tweet or community ID.
3. Count reuse of the same tweet ID, handle or community ID across tokens in our DB.

**Only for `depth=full` requests (§6.3):** fetch in this order, stopping at the first success. Each source gets a circuit breaker.

| Order | Source | Gives | Notes |
|---|---|---|---|
| 1 | **FxTwitter** `GET https://api.fxtwitter.com/2/status/{id}`, `/2/profile/{handle}`, `/2/profile/{handle}/about` | Richest free source: text, author, followers, verification type, media, quote, community (when the post is *inside* one), **username-change count** | Requires a descriptive `User-Agent`. Third-party; commercial use unclear (FxEmbed issue #2550). MIT-licensed and self-hostable. |
| 2 | **vxTwitter** `GET https://api.vxtwitter.com/i/status/{id}`, `/{handle}` | text, media, followers (no verified flag) | Third-party |
| 3 | **X syndication** `GET https://cdn.syndication.twimg.com/tweet-result?id={id}&lang=en&token={syndication_token(id)}` | text, author, verification, media, quote; `TweetTombstone` = deleted | X's own embed CDN. No follower count. Reported flaky from cloud IPs. Token function is in `xref.py` (fuzz-tested identical to JS on 2,300 IDs). |
| 4 | **oEmbed** `GET https://publish.x.com/oembed?url=…&omit_script=1&dnt=true` | text, author, date inside HTML | Sanctioned, minimal |
| 5 (paid, opt-in) | **twitterapi.io** (~$0.15/1k tweets, $0.18/1k profiles, community info $0.0002/call) or **SocialData** (~$0.20/1k; community details incl. member count and rules) | Everything incl. **X Communities** | Off by default. Enabled with an env var and a daily spend cap. |
| 6 (paid, opt-in) | **Official X API pay-per-use** ($0.005/post read, $0.010/user read) | Terms-of-service-clean | The Free tier closed to new developers on 2026-02-06 |

- **Communities without paying:** the snowflake creation time only. Optionally try a link-preview User-Agent fetch of the community page for `og:` meta tags; this is untested and fragile.
- **Public Nitter is dead.** X sent cease-and-desist letters in Aug 2026, and only a few instances still work. Do not use it.
- **Caching:**
  - Tweets: **forever, first-seen snapshot kept**, because narrative tweets get deleted.
  - Profiles: 6–24 h TTL, keyed by **user ID** so renames are detected.
  - Communities: 24 h.
  - Tombstones: short TTL, then permanent.
  - Use a single-flight lock per tweet ID, because many tokens link the same celebrity tweet.

### 4.4 Knowledge sources (refreshed by cron, stored locally)

The analyzer must **never** depend on a live third-party call to produce a basic result. Knowledge is pulled in by scheduled jobs and kept in Postgres or packaged files.

| Knowledge | Source | Refresh | Use |
|---|---|---|---|
| **Known coins** (name, symbol, aliases, lore, categories, logo hash) | CoinGecko free Demo key (~100 calls/min, 10k/month). Categories `meme-token`, `solana-meme-coins`, `pump-fun`, AI-meme, politifi, cat/dog themed (verify IDs with `/coins/categories/list`). **Plus our own DB of every pump.fun coin seen.** Graduated coins and high-reply coins become "notable". | weekly (CoinGecko); continuous (own DB) | copycat/derivative detection, referent inheritance |
| **Trending topics** | Wikimedia pageviews: daily top-1000 for en.wikipedia, plus per-article history for spike ratio. Needs a proper User-Agent (200 req/min with one, ~10/min without). | daily (~1-day lag) | "references trending topic" |
| **Breaking-news check** | Google News RSS `https://news.google.com/rss/search?q=<query>+when:2d&hl=en-US&gl=US&ceid=US:en` | on demand, ≤1 req/s, cached | confirm a trend match; attach a headline as evidence (full-depth tokens only) |
| **Entities gazetteer** (memes, famous animals, celebrities, politicians, AI bots, countries) | Wikidata SPARQL, run offline or in a monthly job, P31 chains (e.g. Q2927074 "Internet meme"), with aliases and short descriptions. *Built:* `tokensage/sources/wikidata.py`, packaged snapshot from `scripts/build_gazetteer.py`, monthly refresh into `entity` by the knowledge cron; full depth adds a cached Wikipedia search for name spans it does not cover (`engine/wikilookup.py`) | monthly | referent detection, categories |
| **Common-noun classes** (animals, foods, objects) | WordNet hyponym sets, **precomputed offline** into JSON (do NOT load NLTK WordNet at runtime: ~294 MB RSS) | build-time | category rules |
| **Slang lexicon** | Hand-curated YAML (~200 entries), optionally seeded from Wiktionary slang senses (CC BY-SA, attribute) | by hand | ticker and name meaning |
| **Emoji meanings** | Unicode CLDR `annotations.json` (shipped as a file) | per release | emoji → keywords |
| **Meme template images** | Hand-curated small set; **store hashes only** | by hand | image template match |
| Urban Dictionary (unofficial `api.urbandictionary.com/v0/define`) | unofficial, noisy, offensive | on demand, low weight | last-resort unknown-word lookup; require thumbs_up > 100 and a ratio > 2; never display raw text unfiltered |

Do **not** use:
- **Know Your Meme**: they explicitly forbid scraping.
- **pytrends**: archived in April 2025.
- **Unauthenticated Reddit JSON**: reportedly blocked since May 2026.
- **spaCy NER**: tested poorly on this text, and costs ~150 MB.

---

## 5. Understanding engine design

A pipeline of pure, individually testable stages. Each stage reads the token context and appends **Evidence** records. Stages never call each other directly.

```
TokenContext(raw on-chain + metadata + optional enrichment)
  │
  ├─ S1 normalize        → normalized forms, markers, obfuscation flags, emoji keywords
  ├─ S2 segment          → name/ticker/description token candidates (meme-aware)
  ├─ S3 lexicon match    → slang, CLDR, WordNet classes, gazetteer entities (Aho-Corasick)
  ├─ S4 ticker↔name      → ticker explanation (subsequence / vowel-drop / acronym / lore)
  ├─ S5 known-coin match → copycat / derivative / template-family evidence
  ├─ S6 image            → hashes, near-dupes, OCR, palette, (optional) visual labels
  ├─ S7 X reference      → parse, snowflake timing, reuse count, (tier-gated) fetch + relation
  ├─ S8 trend match      → trending-entity hits (+ optional news confirmation)
  ├─ S9 aggregate        → per-label noisy-OR confidences, referent selection, flags
  └─ S10 render          → summary + caveats from templates
```

### 5.1 S1 Normalization (order matters; tested in research)
1. `unicodedata.normalize("NFKC")` folds full-width characters and ligatures.
2. Extract emoji (`emoji.emoji_list`) and map them to keywords with the CLDR annotations. **Use the keyword list:** 🐿's short name is "chipmunk", but its keywords include "squirrel".
3. Strip zero-width and Cf-category characters, keeping the ZWJ inside emoji sequences.
4. Run homoglyph detection with `confusable_homoglyphs`. A mixed-script or dangerous result produces an `obfuscation` flag. Then fold to ASCII with **`anyascii`** (ISC licence). **Not `Unidecode`**, which is GPL.
   - **Translate Han words before folding** (review recommendation 7). `data/cjk_words.yaml` (CC0, hand-written) maps the few hundred characters and words that recur in Chinese-meta coins to English, longest match first: `中国龙` → `china dragon`, `猫` → `cat`, `币圈大哥` → `crypto big bro`. Characters with no entry still fold to pinyin, and an entry of `""` drops a particle (`的`). The English tokens then reach the lexicon and WordNet passes like any Latin name, so a CJK coin gets a subject as well as the `regional_language` script flag, whose evidence detail lists the translations. The pinyin reading is kept in `Normalized.name_pinyin` for the ticker step only (`$MAO` for `猫`, `$ZGL` for `中国龙`). Descriptions are translated the same way.
5. **Detect version and derivative markers before stripping punctuation** (`2.0`, `v2`, `II`, `classic`, `og`, `real`, `baby`, `mini`, `inu`, `ai`, `wif`…).
6. Split camelCase *before* lowercasing (`AIAgentSupercycle` → `AI Agent Supercycle`).
7. Strip the `$`, `#` and `@` prefixes, but remember that `$WORD` in a description is a ticker mention.
8. Squeeze runs of 3+ repeated letters, generating both 2-letter and 1-letter variants (`moooon` → `moon`).
9. Undo leetspeak **only within mixed letter-digit tokens** (`p3anu7` → `peanut`), so real numbers like 420, 69 and 2.0 survive.

### 5.2 S2 Segmentation
- Use **`wordsegment`** (Apache-2.0, ~100 MB RSS) with **injected custom unigrams**: the slang lexicon, every known-coin name, famous-animal and celebrity names, and the daily trending titles.
  - Research result: stock wordsegment gets `dog wif hat`, while wordninja gives `dog w if hat`.
  - With custom vocabulary: `michi meow`, `elons dog`, `fwog wif hat`.
- `wordninja` (~28 MB) is the low-RAM fallback.
- **Do not use SymSpell compound segmentation.** It "corrects" slang into English (`dog with at`, `catfish at`).
- Generate several candidate segmentations and **score them by gazetteer hits**.

### 5.3 S3 Lexicon and gazetteer matching
- Use one **Aho-Corasick automaton** (`pyahocorasick`) over all gazetteer surface forms and aliases: entities, known coins, slang, WordNet classes and countries/demonyms. Build it at startup from Postgres and packaged files, and rebuild it when versions change.
- Detect script with the Unicode script property. A CJK, Cyrillic or other non-Latin name is a regional/language-meta signal.
- **Watch the context-sensitive terms.** `wif` is a *template* marker (the X-wif-hat family), **not** a dog keyword. The research prototype mislabelled "Trump wif Hat" as dog because of this.

### 5.4 S4 Ticker ↔ name explanation
Compute these features and pick the explanation that fits, in this priority order:
1. **Exact or known.** The ticker equals a known coin, or a known lore ticker (MEW ↔ "cat in a dogs world").
2. **Ticker equals a name token** or the compacted name.
3. **Subsequence.** The ticker is a subsequence of the compacted name and the first letters match (WIF ⊂ dogwifhat, PNUT ⊂ peanut).
4. **Vowel drop.** The name with vowels removed is within edit distance 1 of the ticker (peanut → pnt ≈ pnut).
5. **Acronym** of the name's words.
6. **Fuzzy.** `rapidfuzz.fuzz.partial_ratio(ticker.lower(), compact_name)`. Always apply `utils.default_process`: WRatio without it scores PNUT/Peanut at 36.
7. **Phonetic or baby-talk** (`jellyfish` Metaphone, an explicit `fw→fr` map) as a tiebreaker only.

If nothing fits, report "ticker unrelated to name" (itself mildly informative).

### 5.5 S5 Known-coin / copycat / derivative
- **Ticker base:** strip prefixes (`B`, `BABY`, `MINI`, `2`, `V2`) and suffixes (`INU`, `AI`, `2`, `20`) and compare to known tickers. Research found the prototype missed `BPNUT` because this step was absent.
- **Name:** use `rapidfuzz.process.cdist` / `extractOne` with `token_set_ratio`, **and** compare the space-stripped compact forms. `m00 deng classic` only matches "moo deng" via the compact form `moodeng ⊂ moodengclassic`.
- **Template families:** match character n-grams (`wifhat`) against templates such as X-wif-hat, Baby-X and X-2.0.
- **Image:** pHash/PDQ near-duplicates of a known logo (S6).
- **Same creator wallet** as earlier coins in our database: serial launcher.
- **Original vs clone:** compare creation times of same-name coins from the on-demand searches (§4.1) and our database. The earliest one is the likely original; report this coin's rank ("3rd of 41 `$PNUT` coins in 24 h") when the corpus (§6.6) is enabled.
- **Referent inheritance:** a derivative inherits the parent coin's referent and categories (e.g. "dogwifhat 2.0" → derivative + animal/dog + meme-template), with reduced confidence.

### 5.6 S6 Image analysis
- **Decode safely** (§10):
  - Sniff the type from magic bytes.
  - Set Pillow's `MAX_IMAGE_PIXELS` and use `draft()`.
  - Downscale to 512 px RGB.
  - For animated GIF or WebP, sample frames 0, n/2 and n−1.
- **Perceptual hashes** (`ImageHash`: pHash + dHash, ~3 ms; plus **PDQ** via `pdqhash` for flip/rotation robustness with dihedral variants).
  - Research thresholds, from synthetic images; **retune on real logos**:
    - pHash ≤ 8/64: same image.
    - 9–14: edited, flag for review.
    - Mirroring breaks pHash (distance 28), so also hash the mirrored image or use PDQ dihedral (threshold ~31/256).
- **Near-duplicate index:** store 64-bit pHash as `BIGINT`. Search the most recent N days in memory with a numpy XOR+popcount scan (~5–10 ms per 1M hashes), and use multi-index hashing (4×16-bit chunks) in Postgres for older history. Compare against:
  1. every pump.fun image TokenSage has seen (analysed tokens, plus the corpus if §6.6 is enabled);
  2. famous-coin logos;
  3. meme-template hashes.
- **OCR** with **RapidOCR** (`rapidocr_onnxruntime`, Apache-2.0, ~130 MB RSS, 180–300 ms per image; read `$PNUT` at 0.97–1.0 confidence on test images). Feed the OCR text back through S1–S5. A ticker in the image that differs from the metadata ticker is a copycat signal. Prefer it over Tesseract, which is worse on stylized logo text and would force apt packages.
- **Dominant colours:** Pillow `quantize(5)` (~1 ms), mapped to colour names. A weak cue (green + frog → Pepe-like), and nice in the UI.
- **AI-generation metadata:**
  - Check PNG `tEXt` `parameters`/`prompt` chunks, EXIF `Software`, and C2PA. A Stable Diffusion prompt chunk is a free textual description of the image.
  - Absence of these markers means nothing.
- **No NSFW / image moderation.** This is an owner decision: TokenSage analyses meaning, and the consumer app decides what to show. Do not add an NSFW classifier.
- **Optional visual labels (flag `ENABLE_CLIP`, needs a 2 GB worker):**
  - CLIP ViT-B/32 **vision tower only** in ONNX (~0.34 GB file, ~0.5 GB RSS), with **text-label embeddings precomputed offline** for ~300 prompts ("a dog wearing a hat", "Pepe the frog meme", "a squirrel", "a baby hippo", "pixel art", "a photograph of food"…).
  - Report labels only above an absolute cosine threshold (~0.25–0.28; tune it), marked "visual guess".
  - Embeddings also give semantic near-duplicate detection.
  - MobileCLIP-S0 is smaller and better, but its Apple licence needs review.
  - **This is local inference, not an external AI API**, and the engine must work fully without it.

### 5.7 S7 X reference analysis
Signals, with research-backed meanings:

| Signal | Interpretation |
|---|---|
| tweet time < token time | **narrative source** (coin made *about* this tweet); a gap of minutes to hours is typical |
| tweet after launch, author looks like the project account | launch announcement; weight by account age and followers |
| author is a big or verified account unrelated to the creator | **borrowed narrative** (`relation = narrative_reference`); never call it "official" |
| URL handle ≠ fetched author | **spoof attempt**: strong red flag |
| tweet mentions the ticker, CA or pump.fun link | direct link; rare and strong if the author is notable |
| `verified_type` Business/Government vs paid blue check | gold or grey checks are strong; blue is weak |
| account joined days ago, few posts | disposable dev account |
| `username_changes.count` > 0 (FxTwitter `/about`) | **recycled or bought account**: strong rug signal |
| tombstone, 404 or suspended | deleted narrative or banned account; keep the cached copy |
| community created minutes before the token | purpose-built shell community: weak or neutral |
| search or hashtag link instead of an account | no real socials: weak negative |
| same tweet, handle or community linked by N other tokens | narrative farming or copycat swarm |

Tweet text is fed back through S1–S5 and S8. **The tweet is often the "meaning"** (e.g. the news post that inspired the coin).

### 5.8 S8 Trend matching (deterministic)
1. **Daily job:**
   - Pull the Wikipedia top-1000, compute the spike ratio as views(day) ÷ median(prior 30 days), and keep articles with a spike > 3.
   - Add their Wikidata aliases.
   - Add n-grams from Google News top-story headlines.
   - Store all of this as `trend_terms` with `first_seen` and `score`.
2. **Per token:** match the candidate phrases (segmented name, ticker expansions, description capitalised spans, tweet text spans, emoji keywords) with Aho-Corasick for exact matches and rapidfuzz `token_set_ratio` ≥ 90 for fuzzy ones.
3. **Weight** by term specificity, using IDF over our token history: "trump" appears in thousands of tokens, so a match on it says little about a *new* event. Also weight by recency.
4. **Full-depth tokens only:** confirm with one cached Google News RSS query and attach the top headline.

### 5.9 S9 Aggregation and calibration
- Each rule emits `Evidence(kind, label, weight, detail, source, url?)`.
- **Per-label confidence** is a noisy-OR, `1 − Π(1 − wᵢ)`, capped, with:
  - a **source-diversity bonus**: text, image, X and trend agreeing beats three text rules;
  - **conflict penalties**: e.g. a dog keyword plus a "cat" visual label lowers both.
- **Referent selection is separate from categories.** Choose the best entity among known coins, gazetteer entities, trend terms and meme templates, each with its own confidence. Ambiguity (top two within 0.1) produces a caveat.
- **Starting weights:**

  | Evidence | Weight |
  |---|---|
  | gazetteer hit in the name | 0.6–0.8 |
  | gazetteer hit only in the description | 0.3–0.5 |
  | emoji keyword | 0.3 |
  | pHash template or logo match | 0.8 |
  | visual label | 0.2–0.5 |
  | known-coin copy | 0.9 |

- **Calibration (Phase 6):**
  - Hand-label 300–500 real tokens with a small internal labelling page (part of the debug pages, admin key only).
  - Fit per-rule weights by logistic regression (scikit-learn, still classical).
  - Check reliability curves, so that "0.8" means right about 80% of the time.
  - Version the weights.

### 5.10 Taxonomy (multi-label; keep it in a YAML config, not code)
`animal/{dog,cat,frog,monkey,hippo,squirrel,bird,bear_bull,fish,other}` · `meme_template/{pepe_wojak_chad,x_wif_hat,chill_guy,npc,brainrot,copypasta,other}` · `ai_agent` · `political` · `celebrity/{elon,musician,athlete,streamer_kol,other}` · `news_event` · `food_object_abstract` · `regional_language` · `crypto_native/{slang,pumpfun_meta,cto,utility_claim}` · `derivative` (with subtypes `copycat`, `template_family`, `sequel`, `homoglyph_spoof`, `logo_reuse`) · `humor_crude_offensive`.

---

## 6. System architecture on Render

### 6.1 Services

```
                 ┌──────────────────── Render (one region, e.g. oregon) ───────────────────────┐
 consumer app ───┼─▶ [web] tokensage-api  (FastAPI, Starter 512MB, always on)                  │
  (API key)      │     • validates CA, serves cached analysis, enqueues jobs, waits ≤ N s     │
                 │     • no heavy imports (no OCR/ONNX) so it stays small and fast             │
                 │                 │  Postgres: job queue + cache + knowledge                  │
 Solana RPC ◀────┼── [worker] tokensage-analyzer (Starter 512MB → Standard 2GB if CLIP/OOM)    │
 IPFS / hosts ◀──┼──   • resolve CA → metadata → engine stages → write analysis → NOTIFY       │
 X mirrors ◀─────┼──   • background retries (unresolved metadata, deleted-tweet checks)       │
                 │ [worker] tokensage-corpus  (OPTIONAL, §6.6; can share the analyzer process) │
                 │ [cron] tokensage-knowledge   (daily: trends; weekly: known coins)          │
                 │ [cron] tokensage-maintenance (hourly: retries, refresh, GC)                │
                 │ [postgres] tokensage-db (basic-256mb → basic-1gb)                          │
                 └─────────────────────────────────────────────────────────────────────────────┘
```

- **Why a separate analyzer worker:**
  - Image decoding and OCR spike RAM and CPU. If the analyzer runs out of memory, Render restarts it, but the API keeps answering (cached results, `202 pending` for the rest) instead of dropping every in-flight request.
  - The API process stays at ~150 MB and responds in milliseconds for cache hits.
  - **Budget option:** set `INLINE_ANALYZER=true` to run the analyzer inside the web process with concurrency 1. This saves $7/month and is fine for development and very low traffic, but an OOM then takes the API down too. Keep the boundary clean in code either way: the API only talks to the analyzer through the job table.
- **Coordination goes through Postgres** (workers and cron jobs have no inbound network on Render):
  - a `job` table claimed with `SELECT … FOR UPDATE SKIP LOCKED`;
  - `LISTEN/NOTIFY`: the API listens on `job_done`, and workers listen on `job_new`.
  - This avoids paying for Key Value in v1. Add it later only for heavier caching or multi-instance rate limits.
- **Deploy overlap:** during a deploy the old and new instances both run for ~60 s. So:
  - all writes are idempotent (`INSERT … ON CONFLICT … DO UPDATE`);
  - jobs are claimed with leases (`locked_until`) and are retried after a lease expires.
- **SIGTERM:** stop claiming, finish or release claimed jobs, close sockets, exit 0. Set `maxShutdownDelaySeconds: 60`.

### 6.2 Request flow for `GET /v1/tokens/{ca}`

1. **Authenticate** the API key and apply its rate limit (§6.4).
2. **Validate the CA** (§4.1 step 1). Invalid → `400` immediately.
3. **Cache check.** If an analysis exists at the requested depth (or deeper) and is fresher than `max_age` (§6.3), return it: `200`, `status: "complete"`.
4. **Enqueue** a job `(kind=analyze, mint, depth)`. The unique constraint makes concurrent requests for the same CA share one job (single-flight).
5. **Wait** up to `wait` seconds (default 10, max 25) for the `job_done` notification.
   - Done in time → `200` with the analysis. If some sources failed, `status: "partial"`, the `errors` list says which, and the caveats explain the effect.
   - Not done → `202` with `status: "pending"`, a `job_id`, and `Retry-After`. The consumer polls `GET /v1/jobs/{job_id}` or simply repeats the same `GET`. If a stale analysis exists, include it in the `202` body under `stale_analysis` so the consumer has something to show.
6. **Analyzer** (in the worker):
   1. resolve the CA on-chain (§4.1); if it is not a token mint, finish the job with that error;
   2. fetch the metadata JSON (§4.2) and image;
   3. run S1–S10 for the requested depth;
   4. write `analysis`, mark the job done and `NOTIFY job_done`.

**Latency targets** (to confirm with the consumer app's needs, §13):

| Case | Target |
|---|---|
| cache hit | p95 < 100 ms |
| cold `depth=basic` | p95 < 4 s (dominated by the IPFS fetch) |
| cold `depth=full` | p90 < 20 s (X fetch chain, OCR, news check) |

### 6.3 Depth, freshness and caching

Analysis depth is chosen **per request** by the consumer:

| Depth | Work | Cost |
|---|---|---|
| **basic** | resolve; metadata; S1–S5 on name, ticker, description; X link parse, snowflake time and reuse count (no X fetch); image fetch + hashes + near-duplicates; on-demand copycat lookup; S9/S10 | ~300 ms CPU + a few fetches; never any paid call |
| **full** (default) | everything in basic, plus OCR, the X fetch chain (§4.3), trend matching and news confirmation, optional CLIP | seconds; the paid X fallback only here, under a daily cap |

**What never changes and is cached forever:**
- on-chain name, symbol, uri, creator and creation time;
- the metadata JSON (by CID);
- image hashes and OCR (by image CID or content hash);
- the first-seen snapshot of a linked tweet.

**What changes, and the default `max_age` before a request triggers re-analysis:**

| Token age | Default `max_age` | Why |
|---|---|---|
| < 1 hour | 5 min | copycat counts, trends, X reuse and bonding-curve state move fast |
| 1 hour – 7 days | 1 hour | |
| > 7 days | 24 hours | |

- The consumer can override with `?max_age=<seconds>` or force a recompute with `?refresh=true`. `refresh` is rate-limited more strictly, since it skips the cache.
- **Re-analysis reuses the cached immutable parts.** Only the changing signals (curve state, copycat counts, X reuse, trend match, X profile) are recomputed, so a refresh is cheap.
- Each re-analysis gets a new `analysis.version`. Keep old versions for a while so a change in conclusion can be audited.

### 6.4 API specification (v1)

All paths are under `/v1`. FastAPI serves the OpenAPI schema at `/openapi.json`, so the consumer app can generate a typed client. Every response is JSON.

**Authentication:** `Authorization: Bearer <api_key>`.
- v1 can read keys from an env var (`API_KEYS`, comma-separated `name:key` pairs). Move them to the `api_key` table (stored as SHA-256 hashes) when there is more than one consumer.
- Each key has its own rate limit and daily quota of `depth=full` and `refresh=true` calls. A token bucket in process memory is enough while the API runs as one instance.

**Endpoints:**

| Method and path | Purpose |
|---|---|
| `GET /v1/tokens/{ca}` | The main call. Query: `depth=basic\|full` (default `full`), `wait=0..25` (default 10), `max_age=<s>`, `refresh=true\|false`, `include=evidence,raw` (evidence is included by default; `raw` adds the raw metadata). |
| `POST /v1/tokens:batch` | Body `{"cas": [... up to 50], "depth": "basic"}`. Returns one entry per CA: cached analyses immediately, `pending` + `job_id` for the rest. For bulk prefetching. |
| `GET /v1/jobs/{job_id}` | Job status: `pending\|running\|done\|failed`, plus the analysis when done. |
| `GET /v1/meta` | Versions (`schema_version`, rules, lexicon, known-coins date), the full category taxonomy, and the list of flag codes with descriptions, so the consumer can map labels without hard-coding them. |
| `GET /healthz` | Liveness, no DB call (Render health check). |
| `GET /readyz` | DB reachable, queue depth, circuit-breaker state of each upstream source, today's paid X spend. Protected by an admin key. |

**Example call from the consumer app:**

```bash
curl -s -H "Authorization: Bearer $TOKENSAGE_KEY" \
  "https://tokensage-api.onrender.com/v1/tokens/3arUrpH3nzaRJbbpVgY42dcqSq9A5BFgUxKozZ4npump?depth=full&wait=15"
```

Recommended client behaviour: on `200`, use the result; on `202`, retry the same URL after `Retry-After` seconds (up to ~60 s total); on `429` or `503`, back off as told; never retry `400`/`404`/`422`.

**Response envelope** (`GET /v1/tokens/{ca}` and job results):

```jsonc
{
  "ca": "3arUrpH3nzaRJbbpVgY42dcqSq9A5BFgUxKozZ4npump",
  "status": "complete",              // complete | partial | pending | failed
  "depth": "full",
  "analysis": { /* the §3 Analysis document */ },
  "freshness": { "analyzed_at": "…", "age_s": 42, "max_age_s": 300, "from_cache": true },
  "errors": [ { "source": "x.fxtwitter", "code": "timeout" } ],   // upstream problems behind a "partial"
  "job_id": null,                    // set when status is pending
  "request_id": "…"                  // echo in logs for support
}
```

**Errors** use one shape: `{"error": {"code": "invalid_ca", "message": "…", "request_id": "…"}}`.

| HTTP | `code` | When |
|---|---|---|
| 400 | `invalid_ca` | not base58 or not 32 bytes |
| 401 | `unauthorized` | missing or unknown API key |
| 404 | `token_not_found` | the address has no account on-chain (or not yet visible) |
| 422 | `not_a_token_mint` | the address exists but is not an SPL / Token-2022 mint (e.g. a wallet) |
| 422 | `not_pumpfun` | only when `ACCEPT_NON_PUMP=false` |
| 429 | `rate_limited` | per-key limit or quota; includes `Retry-After` |
| 503 | `overloaded` | job queue above its limit, or the RPC provider is down; includes `Retry-After` |

Upstream failures *after* the token is resolved (IPFS down, X blocked, OCR crash) are **not errors**: they produce `status: "partial"` with the problem listed in `errors` and explained in `caveats`.

**Images:** the API never proxies image bytes (Render's Hobby workspace includes only 5 GB/month of bandwidth). `analysis.image.source_url` carries the gateway URL. There is no NSFW screening; the consumer app decides whether and how to display images.

**Optional, later:** a `callback_url` on `POST /v1/tokens:batch`. When a job finishes, TokenSage POSTs the envelope to that URL, signed with an HMAC-SHA256 header using a per-key secret. Only HTTPS, and the callback host goes through the same SSRF guard as any fetch (§10).

**Debug page (optional):** `GET /debug/tokens/{ca}` renders the analysis and evidence as plain HTML for the developer, behind the admin key. It is a tool for building the rules, not a product surface.

### 6.5 Memory budget (estimates from research; profile in Phase 0/3)

| Component | RSS |
|---|---|
| Python + FastAPI/asyncpg | 80–150 MB |
| wordsegment | ~100 MB (wordninja ~28 MB) |
| rapidfuzz/emoji/anyascii/confusables + gazetteers | ~40 MB |
| Pillow + ImageHash + numpy/scipy | ~60 MB |
| onnxruntime base | ~44 MB |
| RapidOCR | ~130 MB |
| CLIP B/32 vision | ~500 MB |

- **Analyzer without CLIP:** ~300–350 MB, which fits on 512 MB with care. Process one image at a time, load OCR lazily, and cap the image pixel count. If it runs out of memory, move the analyzer to Standard (2 GB, $25).
- **API:** ~150 MB. It must not import the analysis modules.

### 6.6 Optional corpus ingester (improves copycat detection)

Without a corpus, TokenSage only knows the coins it has been asked about, the known-coins table and what the on-demand search endpoints return (§4.1). That is enough for v1. A corpus makes these answers much better:
- "There are 41 other `$PNUT` coins created in the last 24 hours; this one is the 3rd."
- "This is the original: the earliest coin with this name and logo."
- "This creator wallet launched 17 coins this week."
- "This tweet is linked by 37 other coins."

**What it does:** listen to a free feed (RPC `logsSubscribe` or PumpPortal, §4.1) and store each `CreateEvent` (mint, name, symbol, uri, creator, timestamp) in `token`. Optionally fetch each metadata JSON too (small; it gives the socials for tweet-reuse counts), and optionally hash each image, at the cost of ~30k image downloads per day.

**What it does not do:** analyse the tokens. Full analysis still happens only when the consumer asks.

**Operating notes:**
- One websocket per feed. Ping about every 20 s. Reconnect with exponential backoff plus jitter, and resubscribe.
- On reconnect, backfill the gap with the high-water mark in `feed_state`, using `getSignaturesForAddress` on the pump program or the frontend API's `/coins?sort=created_timestamp`.
- Record which feed saw each mint (`seen_by`) to measure coverage.
- Insert with `ON CONFLICT (mint) DO NOTHING`.
- It can run as a task inside the analyzer worker (no extra cost) or as its own Starter worker ($7) if it competes for CPU.
- Garbage-collect corpus rows of dead tokens after ~90 days, keeping only (name, symbol, creator, image hash, created_at) for history.

---

## 7. Data model (Postgres 17)

```sql
create table token (
  mint text primary key,
  name text, symbol text, uri text,
  creator text, bonding_curve text, token_program text, quote_mint text,
  is_pumpfun boolean not null,                            -- bonding-curve account exists
  is_mayhem boolean, created_at timestamptz,              -- null if it could not be determined (§4.1)
  created_at_source text,                                 -- create_event | frontend_api | rpc_history
  launcher text,                                          -- metadata.createdOn or uri host
  seen_by text[] not null default '{}',                   -- {'request','rpc','pumpportal','search'}
  first_seen_at timestamptz not null default now()
);
create index on token (created_at desc);
create index on token (upper(symbol));
create index on token (creator);

create table token_metadata (
  mint text primary key references token on delete cascade,
  status text not null,              -- ok | unresolved | invalid | pending
  content_key text,                  -- ipfs CID or sha256 of body
  description text, image_url text, twitter text, telegram text, website text,
  attempts int not null default 0, next_retry_at timestamptz,
  raw jsonb, fetched_at timestamptz
);

create table token_market (        -- latest bonding-curve / market snapshot (short TTL)
  mint text primary key references token on delete cascade,
  complete boolean, curve_progress real, usd_market_cap double precision,
  reply_count int, hidden boolean, is_banned boolean,
  updated_at timestamptz
);

create table image (                -- keyed by content, shared across tokens
  content_key text primary key,      -- CID or sha256
  phash bigint, dhash bigint, phash_mirror bigint, pdq bytea,
  ocr text[], palette text[], labels jsonb, clip vector null, -- pgvector optional
  width int, height int, animated boolean, analyzed_at timestamptz
);
create index on image (phash);

create table x_ref (                -- parsed link per token
  mint text primary key references token on delete cascade,
  kind text, tweet_id text, community_id text, handle text, user_id text,
  object_time timestamptz            -- snowflake-decoded
);
create index on x_ref (tweet_id);
create index on x_ref (lower(handle));
create index on x_ref (community_id);

create table x_tweet   (tweet_id text primary key, first_snapshot jsonb, latest jsonb,
                        status text, source text, fetched_at timestamptz);
create table x_profile (user_id text primary key, handle text, snapshot jsonb,
                        source text, fetched_at timestamptz);
create table x_profile_history (user_id text, handle text, followers int, seen_at timestamptz);

create table analysis (
  mint text references token on delete cascade,
  version int not null,              -- increments on re-analysis
  depth text not null,               -- basic | full
  doc jsonb not null,                -- the §3 contract
  referent text, categories text[], flags text[],
  created_at timestamptz not null default now(),
  primary key (mint, version)
);
create index on analysis using gin (categories);

create table known_coin (id text primary key, chain text, mint text, name text, symbol text,
  aliases text[], lore text, categories text[], logo_phash bigint, source text, updated_at timestamptz);
create table entity (id text primary key, label text, aliases text[], kind text, description text,
  source text, popularity real);
create table trend_term (term text, source text, score real, spike real, first_seen date,
  day date, primary key (term, source, day));

create table job (
  id bigserial primary key, kind text not null,          -- analyze | retry_metadata | refresh_x ...
  mint text, depth text, priority int not null default 100,   -- API requests jump the queue
  status text not null default 'pending',               -- pending | running | done | failed
  run_after timestamptz not null default now(), attempts int not null default 0,
  locked_until timestamptz, last_error text, result_version int,
  requested_by text,                                      -- api key name, for quotas and debugging
  created_at timestamptz not null default now(), finished_at timestamptz
);
-- single-flight: one open job per (kind, mint, depth)
create unique index on job (kind, mint, depth) where status in ('pending','running');
create index on job (priority, run_after) where status = 'pending';

create table api_key (name text primary key, key_sha256 text unique not null,
  rate_per_min int not null default 60, full_per_day int not null default 2000,
  refresh_per_day int not null default 200, callback_secret text, created_at timestamptz default now(),
  revoked_at timestamptz);
create table api_usage (key_name text, day date, requests int, full_calls int, refreshes int,
  primary key (key_name, day));

create table feed_state (feed text primary key, high_water jsonb, updated_at timestamptz);
create table source_health (source text primary key, state text, failures int, open_until timestamptz,
  calls_today int, spend_today_usd numeric);
```

**Retention:**
- Without the corpus, the database grows with the consumer's request volume: small.
- With the corpus (§6.6), about 30k token rows/day, roughly 11M rows/year. Keep only the slim columns for dead corpus tokens after ~90 days.
- Keep the latest `analysis` version per (mint, depth) forever, and older versions for 30 days.
- Use `jsonb` compression. Watch disk growth on `basic-256mb`; storage is $0.30/GB/month.

---

## 8. Deployment: Render Blueprint

### 8.1 `render.yaml` (starting point; validate with `render blueprints validate`)

```yaml
previews:
  generation: off

envVarGroups:
  - name: tokensage-shared
    envVars:
      - key: LOG_LEVEL
        value: info
      - key: HTTP_USER_AGENT
        value: "TokenSage/0.1 (+https://github.com/sollama58/TokenSage)"
      - key: ACCEPT_NON_PUMP
        value: "true"
      - key: ENABLE_CLIP
        value: "false"
      - key: ENABLE_CORPUS
        value: "false"            # §6.6; runs as a task inside the analyzer worker when true
      - key: ENABLE_PAID_X
        value: "false"
      - key: PAID_X_DAILY_USD_CAP
        value: "1.00"
      - key: IPFS_GATEWAYS
        value: "https://pump.mypinata.cloud,https://dweb.link,https://ipfs.io,https://gateway.pinata.cloud"

services:
  - type: web
    name: tokensage-api
    runtime: docker
    plan: starter                 # NOT free: a free service sleeps after 15 min idle and the consumer app would hit ~1 min cold starts
    region: oregon
    dockerfilePath: ./Dockerfile
    # no dockerCommand: env TOKENSAGE_ROLE=api selects the entrypoint (tokensage/run.py)
    preDeployCommand: alembic upgrade head
    healthCheckPath: /healthz
    autoDeployTrigger: commit
    buildFilter:
      paths: ["tokensage/**", "migrations/**", "data/**", "Dockerfile", "pyproject.toml", "uv.lock", "render.yaml"]
      ignoredPaths: ["**/*.md", "tests/**", "docs/**"]
    envVars:
      - fromGroup: tokensage-shared
      - key: DATABASE_URL
        fromDatabase: { name: tokensage-db, property: connectionString }
      - key: API_KEYS              # "consumer:<long random key>" pairs, comma-separated
        sync: false
      - key: ADMIN_KEY
        generateValue: true
      - key: INLINE_ANALYZER       # "true" = run the analyzer in this process (dev / tiny traffic)
        value: "false"
      - key: WEB_CONCURRENCY
        value: "1"

  - type: worker
    name: tokensage-analyzer
    runtime: docker
    plan: starter                 # → standard (2 GB) when ENABLE_CLIP=true or on OOM
    region: oregon
    dockerfilePath: ./Dockerfile
    # no dockerCommand: env TOKENSAGE_ROLE=worker selects the entrypoint (tokensage/run.py)
    numInstances: 1
    maxShutdownDelaySeconds: 60
    autoDeployTrigger: commit
    buildFilter:
      paths: ["tokensage/**", "data/**", "Dockerfile", "pyproject.toml", "uv.lock", "render.yaml"]
      ignoredPaths: ["**/*.md", "tests/**", "docs/**"]
    envVars:
      - fromGroup: tokensage-shared
      - key: DATABASE_URL
        fromDatabase: { name: tokensage-db, property: connectionString }
      - key: SOLANA_RPC_URL        # HTTPS RPC, e.g. Helius free (includes the api key)
        sync: false
      - key: SOLANA_WS_URL         # only needed when ENABLE_CORPUS=true
        sync: false
      - key: COINGECKO_API_KEY
        sync: false
      - key: TWITTERAPI_IO_KEY     # optional paid X fallback
        sync: false

  - type: cron
    name: tokensage-knowledge
    runtime: docker
    plan: starter
    region: oregon
    schedule: "17 3 * * *"        # daily 03:17 UTC: trends; known coins weekly inside the job
    dockerfilePath: ./Dockerfile
    # no dockerCommand: env TOKENSAGE_ROLE=knowledge selects the entrypoint (tokensage/run.py)
    envVars:
      - fromGroup: tokensage-shared
      - key: DATABASE_URL
        fromDatabase: { name: tokensage-db, property: connectionString }
      - key: COINGECKO_API_KEY
        sync: false

  - type: cron
    name: tokensage-maintenance
    runtime: docker
    plan: starter
    region: oregon
    schedule: "7 * * * *"         # hourly: requeue expired leases, retry unresolved metadata, GC
    dockerfilePath: ./Dockerfile
    # no dockerCommand: env TOKENSAGE_ROLE=maintenance selects the entrypoint (tokensage/run.py)
    envVars:
      - fromGroup: tokensage-shared
      - key: DATABASE_URL
        fromDatabase: { name: tokensage-db, property: connectionString }

databases:
  - name: tokensage-db
    plan: basic-256mb             # 'free' expires after 30 days, so never use it for real data
    region: oregon
    databaseName: tokensage
    user: tokensage
    postgresMajorVersion: "17"
    ipAllowList: []               # private network only; add your IP CIDR temporarily for psql
```

The consumer app calls `https://tokensage-api.onrender.com/v1/...` (or a custom domain added under `domains:`). If the consumer app also runs on Render in the same region, it can instead call the API over the private network at the internal hostname and port (wire it with `fromService: {type: web, name: tokensage-api, property: hostport}` in the consumer's Blueprint). The API key is still required.

### 8.2 Render gotchas (from research; each one has bitten people)
1. **All resources go in one region.** Internal DB URLs and the private network require it, and the region is immutable.
2. **`fromDatabase.connectionString` is `postgres://…`.**
   - Rewrite it to `postgresql+asyncpg://` for SQLAlchemy, or pass it straight to `asyncpg`.
   - asyncpg rejects `sslmode` in the URL. The internal URL doesn't need SSL.
3. **`sync: false` secrets prompt only at the first Blueprint creation.** Ones added later must be set by hand in the Dashboard.
4. **Blueprint sync overwrites conflicting Dashboard edits.** Resources deleted in the Dashboard are recreated.
5. **`buildFilter`:** always give both `paths` and `ignoredPaths`; a missing list is treated as empty on sync.
6. **Python version:**
   - The native default is now 3.14.
   - **Pin 3.12 in the Dockerfile** (onnxruntime and opencv wheels).
   - `PYTHON_VERSION` doesn't apply to Docker builds.
7. **Docker:**
   - Bind `0.0.0.0:$PORT` (default 10000).
   - Don't put per-service start commands in `dockerCommand`. The first deploy exited with status 127 (command not found) on the `sh -c "exec …"` form. Instead the image has one exec-form `CMD` (`/app/.venv/bin/python -m tokensage.run`) and each service sets `TOKENSAGE_ROLE` (`api`, `worker`, `knowledge`, `maintenance`). Python reads `$PORT` itself, and as PID 1 it receives SIGTERM directly.
   - Builds are amd64 only.
8. **Bake models and data files into the image** at build time. Never download them at startup: the filesystem is ephemeral, and downloads slow health checks.
9. **Free web services sleep after 15 min** and take ~1 min to wake, which a calling application would see as timeouts. Use Starter for the API. Workers and cron jobs cannot be free.
10. **Cron** runs one at a time, has no retries and a 12 h maximum. Make jobs idempotent and resumable.
11. **Health check:** keep `/healthz` free of DB calls. A deploy is cancelled if the service isn't healthy within 15 min.
12. **Bandwidth:** the Hobby workspace includes 5 GB/month egress since the April 2026 repricing (third-party sources; verify). **Don't proxy images.**
13. **`preDeployCommand`** (migrations) is paid-plan only and runs on a separate instance.
14. **Logs** are kept 7 days on Hobby. Log structured JSON, and keep important history in Postgres (`source_health`, job errors).

### 8.3 Dockerfile sketch

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*          # libs for opencv-headless/rapidocr if needed
COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev
# Optional: fetch ONNX models at build time into /app/models (pinned URLs + sha256 check)
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
CMD ["/app/.venv/bin/python", "-m", "tokensage.run"]   # TOKENSAGE_ROLE picks the service
```

---

## 9. Implementation plan (phases with acceptance criteria)

**Tech stack:**
- **Runtime and web:** Python 3.12, `uv`, FastAPI (OpenAPI generated from pydantic models).
- **Data:** asyncpg with plain SQL (or SQLAlchemy Core), Alembic migrations.
- **Network:** `httpx` (async), `websockets` (corpus only), `pydantic` v2 for every external payload.
- **Solana:** plain JSON-RPC over `httpx` plus the reference decoders; `solders` is allowed for address math.
- **Testing and quality:** `pytest` + `respx` for HTTP fixtures, `ruff`, `mypy` (lenient), `structlog`.

### Phase 0: Reality check from Render (½–1 day). **Do first.**
Deploy a throwaway Starter worker (or use the Render shell) that runs `scripts/smoke_test.py`. Record the results in `docs/smoke-test-results.md`. The script checks:
- **CA resolution on the chosen RPC** for a list of ~30 test CAs: legacy and `create_v2` pump coins, graduated coins, a letsbonk coin, a plain SPL token, a wallet address, an invalid string. Check `getAccountInfo` on the mint and the bonding curve (`pump_ca.py`), and the on-chain name/symbol/uri for both token programs. Measure latency and credits per resolve. If on Helius, compare with DAS `getAsset`.
- **Creation time:** frontend-api `created_timestamp` vs the RPC-history method (how many pages for an active coin?).
- **IPFS gateways:** success rate and latency per gateway for 100 recent URIs; how many URIs are non-IPFS hosts.
- **`frontend-api-v3.pump.fun`:** `/coins-v2/{mint}` and `/coins/search` from Render, 200 vs 403/Cloudflare.
- **DexScreener search** for a ticker.
- **X:** the full curl list in `docs/research/02-x-twitter-access.md` §8 (FxTwitter, vxTwitter, syndication, oEmbed, crawler-UA community page).
- **Knowledge APIs:** CoinGecko categories, Wikimedia pageviews top, Wikidata search, Google News RSS, Urban Dictionary.
- **Corpus feeds** (only if §6.6 is wanted): `logsSubscribe` messages and bytes per second with buys and sells included, the monthly credit cost, and PumpPortal coverage versus RPC over 10 minutes.

**Accept when** every source is marked works / flaky / blocked from Render. Update §4 and §13 of this guide accordingly.

### Phase 1: Skeleton, API contract and deploy pipeline
- Repo layout (§12), `pyproject.toml`, Dockerfile, `render.yaml`, Alembic with the §7 schema, `/healthz`, and an empty worker loop with SIGTERM handling.
- **The API contract first:** pydantic models for the §3 Analysis and the §6.4 envelope and errors, all `/v1` routes returning stub data, API-key auth and rate limiting. Publish `/openapi.json` so the consumer app's developer can start integrating against stubs immediately.
- CI (GitHub Actions): ruff, mypy, pytest, plus a check that the OpenAPI schema hasn't changed incompatibly (diff against a committed `openapi.v1.json`).

**Accept when:**
- the Blueprint deploys cleanly from a fresh Render account and migrations run in preDeploy;
- the consumer app can call `GET /v1/tokens/{ca}` with its key and receive a schema-valid stub;
- invalid CAs return `400`, missing keys `401`.

### Phase 2: CA resolution, job queue and metadata
- The resolver (§4.1) with RPC calls and caching, including the `404`/`422` cases.
- Job queue with single-flight, leases, `LISTEN/NOTIFY` and the wait-or-202 flow (§6.2). `GET /v1/jobs/{id}`.
- Metadata and image fetcher with the gateway chain, CID cache and SSRF guard. Background retry of unresolved metadata.

**Accept when:**
- all Phase 0 test CAs resolve correctly (right error code for the non-token ones);
- 20 concurrent requests for the same CA create one job;
- metadata resolves for ≥ 95% of recent pump.fun CAs within the request budget;
- a worker restart mid-job loses nothing (the lease expires and the job is retried).

### Phase 3: Basic-depth understanding engine (the heart)
- S1–S5, S7 (parse, snowflake, reuse only), S9 and S10, with the §3 schema.
- **Image:** hashes and near-duplicates.
- On-demand copycat lookups (frontend-api search, DexScreener search, own DB).
- Packaged knowledge: the slang YAML, CLDR emoji, WordNet-derived class lists (built by a script in `scripts/`), and a seed `known_coin` table.
- **Golden test set** `tests/golden/*.yaml`: ≥ 60 hand-written cases covering every pattern in §2.2 and every pitfall in research. They run against the engine directly (raw name, symbol, description, link as input), so they need no network. Must include:
  - `dogwifhat`, `catwifhat`, `Trump wif Hat` (must NOT be dog)
  - `BPNUT`, `m00 deng classic`, `dogwifhat2.0`, `ʙᴀʙʏ ᴘɴᴜᴛ`
  - Cyrillic `Рepe`, `$Ｐ​ＮＵＴ` (full-width with zero-width), `p3anu7`, `🐿🥜`
  - `justachillguy`, `AIAgentSupercycle`, a CJK name, a URL-handle spoof, a reused tweet
- **Recorded end-to-end fixtures:** for ~20 real CAs, record the RPC, metadata and image responses (`tests/fixtures/`) so the whole CA → analysis path is tested offline.

**Accept when:**
- all golden cases and recorded end-to-end cases pass;
- p95 engine CPU for basic depth is under 300 ms;
- cold `depth=basic` p95 is under 4 s from Render;
- analyzer RSS stays under 400 MB on Starter.

### Phase 4: Full depth
- X fetch chain with circuit breakers, caching, single-flight and spend cap. The paid fallback stays off by default.
- OCR. Trend pipeline (the knowledge cron) and news confirmation.
- Freshness rules and partial re-analysis (§6.3).

**Accept when:**
- cold `depth=full` p90 is under 20 s;
- X fetch success ≥ 90% for tweet links on the free chain (or the documented reason it isn't);
- an upstream outage (simulate by blocking FxTwitter) yields `status: "partial"` with the right `errors` entry, never a 5xx;
- the trend match fires on a manufactured test (a known spiking article).

### Phase 5: Integration hardening
- `POST /v1/tokens:batch`, `GET /v1/meta`, `/readyz`, per-key quotas and usage counters, queue back-pressure (`503 overloaded`).
- Structured logs with `request_id`. An integration guide for the consumer app (`docs/API.md`) with example requests, every error code, and polling and retry advice.
- Optional: signed webhook callbacks.
- Load test: replay 1 hour of realistic traffic at 2× the consumer's expected rate.

**Accept when** the consumer app is integrated against the live service and the load test shows no errors beyond `202`/`429` and stable memory.

### Phase 6: Calibration and quality
- A labelling page (debug pages, admin key). Label 300–500 real tokens.
- Logistic-regression weight fit, reliability curve report, threshold tuning for pHash/PDQ/CLIP on real data. Version and store the weights.

**Accept when** top-1 category precision is ≥ 0.8 at the shown confidence, and the referent is correct on ≥ 70% of tokens where a human could identify one.

### Phase 7: Corpus ingester (optional, recommended)
- §6.6: feed listener as a task in the analyzer worker, `feed_state` and backfill, slim storage and GC. Copycat rank and creator-history signals switched on.

**Accept when** 24 h of running captures ≥ 95% of the creates seen by a reference sample, and the copycat rank for a popular ticker matches a manual check.

### Phase 8: Optional local models
- `ENABLE_CLIP`: the vision tower in ONNX, precomputed label embeddings, worker on Standard.
- MobileCLIP after a licence review.

**Accept when** the measured precision lift on the labelled set justifies the extra $18/month.

---

## 10. Security, safety and legal

- **SSRF:** metadata `uri`, `image` and `website` values are attacker-controlled URLs. The fetcher must:
  - allow only `https` (and the `ipfs://` rewrite);
  - resolve DNS and **reject private, loopback, link-local and metadata IP ranges** (incl. `169.254.169.254` and IPv6 equivalents), then connect to the resolved IP;
  - cap redirects at 3 and re-check each hop;
  - cap size and time;
  - never send cookies or secrets.
- **Decompression and image bombs:**
  - Set Pillow `Image.MAX_IMAGE_PIXELS` (e.g. 40M).
  - Check the header dimensions before decoding.
  - Limit GIF frames.
  - Process in a try/except with a per-job timeout.
- **Untrusted strings in responses:**
  - Names, descriptions, tweet text and OCR output are returned as data. Document in `docs/API.md` that **the consumer app must escape them** when displaying them.
  - URL fields (`website`, `telegram`, `twitter`, image URLs) are normalised, and only `https` URLs on non-private hosts are returned. `javascript:`, `data:` and anything unparsable become `null`, with the raw value available only under `include=raw`.
  - The debug pages use Jinja autoescape and a strict CSP header.
- **Image content:** there is no NSFW screening (owner decision); `docs/API.md` must say so plainly, so the consumer app knows images are unscreened. Never re-host or cache image bytes beyond processing. Store only hashes, OCR text and labels.
- **Abuse of the API:**
  - Every `/v1` call needs an API key. Use long random keys, store only their SHA-256, and compare in constant time.
  - Per-key rate limits and daily quotas (`full` and `refresh` cost more), and a global queue limit that returns `503 overloaded`.
  - CA validation happens before any network call, so junk input costs nothing.
  - Only HTTPS. Never log full API keys.
- **Secrets:** Render env vars only (`sync: false`), never in the repo or Docker `ARG`s.
- **Legal / ToS (decide consciously, document in README):**
  - **pump.fun frontend API:** undocumented; their ToS may forbid scraping. Keep it optional, throttled and server-side only.
  - **X:**
    - The ToS forbid scraping, with liquidated damages of $15k per 1M posts in 24 h.
    - FxTwitter and vxTwitter are third-party services, and commercial use is unclear.
    - The only clearly sanctioned paths are the official pay-per-use API and oEmbed.
    - At our lazy volume the practical risk is IP blocks, but **this is a product/legal decision for the owner**.
  - **Know Your Meme:** do not scrape.
  - **Licences:**
    - Use `anyascii`, not GPL `Unidecode`.
    - Wiktionary data is CC BY-SA (attribute). Wikidata is CC0.
    - MobileCLIP uses an Apple licence (review).
- **Not financial advice:** `GET /v1/meta` and `docs/API.md` state that flags and categories are informational only. The consumer app should surface that.

---

## 11. Cost estimate (Render Hobby workspace; verify at render.com/pricing)

| Item | Plan | $/month |
|---|---|---|
| API web | starter (always on; free would sleep) | 7 |
| Analyzer worker | starter → standard if CLIP or OOM (or `INLINE_ANALYZER=true` for $0 extra at very low traffic) | 7 → 25 |
| Cron × 2 | per-minute billing, $1 minimum each | ~2–4 |
| Postgres | basic-256mb + ~1–5 GB storage at $0.30/GB (more with the corpus) | ~7–8 |
| Solana RPC | Helius free: a cold resolve is 2–4 calls; the corpus feed is the expensive part (verify in Phase 0) | 0 (→ 49 Developer if needed) |
| Paid X fallback | off by default; roughly $0.30 per 1,000 `full` analyses that hit it | 0 |
| **Total v1** | | **≈ $23–27** (≈ $43–47 with a Standard worker) |

Costs scale with the consumer's request rate. Cache hits cost nothing beyond the always-on instances.

---

## 12. Suggested repository layout

```
TokenSage/
├── PROJECT_GUIDE.md            # this file (keep it updated)
├── README.md
├── render.yaml
├── Dockerfile
├── pyproject.toml / uv.lock
├── migrations/                 # alembic
├── data/                       # packaged knowledge (versioned)
│   ├── slang.yaml
│   ├── taxonomy.yaml
│   ├── cldr_annotations_en.json
│   ├── wordnet_classes.json    # generated by scripts/build_wordnet_classes.py
│   ├── templates.yaml          # meme templates + hashes, ticker/name markers
│   ├── cjk_words.yaml          # Han word -> English for Chinese-meta names (CC0)
│   └── known_coins_seed.yaml
├── tokensage/
│   ├── config.py               # pydantic-settings; all thresholds/weights here or in data/
│   ├── db.py
│   ├── net/                    # safe_fetch (SSRF guard), ipfs gateways, circuit breaker, single-flight
│   ├── resolve/                # ca.py (validate, PDAs), rpc.py, bonding_curve.py, onchain_metadata.py,
│   │                           # creation.py (creation time/creator), pump_event.py
│   ├── corpus/                 # OPTIONAL §6.6: rpc_logs.py, pumpportal.py, backfill.py
│   ├── sources/                # pumpfun_api.py, x_fx.py, x_vx.py, x_syndication.py, x_oembed.py,
│   │                           # x_paid.py, coingecko.py, wikimedia.py, wikidata.py, gnews.py
│   ├── engine/
│   │   ├── context.py          # TokenContext, Evidence, Analysis models (the §3 contract)
│   │   ├── normalize.py  segment.py  lexicon.py  ticker.py  known_coins.py
│   │   ├── image.py  xref.py  trends.py  aggregate.py  render_summary.py
│   │   └── pipeline.py         # orchestrates stages per depth (basic/full)
│   ├── queue.py                # job claim/lease/notify, single-flight
│   ├── worker.py               # analyzer loop (+ corpus task if enabled), SIGTERM handling
│   ├── jobs/                   # knowledge.py, maintenance.py (cron entrypoints)
│   └── api/                    # app.py, auth.py, ratelimit.py, routes_v1.py, schemas.py, debug/ (admin-only)
├── openapi.v1.json             # committed contract; CI fails on incompatible changes
├── scripts/                    # offline builders (wordnet classes, wikidata gazetteer), smoke_test.py
├── tests/
│   ├── golden/                 # hand-labelled meaning cases (yaml)
│   ├── fixtures/               # recorded payloads per test CA: RPC accounts, metadata JSON, images, X responses
│   └── test_*.py
└── docs/
    ├── API.md                  # integration guide for the consumer app (Phase 5)
    ├── research/               # the four research reports
    └── reference/              # tested reference code (port into tokensage/)
```

---

## 13. Open questions and verification checklist

Unless noted, these must be resolved by the Phase 0 smoke test.

1. **From the owner / consumer app:** expected request rate (average and peak), latency needs (is "202, poll again" acceptable, or must most calls finish synchronously?), whether `basic` or `full` should be the default depth, and whether non-pump.fun mints should be analysed or rejected (`ACCEPT_NON_PUMP`).
2. **From the owner:** is the corpus ingester (§6.6) wanted in v1? It improves copycat answers but adds feed bandwidth and storage.
3. How many RPC calls and credits does a cold resolve cost on the chosen provider? How often does the creation-time lookup need the RPC-history fallback, and how deep?
4. If the corpus is enabled: is `logsSubscribe` sustainable on a free RPC, and what is PumpPortal's real coverage versus RPC?
5. Does `frontend-api-v3.pump.fun` (`/coins-v2`, `/coins/search`) answer from Render IPs (Cloudflare)?
6. Which IPFS gateways work best from Render? Is `pump.mypinata.cloud` usable by third parties?
7. Do FxTwitter, vxTwitter, syndication and oEmbed answer from Render? Real rate limits? Is the commercial-use stance of FxTwitter acceptable to the owner?
8. Are the CoinGecko category IDs and Demo quotas as documented? Wikimedia 2026 rate limits with our UA?
9. Is the Render pricing after the April 2026 change as summarised (Hobby 5 GB egress, Starter $7, Postgres basic-256mb $6)?
10. What are the real pHash/PDQ/CLIP thresholds on real pump.fun logos? Does the ~18% near-duplicate rate reproduce?
11. How accurate is RapidOCR on stylized meme logos (only clean synthetic text was tested)?
12. **Owner decisions:** whether to enable the paid X fallback and its daily cap; the ToS risk appetite for unofficial X mirrors and the pump.fun frontend API.

---

## Appendix A: Reference code in `docs/reference/` (tested)

| File | What | Tests |
|---|---|---|
| `pump_ca.py` | `parse_ca()` (validates a CA, accepts pump.fun/explorer URLs); `bonding_curve_pda()` (pure-Python program-derived-address derivation, seeds `["bonding-curve", mint]`); `decode_bonding_curve()` (tolerant of older, shorter accounts) | `test_pump_ca.py`: PDA matches 7 real mint/curve pairs |
| `xref.py` | `parse_x_ref()` (18 URL shapes incl. communities, intents, t.co, spoofable status URLs); `snowflake_time()`; `syndication_token()` (V8 `toString(36)` port, fuzz-matched against Node on 2,300 IDs) | `test_xref.py` |
| `pump_event.py` | Tolerant Borsh decoder for `CreateEvent` (handles older, shorter layouts; reports unknown tail bytes) | `test_pump_event.py` against `create_events.txt`: 6 real `Program data:` lines from 2026, incl. non-IPFS launcher URIs and non-`pump` mints |
| `xurl.reference.mjs` | Original JS version of the X URL parser and token function | — |

Run them with: `cd docs/reference && python -m pytest -q`.

## Appendix B: Research reports
1. `docs/research/01-pumpfun-data-sources.md`: frontend APIs and real payloads, feeds and vendors, on-chain program and IDL details, metadata/IPFS, volume, pitfalls.
2. `docs/research/02-x-twitter-access.md`: official API pricing 2026, free mirrors with schemas, paid scrapers, communities, URL shapes, signals, tiered strategy, smoke-test commands.
3. `docs/research/03-understanding-techniques.md`: normalization, segmentation benchmarks, lexicons/APIs, fuzzy and phonetic matching, trend sources, taxonomy, image hashing/OCR/NSFW measurements (NSFW is out of scope; see §5.6), local models, explainable output, library table with licences and RAM.
4. `docs/research/04-render-platform.md`: Blueprint field reference, pricing, Python/Docker specifics, worker/cron/deploy semantics, memory, gotchas.

> **Research caveat:** the research sandbox could not reach pump.fun, IPFS gateways, Solana RPCs, X or its mirrors, or Wikimedia/CoinGecko directly. Findings come from official repos (pump-fun/pump-public-docs IDLs as of 2026-09-29, render-oss/skills), the source code of the relevant open-source tools, captured real payloads in public repos, and 2026 web sources. Hence Phase 0.
