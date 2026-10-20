# TokenSage code review

Review pass of `main` at `a27de78` (2026-10-20, "Charge max_age=0 against the refresh allowance"). Findings SG-001–SG-008 are addressed on branch `cursor/tokensage-code-review-eeee`. The two items called out as already decided or in flight (webhook HMAC key equals the stored key hash, and charging `max_age=0` like `refresh=true`) are omitted from the findings table.

## Summary

The service is in good shape for its age: auth compares digests in constant time, quotas are charged inside the enqueue transaction, image and metadata parsers are capped and mostly fail closed, and the guarded fetcher blocks private literals, mapped IPv4, CGNAT, and redirects. The suite is green. The problems that are left are concentrated where attacker-controlled URLs and caller hints meet shared stored state.

Fix these first:

1. Bind outbound connections to the addresses the guard already checked. A second DNS lookup can still reach a private or metadata address.
2. Stop putting that guard's "resolves to \<ip\>" text into the public analysis.
3. Stop writing one API key's hints into the shared analysis, `x_ref`, and `token_read` that every other key reads.
4. Restrict `GET /v1/jobs/{id}` to the key that created the job. Ids are sequential.
5. Make `HTTPS_PROXY` obey the same "no env proxy" rule as `trust_env=False`, and make the metadata-retry reschedule conditional on the lease.

Checks (all run from a clean `uv sync --extra dev` on Python 3.12):

| Check | Result |
|---|---|
| `uv run ruff check tokensage tests scripts` | pass |
| `uv run ruff format --check tokensage tests scripts` | pass (151 files) |
| `uv run mypy tokensage scripts` | pass (92 files) |
| `uv run python scripts/export_openapi.py --check` | pass (`openapi.v1.json` is current) |
| `uv run pytest -q` | pass, **903 passed** in 51s |

Database tests ran against a local Postgres 16 at `TEST_DATABASE_URL` (CI uses 17). Nothing was skipped for a missing database. No paid or production APIs were called.

## Findings

| ID | Severity | Area | Title | Location |
|---|---|---|---|---|
| SG-001 | High | SSRF | Guard checks DNS, then connects by name again | `tokensage/net/safe_fetch.py`, `tokensage/callbacks.py` |
| SG-002 | High | SSRF / info leak | Resolved private addresses are returned in the analysis | `tokensage/net/safe_fetch.py`, `tokensage/resolve/metadata.py`, `tokensage/analyzer.py` |
| SG-003 | High | Data integrity | One key's hints become every key's stored result | `tokensage/analyzer.py`, `tokensage/queue.py`, `tokensage/api/service.py` |
| SG-004 | High | Auth | Any API key can read any analysis job | `tokensage/api/routes_v1.py` |
| SG-005 | Medium | SSRF | `HTTPS_PROXY` is applied even when `trust_env=False` | `tokensage/net/metrics.py`, `tokensage/analyzer.py` |
| SG-006 | Medium | Queue | Metadata retry reschedule ignores the lease | `tokensage/analyzer.py` |
| SG-007 | Low | Sources | One DexScreener 404 disables that source for 6 hours | `tokensage/sources/lookups.py` |
| SG-008 | Low | Sources | Non-finite GeckoTerminal numbers fail the `now` cache write | `tokensage/sources/geckoterminal.py`, `tokensage/now.py` |

## Finding details

### SG-001 — Guard checks DNS, then connects by name again

**Severity:** High. **Confidence:** High.

**What's wrong.** `check_url` resolves the host and rejects private, loopback, link-local, multicast, reserved, and non-global addresses. The fetch that follows does not connect to those addresses. It hands the original URL to httpx, which resolves the name again and connects to whatever that second lookup returns. The file's own comment describes this check-then-connect window. Redirects are re-checked, but each hop has the same window. Callback delivery does the same thing: `check_url`, then `POST` the URL.

**Path to failure.**

1. A token's metadata `uri` or `image`, a hint `image_url`, an X media URL, or a batch `callback_url` is `https://attacker.example/…`, where the attacker controls DNS.
2. `check_url` calls `getaddrinfo` and sees a public address (`tokensage/net/safe_fetch.py` lines 110–122).
3. `safe_get` then calls `client.stream("GET", current, …)` with that same hostname (`tokensage/net/safe_fetch.py` lines 152–163). httpx resolves it again. With a TTL of 0 the second answer can be `169.254.169.254`, `10.x`, or the private address of another Render service.
4. Callbacks take the same path: `deliver` calls `check_url` (`tokensage/callbacks.py` lines 89–93) and `_post` streams the POST by URL (`tokensage/callbacks.py` lines 120–125). Redirects are not followed, so a 3xx is not a second hop, but the first connection is still unbound.

Token metadata and hint image URLs are attacker-controlled and are fetched on every cold read. A basic-depth hint does not spend the full-depth quota, so the rate limit (300/min in `render.yaml`) is the main cap on attempts.

**Impact.** A fetch that the guard rejected a moment earlier can still be sent to a private, link-local, or metadata address. The worker shares a private network with `tokensage-db` and any other private Render service. This is a GET or a signed POST, not a general TCP proxy, and the race has to be won, but it is the standard DNS-rebinding bypass of a check-then-connect guard. The comment that there are no internal services to reach is not true of the private network this process runs on.

**Suggested fix.** After a lookup passes, connect to one of the checked addresses (httpx transport pinned to that IP, with the original hostname left as SNI and Host). Re-resolve and re-check on every redirect, and pin that hop too. Do the same for the callback POST. No API change for callers whose hosts only have public addresses.

**Test to add.** A resolver that returns a public address on the first call and `10.1.2.3` on the second. `safe_get` and `callbacks.deliver` must raise, and the test transport must see no connection to `10.1.2.3`.

### SG-002 — Resolved private addresses are returned in the analysis

**Severity:** High. **Confidence:** High.

**What's wrong.** When the guard rejects a host, the exception text includes the address it resolved to (`"{host} resolves to non-public {ip}"`). That string is stored on the analysis and returned to API clients. The batch route was deliberately changed so a rejected callback URL does not reveal this (the comment in `routes_v1.py` says the message would let a caller map internal DNS). The metadata and image paths still return it.

**Path to failure.**

1. Any API key `POST /v1/tokens/{ca}?depth=basic` with hints `name` and `image_url` set to `https://<internal-name>/a.png`, using a valid CA that is not on chain yet.
2. Resolve raises `token_not_found`. Because metadata hints are present, `analyze` builds the coin from the hints (`tokensage/analyzer.py` lines 1486–1502) and downloads the image.
3. `attach_image` sets `image_error` to `unsafe image url: {UnsafeUrl}` (`tokensage/resolve/metadata.py` lines 401–402). `UnsafeUrl` is raised with the resolved address at `tokensage/net/safe_fetch.py` line 121.
4. There is no on-chain URI, so the logo fallback does not replace it. `build_document` appends `image could not be fetched: {image_error}` to `caveats` (`tokensage/analyzer.py` lines 813–818).
5. The analysis is stored and returned to the caller (and, until it ages out, to every other key; see SG-003).

The same text is embedded when the on-chain metadata URI itself is refused: `fetch_metadata` returns `error=f"unsafe uri: {e}"` (`tokensage/resolve/metadata.py` line 252) and `build_document` puts that in `flags[].detail` (`tokensage/analyzer.py` lines 819–826). A token creator chooses that URI.

Names that are blocked before DNS (`localhost`, `*.internal`, `*.local`, `metadata.google.internal`) do not leak an address. Names the worker can resolve and the caller cannot — other hosts on the Render private network — do.

**Impact.** Any API key can turn the worker into an internal DNS oracle and read the result from the analysis it just paid for (rate limit only, at `depth=basic`). That is the disclosure the callback error was rewritten to prevent.

**Suggested fix.** Log the resolved address server-side. Persist and return a fixed string (`unsafe url` / `metadata unavailable`) with no address and no hostname. Same treatment for `x_media.error`, which stores `str(e)` (`tokensage/fulldepth.py` around the media fetch). No API change beyond the wording of those error strings.

**Test to add.** Hint an image URL whose resolver returns `10.1.2.3`. The HTTP response and the stored `analysis.doc` must not contain `10.1.2.3`.

### SG-003 — One key's hints become every key's stored result

**Severity:** High. **Confidence:** High.

**What's wrong.** Hints are documented as untrusted and are cleaned like fetched JSON. They are not kept as that caller's private input. If nothing is cached yet, they replace the metadata fetch. The resulting analysis is the mint's shared cache. The X link is written to `x_ref`. The referent and launch time are written to `token_read`. A mint that is not on chain yet still gets a stub `token` row, an analysis, an `x_ref`, and a `token_read`. A later caller can also attach hints to an in-flight job that has none (`payload = coalesce(job.payload, excluded.payload)`).

**Path to failure.**

1. Key A calls `POST /v1/tokens/{ca}` (or a batch item) with hints: description, twitter, image URL, and optionally `created_at`. Key B has not fetched this mint, so `token_metadata` is empty.
2. `get_or_enqueue` stores the hints on the job (`tokensage/api/service.py` lines 314–325). If a hint-less job is already open, the enqueue update fills its null payload (`tokensage/queue.py` lines 136–138).
3. `analyze` skips the metadata fetch and uses `from_hints` (`tokensage/analyzer.py` lines 1496–1502). Only `name` and `symbol` are corrected back to the on-chain values (lines 1523–1530). Description, twitter, telegram, website, and image stay as the caller sent them.
4. If the mint is not visible yet, the same function invents a `Resolved` from the hints and inserts a stub token (lines 1486–1492, 1447–1457).
5. `_store_analysis` saves that document as the mint's analysis. `_store_xref` upserts the hinted twitter/tweet/community (`tokensage/analyzer.py` lines 1246–1264 and 1659). `_store_read` upserts `token_read` when `created_at` is set (lines 1205–1224).
6. Key B calls `GET /v1/tokens/{ca}` inside the cache window (`default_max_age`: 5 minutes, 1 hour, or 24 hours). `get_or_enqueue` returns the stored document (`tokensage/api/service.py` lines 258–283). Later analyses of other coins count A's `x_ref` in `_x_reuse` (`tokensage/analyzer.py` lines 247–256). The `x_link_reused` flag fires at 5 (`tokensage/engine/pipeline.py` lines 942–953). Referent waves count A's `token_read` rows for 7 days (`tokensage/analyzer.py` lines 1945–1955).

The caveat `hints: metadata supplied by caller` is set, so a careful client can see the provenance. The referent, categories, summary, reuse count, and wave counts are still computed from that input and stored as the coin's result.

**Impact.** An outside API key can, before anyone else reads a mint, plant the description, socials, and logo the shared cache will serve (up to 24 hours for an old coin, 1 hour if no creation time is supplied). It can also attach one tweet or profile to many real or invented mints. Those `x_ref` rows survive until something re-analyses the mint or retention deletes the token (90 days) and are enough to raise `x_link_reused` on the real coins that link that post. Invented mints with a hint `created_at` inside the last week are counted in referent waves until detail retention (30 days). This is cross-key poisoning of stored results, not a private scratchpad.

**Suggested fix.** Keep using hints for the caller's own response when the fetch has not landed. Do not let that document be the cache hit for a different key. Do not write `x_ref` or `token_read` from hint-origin metadata, and do not insert a stub token for a mint that was not visible on chain, unless those rows are marked and excluded from reuse and wave queries. Ignore a joining caller's hints when the open job already belongs to someone else. **API change:** a second key will no longer receive another key's hint-derived analysis as a cache hit, and TrenchScanner will not see hint-derived `x_ref` / wave counts for mints it did not itself just read. The caller that sent the hints can still get them on that response.

**Test to add.** Key A posts hints (a distinctive description and a tweet URL) for a CA with no metadata row. Key B's `GET` must not return that description. `x_ref` and `token_read` must not gain a row for A's tweet. A second case: an open job with a null payload must not take a different key's hints.

### SG-004 — Any API key can read any analysis job

**Severity:** High. **Confidence:** High.

**What's wrong.** `GET /v1/jobs/{id}` requires an API key and then loads the job by primary key. It never compares `requested_by` to the caller. Analysis jobs are returned in full: mint, status, error text, and the stored analysis. Job ids are bigserial, so they can be walked. Non-analysis jobs were hidden for this reason (a callback error named the address a URL resolved to). Analysis jobs were left open.

**Path to failure.**

1. Key A (for example TrenchScanner) enqueues analyses. `requested_by` is A's name (`tokensage/api/routes_v1.py` line 174, `tokensage/queue.py` lines 132–133).
2. Key B calls `GET /v1/jobs/1`, `/2`, … with its own bearer token.
3. `get_job` loads the row (`tokensage/api/routes_v1.py` lines 364–370). The only filter is `kind == "analyze"`. `Job.from_record` does not even keep `requested_by`, so the handler cannot check it.
4. The response includes `ca`, `status`, `error` (the failed job's `last_error`), and `result` (`tokensage/api/routes_v1.py` lines 384–393).

**Impact.** Every outside API key can list the mints another customer is reading, including jobs that are still pending, plus the failure text. The analysis body for a finished public mint is also available by requesting that mint (the cache is shared). The extra leak is the other customer's queue: which coins they are scanning and which reads failed. That breaks the rule that one key cannot read another key's jobs.

**Suggested fix.** `404 job_not_found` unless `job.requested_by` equals the caller's key name. Apply the same check before scheduling or delivering a callback, so a guessed id cannot attach a webhook to someone else's job. **API change:** a key can no longer poll a job id it did not create. Clients that only poll ids from their own `202` responses are unaffected.

**Test to add.** Two keys. Key A starts a job. Key B's `GET /v1/jobs/{that id}` is 404 and the body does not contain the mint.

### SG-005 — `HTTPS_PROXY` is applied even when `trust_env=False`

**Severity:** Medium. **Confidence:** High.

**What's wrong.** The analyzer and the knowledge cron build their httpx client with `trust_env=False` so an env proxy cannot resolve hosts after the IP check (`tokensage/analyzer.py` lines 158–161, `tokensage/jobs/knowledge.py` lines 208–213). Both clients pass `metrics.MeteredTransport`. That transport reads `HTTPS_PROXY` / `https_proxy` itself and gives the proxy to `httpx.AsyncHTTPTransport` (`tokensage/net/metrics.py` lines 232–237). `trust_env=False` has no effect once a transport is installed. `render.yaml` does not set the variable, so production as checked in does not use a proxy. The hole opens as soon as an operator sets one (the usual way to egress from a locked-down network).

**Path to failure.**

1. `HTTPS_PROXY` is set in the worker environment.
2. A token URL passes `check_url` (local DNS, public address).
3. httpx sends the URL to the proxy. The proxy does its own DNS and its own TCP connection. The addresses `check_url` accepted are not the ones that get connected.

**Impact.** With the variable set, the guard no longer constrains where a token URL, image URL, or callback is actually fetched. Combined with SG-001, the second lookup happens on the proxy, which is often able to reach addresses the app host cannot. With the variable unset, behavior matches the comment and this does not fire.

**Suggested fix.** Do not read the proxy env vars inside `MeteredTransport`. If a deployment needs a proxy, add an explicit setting and still pin the guard's resolved addresses (or do not send token-controlled URLs through it). No API change.

**Test to add.** Set `HTTPS_PROXY` to a local proxy, `trust_env=False` on the client, fetch a token URL, and assert the proxy received nothing. Today that request is proxied.

### SG-006 — Metadata retry reschedule ignores the lease

**Severity:** Medium. **Confidence:** Medium.

**What's wrong.** A `retry_metadata` job that still cannot fetch the URI puts itself back to `pending` with an unconditional update: `where id=$1` only. It does not require `status='running'` or the caller's `lease_token`, and it does not clear `lease_token`. `complete`, `fail`, `defer`, `renew_lease`, and `release` all check the lease. This path does not.

**Path to failure.**

1. Worker A is inside `retry_metadata` (`tokensage/analyzer.py` lines 2222–2241). Its heartbeat fails to renew (the lease row was reclaimed, or the maintenance requeue moved it).
2. Worker B's `claim` has set the row back to `running` with B's lease token and `attempts + 1`.
3. A's `_schedule_retry` update still matches (`tokensage/analyzer.py` lines 1374–1380): status becomes `pending`, `attempts` is decremented, `run_after` moves to `next_retry_at`.
4. The worker then notices the cancel. Because `lost` is set it returns without undoing the update (`tokensage/worker.py` lines 168–174).
5. B's `complete` requires `status='running'` and B's token, so it does not land. The job sits `pending` until `run_after`, and `claim` can hand it to a third worker while B is still in `analyze`.

The lease is 120s and the heartbeat runs at a third of that, so this needs a failed renewal, not a slow fetch by itself. The window is the update that is already in flight when the cancel arrives.

**Impact.** A metadata retry can be duplicated, and the worker that lost the lease can postpone a job another worker has already claimed. The coin can be analysed twice; the status row no longer matches the worker that is writing it.

**Suggested fix.** Use the same predicate as `defer`: `status='running'` and `lease_token` equals the holder, and clear `lease_token`. If the update matches zero rows, do not raise `RetryRescheduled`. No API change.

**Test to add.** Claim a `retry_metadata` job, change its `lease_token` as a second worker would, then run the reschedule update. The row must stay `running` with the second token.

### SG-007 — One DexScreener 404 disables that source for 6 hours

**Severity:** Low. **Confidence:** High.

**What's wrong.** `dexscreener_search` treats HTTP 404 like the pump.fun search route disappearing. On 404 it calls `breaker.open_for("dexscreener_search", PUMP_SEARCH_GONE_S)` (6 hours) and logs `lookups.pumpfun_search_gone`. The comment above it is the pump.fun comment, including the path `Cannot GET /coins/search`. DexScreener's search URL is a different route. One 404 — a missing pair response, a proxy, or a short upstream blip — turns same-name lookups off for every coin for six hours. Pump.fun's own 404 handling is intentional; this copy is not.

**Path to failure.** `analyze` → `_external_namesakes` → `dexscreener_search` (`tokensage/sources/lookups.py` lines 89–99) → any 404 → breaker open until `PUMP_SEARCH_GONE_S`.

**Impact.** Copycat context from DexScreener goes empty process-wide for 6 hours. The in-process breaker is not shared across processes, but the one analyzer process is the production worker. No API schema change.

**Suggested fix.** Count a DexScreener 404 as a normal failure (or ignore it, if 404 means "no pairs"). Do not call `open_for` with the pump.fun gone interval. Log `lookups.dexscreener_status`.

**Test to add.** One mocked 404, then a second search. The second search must still be attempted.

### SG-008 — Non-finite GeckoTerminal numbers fail the `now` cache write

**Severity:** Low. **Confidence:** Medium.

**What's wrong.** Metadata JSON maps `NaN` and `Infinity` to null so a hostile document can be stored in jsonb (fixed in #78). GeckoTerminal parsing only drops NaN. `float("Infinity")` and an overflow such as `1e309` both become `inf`, and `f == f` is true for infinity, so `_num` returns it (`tokensage/sources/geckoterminal.py` lines 216–221).

**Path to failure.** `POST /v1/tokens:now` → `now.refresh` → `story_heat` → `parse_pools` puts `inf` on a market-cap or change field → `heat.model_dump(mode="json")` → the jsonb codec emits `Infinity` → Postgres rejects the `lookup_cache` insert (`tokensage/now.py` lines 338–342). Confirmed locally: `SELECT '{"a": Infinity}'::jsonb` raises `invalid input syntax for type json`. The worker then fails the `now` job and retries it until `job_max_attempts`.

**Impact.** A non-finite number on the pool that leads a story makes that mint's decision-time refresh fail instead of storing a partial heat. GeckoTerminal has to return that number; a token creator cannot put it in metadata. Blast radius is the `now` job for coins whose search hit that pool, not the whole API.

**Suggested fix.** Treat non-finite floats like NaN in `_num` (`math.isfinite`). No API change.

**Test to add.** `parse_pools` on a body with `"market_cap_usd": "Infinity"`, then insert the resulting `StoryHeat` into `lookup_cache`. The insert must succeed and the stored cap must be null.

## Unverified / worth a look

- Worker pool size is `concurrency + 6` (`tokensage/worker.py` around the `create_pool` call) while each slot holds a connection for the whole job, plus the LISTEN socket, the metrics flusher, and the trend refresh. Heartbeats borrow the remainder. I did not see a missed lease under that math, but a long trend refresh plus a DB stall would be the place a lease expires without a crash.
- `geckoterminal.pool_peak` interpolates the pool address into the URL path. The address comes back from GeckoTerminal, not from the token. A slash in that field would change the path on that host only.
- `_SerialConn.__getattr__` (`tokensage/fulldepth.py`) forwards any other method to the raw connection without the lock. Current callers stick to `execute` / `fetch*`. A new helper that starts a transaction on the shared connection would interleave queries.
- `t.co` links are classified as `shortlink` with `needs_resolve` and never fetched (`tokensage/engine/xref.py`). That is safe. It also means those coins never get an X author. Product gap, not a crash.

## Refactor and cleanup opportunities

- One pinned-IP transport used by `safe_get` and callbacks, instead of "check, then let httpx dial the name" in two places.
- Hint application is split across enqueue (`coalesce` of payloads), `analyze` (skip the fetch), and three writes (`analysis`, `x_ref`, `token_read`). A single "hint provenance" flag that those writes consult would make SG-003 hard to reintroduce.
- `dexscreener_search`'s 404 branch should not share pump.fun's log message or gone-interval constant.
- The Docker base (`python:3.12-slim`) and the `uv` image are tags, not digests. The vision ONNX download is pinned by SHA-256, which is the part that matters most. Running as uid 10001 is already right.
- `uv sync` pulls both `opencv-python` and `opencv-python-headless` (RapidOCR). The container installs `libgl1` for that. Importing `cv2` from the non-headless wheel is the one that needs a display stack.

## Checked and sound

- **No hosted model APIs.** Runtime code does not call OpenAI, Anthropic, Gemini, Cloud Vision, or Hugging Face inference. The SigLIP ONNX file is downloaded at image build and run locally behind `ENABLE_CLIP`. Embeddings are the same pattern, flag off.
- **The guard's address checks, as a check.** Literals and resolved addresses cover loopback, RFC1918, link-local, CGNAT (`not is_global`), multicast, reserved (including the NAT64 prefix `64:ff9b::/96` on this Python), IPv4-mapped IPv6, and unspecified. Redirects are manual, capped at 3, and each hop must be https with no userinfo. Odd numeric hosts (`2130706433`, `127.1`, octal) are not parsed as IPs; DNS failure fails the fetch closed. `.internal` / `.local` / `metadata.google.internal` are rejected before DNS.
- **Response limits on the guarded fetcher.** `Accept-Encoding: identity`, refusal of any other `Content-Encoding`, `Content-Length` and streamed body caps, total timeout via `asyncio.timeout`. Metadata JSON is size-capped (64 KB), nested-scrubbed, and non-finite numbers become null.
- **Images.** Pillow is limited to PNG/JPEG/GIF/WEBP/AVIF, `MAX_IMAGE_PIXELS` is 16M, and a wide 16-bit image is shrunk before the float conversion. SVG is sniffed and then not decoded. OCR has a thread cap, a thin-image rejection, and a 30s budget.
- **Auth and quotas.** Keys are stored as SHA-256 and compared with `hmac.compare_digest`. Admin and monitor are separate digests; monitor routes are GET-only; empty `MONITOR_KEY` does not match. Full and refresh quotas take a per-key advisory lock and charge inside the enqueue transaction, so a rolled-back insert does not burn quota. The `now` counter is an atomic upsert that rolls back when the raise happens. Joining an open job is free, which matches the documented poll loop.
- **Callback delivery besides the DNS pin.** One open callback per job, key, and URL; URL length capped; response body capped at 4 KB; overall timeout 15s; redirects not followed; the callback error is not returned on the batch route.
- **Queue leases.** `complete` / `fail` / `renew_lease` / `release` / `defer` all require the lease token. Expired leases with no attempts left become `worker_died` instead of looping. Low-priority launch reads are dropped after 15 minutes rather than filling the queue, and a low backlog does not 503 a higher-priority read.
- **Database.** Queries are parameterized. The f-string SQL in the analyzer is built from constants plus bound parameters. Retention and condense exist and are tested. `render.yaml` keeps Postgres on a private allow-list and runs the container as a non-root uid.
- **Engine regexes that take coin text.** `written_as_title` escapes the surface. Fee patterns and marker patterns come from repo YAML and are linear. X tweet ids are checked against a digit pattern before they are put in a URL.
