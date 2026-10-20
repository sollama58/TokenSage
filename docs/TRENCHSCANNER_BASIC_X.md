# TrenchScanner integration: basic-depth X interpretation (rules 0.36.0)

TokenSage now interprets **linked X post/profile text on `depth=basic`** (your quick / initial reads), not only on `depth=full`. This document is for wiring TrenchScanner to the new behavior and setting expectations on **narrative quality** and **latency**.

## What changed on the API

| Field / behavior | Before 0.36.0 (`depth=basic`) | After 0.36.0 (`depth=basic`) |
|------------------|-------------------------------|-------------------------------|
| `referent` from launch post | Only if name/ticker/image already said so | Often from post text (`source: "x"`, `post_id` set) |
| `x.text`, `x.relation`, `x.author` | Empty / `not_fetched` | Populated when tweet/profile text is loaded |
| `x.match`, `x.credibility`, engagement | Still **full only** | Unchanged |
| `trend` | Still **full only** | Unchanged |
| Quota | Basic not counted toward `full_per_day` | Still true (basic X uses infra, not full quota) |

**You do not need a new query parameter** if you already use `depth=basic`. Behavior is controlled on the **TokenSage deployment** via environment variables (below).

## Deployment settings (TokenSage ops)

| Env var | Default | Meaning |
|---------|---------|---------|
| `ENABLE_BASIC_X` | `true` | Master switch for basic X interpretation |
| `ENABLE_BASIC_X_FETCH` | `true` | `false` = **tier A only** (use `x_tweet` / `x_profile` cache, no new HTTP) |
| `BASIC_X_TIMEOUT_S` | `3` | Max seconds to wait on a cold tweet/profile fetch before skipping X text |

Recommended for production rollout:

1. Ship with **`ENABLE_BASIC_X=true`** and **`ENABLE_BASIC_X_FETCH=true`**.
2. If mirror load or p95 latency spikes, set **`ENABLE_BASIC_X_FETCH=false`** (cache-only) until full reads populate `x_tweet`.

## TrenchScanner client changes

### 1. Keep using `depth=basic` for the first read

No contract change to URLs. Continue:

```http
GET /v1/tokens/{mint}?depth=basic&wait=…
```

or your batch prefetch with `depth: "basic"`.

### 2. Use the richer fields you already have

After deploy, treat these as **first-class on basic** (when `x.status == "ok"`):

- `referent` — especially `supported_by` containing `"x"`, `post_id` for post-scoped waves
- `summary` — may include the launch-post narrative clause
- `x.relation` — `narrative_reference` vs launch-style signals
- `flags` — e.g. `borrowed_narrative`, `tweet_deleted`

**Still schedule `depth=full`** (or rely on a later full job) when you need:

- `x.match.fit` / `x_content_mismatch`
- `x.credibility` / account-age signals
- `trend.matched`
- Borrowed narrative from **quoted/replied** posts only (basic loads the **primary** tweet/profile, not the full thread)

### 3. Handle caveats and “no X text”

New automatic caveats (non-fatal):

- `basic x: tweet fetch timed out after … s`
- `basic x: linked tweet could not be fetched`

When fetch fails or times out, the analysis is still **`complete`** at basic depth; narrative falls back to name/logo-only (same as before 0.36.0).

Check `x.status`:

| `x.status` | Meaning for TrenchScanner |
|------------|---------------------------|
| `ok` | Post text was interpreted |
| `not_fetched` | No cache and fetch disabled/failed/timeout |
| `deleted` | Post gone; do not trust referent from X |
| `failed` | Mirrors failed |

### 4. Waves and “story heat”

- `referent.post_id` — wave key `x:<post_id>` when subject came from the post (same as full).
- Generic referents still have **no** `referent.wave`.
- `POST /v1/tokens:now` still requires a stored **full** read for trend refresh; basic+X does not replace `:now`.

### 5. Optional gating in TrenchScanner

Suggested decision logic (pseudocode):

```text
if analysis.depth == "basic" and analysis.referent?.supported_by?.includes("x"):
  use referent with high confidence for narrative bucket
elif analysis.referent?.generic or !analysis.referent:
  enqueue or upgrade to depth=full (quota-aware)
if analysis.x?.status == "not_fetched" and metadata.twitter links a tweet:
  optional: retry later (cache may fill from another coin on same post)
```

## Expected impact

### Narrative quality (qualitative)

| Scenario | Expected improvement |
|----------|----------------------|
| Generic name + specific launch tweet | **Large** — referent often appears on first basic read |
| Viral post, many coins (high `x.reuse_count`) | **Large** on tier A (cache hit, no extra latency) |
| Reply-launch riding parent thread | **Small–medium** until full (no quoted/replied fetch on basic) |
| Profile-link launches | **Medium** when bio/display name carries the story |
| No twitter / search-only link | **None** (unchanged) |

Internal golden-style check: coin name matching a quoted subject in tweet text gets `referent.source == "x"` on `run_basic` with `tweet` set (`tests/test_basic_x.py`).

### Latency (order of magnitude)

Measured on TokenSage worker path as `analysis.timing.basic_x_ms` in logs.

| Case | Added time on basic read |
|------|---------------------------|
| Tier A: `x_tweet` / `x_profile` hit | ~1–5 ms (one SQL) |
| Tier B: cold tweet fetch | ~0.3–2 s typical; capped by `BASIC_X_TIMEOUT_S` (default 3 s) |
| Tier B: timeout | 0 narrative from X; +~3 s worst case unless you lower timeout |

**End-to-end basic read** (RPC + IPFS + DB context + engine) is still often **1–5+ s** on cold coins; X adds noticeably only on **cold tweet** links.

Compared to always waiting for **`depth=full`**: you still save trends, wiki, media hashing, and the 20 s full-depth bundle — basic remains the right default for firehose scanning.

## Rollout checklist

- [ ] Deploy TokenSage build with `versions.rules` ≥ `0.36.0-full`
- [ ] Confirm env: `ENABLE_BASIC_X=true` (and fetch policy above)
- [ ] Monitor: `basic_x_ms` p95, X mirror breaker trips, referent null-rate on basic vs before
- [ ] TrenchScanner: log when `supported_by` includes `"x"` on basic for analytics
- [ ] Keep full-depth quota for ambiguous coins and for `x.match` / trends

## References

- Narrative pipeline: `NARRATIVE_AUDIT.md`
- API contract: `docs/API.md` (basic vs full)
- Code: `tokensage/fulldepth.py` (`basic_x_load`), `tokensage/analyzer.py`, `tokensage/engine/pipeline.py`
