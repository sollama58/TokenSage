# Integration Brief: pump.fun Token Meaning Analysis ("TokenSage")

> **What this is:** a self-contained brief for adding one capability to an existing project. **Give it a pump.fun token Contract Address (CA); get back a structured explanation of what the token means**, based on its name, ticker, image and description, read in the context of its linked X/Twitter content.
>
> **Who it is for:** the engineer or AI coding agent working in the host project. It is written to be stack-agnostic. The reference code is Python (tested), and the logic ports easily to TypeScript or other languages.
>
> **Date of research:** 2026-10-05. The full research reports (with sources) live in the TokenSage repo: `github.com/sollama58/TokenSage`, branch `claude/awesome-hypatia-25bpzu`, under `docs/research/`.

---

## 1. Goal and hard constraints

**Input:** a pump.fun CA (the Solana mint address). Bare addresses, and pump.fun/explorer URLs ending in one, are both accepted.

**Output:** one JSON `Analysis` object (§4), containing:
- **referent:** what the coin is about, e.g. "Peanut (squirrel)";
- **categories:** multi-label with confidences, e.g. animal/squirrel, news event, derivative;
- **ticker explanation:** e.g. "PNUT = vowel-dropped 'peanut'";
- **copycat / derivative findings;**
- **image findings:** text in the image, near-duplicates of known logos, NSFW verdict;
- **X/Twitter context:** whose tweet it is, whether it predates the coin, and whether the narrative is borrowed;
- **flags:** e.g. spoofed handle, homoglyph ticker, recycled X account;
- **a template-generated summary,** with **evidence** for every claim.

**Hard constraints:**
1. **No external AI.** No OpenAI, Anthropic, Gemini, Google Vision, hosted inference APIs, or anything similar.
   - The core is deterministic: normalization, lexicons, gazetteers, fuzzy matching, perceptual hashing and OCR.
   - Small local CPU models (ONNX), e.g. CLIP for visual labels, are allowed only as **optional, flag-gated** extras. Everything must work without them.
2. **Explainable.** Every label carries evidence records. A conclusion with no evidence is a bug.
3. **Untrusted input.** Names, metadata JSON, URLs, images and tweets are attacker-controlled (§9).
4. **Tolerant.** pump.fun and the X mirrors change shape without notice. Validate, degrade to a `partial` result, and never crash on one bad token.

**Non-goals:** trading signals, price prediction, a token discovery feed, and reverse image search via external services.

---

## 2. How to integrate (decide with the project owner)

Expose **one entry point** and keep everything else internal:

```python
async def analyze_token(ca: str, depth: Literal["basic", "full"] = "full",
                        max_age_s: int | None = None) -> AnalysisResult
```

`AnalysisResult` has these fields:
- `status`: `complete | partial | failed`
- `analysis`: the §4 object
- `errors`: upstream problems, e.g. `x.fxtwitter: timeout`
- `freshness`: `analyzed_at`, `from_cache`

Raise or return typed errors for the input cases:
- `invalid_ca`: not base58, or not 32 bytes;
- `token_not_found`: no account on-chain;
- `not_a_token_mint`: e.g. a wallet address;
- `not_pumpfun`: only if non-pump mints are rejected (see open questions).

**Two placement options:**

| Option | When | Notes |
|---|---|---|
| **A. In-process module** inside the host app | The host runs Python (or the logic is ported), traffic is modest, and RAM is available | Simplest. Run heavy steps (image decode, OCR) in a worker or thread pool with a concurrency limit, so a big image can't stall request handling. |
| **B. Sidecar service** with an HTTP API | The host is another language, needs isolation, or is memory-constrained | `GET /v1/tokens/{ca}?depth=&wait=` returns 200 with the result, or 202 with a job id to poll. API key auth. This is the full design in the TokenSage repo's `PROJECT_GUIDE.md` §6, including a Render Blueprint. |

**What either option needs from the host:**
- outbound HTTPS;
- a Solana RPC URL (Helius' free tier works);
- a persistent store for caches. Use the host's existing DB (Postgres ideal) or a KV store;
- optionally a background job runner (for `full` depth, metadata retries and daily knowledge refresh).

**Latency targets:**

| Case | Target |
|---|---|
| cache hit | < 100 ms |
| cold `basic` | ~1–4 s (dominated by the IPFS metadata fetch) |
| cold `full` | ~5–20 s (X fetches, OCR, news check) |

If the host needs synchronous answers, default to `basic` and run `full` in the background.

**Caching rules:**
- **Forever** (immutable): on-chain name, symbol, uri, creator, creation time; metadata JSON by IPFS CID; image hashes, OCR and NSFW verdict by image CID; the *first-seen* copy of a linked tweet. Narrative tweets get deleted, so keep the first copy.
- **Short TTL:**
  - bonding-curve state: ~60 s;
  - whole re-analysis: 5 min for tokens under 1 h old, 1 h for tokens up to 7 days old, 24 h after that;
  - X profiles: 6–24 h, keyed by **user ID**, so renames are detected.
- **Single-flight per CA:** concurrent requests for the same CA share one computation.

---

## 3. The pipeline

```
CA ─▶ 1 validate ─▶ 2 resolve on-chain ─▶ 3 fetch metadata JSON + image ─▶ 4 parse X link
                                                                     │
      ┌──────────────────────────────────────────────────────────────┘
      ▼
 5 understand text (normalize → segment → lexicon/gazetteer → ticker↔name → known-coin/copycat)
 6 understand image (hashes → near-dup → NSFW → [full] OCR → [optional] CLIP labels)
 7 [full] fetch X content → relation signals;  [full] trend/news match
 8 aggregate evidence → categories + referent + flags → template summary + caveats
```

`basic` = steps 1–6 without OCR, plus X link parsing and free signals. `full` = everything.

### 3.1 Steps 1–2: Validate and resolve the CA (standard Solana RPC only)

Reference code is in Appendix A. It is tested, and the bonding-curve derivation matches 7 real mint/curve pairs.

1. **Validate.**
   - Strip whitespace and any URL prefix.
   - The address must be base58 and decode to exactly 32 bytes, else `invalid_ca`.
   - Do this before any network call, so junk input costs nothing.
2. **Fetch the mint account** with `getAccountInfo(ca, {encoding:"jsonParsed"})`.
   - Missing account → `token_not_found`. Very new tokens may need a retry.
   - Owner program tells you what kind of mint it is:
     - `TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA` (SPL Token): a legacy pump.fun `create`.
     - `TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb` (Token-2022): a `create_v2` coin (since Nov 2025).
     - Anything else, or not a mint → `not_a_token_mint`.
3. **Check it is a pump.fun coin.**
   - Derive the bonding-curve address: the program-derived address with seeds `["bonding-curve", mint]` under the pump program `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P`.
   - If that account exists and starts with the `BondingCurve` discriminator, it is a pump.fun coin. The account persists after graduation.
   - Decode it **tolerantly**, because older accounts are shorter. It gives `complete` (graduated), reserves, `creator`, `is_mayhem_mode` and `quote_mint`.
   - Curve progress = 1 − real_token_reserves ÷ initial_real_token_reserves. Read the initial value from the pump `Global` account; don't hard-code it.
   - **Do not check for the "…pump" address suffix.** It is a UI vanity convention, and ~30% of coins in one Oct-2026 sample lacked it.
4. **Name, symbol and uri on-chain.**
   - Token-2022: the jsonParsed mint has the `tokenMetadata` extension.
   - Legacy: Metaplex metadata account. Seeds are `["metadata", METAPLEX_ID, mint]` under the Metaplex program `metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s`; Borsh-decode name/symbol/uri and strip NUL padding.
   - On Helius, one DAS `getAsset(ca)` call gives both kinds. Keep the raw path as fallback.
5. **Creation time and creator.** Use the first that works:
   1. your own cache;
   2. `GET https://frontend-api-v3.pump.fun/coins-v2/{ca}` → `created_timestamp` (ms), `creator`;
   3. RPC history: page `getSignaturesForAddress(bonding_curve)` back to the oldest signature, `getTransaction`, and decode the `CreateEvent` from the `Program data:` log (Appendix A). Cap the paging (e.g. 5 pages) and otherwise return `created_at: null`.

### 3.2 Step 3: Metadata JSON and image

- `uri` is usually `https://ipfs.io/ipfs/<CID>`. Third-party launchers use their own hosts, e.g. `metadata.j7tracker.io`, `meta.uxento.io`, `usepaid.app`.
- **IPFS URLs:**
  - Extract the CID and try gateways in order: `pump.mypinata.cloud` → `dweb.link` → `ipfs.io` → `gateway.pinata.cloud`. Race a second gateway after ~1.5 s.
  - Timeouts: 5 s connect / 10 s total. Size caps: 64 KB for JSON, 5 MB for images.
  - Rewrite the dead `cf-ipfs.com` / `cloudflare-ipfs.com` hosts (shut down Aug 2024).
- **JSON fields:** `name, symbol, description, image, showName, createdOn, twitter, telegram, website, video?`.
  - Any field may be missing, `""`, the wrong type or hostile.
  - `createdOn` identifies the launcher.
- If the metadata can't be fetched in time, return `partial` from the on-chain name and ticker, and retry in the background.
- **Optional enrichment:** `frontend-api-v3.pump.fun/coins-v2/{ca}`.
  - Undocumented, server-side only (CORS), throttled to ≤2–4 req/s.
  - It may be Cloudflare-blocked from datacenter IPs.
  - Gives parsed socials and `nsfw`/`is_banned`/`hidden` flags, `usd_market_cap`, `reply_count`.
  - Never trust its `token_program` field (pump.fun's own warning).

### 3.3 Steps 4 and 7: The X/Twitter link

**Always, with no network:**
- Parse the `twitter` field into one of: `tweet`, `profile`, `community`, `search`, `shortlink` (t.co), `foreign`, `invalid` or `empty`. Parser in Appendix B, tested on 18 URL shapes.
- Tweet, community and user IDs are snowflakes, so their **creation time decodes offline**: `ms = (id >> 22) + 1288834974657`. That tells you whether the tweet or community existed before the token.
- Count how many other tokens (in your store) link the same tweet ID, handle or community. Many means narrative farming or a copycat swarm.

**`full` depth: fetch, stopping at the first success.** Each source gets a circuit breaker and a descriptive User-Agent.

| # | Source | Gives |
|---|---|---|
| 1 | `https://api.fxtwitter.com/2/status/{id}`, `/2/profile/{handle}`, `/2/profile/{handle}/about` | Richest: text, author, followers, verification type, media, quote, **username-change count**, community info when the post is inside one. Third-party (MIT, self-hostable); commercial-use stance unclear. |
| 2 | `https://api.vxtwitter.com/i/status/{id}`, `/{handle}` | Text, media, followers (no verified flag) |
| 3 | `https://cdn.syndication.twimg.com/tweet-result?id={id}&lang=en&token={syndication_token(id)}` | X's embed CDN: text, author, verification, media. `TweetTombstone` = deleted. No follower count. Flaky from cloud IPs. |
| 4 | `https://publish.x.com/oembed?url=…&omit_script=1&dnt=true` | Text, author, date inside HTML |
| 5 (paid, opt-in) | twitterapi.io (~$0.15/1k tweets; community info $0.0002) or SocialData (~$0.20/1k; community member count and rules) | Everything, incl. **X Communities** |
| 6 (paid, opt-in) | Official X API pay-per-use ($0.005/post, $0.010/user) | The only ToS-clean read path. The Free tier closed to new developers in Feb 2026. |

Public Nitter is effectively dead (cease-and-desist letters, Aug 2026). Without paying, community details are limited to the snowflake creation date.

**Rule:** X ignores the handle in a status URL. `x.com/elonmusk/status/<someone else's tweet>` resolves to the other tweet, and scammers use exactly this. **Always use the fetched author; a URL handle that differs from it is a red flag.**

### 3.4 Step 5: Understanding the text (no LLM)

1. **Normalize**, in this order (tested):
   1. NFKC (folds full-width `ＰＮＵＴ`).
   2. Extract emoji and map them with **Unicode CLDR annotation keywords**. 🐿's short name is "chipmunk", but its keywords include "squirrel".
   3. Strip zero-width characters.
   4. Detect homoglyphs with `confusable_homoglyphs` (a Cyrillic `Рepe` is a spoof flag), then fold to ASCII with **`anyascii`**. Do not use GPL `Unidecode`.
   5. **Detect version/derivative markers before stripping punctuation:** `2.0`, `v2`, `II`, `classic`, `og`, `real`, `baby`, `mini`, `inu`, `ai`, `wif`.
   6. Split camelCase before lowercasing.
   7. Squeeze runs of 3+ repeated letters.
   8. Undo leetspeak only in tokens that mix letters and digits (`p3anu7` → peanut; keep `420`, `69`).
2. **Segment** concatenated names with `wordsegment`, injecting a custom vocabulary: slang, known coin names, famous animals and people, and trending titles.
   - It gets `dogwifhat` → "dog wif hat" (wordninja gives "dog w if hat").
   - **Don't use SymSpell compound mode;** it "corrects" slang into English ("dog with at").
3. **Gazetteer and lexicon matching** with one Aho-Corasick automaton (`pyahocorasick`) over:
   - known coins and their aliases;
   - Wikidata-derived entities (memes, famous animals, celebrities, politicians, AI bots, countries);
   - WordNet-derived noun classes (animals, foods), **precomputed offline to JSON**, because loading WordNet live costs ~300 MB;
   - a hand-curated slang lexicon (~200 entries: gm, wagmi, jeet, cto, wif, fwog, chad, pepe, …).
   - **Gotcha:** `wif` is a *template marker* (the "X wif hat" family), not a dog keyword.
   - Don't rely on spaCy NER here: it found no entities in "Elon Musk just tweeted about his dog".
4. **Explain the ticker**, in priority order:
   1. known coin or known lore ticker (MEW ↔ "cat in a dogs world");
   2. ticker equals a name token;
   3. subsequence of the compact name (WIF ⊂ dogwifhat);
   4. vowel-dropped name within edit distance 1 (peanut → pnt ≈ PNUT);
   5. acronym of the name;
   6. `rapidfuzz.partial_ratio`, **always with `utils.default_process`**;
   7. phonetic or baby-talk map as a tiebreaker.
5. **Known-coin / copycat check:**
   - Ticker base: strip prefixes `B/BABY/MINI/2/V2` and suffixes `INU/AI/2`, then compare to known tickers.
   - Fuzzy name match **plus** a comparison of space-stripped forms (`m00 deng classic` → `moodengclassic` ⊃ `moodeng`).
   - Template families via character n-grams (`wifhat`).
   - On-demand lookups:
     - `frontend-api-v3.pump.fun/coins/search?searchTerm=<ticker>` returns other same-name coins with creation times (unofficial).
     - `https://api.dexscreener.com/latest/dex/search?q=<ticker>` (free) covers graduated coins.
   - The earliest coin is the likely original.
   - A derivative **inherits** its parent's referent and categories, at reduced confidence.

### 3.5 Step 6: Understanding the image (no external AI)

- **Decode safely:** sniff magic bytes, cap pixels (`Image.MAX_IMAGE_PIXELS`), downscale to 512 px, and sample 3 frames of animated GIF/WebP.
- **Perceptual hashes:** `ImageHash` pHash + dHash (~3 ms), plus PDQ (`pdqhash`) for mirror/rotation robustness.
  - pHash ≤ 8/64 means "same image"; 9–14 means "edited". These thresholds come from synthetic tests, so **retune on real logos**.
  - Compare against famous-coin logos, meme-template hashes, and every image previously analysed. Store a 64-bit pHash as BIGINT; a numpy XOR+popcount scan handles ~1M hashes in ~10 ms.
  - About 18% of pump.fun images are near-duplicates.
- **OCR (`full`):** RapidOCR (`rapidocr_onnxruntime`, Apache-2.0, ~130 MB RAM, ~200–300 ms per image). It read `$PNUT` at 0.97+ confidence in tests.
  - Run the OCR text back through step 5. A ticker in the image that differs from the metadata ticker is a copycat signal.
  - It beats Tesseract on stylized text, and needs no system packages.
- **NSFW (required):** a local classifier.
  - Prefer Yahoo **open_nsfw** (BSD) in ONNX. NudeNet works well but is **AGPL**.
  - Also honour pump.fun's `nsfw`/`hidden` flags.
  - Expose `display_url` **only** when the verdict is `safe`.
- **Cheap extras:**
  - Dominant colours via Pillow `quantize(5)`.
  - PNG `parameters`/`prompt` chunks: a Stable Diffusion prompt is a free description of the image.
- **Optional CLIP (flag-gated, ~0.5 GB RAM):**
  - ViT-B/32 vision tower in ONNX, with ~300 label prompts embedded offline ("a dog wearing a hat", "Pepe the frog meme", "a squirrel", "pixel art", …).
  - Report only labels above a cosine threshold, marked "visual guess".

### 3.6 Step 7: X signals and trends

| Signal | Meaning |
|---|---|
| tweet time < token time | **narrative source**: the coin was made *about* this tweet |
| tweet after launch from a young, small account | the dev's launch announcement |
| big or verified account unrelated to the creator | **borrowed narrative**; never call it "official" |
| URL handle ≠ fetched author | **spoof**: strong red flag |
| tweet mentions the ticker, CA or pump.fun link | direct link: strong if the author is notable, rare |
| verified type Business/Government vs blue check | gold or grey checks are strong; blue is weak |
| `username_changes > 0` | **recycled or bought account**: strong rug signal |
| account days old with few posts | disposable account |
| deleted tweet or suspended account | keep the cached copy and flag it |
| community created minutes before the token | purpose-built shell: weak or neutral |
| search or hashtag link instead of an account | no real socials: weak negative |
| same tweet, handle or community linked by many tokens | narrative farming or copycat swarm |

Tweet text goes back through step 5. **The tweet is often the actual "meaning".**

**Trend matching:**
- **Daily job:** pull Wikimedia pageviews top-1000 (`wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/YYYY/MM/DD`; send a proper User-Agent).
  - Spike ratio = views ÷ 30-day median. Keep spikes > 3, plus their Wikidata aliases.
- **Per token:** match name, ticker expansions, description and tweet spans against those terms, weighted by rarity (a match on "trump" means little).
- **`full` only:** confirm with one cached Google News RSS query (`news.google.com/rss/search?q=<q>+when:2d&hl=en-US&gl=US&ceid=US:en`) and attach the headline.
- Don't use pytrends (archived), Know Your Meme (forbids scraping) or unauthenticated Reddit JSON (blocked).

### 3.7 Step 8: Scoring and output

- Each rule emits `Evidence(kind, label, weight, detail, source)`.
- **Per-label confidence** = noisy-OR `1 − Π(1 − wᵢ)`, with a bonus when independent sources agree (text + image + X + trend) and a penalty for conflicts.
- **Starting weights:**

  | Evidence | Weight |
  |---|---|
  | gazetteer hit in the name | 0.6–0.8 |
  | gazetteer hit only in the description | 0.3–0.5 |
  | emoji keyword | 0.3 |
  | logo/template hash match | 0.8 |
  | known-coin copy | 0.9 |
  | CLIP label | 0.2–0.5 |

  Later, fit the weights on 300–500 hand-labelled tokens with logistic regression (still classical).
- **The referent is chosen separately from the categories:** the best entity among known coins, gazetteer entities, trend terms and meme templates. If the top two are within 0.1 of each other, add a caveat.
- **Summary:** template-generated (never free text), with up to 5 evidence bullets. Caveats are added automatically when:
  - there is only one weak source;
  - the referent is ambiguous;
  - homoglyphs are present;
  - the image is missing;
  - the tweet was deleted or couldn't be fetched.
- **Taxonomy** (keep it in config):
  - `animal/{dog,cat,frog,monkey,hippo,squirrel,bird,bear_bull,fish,other}`
  - `meme_template/{pepe_wojak_chad,x_wif_hat,chill_guy,npc,brainrot,copypasta,other}`
  - `ai_agent`, `political`, `celebrity/{elon,musician,athlete,streamer_kol,other}`, `news_event`
  - `food_object_abstract`, `regional_language`, `crypto_native/{slang,pumpfun_meta,cto,utility_claim}`
  - `derivative/{copycat,template_family,sequel,homoglyph_spoof,logo_reuse}`, `humor_nsfw_offensive`

---

## 4. Output contract (version it; consumers ignore unknown fields)

```jsonc
{
  "schema_version": "1",
  "mint": "…", "created_at": "2026-10-05T12:00:00Z", "launchpad": "pump.fun",
  "market": { "complete": false, "curve_progress": 0.42, "creator": "…", "quote_mint": "SOL" },
  "raw": { "name": "Peanut the Squirrel 2.0", "symbol": "PNUT2", "description": "…",
           "twitter": "…", "telegram": null, "website": null },
  "normalized": { "name_tokens": ["peanut","the","squirrel","2.0"], "ticker_base": "PNUT",
                  "markers": ["version:2"], "emoji_keywords": ["squirrel"], "obfuscation": [] },
  "referent": { "label": "Peanut (squirrel)", "kind": "famous_animal",
                "desc": "Pet squirrel seized by NY officials (Oct 2024)", "source": "wikidata:Q…", "confidence": 0.86 },
  "categories": [ {"label": "derivative", "confidence": 0.92}, {"label": "animal/squirrel", "confidence": 0.80} ],
  "ticker_explanation": "PNUT = vowel-dropped 'peanut'; '2' = sequel marker",
  "copy_of": [ {"ticker": "PNUT", "mint": "…", "signals": ["ticker_base", "name", "logo_phash:6"]} ],
  "image": { "phash": "…", "ocr": ["$PNUT"], "palette": ["#c87f3a"], "near_duplicates": [],
             "labels": [], "nsfw": "safe", "display_url": "…" },
  "x": { "ref": {"kind": "tweet", "tweet_id": "…", "url_handle": "…"}, "object_time": "…",
         "predates_token_by_s": 10800, "author": {"handle": "…", "verified_type": null, "followers": 0,
         "username_changes": 0}, "text": "…", "relation": "narrative_reference",
         "reuse_count": 37, "fetch_source": "fxtwitter", "status": "ok" },
  "trend": { "matched": false, "terms": [] },
  "flags": [ {"code": "spoofed_tweet_handle", "severity": "high", "detail": "…"} ],
  "summary": "…", "evidence": [ {"kind": "known_coin_match", "label": "derivative", "weight": 0.9,
               "detail": "ticker PNUT2 → base PNUT = known coin $PNUT", "source": "known_coins:pnut"} ],
  "caveats": [ "…" ], "depth": "full", "analyzed_at": "…",
  "versions": { "rules": "0.1.0", "lexicon": "2026-10-05", "known_coins": "2026-10-04" }
}
```

---

## 5. Domain cheat sheet

**pump.fun basics:**
- On-chain storage holds **only name (≤32 chars), symbol (≤13) and uri (≤200)**. Description, image and socials are in the off-chain JSON.
- Coins trade on a bonding curve and **graduate** at ~85 SOL to PumpSwap (`pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`); before ~Mar 2025 they graduated to Raydium.
- 2025–26 additions:
  - `create_v2`/Token-2022;
  - Mayhem mode;
  - **non-SOL quote mints (USDC)**, so "market cap in SOL" isn't always SOL;
  - holder-reward coins.
- Volume: ~30k launches/day, about 69% dead on day one, <2% graduate. Every popular name gets cloned within minutes.

**Where meaning comes from** (seed a known-coins table from CoinGecko categories `meme-token`, `solana-meme-coins`, `pump-fun`, and verify the facts):
- **Animal or famous animal:** $WIF (dog wearing a hat), $BONK, $POPCAT, $MEW, $MOODENG (baby pygmy hippo), $PNUT (Peanut the Squirrel), $FWOG, $MICHI.
- **Meme template:** $CHILLGUY, $GIGA, "X wif hat".
- **Templated derivatives:** Baby X, X 2.0, X Classic, X Inu.
- **Copycats:** the same ticker, homoglyphs, a re-uploaded logo.
- **News / viral event.**
- **Celebrity / Elon tweet.**
- **Political.**
- **AI agent:** $GOAT (pushed by the Truth Terminal bot), $AI16Z, $ZEREBRO.
- **Crypto-native slang / self-referential:** $FARTCOIN, CTO coins.
- **Regional / CJK-name metas.**
- **NSFW / offensive:** detect and suppress.

**Ticker conventions:**
- vowel dropping (PNUT), acronyms, lore tickers (MEW);
- baby talk (wif = with, fwog = frog, smol, wen, ser);
- obfuscation (leet, full-width, small caps, Cyrillic, zero-width). **Obfuscation is itself a signal.**

---

## 6. Dependencies and resource budget (Python reference)

| Purpose | Library (licence) | RAM |
|---|---|---|
| Unicode | `anyascii` (ISC), `confusable_homoglyphs` (MIT), `emoji` (BSD) + CLDR annotations JSON | ~20 MB |
| Segmentation | `wordsegment` (Apache-2.0); `wordninja` (MIT) for low RAM | ~100 MB / ~28 MB |
| Matching | `rapidfuzz` (MIT), `jellyfish` (MIT), `pyahocorasick` (BSD) | small |
| Images | `Pillow`, `ImageHash` (BSD), `pdqhash` (MIT bindings) | ~60 MB |
| OCR | `rapidocr_onnxruntime` (Apache-2.0) + `onnxruntime` (MIT) | ~130 MB + 44 MB |
| NSFW | open_nsfw ONNX (BSD-2) | ~100 MB (est.) |
| Optional visual labels | CLIP ViT-B/32 vision ONNX (MIT) | ~500 MB |
| HTTP | `httpx` (async), `feedparser` for RSS | small |

Totals: text-only ≈ 200 MB; plus image + OCR + NSFW ≈ 400–450 MB; plus CLIP ≈ 1 GB. On a 512 MB host, isolate image/OCR in a worker, or skip OCR at `basic` depth.

---

## 7. Knowledge the module needs (refreshed by scheduled jobs, never fetched live at request time)

| Knowledge | Source | Refresh |
|---|---|---|
| Known coins (name, ticker, aliases, lore, categories, logo pHash) | CoinGecko free Demo key (~100 calls/min, 10k/month), plus every token you analyse | weekly |
| Trending entities | Wikimedia pageviews top-1000, Wikidata aliases | daily |
| Entity gazetteer (memes, famous animals, celebrities, politicians, AI bots, countries) | Wikidata SPARQL, run offline | monthly |
| Noun classes (animals, foods, objects) | WordNet, precomputed to JSON | build time |
| Slang lexicon, meme-template hashes, taxonomy, markers | hand-curated YAML/JSON | by hand |
| Emoji meanings | Unicode CLDR `annotations.json` | per release |

---

## 8. Build order and acceptance tests

1. **Smoke test from the real deployment environment first.** None of these endpoints could be tested live during research; datacenter IPs are often treated differently. Check:
   - RPC `getAccountInfo` for ~30 test CAs: legacy and v2 pump coins, graduated coins, a letsbonk coin, a plain SPL token, a wallet, junk input;
   - IPFS gateways;
   - frontend-api `coins-v2` and `coins/search`;
   - DexScreener search;
   - FxTwitter, vxTwitter, syndication and oEmbed;
   - CoinGecko, Wikimedia and Google News RSS.

   Record each as works, flaky or blocked.
2. **Contract first:** the result types (§4) and the entry point returning stubs, so host integration can start in parallel.
3. **Resolution + metadata** (§3.1–3.2), with the error cases, caching and single-flight.
4. **`basic` engine** (§3.4–3.5 without OCR, §3.3 free part, §3.7) with a **golden test set** of ≥60 hand-written cases that run with no network. It must include:
   - `dogwifhat`, `catwifhat`, `Trump wif Hat` (must **not** be tagged dog);
   - `BPNUT`, `m00 deng classic`, `dogwifhat2.0`, `ʙᴀʙʏ ᴘɴᴜᴛ`, Cyrillic `Рepe`, `$Ｐ​ＮＵＴ` (full-width + zero-width), `p3anu7`, `🐿🥜`;
   - `justachillguy`, `AIAgentSupercycle`, a CJK name, a spoofed tweet-URL handle, a reused tweet.

   Also record real RPC/IPFS responses for ~20 CAs as offline end-to-end fixtures.
5. **`full` depth:** X fetch chain with circuit breakers and caching, OCR, trend + news.
   - Accept when an upstream outage yields `partial`, never an exception.
6. **Calibration:** label 300–500 real tokens, fit weights, and tune the hash/CLIP thresholds on real logos.
   - Target: top-1 category precision ≥ 0.8; referent correct on ≥ 70% of tokens where a human can identify one.
7. **Optional:** corpus ingester for stronger copycat context (records every new pump.fun coin from a free feed, e.g. RPC `logsSubscribe` on the pump program or PumpPortal `subscribeNewToken`), then CLIP.

---

## 9. Security, legal and gotchas

- **SSRF:** metadata `uri`, `image` and `website` are attacker-controlled. The fetcher must:
  - allow only `https`;
  - resolve DNS and **reject private, loopback, link-local and metadata IPs**, then connect to the checked IP;
  - allow ≤3 redirects, re-checking each;
  - cap size and time;
  - send no cookies or secrets.
- **Image bombs:** pixel caps, frame limits, per-job timeouts.
- **Output strings are untrusted:** the host must escape them when rendering. Return only normalised `https` URLs; `javascript:`/`data:` become `null`.
- **NSFW:** never return a displayable image URL unless the verdict is `safe`. Don't store image bytes; keep hashes and labels only.
- **Licences:** `anyascii`, not `Unidecode` (GPL). NudeNet is AGPL. Wiktionary data is CC BY-SA. MobileCLIP uses an Apple licence (review it).
- **ToS:**
  - pump.fun's frontend API is undocumented, so keep it optional and throttled.
  - X's terms forbid scraping, and FxTwitter/vxTwitter are third-party. The sanctioned paths are the official pay-per-use API and oEmbed. **The owner decides the risk appetite.**
  - Don't scrape Know Your Meme.
- **Not financial advice:** label flags as informational.
- **Parser gotchas:**
  - `CreateEvent` and `BondingCurve` layouts grow over time, so decode leading fields and tolerate extra or missing tail bytes.
  - Match log lines `Program log: Instruction: Create` / `CreateV2` **exactly**; substrings hit `CreatePool`.
  - Skip failed transactions (`err != null`).

---

## 10. Open questions for the project owner

1. Placement: in-process module (A) or sidecar service (B)? What is the host stack?
2. Expected call rate, and must answers be synchronous? This decides the default depth (`basic` vs `full`).
3. Analyse non-pump.fun mints best-effort, or reject them with `not_pumpfun`?
4. Enable the paid X fallback (and a daily spend cap)? What is the risk appetite for the unofficial X mirrors and pump.fun's frontend API?
5. Is the optional corpus ingester wanted (better copycat answers, more bandwidth and storage)?
6. NSFW model choice (open_nsfw vs AGPL NudeNet), and whether CLIP's RAM cost is acceptable.

---

## Appendix A: CA resolution and pump.fun decoders (Python, tested)

Tested against real data:
- `bonding_curve_pda()` reproduces the bonding curve of 7 real mints;
- `decode_create_event()` decodes 6 real 2026 `Program data:` lines, including non-IPFS launcher URIs and mints without the "pump" suffix.

The two files sit side by side: `pump_ca.py` imports `b58encode` from `pump_event.py`. Both are dependency-free. In production you may swap the address math for `solders`.

### `pump_event.py`: CreateEvent decoder and base58

```python
"""Tolerant decoder for pump.fun CreateEvent `Program data:` log lines (reference)."""
from __future__ import annotations

import base64
import struct

CREATE_EVENT_DISC = bytes.fromhex("1b72a94ddeeb6376")
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


class _R:
    def __init__(self, buf: bytes):
        self.b, self.o = buf, 0

    def left(self) -> int:
        return len(self.b) - self.o

    def take(self, n: int) -> bytes:
        if self.left() < n:
            raise EOFError
        v = self.b[self.o:self.o + n]
        self.o += n
        return v

    def string(self) -> str:
        (n,) = struct.unpack("<I", self.take(4))
        return self.take(n).decode("utf-8", "replace")

    def pubkey(self) -> str:
        return b58encode(self.take(32))

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def i64(self) -> int:
        return struct.unpack("<q", self.take(8))[0]

    def boolean(self) -> bool:
        return self.take(1) != b"\0"


# (field, reader) in IDL order. Fields are appended over time; older events are shorter.
_FIELDS = [
    ("name", "string"), ("symbol", "string"), ("uri", "string"),
    ("mint", "pubkey"), ("bonding_curve", "pubkey"), ("user", "pubkey"), ("creator", "pubkey"),
    ("timestamp", "i64"), ("virtual_token_reserves", "u64"), ("virtual_sol_reserves", "u64"),
    ("real_token_reserves", "u64"), ("token_total_supply", "u64"),
    ("token_program", "pubkey"), ("is_mayhem_mode", "boolean"), ("is_cashback_enabled", "boolean"),
    ("quote_mint", "pubkey"), ("virtual_quote_reserves", "u64"), ("creator_fee_bps", "u64"),
    ("is_holder_reward", "boolean"),
]
_REQUIRED = {"name", "symbol", "uri", "mint", "bonding_curve", "user"}


def decode_create_event(program_data_b64: str) -> dict | None:
    """Return the decoded event, or None if the line is not a CreateEvent."""
    raw = base64.b64decode(program_data_b64)
    if raw[:8] != CREATE_EVENT_DISC:
        return None
    r, out = _R(raw[8:]), {}
    for name, kind in _FIELDS:
        try:
            out[name] = getattr(r, kind)()
        except EOFError:
            break
    if not _REQUIRED <= out.keys():
        raise ValueError("truncated CreateEvent")
    out["_unparsed_tail_bytes"] = r.left()  # >0 means the IDL grew: log it
    return out
```

### `pump_ca.py`: CA validation, bonding-curve address, BondingCurve decoder

```python
"""CA (mint) helpers for TokenSage (reference; tested in test_pump_ca.py).

- validate a Solana base58 address
- derive the pump.fun bonding-curve PDA for a mint: seeds ["bonding-curve", mint]
- decode the BondingCurve account (tolerant of older, shorter layouts)

Production code may use `solders` (Pubkey.find_program_address) instead of the
pure-Python ed25519 on-curve check below.
"""
from __future__ import annotations

import hashlib
import struct

from pump_event import b58encode

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
BONDING_CURVE_DISC = bytes([23, 183, 248, 55, 96, 216, 172, 96])
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_IDX = {c: i for i, c in enumerate(_B58)}


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58_IDX[c]  # KeyError on invalid characters
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(s) - len(s.lstrip("1"))) + body


def parse_ca(raw: str) -> str:
    """Return the canonical mint address or raise ValueError. Accepts surrounding
    whitespace and pump.fun / explorer URLs ending in the address."""
    s = raw.strip().rstrip("/").split("/")[-1].split("?")[0]
    if not 32 <= len(s) <= 44:
        raise ValueError("not a Solana address (length)")
    try:
        b = b58decode(s)
    except KeyError:
        raise ValueError("not a Solana address (base58)") from None
    if len(b) != 32:
        raise ValueError("not a Solana address (32 bytes)")
    return s


# --- ed25519 on-curve check (needed to emulate find_program_address) ---
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def _on_curve(b: bytes) -> bool:
    y = int.from_bytes(b, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    y2 = y * y % _P
    u, v = (y2 - 1) % _P, (_D * y2 + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    if x2 == 0:
        return True
    return pow(x2, (_P - 1) // 2, _P) == 1  # quadratic residue => decompressible


def find_program_address(seeds: list[bytes], program_id: str) -> tuple[str, int]:
    pid = b58decode(program_id)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + pid + b"ProgramDerivedAddress").digest()
        if not _on_curve(h):
            return b58encode(h), bump
    raise ValueError("no viable bump")


def bonding_curve_pda(mint: str) -> str:
    return find_program_address([b"bonding-curve", b58decode(mint)], PUMP_PROGRAM)[0]


_BC_FIELDS = [
    ("virtual_token_reserves", "<Q"), ("virtual_quote_reserves", "<Q"),
    ("real_token_reserves", "<Q"), ("real_quote_reserves", "<Q"),
    ("token_total_supply", "<Q"), ("complete", "?"), ("creator", "pk"),
    ("is_mayhem_mode", "?"), ("is_cashback_coin", "?"), ("quote_mint", "pk"),
    ("creator_fee_bps", "<Q"), ("can_edit_creator_fee", "?"), ("is_holder_reward", "?"),
]


def decode_bonding_curve(data: bytes) -> dict:
    """Decode BondingCurve account data (from getAccountInfo, base64-decoded).
    Older accounts are shorter: missing trailing fields are simply absent.
    A creator of all zeros / quote_mint of all zeros means 'unset' / SOL."""
    if data[:8] != BONDING_CURVE_DISC:
        raise ValueError("not a pump.fun BondingCurve account")
    o, out = 8, {}
    for name, fmt in _BC_FIELDS:
        size = 32 if fmt == "pk" else struct.calcsize(fmt)
        if o + size > len(data):
            break
        chunk = data[o:o + size]
        out[name] = b58encode(chunk) if fmt == "pk" else struct.unpack(fmt, chunk)[0]
        o += size
    return out
```

### Test vectors

```python
# frontend-api sample: mint -> bonding curve
assert bonding_curve_pda("3arUrpH3nzaRJbbpVgY42dcqSq9A5BFgUxKozZ4npump") == "45YS7EqqWbhug1w5p2iAyVJb4JrqtS3T6mRpjb6Nz3fS"

# real CreateEvent (Program data: line), 2026
e = decode_create_event("G3KpTd7rY3YLAAAAZG9nIHdpZiBjYXADAAAAY2FwUAAAAGh0dHBzOi8vaXBmcy5pby9pcGZzL2JhZmtyZWlncWt4c2lidHo1dGdlbHFlY2FnemhvNjZ3aG1xZHlzcnh4Y29yZ2ZuZml1Y3Zwcmt4dnppLaC2+V3Nh0HtdYKwc8/iu1Rv43LC2I3Xre76gUtUtl9hbg9dt5I6BnqHrQks9w64JoU7a+OAC5ddjukh4X9cpqpE0/SoD/gUsZ4GscW7mysoQU4ciKzgqTwazrOwWIpTqkTT9KgP+BSxngaxxbubKyhBThyIrOCpPBrOs7BYilOnWqlqAAAAAAAQ2EfjzwMAAKwj/AYAAAAAeMX7UdECAACAxqR+jQMABt324e51j94YQl285GzN2rYa/E2DuQ0n/r35KNihi/wBAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAKwj/AYAAAAAAAAAAAAAAAA=")
assert (e["name"], e["symbol"]) == ("dog wif cap", "cap")
assert e["mint"] == "457V2vvjqXTMFzivq9tvBqhDaxfke2523hHDB6brpump"
assert bonding_curve_pda(e["mint"]) == e["bonding_curve"]
```

## Appendix B: X/Twitter link parser, snowflake time, syndication token (Python, tested)

Tested on 18 URL shapes. `syndication_token()` is a port of JavaScript's `Number.prototype.toString(36)` and matched Node's output on 2,300 random IDs.

```python
"""Reference implementations for TokenSage (tested; see test_xref.py)."""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qs, unquote

X_HOSTS = {
    "twitter.com", "x.com", "mobile.twitter.com", "mobile.x.com", "m.twitter.com",
    "fxtwitter.com", "fixupx.com", "vxtwitter.com", "fixvx.com", "twittpr.com",
    "nitter.net", "xcancel.com", "api.fxtwitter.com", "api.vxtwitter.com",
    "d.fxtwitter.com", "d.fixupx.com",
}
RESERVED = {
    "i", "home", "explore", "search", "hashtag", "settings", "notifications", "messages",
    "intent", "share", "login", "signup", "tos", "privacy", "compose", "communities",
    "lists", "status", "web",
}
HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
ID = re.compile(r"^\d{1,20}$")
TWITTER_EPOCH_MS = 1288834974657


def parse_x_ref(raw: str | None) -> dict:
    """Classify a pump.fun `twitter` metadata value into a typed X reference.

    NOTE: the handle in a status URL is NOT trustworthy (X ignores it). Always use
    the author returned by the fetch.
    """
    if raw is None:
        return {"kind": "empty"}
    s = re.sub(r"""^[<"'(\[]+|[>"')\],.]+$""", "", str(raw).strip())
    if not s:
        return {"kind": "empty"}
    if re.fullmatch(r"@?[A-Za-z0-9_]{1,15}", s) and not s.lstrip("@").isdigit():
        return {"kind": "profile", "handle": s.lstrip("@")}
    if not re.match(r"^[a-z]+://", s, re.I):
        s = "https://" + s
    try:
        u = urlsplit(s)
        host = (u.hostname or "").lower()
    except ValueError:
        return {"kind": "invalid", "raw": raw}
    if not host or " " in s:
        return {"kind": "invalid", "raw": raw}
    host = host.removeprefix("www.")
    if host == "t.co":
        return {"kind": "shortlink", "url": s, "needs_resolve": True}
    if host not in X_HOSTS:
        return {"kind": "foreign", "host": host, "url": s}
    seg = [unquote(p) for p in u.path.split("/") if p]
    low = [p.lower() for p in seg]
    q = parse_qs(u.query)
    get = lambda k: (q.get(k) or [None])[0]  # noqa: E731
    at = lambda i: seg[i] if len(seg) > i else ""  # noqa: E731

    if low[:2] == ["i", "communities"] and ID.match(at(2)):
        return {"kind": "community", "community_id": seg[2]}
    if low[:1] == ["communities"] and ID.match(at(1)):
        return {"kind": "community", "community_id": seg[1]}
    for si, p in enumerate(low):
        if p in ("status", "statuses") and ID.match(at(si + 1)):
            h = seg[si - 1] if si > 0 and HANDLE.match(seg[si - 1]) and low[si - 1] not in RESERVED else None
            return {"kind": "tweet", "tweet_id": seg[si + 1], "url_handle": h}
    if low[:2] == ["i", "lists"] and ID.match(at(2)):
        return {"kind": "list", "list_id": seg[2]}
    if low[:2] == ["i", "user"] and ID.match(at(2)):
        return {"kind": "profile", "user_id": seg[2]}
    if low[:1] == ["intent"] and len(low) > 1 and low[1] in ("user", "follow"):
        h, uid = get("screen_name"), get("user_id")
        if h and HANDLE.match(h):
            return {"kind": "profile", "handle": h}
        if uid and ID.match(uid):
            return {"kind": "profile", "user_id": uid}
    if low[:1] == ["search"]:
        return {"kind": "search", "query": get("q") or ""}
    if low[:1] == ["hashtag"] and len(seg) > 1:
        return {"kind": "search", "query": "#" + seg[1]}
    if seg and HANDLE.match(seg[0]) and low[0] not in RESERVED:
        return {"kind": "profile", "handle": seg[0]}
    if not seg:
        return {"kind": "homepage"}
    return {"kind": "unknown", "url": s}


def snowflake_time(id_: str | int) -> datetime | None:
    """Creation time of a tweet / community / user id (snowflakes, Nov 2010+)."""
    n = int(id_)
    if n < 2**22 * 1000:  # pre-snowflake ids (e.g. tweet 20)
        return None
    return datetime.fromtimestamp(((n >> 22) + TWITTER_EPOCH_MS) / 1000, tz=timezone.utc)


def _js_float_to_base36(x: float) -> str:
    """Port of V8's Number.prototype.toString(36) (DoubleToRadixCString)."""
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    integer = math.floor(x)
    fraction = x - integer
    delta = max(0.5 * (math.nextafter(x, math.inf) - x), math.nextafter(0.0, 1.0))
    frac_digits = []
    if fraction >= delta:
        while True:
            fraction *= 36
            delta *= 36
            digit = int(fraction)
            frac_digits.append(digit)
            fraction -= digit
            if fraction > 0.5 or (fraction == 0.5 and (digit & 1)):
                if fraction + delta > 1:
                    # round up and propagate carry
                    i = len(frac_digits) - 1
                    while True:
                        if i < 0:
                            integer += 1
                            break
                        frac_digits[i] += 1
                        if frac_digits[i] < 36:
                            break
                        frac_digits[i] = 0
                        i -= 1
                    break
            if fraction < delta:
                break
    int_digits = ""
    n = int(integer)
    while True:
        n, r = divmod(n, 36)
        int_digits = chars[r] + int_digits
        if n == 0:
            break
    out = int_digits
    if frac_digits:
        # strip trailing zeros produced by carry
        while frac_digits and frac_digits[-1] == 0:
            frac_digits.pop()
        if frac_digits:
            out += "." + "".join(chars[d] for d in frac_digits)
    return out


def syndication_token(tweet_id: str | int) -> str:
    """Token for cdn.syndication.twimg.com/tweet-result (same as vercel/react-tweet)."""
    s = _js_float_to_base36((float(int(tweet_id)) / 1e15) * math.pi)
    return re.sub(r"(0+|\.)", "", s) or "0"
```

### Test vectors

```python
assert parse_x_ref("https://x.com/elonmusk/status/1791351500217754008?s=20&t=abc") == \
    {"kind": "tweet", "tweet_id": "1791351500217754008", "url_handle": "elonmusk"}  # url_handle is NOT trusted
assert parse_x_ref("https://x.com/i/communities/1804846498066116981/about")["kind"] == "community"
assert parse_x_ref("@pumpdotfun") == {"kind": "profile", "handle": "pumpdotfun"}
assert parse_x_ref("https://t.co/AbCdEf123")["kind"] == "shortlink"
assert parse_x_ref("https://pump.fun/coin/abc")["kind"] == "foreign"
assert snowflake_time("1804846498066116981").isoformat().startswith("2024-06-23T11:58:32")
assert syndication_token("20") == "6dq1a2xwd93"
assert syndication_token("1577730467436138524") == "3tol417ti8o"
```
