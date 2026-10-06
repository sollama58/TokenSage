# TokenSage API (v1) — integration guide

For the developer of the application that calls TokenSage. The machine-readable contract is
`openapi.v1.json` in the repo root, also served live at `GET /openapi.json`; generate a typed
client from it.

> **Status:** the contract is final for v1 and every field is now populated. `depth=basic`
> gives on-chain/metadata facts plus the meaning analysis (`referent`, `categories`,
> `ticker_explanation`, `copy_of`, `normalized`, `image` hashes/near-duplicates, `flags`,
> `summary`, `evidence`). `depth=full` additionally fills `x.author`/`x.text`/`x.relation` from
> the fetched tweet or profile, `image.ocr`, `trend`, and the X account-quality flags. Only
> `image.labels` (optional local CLIP, Phase 8) stays empty. `versions.rules` tells you which
> build produced a document.

## Try it in a browser

`GET /` on the service serves a test console: paste your API key, enter a CA, and see the request
flow and the rendered result. Useful for checking what a given CA returns before wiring code.

## Base URL and auth

- Base URL: `https://<tokensage-api host>` (Render: `https://tokensage-api.onrender.com`).
- Every `/v1` call needs `Authorization: Bearer <api key>`. Keys are issued by the TokenSage
  owner. Missing or unknown key → `401`.
- Each key has a per-minute rate limit (default 60/min). Exceeding it → `429 rate_limited` with
  `Retry-After` (seconds).
- Each key also has **daily quotas** (UTC day) for the expensive calls: `depth=full` analyses
  (default 2000/day) and `refresh=true` re-analyses (default 200/day). Cached reads and
  `depth=basic` work are not quota-limited. A unit is charged only when a request creates a
  new analysis job: re-requesting a pending analysis (the 202 → retry loop), polling
  `GET /v1/jobs/{id}`, or a batch item that joins an already-open job costs nothing, even after
  the quota is used up. Exceeding a quota → `429 quota_exceeded` with `Retry-After` set to the
  seconds until UTC midnight. The owner can see today's counters per key.
- `GET /v1/tokens/{ca}` and `POST /v1/tokens:batch` responses carry today's remaining quota in
  `X-Quota-Full-Remaining` and `X-Quota-Refresh-Remaining`, so you can back off before a `429`.
- Send an `X-Request-Id` header if you want to correlate logs; otherwise one is generated.
  The response echoes it in `X-Request-Id` and in every body's `request_id`.

## The main call

```
GET /v1/tokens/{ca}?depth=full&wait=10
```

`{ca}` is the token's mint address (bare base58, or a pump.fun/explorer URL ending in it).

| Query param | Default | Meaning |
|---|---|---|
| `depth` | `full` | `basic` (fast, text + image hashes, no X fetch) or `full` (adds OCR, X content, trends) |
| `wait` | `10` | Seconds to wait for a fresh analysis before answering `202` (0–25) |
| `max_age` | by token age | Accept a cached analysis up to this many seconds old. Defaults: 5 min for tokens < 1 h old, 1 h up to 7 days, 24 h after that |
| `refresh` | `false` | Force a new analysis (rate-limited more strictly) |
| `include` | all parts | Comma list of optional parts to keep: `evidence`, `raw`. Omit it to get everything; a part you leave out is emptied (`evidence: []`, `raw` fields null). `include=evidence` drops `raw`; `include=raw` drops `evidence` |

### Status codes

| HTTP | Meaning | What to do |
|---|---|---|
| `200` | Analysis in body. `status` is `complete`, `partial` (some upstream source failed; see `errors` and `analysis.caveats`) or `failed` (the analysis job failed; `errors[0].detail` says why, `job_id` names the job). A failure is reported as `failed` for 10 minutes without starting a new job or spending quota; pass `refresh=true` to retry at once. The same applies per item in a batch | Use it; for `failed`, retry later or with `refresh=true` |
| `202` | Not ready yet. Body has `status: "pending"`, a `job_id`, and `stale_analysis` if an older result exists | Retry the same URL after `Retry-After` seconds (≈3 s), or poll `GET /v1/jobs/{job_id}`. Give up after ~60 s total |
| `400` | `invalid_ca`: not a Solana address; `invalid_callback_url` (batch only) | Don't retry |
| `401` | `unauthorized` | Fix the key |
| `404` | `token_not_found`: no account on-chain (very new tokens may appear after a few seconds) | Retry once after a few seconds, then treat as unknown |
| `422` | `not_a_token_mint` (e.g. a wallet address) or `not_pumpfun` (only if the service is configured to reject non-pump.fun mints) | Don't retry |
| `429` | `rate_limited` (per-minute) or `quota_exceeded` (daily full/refresh quota) | Back off per `Retry-After`; for `quota_exceeded`, fall back to `depth=basic` or a cached read |
| `503` | `overloaded`: queue full or RPC down | Back off per `Retry-After` |

Every error has one shape:

```json
{ "error": { "code": "invalid_ca", "message": "not a Solana address (base58)", "request_id": "…" } }
```

### Response envelope

```jsonc
{
  "ca": "3arUrpH3nzaRJbbpVgY42dcqSq9A5BFgUxKozZ4npump",
  "status": "complete",                 // complete | partial | pending | failed
  "depth": "full",
  "analysis": { … },                    // the Analysis document, see below
  "stale_analysis": null,               // on 202: an older analysis if one exists
  "freshness": { "analyzed_at": "…", "age_s": 42, "max_age_s": 300, "from_cache": true },
  "errors": [],                         // upstream problems behind a "partial" result
  "job_id": null,                       // set on 202
  "request_id": "…"
}
```

### The Analysis document (what you actually want)

| Field | Meaning |
|---|---|
| `schema_version` | `"1"`. Adding fields/labels is compatible; breaking changes get a new version and path |
| `mint`, `created_at`, `launchpad` | Canonical CA; token creation time (may be `null`); `pump.fun` or `unknown` |
| `market` | Bonding-curve state: `complete` (graduated), `curve_progress` 0–1, `creator`, `quote_mint`, and `pair` (the token it trades against; see below) |
| `raw` | Name, symbol, description and social links as found in the metadata (**untrusted text, escape before rendering**) |
| `normalized` | Cleaned tokens, ticker base, version markers (`version:2`), emoji keywords, obfuscation flags |
| `referent` | What the token refers to: `label`, `kind`, `desc`, `source`, `confidence`, and `supported_by` (the inputs pointing at it: name, symbol, description, image, x, trend, chain; several independent ones make it more trustworthy). May be `null` |
| `categories[]` | Multi-label with confidences. Labels come from `GET /v1/meta`; expect new ones over time |
| `ticker_explanation` | Plain-language explanation of the ticker |
| `copy_of[]` | Coins this one copies or derives from, with the signals that say so, `created_at` and `recent`. **Copycat = copying a coin launched in the 30 days before this one** (same name/ticker, or a near-identical logo of an earlier token): `recent: true`, category `derivative/copycat`, flag `copycat`. Older namesakes are ignored. A well-known established coin (Bonk, Pepe, …) is a reference: `recent: false`, category `derivative/reference`, flag `references_known_coin` (info). The window is `COPYCAT_WINDOW_DAYS` (default 30) |
| `image` | Hashes, OCR text, palette, near-duplicates, optional visual labels. `source_url` is the gateway URL. **Images are not screened for NSFW content; decide yourself whether to show them** |
| `x` | The linked X/Twitter reference, its creation time, whether it predates the token, the fetched author (handle, followers, verification type, join date, username changes), text, `relation` (`narrative_reference` = the coin is *about* someone else's earlier tweet; `launch_announcement`; `official_account`; `spoofed` = the URL's handle is not the tweet's real author; `search_only`), `status` (`ok`, `deleted`, `suspended`, `not_fetched`, `failed`), `match` (post vs token, see below), `reuse_count` (other tokens linking the same tweet/handle; a Community link has `status: "not_fetched"`, because no free source serves Community name, description or creation time: X's own API and twitterapi.io both charge for it), `quoted` when the linked tweet is a quote tweet and `replied_to` when it is a reply: that post's `id`, `url`, `status`, `author`, `text`, `created_at` and `predates_token_by_s` (a reply whose parent could not be fetched still names its author). A launch post that quotes or answers someone else's earlier post usually takes its meaning from that post, so its text feeds the analysis too, and a large or verified author of it raises `borrowed_narrative`. `accounts` lists everyone involved (`role`: `author`, `quoted_author`, `replied_to_author`, `mentioned`; `handle`, display `name`, `followers`, `verified_type`). Their names and handles are read like text, more weakly than the posts themselves (0.4× for the poster, 0.5× for quoted and replied-to authors, 0.3× for @mentions), so a reply to @elonmusk points at Elon Musk; that evidence says which account it came from |
| `trend` | Trending-topic matches (Wikipedia spikes, news headlines) |
| `flags[]` | `{code, severity, detail}`. Codes and descriptions are listed by `GET /v1/meta` |
| `summary` | Template-generated plain-language summary. It includes a "Context:" sentence pulling the whole picture together: what it trades against, what its X post replies to or quotes, which recent coin it copies, and which trend it matches |
| `evidence[]` | Why: `{kind, label, weight, detail, source, url, where}`; `where` is the input it came from (`name`, `symbol`, `description`, `image`, `x`, `trend`, `chain`, `db`) |
| `caveats[]` | Automatic caveats (single weak source, ambiguous referent, deleted tweet, …) |
| `depth`, `analyzed_at`, `versions` | What was run, when, and with which rule/lexicon versions. A cached analysis made by older rules (before a deploy) is re-run on the next request unless you pass `max_age` |

Confidences are scores in 0–1. **They are uncalibrated until Phase 6** of the build plan, which
fits them against hand-labelled tokens so that about 80% of "0.8" labels are right. Until then,
use them to rank and threshold, not as probabilities. Flags and categories are **informational,
not financial advice**.

### `x.match`: does the linked post match the token? (depth=full)

The blended analysis above folds the post text into the coin's meaning. `x.match` keeps the
two apart and compares them, so a coin whose link points at an unrelated post no longer looks
like a perfect match. It is present at `depth=full` for tweet and profile links. For a profile
link, the display name, handle, bio, avatar and banner stand in for the post.

```json
"match": {
  "name":     {"score": 1.0, "how": "exact", "detail": "the post contains the name 'Pepe Wizard'"},
  "ticker":   {"score": 1.0, "how": "cashtag", "detail": "the post names $PWIZ"},
  "image":    {"score": 0.8, "best_distance": 9, "media_checked": 1,
               "detail": "best of 1 post image(s) is an edited copy of the logo (distance 9)"},
  "referent": {"x_label": null, "x_kind": null, "agrees": null, "confidence": 0.0},
  "x_categories": [{"label": "animal/frog", "confidence": 0.41}],
  "fit": 0.987,
  "verdict": "about_this_coin"
}
```

| Part | How it is computed |
|---|---|
| `name` | The token name against the post text (with the quoted and replied-to posts and the display names of their authors), with the same folding as names: homoglyphs, leet, emoji keywords, camelCase. `how` is one of: `exact` (the name as written appears in the post), `normalized` (it appears once both are folded and compacted), `segment` (some or all of the name's words appear; `score` is the share found), `fuzzy` (a close spelling), or `none` |
| `ticker` | `cashtag` (`$TICKER`), `hashtag` (`#TICKER`), `bare` (the ticker as a word, or the profile handle; a lowercase everyday word like "dog" does not count), `fuzzy` (a cashtag one letter off), or `none` |
| `image` | Up to 4 images are fetched through the same guarded fetch, size caps and time caps as logos, and cached by URL. These are the post's photos and video thumbnails, then the quoted and replied-to posts' media, or a profile's avatar and banner. Each is perceptually hashed and compared with the logo. `best_distance` is the smallest Hamming distance: ≤ 8 is the same image (score 1.0), ≤ 14 an edited copy (0.8), ≤ 20 loosely similar (0.35) |
| `referent` | The engine is run on the post text alone, and separately on the token's name, ticker and image alone. `x_label` is what the post is about. `agrees` says whether that is the same referent the token points to on its own; it is `null` when either side has no confident referent |
| `x_categories` | Categories of the post read on its own (not blended) |
| `fit` | One 0–1 score combining the parts above (noisy-OR). Clear referent disagreement halves it. **Uncalibrated until Phase 6** |
| `verdict` | `about_this_coin` if `fit ≥ 0.6`, `related` if `0.2 ≤ fit < 0.6`, `unrelated` if `fit < 0.2`, `unknown` when nothing could be fetched (deleted tweet, failed fetch) |

Flags: `x_content_mismatch` (warn) when the post or profile was fetched and `fit < 0.2`.
`x_image_match` (info) when a post image is within distance 14 of the logo.

Rough guide: a launch post naming the coin and its cashtag with the logo attached scores
0.95+. A post that only mentions `$TICKER` scores about 0.55 (`related`). A borrowed post by
a large account about something else scores near 0 (`unrelated`, usually alongside
`borrowed_narrative`).

### `market.pair`: the token the coin trades against (all depths)

Most pump.fun coins trade against SOL; some against a stablecoin. Those say nothing about
the coin and only get reported. A coin paired against **another token** was launched into
that token's community, and it often builds on that token by name (e.g. "Baby Bonk" paired
with BONK). That pairing feeds the analysis.

```json
"pair": {
  "mint": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
  "symbol": "BONK", "name": "Bonk",
  "kind": "token",
  "source": "onchain",
  "builds_on": true,
  "builds_on_detail": "same ticker base as $BONK",
  "referent": {"label": "Bonk", "kind": "coin", "desc": "Solana dog coin; 'bonk' meme", "confidence": 0.95},
  "categories": [{"label": "animal/dog", "confidence": 0.97}]
}
```

| Field | Meaning |
|---|---|
| `kind` | `sol`, `stablecoin` (USDC, USDT, USD1, PYUSD), `lst` (JitoSOL, mSOL, bSOL, JupSOL), `major` (cbBTC, WBTC, WETH): reported only. `token` and `tokenized_stock` feed the analysis |
| `underlying` | For `tokenized_stock`: the stock ticker (TSLA for TSLAx) |
| `source` | How the pair token was identified: `neutral` (SOL/stablecoin), `analysis` (our stored analysis of it), `db`, `onchain` (its Metaplex / Token-2022 metadata, cached for a week), `none` (unidentified) |
| `builds_on` | The coin's name or ticker builds on the pair token's: same ticker base, ticker contains it (`BBONK`), or the name contains its ticker or a distinctive word of its name |
| `referent`, `categories` | What the pair token itself is about: from our stored analysis of it, else from reading its own name and ticker |

How it changes the analysis when `kind` is `token`:
- category `crypto_native/paired_ecosystem` is always added (it does not lift `crypto_native`),
  and flag `non_sol_pair` (info) is raised;
- when `builds_on`, category `derivative/pair_family` is added and the pair token's referent
  becomes a strong referent candidate for the coin (Baby Bonk → Bonk). Its categories count
  at 0.6× their confidence;
- otherwise the pair token's categories count weakly (0.25×) and its referent is reported
  here only: the coin's own name, ticker and logo still decide what it refers to.

**Tokenized stocks (xStocks).** A pair token whose symbol is a stock ticker plus `x` (TSLAx,
NVDAx, SPYx) and whose name says "xStock" (or whose mint is one of Backed's `Xs…` addresses)
is `kind: "tokenized_stock"`. The coin gets `tradfi/tokenized_stock` (it was launched for that
stock's crowd) instead of `crypto_native/paired_ecosystem`. The company comes from
`data/stocks.yaml` (64 large caps, meme stocks and index ETFs, e.g. Tesla → `celebrity/elon`,
Nvidia → `ai_agent`). When the coin's name or ticker builds on the company or stock ticker
("Tesla Moon", `$NVDAMOON`) the company becomes the referent; otherwise only its categories
count, weakly. Company names and tickers are also understood in the coin itself ("Tesla",
"$NVDA", "TSLAx"); common-word names and tickers (Apple, Meta, COIN, HOOD, SPY) only count in
an unambiguous form ("apple inc", "metax").

When the pair token is itself a pump.fun coin TokenSage has never analysed, a basic analysis of
it is queued in the background, so later coins paired with it get its full meaning.

`market.pair` is `null` when the quote mint is unknown, e.g. a coin analysed from hints
before it is visible on-chain.

## Other endpoints

| Call | Purpose |
|---|---|
| `POST /v1/tokens:batch` with `{"cas": [...≤50], "depth": "basic", "callback_url": "https://…"}` | Prefetch. Returns cached analyses immediately and `pending` + `job_id` for the rest. Never waits. `callback_url` is optional (see below). Items are handled one by one: if the daily quota runs out or the queue is full partway through, the remaining items come back as `status: "failed"` with `error: "quota_exceeded"` or `"overloaded"` and `retry_after_s`, while items already queued keep their `job_id` |
| `POST /v1/tokens/{ca}` with `{"hints": {...}}` | Same as the GET, with metadata you already have (see below) |
| `GET /v1/jobs/{job_id}` | `pending \| running \| done \| failed`, with the result when done. `result.status` is `complete` or `partial`, exactly as `GET /v1/tokens/{ca}` would report it (webhook callbacks carry the same) |
| `GET /v1/meta` | Schema/rule versions, the full category taxonomy, flag codes, and the disclaimer. Use it instead of hard-coding labels |
| `GET /healthz` | Liveness (no auth) |

### Passing metadata you already have (hints)

If you already hold a coin's pump.fun data, pass it as hints. TokenSage then skips the IPFS
metadata fetch, which is faster and saves RPC credits. A coin seconds old that is not yet
visible on-chain is analysed from the hints instead of returning `404 token_not_found`.

- Single coin: `POST /v1/tokens/{ca}` takes the same query parameters as the GET, plus an
  optional JSON body `{"hints": {...}}`.
- Batch: alongside `cas`, send `items: [{"ca": "...", "hints": {...}}]`. Both forms may be
  mixed in one request, at most 50 CAs in total.

```json
{"hints": {"name": "Peanut the Squirrel 2.0", "symbol": "PNUT2",
           "description": "...", "image_url": "https://ipfs.io/ipfs/bafk...",
           "twitter": "https://x.com/...", "telegram": null, "website": null,
           "created_at": "2026-10-06T19:41:07Z"}}
```

Every field is optional. How hints are treated:

- **Untrusted.** Strings are length-capped and cleaned exactly like fetched metadata. The image
  URL is downloaded through the same SSRF guard (https, public hosts, size and time caps), so
  an unsafe URL is simply not fetched.
- **Still verified on-chain.** TokenSage still reads the mint and bonding curve. If the on-chain
  name or symbol differs from a hint, the on-chain value wins and a caveat says so.
  `created_at` replaces the slow signature-history lookup when nothing better is known. It
  never overrides a creation time from pump.fun or the chain, and is replaced once one is
  available.
- **Visible in the result.** `caveats` contains `hints: metadata supplied by caller`, and
  `evidence` has an entry with `kind: "provenance"` and `source: "hints:caller"` listing the
  fields used.
- **Not on-chain yet.** The result is `partial`, with the caveat
  `partial: mint not yet visible on-chain; analysed from caller hints (market data missing)`.
  `market` fields stay null. A partial result is cached for only 60 seconds, so requesting
  it again shortly afterwards re-analyses it with the on-chain view.
- A cached analysis that is still fresh is returned as is; hints only matter when a new
  analysis runs. Hints are never cached as the coin's metadata.

### Webhook callbacks (optional)

If a batch request carries `callback_url`, TokenSage POSTs the finished `JobResponse` JSON
(the same body `GET /v1/jobs/{job_id}` returns, including `result` or `error`) to that URL once
per job that was not already cached. Rules:

- The URL must be `https` on a public host (validated at submit time and again at delivery;
  otherwise `400 invalid_callback_url`). No redirects are followed.
- Answer with any `2xx` within 8 s. Anything else is retried twice, 30 s apart, then dropped;
  the job result itself stays available via `GET /v1/jobs/{job_id}` either way.
- Every delivery is signed. Headers: `X-TokenSage-Signature: sha256=<hex>`,
  `X-TokenSage-Timestamp: <unix seconds>`, `X-TokenSage-Job: <job_id>`. The signature is
  HMAC-SHA256 over the string `"{timestamp}.{raw body}"`, keyed with the **SHA-256 hex digest
  of your API key** (so no second secret has to be exchanged). Verify it and reject stale
  timestamps (older than ~5 min):

```python
import hashlib, hmac, time

def verify(api_key: str, headers: dict, raw_body: bytes) -> bool:
    secret = hashlib.sha256(api_key.encode()).hexdigest().encode()
    ts = headers["X-TokenSage-Timestamp"]
    if abs(time.time() - int(ts)) > 300:
        return False
    expected = "sha256=" + hmac.new(secret, f"{ts}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, headers["X-TokenSage-Signature"])
```

Deliveries can arrive out of order and, in rare retry cases, twice; key your handling on
`job_id` (or `result.ca` + `result.depth`).

## Recommended client behaviour

```python
async def analyze(ca: str) -> dict | None:
    for attempt in range(8):
        r = await http.get(f"{BASE}/v1/tokens/{ca}", params={"wait": 10}, headers=AUTH)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 202:
            await asyncio.sleep(int(r.headers.get("Retry-After", "3")))
            continue
        if r.status_code in (429, 503):
            await asyncio.sleep(int(r.headers.get("Retry-After", "5")))
            continue
        return None  # 400/401/404/422: don't retry
    return None
```

- Treat all strings in `raw`, `x.text`, `image.ocr` and `summary` as untrusted: escape them.
- Only `https` URLs are returned in link fields; anything else arrives as `null`.
- Cache on your side by `ca` + `analysis.analyzed_at` if you display results repeatedly.

## Example

```bash
curl -s -H "Authorization: Bearer $TOKENSAGE_KEY" \
  "https://tokensage-api.onrender.com/v1/tokens/3arUrpH3nzaRJbbpVgY42dcqSq9A5BFgUxKozZ4npump?depth=full&wait=15" | jq .
```
