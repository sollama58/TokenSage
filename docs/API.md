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
  `depth=basic` work are not quota-limited. Exceeding a quota → `429 quota_exceeded` with
  `Retry-After` set to the seconds until UTC midnight. The owner can see today's counters per key.
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
| `include` | `evidence` | Comma list. Drop `evidence` to get a smaller body |

### Status codes

| HTTP | Meaning | What to do |
|---|---|---|
| `200` | Analysis in body. `status` is `complete`, `partial` (some upstream source failed; see `errors` and `analysis.caveats`) or `failed` | Use it |
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
| `market` | Bonding-curve state: `complete` (graduated), `curve_progress` 0–1, `creator`, `quote_mint` |
| `raw` | Name, symbol, description and social links as found in the metadata (**untrusted text, escape before rendering**) |
| `normalized` | Cleaned tokens, ticker base, version markers (`version:2`), emoji keywords, obfuscation flags |
| `referent` | What the token refers to: `label`, `kind`, `desc`, `source`, `confidence`. May be `null` |
| `categories[]` | Multi-label with confidences. Labels come from `GET /v1/meta`; expect new ones over time |
| `ticker_explanation` | Plain-language explanation of the ticker |
| `copy_of[]` | Coins this one copies or derives from, with the signals that say so |
| `image` | Hashes, OCR text, palette, near-duplicates, optional visual labels. `source_url` is the gateway URL. **Images are not screened for NSFW content; decide yourself whether to show them** |
| `x` | The linked X/Twitter reference, its creation time, whether it predates the token, the fetched author (handle, followers, verification type, join date, username changes), text, `relation` (`narrative_reference` = the coin is *about* someone else's earlier tweet; `launch_announcement`; `official_account`; `spoofed` = the URL's handle is not the tweet's real author; `search_only`), `status` (`ok`, `deleted`, `suspended`, `not_fetched`, `failed`), `reuse_count` (other tokens linking the same tweet/handle), and `quoted` when the linked tweet is a quote tweet: the quoted post's `id`, `url`, `status`, `author`, `text`, `created_at` and `predates_token_by_s`. A launch post that quotes someone else's earlier post usually takes its meaning from that post, so the quoted text feeds the analysis too, and a large or verified quoted author raises `borrowed_narrative` |
| `trend` | Trending-topic matches (Wikipedia spikes, news headlines) |
| `flags[]` | `{code, severity, detail}`. Codes and descriptions are listed by `GET /v1/meta` |
| `summary` | Template-generated plain-language summary |
| `evidence[]` | Why: `{kind, label, weight, detail, source, url}` |
| `caveats[]` | Automatic caveats (single weak source, ambiguous referent, deleted tweet, …) |
| `depth`, `analyzed_at`, `versions` | What was run, when, and with which rule/lexicon versions |

Confidences are probabilities in 0–1, calibrated so that about 80% of "0.8" labels are right
(Phase 6 of the build plan). Flags and categories are **informational, not financial advice**.

## Other endpoints

| Call | Purpose |
|---|---|
| `POST /v1/tokens:batch` with `{"cas": [...≤50], "depth": "basic", "callback_url": "https://…"}` | Prefetch. Returns cached analyses immediately and `pending` + `job_id` for the rest. Never waits. `callback_url` is optional (see below) |
| `GET /v1/jobs/{job_id}` | `pending \| running \| done \| failed`, with the result when done |
| `GET /v1/meta` | Schema/rule versions, the full category taxonomy, flag codes, and the disclaimer. Use it instead of hard-coding labels |
| `GET /healthz` | Liveness (no auth) |

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
