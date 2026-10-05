# TokenSage — Project Guide

> **Audience:** the engineer or AI coding agent who will build TokenSage from an empty repo.
> **Status:** research complete, no code yet. Written 2026-10-05.
> **Companion material:**
> - `docs/research/` holds four detailed research reports with sources. This guide is the synthesis; go to the reports for the evidence behind any claim.
> - `docs/reference/` holds small, **tested** reference implementations: the X URL parser, the tweet-ID date decoder, the syndication token, and the pump.fun `CreateEvent` decoder with real captured events.

---

## 0. How to use this guide

1. **Read §1–§3 first.** They define what we are building and the domain knowledge it depends on.
2. **§4–§8 are the design:** data sources, understanding engine, architecture, data model, deployment.
3. **§9 is the build plan.** Follow the phases in order. **Phase 0 is a live smoke test from Render. Do not skip it.** None of the external endpoints could be tested live during research, because the research sandbox blocked them. Every free data source in this guide is "documented to work" but unconfirmed from Render's IPs.
4. **§13 lists open questions.** Resolve them as you go and update this file.

**Ground rules for the implementer:**
- **No external AI APIs.** No OpenAI, Anthropic, Gemini, Google Cloud Vision, Hugging Face Inference API, or any other hosted model call. Small models running **locally** on CPU inside our own container (ONNX) are allowed, but they are **optional, flag-gated layers** and never required for a useful result. The core engine is deterministic: rules, lexicons, gazetteers, fuzzy matching, perceptual hashing, OCR.
- **Every conclusion must carry evidence.** TokenSage explains *why* it thinks `$PNUT` refers to Peanut the Squirrel. A label with no evidence trail is a bug.
- **Untrusted input everywhere.** Token names, metadata JSON, image bytes, URLs and tweets are attacker-controlled (§10).
- **Tolerant parsers.** pump.fun, its unofficial APIs and the X mirrors change shape without notice. Validate, degrade gracefully, and log unknown shapes. Never crash the pipeline on one bad token.

---

## 1. Product definition

### 1.1 One-sentence goal
For every new pump.fun token, work out **what it is about**, explain that in plain language with confidence scores and evidence, and expose it through an HTTP API and a simple dashboard. The inputs are the token's name, ticker, description, image and linked X/Twitter content.

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
- No exhaustive X scraping. X is fetched only for tokens that pass a cheap filter (§4.4).

### 1.4 Constraints
- **Hosting:** Render, defined entirely in a Render Blueprint (`render.yaml`).
- **Budget:** low. Target about $15–40/month for v1 (§11).
- **Volume:** about **30,000 new pump.fun coins per day** in 2026, peaking near 42,000/day. That is a sustained ~0.35–0.5 creates/second, with bursts of 2–5/second. About 69% stop trading on launch day, and fewer than 2% graduate.
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
| **Toilet humour / NSFW / offensive** | — | Must be detected and hidden, never amplified |

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

Every analysed token produces one **Analysis** document. This is the product, and the API and dashboard just render it. Build the engine to this schema from day one, and version it.

```jsonc
{
  "schema_version": "1",
  "mint": "…", "created_at": "2026-10-05T12:00:00Z",
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
             "palette": ["#c87f3a"], "near_duplicates": [ … ], "labels": [], "nsfw": "safe" },
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
  "depth": "full",                    // "basic" | "full" (see §6.3 tiers)
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

### 4.1 New-token discovery (two free feeds, deduped on `mint`)

**Primary: Solana RPC `logsSubscribe` on the pump program, decoding `CreateEvent`.** This is authoritative, complete and vendor-neutral.

```json
{"jsonrpc":"2.0","id":1,"method":"logsSubscribe",
 "params":[{"mentions":["6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"]},{"commitment":"confirmed"}]}
```

Per notification:
1. Skip it if `value.err != null`.
2. Require an **exact** log line `Program log: Instruction: Create` or `Program log: Instruction: CreateV2`. Substring matching wrongly catches `CreateTokenAccount` and `CreatePool`.
3. Base64-decode the `Program data:` lines and pick the one whose first 8 bytes are `1b72a94ddeeb6376`.
4. Borsh-decode it. Fields are **appended over time**, so decode the leading fields and tolerate a short or long tail.

The reference decoder is in `docs/reference/pump_event.py` and is tested on 6 real 2026 events.

- **RPC choice:** Helius free tier (1M credits/month), or any websocket RPC.
- **Bandwidth risk:** `mentions` also delivers **every buy and sell** on pump.fun, which is far more traffic than creates. Measure credit and bandwidth use in Phase 0. If it is too heavy, drop to PumpPortal as the primary feed.

**Secondary: PumpPortal websocket** `wss://pumpportal.fun/api/data` with `{"method":"subscribeNewToken"}`.
- Free and keyless.
- Use **one connection only**; multiple connections earn about a one-hour ban.
- Filter `pool == "pump"`, because the feed also carries letsbonk creates.
- Messages include `mint, name, symbol, uri, traderPublicKey, marketCapSol, is_mayhem_mode, pool`, but **no description or socials**.
- Reportedly samples rather than covers the chain (Chainstack, Sept 2026).

**Gap recovery (after reconnects and deploys):**
- `getSignaturesForAddress` on the pump program, or
- pump.fun `GET frontend-api-v3.pump.fun/coins?sort=created_timestamp&order=DESC&limit=50&offset=…`. This pages only about 1,000 rows deep and has no cursor.

**Optional:** pump.fun's own NATS feed (`wss://prod-v2.nats.realtime.pump.fun`, subject `newCoinCreated.prod`). Its credentials are scraped from their web bundle and can rotate at any time. Opportunistic only; do not build on it.

### 4.2 Token metadata (description, image, socials)

1. Fetch `uri` from the `CreateEvent`.
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
3. Retry unresolved CIDs with backoff for about 1 h, then mark them `unresolved`.

**Enrichment (optional, throttled):** `GET https://frontend-api-v3.pump.fun/coins-v2/{mint}`.
- This is the one endpoint pump.fun itself documents.
- It returns already-parsed `twitter/telegram/website`, plus `nsfw`, `is_banned`, `hidden`, `usd_market_cap`, `complete`, `reply_count`, `created_timestamp` (ms) and `market_cap` (SOL).
- Call it server-side only (it is CORS-protected), at ≤2–4 requests/second, with backoff on 429.
- It **may be Cloudflare-challenged from Render IPs**; test in Phase 0.
- **Never trust its `token_program` field** (pump.fun's own warning).
- Use it for moderation flags and market signals, not as the primary source.

**Market data for "is this coin alive?" (used for tiering, §6.3):**
- The bonding-curve state from the event, plus later trades. Optionally `subscribeTokenTrade` on PumpPortal, but that is paid.
- Cheaper: re-poll `coins-v2/{mint}` for candidates.
- For graduated coins: DexScreener `/tokens/v1/solana/{≤30 mints}` (free, 300 rpm) and GeckoTerminal (~30 rpm).

### 4.3 X/Twitter content: tiered and lazy

**For every token (free, no network):**
1. Parse the `twitter` field with `parse_x_ref()` (`docs/reference/xref.py`, tested on 18 URL shapes).
2. Decode the snowflake time of any tweet or community ID.
3. Count reuse of the same tweet ID, handle or community ID across tokens in our DB.

**Only for tokens that pass the interest filter (§6.3):** fetch in this order, stopping at the first success. Each source gets a circuit breaker.

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
| **Entities gazetteer** (memes, famous animals, celebrities, politicians, AI bots, countries) | Wikidata SPARQL, run offline or in a monthly job, P31 chains (e.g. Q2927074 "Internet meme"), with aliases and short descriptions | monthly | referent detection, categories |
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
  ├─ S6 image            → hashes, near-dupes, OCR, palette, NSFW, (optional) visual labels
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
- **Same creator wallet** as earlier coins: serial launcher.
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
  1. every pump.fun image seen;
  2. famous-coin logos;
  3. meme-template hashes.
- **OCR** with **RapidOCR** (`rapidocr_onnxruntime`, Apache-2.0, ~130 MB RSS, 180–300 ms per image; read `$PNUT` at 0.97–1.0 confidence on test images). Feed the OCR text back through S1–S5. A ticker in the image that differs from the metadata ticker is a copycat signal. Prefer it over Tesseract, which is worse on stylized logo text and would force apt packages.
- **Dominant colours:** Pillow `quantize(5)` (~1 ms), mapped to colour names. A weak cue (green + frog → Pepe-like), and nice in the UI.
- **AI-generation metadata:**
  - Check PNG `tEXt` `parameters`/`prompt` chunks, EXIF `Software`, and C2PA. A Stable Diffusion prompt chunk is a free textual description of the image.
  - Absence of these markers means nothing.
- **NSFW** (required before an image is ever displayed):
  - Honour pump.fun's `nsfw`/`hidden`/`is_banned` flags when enrichment is available.
  - Run a local classifier. **NudeNet is AGPL-3.0**, so prefer **Yahoo open_nsfw** (BSD-2) converted to ONNX, unless the AGPL implications are accepted.
  - **Default the UI to blurred until the image is classified "safe".**
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
  - Hand-label 300–500 real tokens with a small internal labelling page.
  - Fit per-rule weights by logistic regression (scikit-learn, still classical).
  - Check reliability curves, so that "0.8" means right about 80% of the time.
  - Version the weights.

### 5.10 Taxonomy (multi-label; keep it in a YAML config, not code)
`animal/{dog,cat,frog,monkey,hippo,squirrel,bird,bear_bull,fish,other}` · `meme_template/{pepe_wojak_chad,x_wif_hat,chill_guy,npc,brainrot,copypasta,other}` · `ai_agent` · `political` · `celebrity/{elon,musician,athlete,streamer_kol,other}` · `news_event` · `food_object_abstract` · `regional_language` · `crypto_native/{slang,pumpfun_meta,cto,utility_claim}` · `derivative` (with subtypes `copycat`, `template_family`, `sequel`, `homoglyph_spoof`, `logo_reuse`) · `humor_nsfw_offensive`.

---

## 6. System architecture on Render

### 6.1 Services

```
            ┌──────────────── Render (one region, e.g. oregon) ──────────────────┐
 Solana RPC │  [worker] tokensage-ingest  (Starter 512MB, always on)             │
 (logsSub)──┼─▶  • feed listeners (RPC + PumpPortal), dedupe on mint             │
 PumpPortal─┼─▶  • writes tokens + enqueues jobs (Postgres)                      │
            │  [worker] tokensage-analyzer (Starter 512MB → Standard 2GB if CLIP)│
 IPFS ◀─────┼──  • job loop: fetch metadata → basic analysis → image → (tiered)  │
 X mirrors◀─┼──    X fetch, trend/news, full analysis; writes analyses           │
            │  [web] tokensage-api (FastAPI + server-rendered dashboard)         │
 Users ─────┼─▶  • reads Postgres; POST /analyze/{mint} enqueues priority job    │
            │  [cron] tokensage-knowledge (daily/weekly refresh jobs)            │
            │  [cron] tokensage-maintenance (*/15: backfill gaps, rescore, GC)    │
            │  [postgres] tokensage-db (basic-256mb → basic-1gb)                 │
            └────────────────────────────────────────────────────────────────────┘
```

- **Why two workers:**
  - Ingest must never stall. Image decoding and OCR spikes RAM and CPU, and an OOM in the analyzer must not drop the websocket.
  - Both workers can start as **one** Starter worker running two asyncio tasks, to save $7/month. Split them once RAM or CPU says so. Keep that boundary in the code from day one.
- **Workers and cron jobs have no inbound network** on Render. All coordination goes through **Postgres**:
  - a jobs table with `SELECT … FOR UPDATE SKIP LOCKED`;
  - `LISTEN/NOTIFY` to wake idle workers.
  - This avoids paying for Key Value in v1. Add Render Key Value later only if needed for caching or rate-limit counters.
- **Deploy overlap:** during a worker deploy the old and new instances both run for ~60 s. So:
  - all writes are idempotent (`INSERT … ON CONFLICT (mint) DO NOTHING/UPDATE`);
  - jobs are claimed with leases (`locked_until`).
- **SIGTERM:** stop reading, release or requeue claimed jobs, close sockets, exit 0. Set `maxShutdownDelaySeconds: 60`.

### 6.2 Ingest worker behaviour
- Keep one websocket per feed. Ping about every 20 s. Reconnect with exponential backoff plus jitter, and resubscribe.
- On reconnect, **backfill** the gap (§4.1), using a high-water mark stored in Postgres (`feed_state`).
- Record which feed saw each mint first (`seen_by`), to measure feed coverage over time.
- Insert the `token` row immediately with the on-chain fields, and enqueue an `analyze_basic` job.

### 6.3 Analysis tiers (the key cost-control idea)
About 30k tokens a day arrive and most die within minutes, so analysis depth is **tiered**:

| Tier | Runs for | Work | Budget |
|---|---|---|---|
| **basic** | every token, immediately | metadata fetch; S1–S5 on name, ticker and description; X link parse, snowflake and reuse count; image fetch + hashes + near-dup + NSFW; S9/S10 | ≲300 ms CPU per token, no paid calls |
| **full** | tokens that pass the **interest filter**: still trading after N minutes, bonding-curve progress ≥ X%, graduated, high reply count, *or* requested via the API/dashboard | OCR; X fetch chain; trend + news confirmation; optional CLIP; re-score | seconds per token; paid X fallback only here, under a daily cap |
| **refresh** | full-tier tokens, periodically | re-poll enrichment flags (nsfw/banned/CTO), X profile refresh | cron |

- The interest-filter thresholds are configuration. Start with: alive at 10 min with curve progress ≥ 15%, or graduated, or requested.
- If CPU is tight, OCR can move into basic later, once real throughput is measured.
  - Basic tier is ~0.4 tokens/s × ~50 ms of CPU, which is trivial.
  - OCR is ~300 ms × 0.4/s ≈ 12% of one core, so it is feasible on Starter's 0.5 CPU but is the first thing to watch.

### 6.4 API (FastAPI)
| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Cheap; no DB call |
| `GET /tokens?since=&category=&q=&depth=&flag=` | Paginated list (keyset pagination on `created_at, mint`) |
| `GET /tokens/{mint}` | Token plus latest Analysis JSON |
| `POST /tokens/{mint}/analyze` | Enqueue a priority `full` job; fetches the token from chain or frontend-api if unknown. Requires an API key |
| `GET /trends` | Current trend terms and the tokens matching them |
| `GET /clusters/{id}` | Copycat cluster (same ticker base, logo hash, tweet) |
| `GET /stats` | Feed coverage, queue depth, per-source circuit-breaker state, X spend today |

**Dashboard:** server-rendered (Jinja2 + HTMX, no SPA build step).
- A live list of new tokens with categories and referent, a token detail page with evidence, and clusters and trends views.
- Images are **not proxied** through Render, because Hobby bandwidth is only 5 GB/month. Show the IPFS gateway URL directly, blurred until NSFW-safe.
- Live updates by HTMX polling every few seconds, or SSE. Clients reconnect on deploy.

### 6.5 Memory budget (estimates from research; profile in Phase 0/3)

| Component | RSS |
|---|---|
| Python + FastAPI/asyncpg | 80–150 MB |
| wordsegment | ~100 MB (wordninja ~28 MB) |
| rapidfuzz/emoji/anyascii/confusables + gazetteers | ~40 MB |
| Pillow + ImageHash + numpy/scipy | ~60 MB |
| onnxruntime base | ~44 MB |
| RapidOCR | ~130 MB |
| open_nsfw ONNX | ~100 MB (est.) |
| CLIP B/32 vision | ~500 MB |

- **Analyzer without CLIP:** ~400–450 MB, which is tight on 512 MB. Load OCR lazily, use `WEB_CONCURRENCY=1`, and cap the image pixel count. If it OOMs, move the analyzer to Standard (2 GB, $25) or move OCR to the full tier only.
- **API:** ~150 MB. It should not import the analysis modules.

---

## 7. Data model (Postgres 17)

```sql
create table token (
  mint text primary key,
  name text, symbol text, uri text,
  creator text, bonding_curve text, token_program text, quote_mint text,
  is_mayhem boolean, created_at timestamptz not null,   -- from CreateEvent.timestamp
  launcher text,                                          -- metadata.createdOn or uri host
  seen_by text[] not null default '{}',                   -- {'rpc','pumpportal','backfill'}
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
  raw jsonb, fetched_at timestamptz
);

create table token_market (        -- latest snapshot used for tiering
  mint text primary key references token on delete cascade,
  complete boolean, curve_progress real, usd_market_cap double precision,
  reply_count int, nsfw boolean, hidden boolean, is_banned boolean,
  updated_at timestamptz
);

create table image (                -- keyed by content, shared across tokens
  content_key text primary key,      -- CID or sha256
  phash bigint, dhash bigint, phash_mirror bigint, pdq bytea,
  ocr text[], palette text[], nsfw text, labels jsonb, clip vector null, -- pgvector optional
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
  id bigserial primary key, kind text not null, mint text, priority int not null default 100,
  run_after timestamptz not null default now(), attempts int not null default 0,
  locked_until timestamptz, last_error text, created_at timestamptz not null default now(),
  unique (kind, mint)
);
create index on job (priority, run_after) where locked_until is null;

create table feed_state (feed text primary key, high_water jsonb, updated_at timestamptz);
create table source_health (source text primary key, state text, failures int, open_until timestamptz,
  calls_today int, spend_today_usd numeric);
```

**Retention:**
- About 30k tokens/day means roughly 11M rows/year.
- Keep `analysis` for all tokens, but **garbage-collect basic-tier analyses older than 30 days** for dead tokens (keep the token row and hashes for copycat history).
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
      - key: ENABLE_CLIP
        value: "false"
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
    plan: starter                 # 'free' works for dev but sleeps after 15 min idle and has no preDeploy
    region: oregon
    dockerfilePath: ./Dockerfile
    dockerCommand: sh -c "exec uvicorn tokensage.api.app:app --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers"
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
      - key: API_ADMIN_TOKEN
        generateValue: true
      - key: WEB_CONCURRENCY
        value: "1"

  - type: worker
    name: tokensage-worker        # ingest + analyzer as two asyncio tasks in v1
    runtime: docker
    plan: starter                 # → standard when ENABLE_CLIP=true or on OOM
    region: oregon
    dockerfilePath: ./Dockerfile
    dockerCommand: sh -c "exec python -m tokensage.worker"
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
      - key: SOLANA_WS_URL         # e.g. Helius free websocket URL incl. api key
        sync: false
      - key: COINGECKO_API_KEY
        sync: false
      - key: TWITTERAPI_IO_KEY     # optional paid fallback
        sync: false

  - type: cron
    name: tokensage-knowledge
    runtime: docker
    plan: starter
    region: oregon
    schedule: "17 3 * * *"        # daily 03:17 UTC: trends, known coins (weekly inside), gazetteer deltas
    dockerfilePath: ./Dockerfile
    dockerCommand: sh -c "exec python -m tokensage.jobs.knowledge"
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
    schedule: "*/15 * * * *"      # backfill gaps, promote tokens to full tier, refresh, GC
    dockerfilePath: ./Dockerfile
    dockerCommand: sh -c "exec python -m tokensage.jobs.maintenance"
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
   - Use shell-form `sh -c "exec …"` so `$PORT` expands and SIGTERM reaches Python.
   - Builds are amd64 only.
8. **Bake models and data files into the image** at build time. Never download them at startup: the filesystem is ephemeral, and downloads slow health checks.
9. **Free web services sleep after 15 min.** Workers and cron jobs cannot be free.
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
CMD ["sh","-c","exec uvicorn tokensage.api.app:app --host 0.0.0.0 --port ${PORT:-10000}"]
```

---

## 9. Implementation plan (phases with acceptance criteria)

**Tech stack:**
- **Runtime and web:** Python 3.12, `uv`, FastAPI + Jinja2/HTMX.
- **Data:** asyncpg with plain SQL (or SQLAlchemy Core), Alembic migrations.
- **Network:** `httpx` (async, HTTP/2 off by default), `websockets`, `pydantic` v2 for every external payload.
- **Testing and quality:** `pytest` + `respx` for HTTP fixtures, `ruff`, `mypy` (lenient), `structlog`.

### Phase 0: Reality check from Render (½–1 day). **Do first.**
Deploy a throwaway Starter worker (or use the Render shell) that runs a smoke-test script. Record the results in `docs/smoke-test-results.md`. The script checks:
- **Solana `logsSubscribe` on the chosen RPC:**
  - decode 50 creates with `pump_event.py`;
  - measure messages/second and bytes/second (buys and sells included);
  - estimate the monthly credit cost.
- **PumpPortal:** coverage versus RPC over 10 minutes (the share of RPC mints also seen by PumpPortal).
- **IPFS gateways:** success rate and latency per gateway for 100 recent URIs; how many URIs are non-IPFS hosts.
- **`frontend-api-v3.pump.fun/coins-v2/{mint}`:** 200 vs 403/Cloudflare from Render.
- **X:** the full curl list in `docs/research/02-x-twitter-access.md` §8 (FxTwitter, vxTwitter, syndication, oEmbed, crawler-UA community page).
- **Knowledge APIs:** CoinGecko categories, Wikimedia pageviews top, Wikidata search, Google News RSS, Urban Dictionary.

**Accept when** every source is marked works / flaky / blocked from Render. Update §4 and §13 of this guide accordingly.

### Phase 1: Skeleton and deploy pipeline
- Repo layout (§12), `pyproject.toml`, Dockerfile, `render.yaml`, Alembic with the §7 schema, `/healthz`, and an empty worker loop with SIGTERM handling.
- CI (GitHub Actions): ruff, mypy, pytest.

**Accept when** the Blueprint deploys cleanly from a fresh Render account, migrations run in preDeploy, and the worker logs a heartbeat.

### Phase 2: Ingestion
- RPC listener and PumpPortal listener, dedupe on `mint`, `seen_by`.
- Reconnect, backfill and `feed_state`. Metadata fetcher with the gateway chain, CID cache and SSRF guard.
- `token`, `token_metadata` and `job` rows.

**Accept when:**
- 24 h of running ingests ≥ 99% of the creates seen by a reference (compare against frontend-api `/coins` sampling).
- No duplicate rows across deploys.
- Metadata resolved ≥ 95% within 10 min.

### Phase 3: Basic-tier understanding engine (the heart)
- S1–S5, S7 (parse, snowflake, reuse only), S9 and S10, with the §3 schema.
- **Image:** hashes, near-dup and NSFW. OCR stays behind the tier flag.
- Packaged knowledge: the slang YAML, CLDR emoji, WordNet-derived class lists (built by a script in `scripts/`), and a seed `known_coin` table.
- **Golden test set** `tests/golden/*.yaml`: ≥ 60 hand-written cases covering every pattern in §2.2 and every pitfall in research. Must include:
  - `dogwifhat`, `catwifhat`, `Trump wif Hat` (must NOT be dog)
  - `BPNUT`, `m00 deng classic`, `dogwifhat2.0`, `ʙᴀʙʏ ᴘɴᴜᴛ`
  - Cyrillic `Рepe`, `$Ｐ​ＮＵＴ` (full-width with zero-width), `p3anu7`, `🐿🥜`
  - `justachillguy`, `AIAgentSupercycle`, a CJK name, a URL-handle spoof, a reused tweet

**Accept when:**
- all golden cases pass;
- p95 basic analysis is under 300 ms CPU excluding network;
- analyzer RSS is under 450 MB on Starter;
- it keeps up with live volume for 24 h (queue depth stable).

### Phase 4: Full tier
- Interest filter and promotion (maintenance cron).
- X fetch chain with circuit breakers, caching, single-flight and spend cap. The paid fallback stays off by default.
- OCR. Trend pipeline (the knowledge cron) and news confirmation.
- `POST /tokens/{mint}/analyze`.

**Accept when:**
- a requested token gets a full analysis in under 20 s (p90);
- X fetch success ≥ 90% for tweet links on the free chain (or the documented reason it isn't);
- the trend match fires on a manufactured test (a known spiking article).

### Phase 5: API and dashboard
- The endpoints in §6.4, the list/detail/cluster/trend pages, NSFW blur, and evidence display.

**Accept when** a non-technical user can open a token and understand *why* TokenSage concluded what it did.

### Phase 6: Calibration and quality
- A labelling page (protected by an admin token). Label 300–500 tokens.
- Logistic-regression weight fit, reliability curve report, threshold tuning for pHash/PDQ/CLIP on real data. Version and store the weights.

**Accept when** top-1 category precision is ≥ 0.8 at the shown confidence, and the referent is correct on ≥ 70% of tokens where a human could identify one.

### Phase 7: Optional local models
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
- **XSS:**
  - Names, descriptions, tweet text and OCR output are rendered **escaped**, with Jinja autoescape on.
  - Links are shown as text or with `rel="noopener nofollow ugc"`.
  - **Never auto-link** `javascript:` or `data:` URLs.
  - Add a strict CSP header.
- **NSFW and illegal content:**
  - Blur by default until classified safe.
  - Never re-host or cache flagged image bytes. Store only hashes and labels.
  - Honour pump.fun moderation flags when available.
- **Abuse of the API:** use an API key for `POST /analyze` and rate-limit per IP.
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
    - **NudeNet is AGPL**; prefer open_nsfw (BSD).
    - Wiktionary data is CC BY-SA (attribute). Wikidata is CC0.
    - MobileCLIP uses an Apple licence (review).
- **Not financial advice:** the UI states that flags and categories are informational only.

---

## 11. Cost estimate (Render Hobby workspace; verify at render.com/pricing)

| Item | Plan | $/month |
|---|---|---|
| API web | starter (free possible for dev) | 7 (0) |
| Worker (ingest + analyzer) | starter → standard if CLIP or OOM | 7 → 25 |
| Cron × 2 | per-minute billing, $1 minimum each | ~2–4 |
| Postgres | basic-256mb + ~5–15 GB storage at $0.30/GB | ~8–11 |
| Solana RPC | Helius free (verify the credit burn in Phase 0) | 0 (→ 49 Developer if needed) |
| Paid X fallback | off by default; ~$1–2/day if enabled for ~5k full-tier tokens/day | 0 (→ ~30–60) |
| **Total v1** | | **≈ $25–30** (≈ $45–50 with a Standard worker) |

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
│   └── known_coins_seed.yaml
├── tokensage/
│   ├── config.py               # pydantic-settings; all thresholds/weights here or in data/
│   ├── db.py
│   ├── net/                    # safe_fetch (SSRF guard), ipfs gateways, circuit breaker, single-flight
│   ├── ingest/                 # rpc_logs.py, pumpportal.py, backfill.py, pump_event.py
│   ├── sources/                # pumpfun_api.py, x_fx.py, x_vx.py, x_syndication.py, x_oembed.py,
│   │                           # x_paid.py, coingecko.py, wikimedia.py, wikidata.py, gnews.py
│   ├── engine/
│   │   ├── context.py          # TokenContext, Evidence, Analysis models (the §3 contract)
│   │   ├── normalize.py  segment.py  lexicon.py  ticker.py  known_coins.py
│   │   ├── image.py  xref.py  trends.py  aggregate.py  render_summary.py
│   │   └── pipeline.py         # orchestrates stages per tier
│   ├── worker.py               # ingest + analyzer tasks, SIGTERM handling
│   ├── jobs/                   # knowledge.py, maintenance.py (cron entrypoints)
│   └── api/                    # app.py, routes, templates/ (Jinja2 + HTMX)
├── scripts/                    # offline builders (wordnet classes, wikidata gazetteer), smoke_test.py
├── tests/
│   ├── golden/                 # hand-labelled meaning cases (yaml)
│   ├── fixtures/               # recorded payloads: CreateEvents, metadata JSON, X responses
│   └── test_*.py
└── docs/
    ├── research/               # the four research reports
    └── reference/              # tested reference code (port into tokensage/)
```

---

## 13. Open questions and verification checklist

Unless noted, these must be resolved by the Phase 0 smoke test.

1. Is the RPC `logsSubscribe` bandwidth and credit cost sustainable on Helius free (or another free RPC)? If not: PumpPortal primary plus RPC sampling, or a paid RPC.
2. What is PumpPortal's real coverage versus RPC?
3. Does `frontend-api-v3.pump.fun` answer from Render IPs (Cloudflare)?
4. Which IPFS gateways work best from Render? Is `pump.mypinata.cloud` usable by third parties?
5. Do FxTwitter, vxTwitter, syndication and oEmbed answer from Render? Real rate limits? Is the commercial-use stance of FxTwitter acceptable to the owner?
6. Are the CoinGecko category IDs and Demo quotas as documented? Wikimedia 2026 rate limits with our UA?
7. Is the Render pricing after the April 2026 change as summarised (Hobby 5 GB egress, Starter $7, Postgres basic-256mb $6)?
8. What are the real pHash/PDQ/CLIP thresholds on real pump.fun logos? Does the ~18% near-duplicate rate reproduce?
9. How accurate is RapidOCR on stylized meme logos (only clean synthetic text was tested)?
10. Which NSFW model, licence-wise (open_nsfw vs NudeNet AGPL)?
11. **Owner decisions:** whether to enable the paid X fallback and its daily cap; the ToS risk appetite for unofficial X mirrors and the pump.fun frontend API; public vs private dashboard.

---

## Appendix A: Reference code in `docs/reference/` (tested)

| File | What | Tests |
|---|---|---|
| `xref.py` | `parse_x_ref()` (18 URL shapes incl. communities, intents, t.co, spoofable status URLs); `snowflake_time()`; `syndication_token()` (V8 `toString(36)` port, fuzz-matched against Node on 2,300 IDs) | `test_xref.py` |
| `pump_event.py` | Tolerant Borsh decoder for `CreateEvent` (handles older, shorter layouts; reports unknown tail bytes) | `test_pump_event.py` against `create_events.txt`: 6 real `Program data:` lines from 2026, incl. non-IPFS launcher URIs and non-`pump` mints |
| `xurl.reference.mjs` | Original JS version of the X URL parser and token function | — |

Run them with: `cd docs/reference && python -m pytest -q`.

## Appendix B: Research reports
1. `docs/research/01-pumpfun-data-sources.md`: frontend APIs and real payloads, feeds and vendors, on-chain program and IDL details, metadata/IPFS, volume, pitfalls.
2. `docs/research/02-x-twitter-access.md`: official API pricing 2026, free mirrors with schemas, paid scrapers, communities, URL shapes, signals, tiered strategy, smoke-test commands.
3. `docs/research/03-understanding-techniques.md`: normalization, segmentation benchmarks, lexicons/APIs, fuzzy and phonetic matching, trend sources, taxonomy, image hashing/OCR/NSFW measurements, local models, explainable output, library table with licences and RAM.
4. `docs/research/04-render-platform.md`: Blueprint field reference, pricing, Python/Docker specifics, worker/cron/deploy semantics, memory, gotchas.

> **Research caveat:** the research sandbox could not reach pump.fun, IPFS gateways, Solana RPCs, X or its mirrors, or Wikimedia/CoinGecko directly. Findings come from official repos (pump-fun/pump-public-docs IDLs as of 2026-09-29, render-oss/skills), the source code of the relevant open-source tools, captured real payloads in public repos, and 2026 web sources. Hence Phase 0.
