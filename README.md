# TokenSage

An HTTP API that takes a Solana pump.fun token's **Contract Address (CA)** and explains what the token *means*: its name, ticker, image, description and linked X/Twitter content, with categories, flags, confidence scores and evidence. Built for other applications to call. No external AI APIs; deployed on Render via a Blueprint.

**Status:** Phases 1–5 done. A CA is resolved on-chain, its metadata and image are fetched safely, and the meaning engine runs at two depths. **basic:** normalization (homoglyphs, leet, emoji, camelCase, markers), meme-aware segmentation, slang/entity/WordNet gazetteers, ticker explanation, known-coin and same-name copycat detection, image perceptual hashes with near-duplicate matching, evidence scoring with a referent and templated summary. **full** adds OCR on the logo, the linked X content (FxTwitter → vxTwitter → syndication → oEmbed, cached) with relation and account-quality signals, Wikipedia-pageview trend matching with Google News confirmation, and a daily knowledge cron (trends, CoinGecko known coins). **Phase 5** adds per-key daily quotas with persisted usage counters, `503` back-pressure, signed webhook callbacks for batch jobs, request access logs, and a load-test harness with a local fake chain. 167 tests incl. 67 golden cases. Next: **Phase 6** calibration on real tokens (needs the Render deploy and real CAs). Build plan: [`PROJECT_GUIDE.md`](PROJECT_GUIDE.md) §9.

- [`docs/API.md`](docs/API.md): integration guide for the consumer application
- [`PROJECT_GUIDE.md`](PROJECT_GUIDE.md): full design; [`FABLE_BRIEF.md`](FABLE_BRIEF.md): kickoff brief
- `docs/research/`: research reports with sources; `docs/reference/`: the original tested reference code (now ported into `tokensage/`)

## Test console

The API serves a single-page test console at `/` (also `/console`), e.g. `https://tokensage-api.onrender.com/`.
Paste an API key (kept only in your browser's localStorage), enter a CA or pump.fun URL, and it runs the
real request flow, including `202` polling, and renders the result as cards plus the raw JSON. It is a
developer tool, not a product surface, and it carries no secrets.

## Run locally

```bash
uv sync --all-extras                       # Python 3.12
export DATABASE_URL=postgresql://tokensage@localhost:5432/tokensage
export API_KEYS="dev:devkey123" ADMIN_KEY=adminkey INLINE_ANALYZER=true
uv run alembic upgrade head
uv run uvicorn tokensage.api.app:app --port 10000
curl -H "Authorization: Bearer devkey123" \
  "localhost:10000/v1/tokens/3arUrpH3nzaRJbbpVgY42dcqSq9A5BFgUxKozZ4npump?wait=5"
```

`INLINE_ANALYZER=true` runs the analyzer inside the web process. In production it runs as the
separate `tokensage-analyzer` worker (`python -m tokensage.worker`).

**Memory note:** `depth=full` loads RapidOCR lazily (~250 MB extra). With the engine that is
~400–450 MB, which is tight on Render's 512 MB Starter; if the worker is OOM-killed, move
`tokensage-analyzer` to `standard` (2 GB) in `render.yaml`. `depth=basic` never loads OCR.

## Develop

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy tokensage scripts
uv run pytest -q                          # DB tests need a Postgres at DATABASE_URL
uv run python scripts/export_openapi.py   # after changing any route or schema
```

## Smoke test (Phase 0)

External sources behave differently from datacenter IPs. Run this from a Render shell or one-off
job and save the output to `docs/smoke-test-results.md`:

```bash
SOLANA_RPC_URL=https://... COINGECKO_API_KEY=... python scripts/smoke_test.py
```

## Load test (local, offline)

`scripts/fake_chain_server.py` is a DEV-ONLY fake Solana RPC + IPFS gateway that mints synthetic
pump.fun tokens; `scripts/load_test.py` replays zipf-distributed traffic with the documented
client behaviour (202 → poll, 429/503 → back off) and prints the status mix and latency
percentiles. Never point a deployed service at the fake server.

```bash
uv run python scripts/fake_chain_server.py --port 9999 --tokens 200     # writes load_cas.txt
SOLANA_RPC_URL=http://127.0.0.1:9999/ IPFS_GATEWAYS=http://127.0.0.1:9999 \
  DEV_ALLOW_INSECURE_FETCH=true INLINE_ANALYZER=true RATE_PER_MIN_DEFAULT=600 \
  API_KEYS=load:<key> DATABASE_URL=... uv run uvicorn tokensage.api.app:app --port 10000
uv run python scripts/load_test.py --base http://127.0.0.1:10000 --key <key> \
  --cas load_cas.txt --rate 300 --duration 60 --depth basic
```

Reference run (2026-10-06, one process, inline analyzer, 200 synthetic tokens): 300 req/min for
60 s and a cold-cache 600 req/min for 30 s both returned 100% `200`, no 5xx; warm p50 7 ms,
p95 ≈ 340 ms, max 3.4 s (cold analyses under 32-way concurrency).

## Layout

```
tokensage/api        FastAPI app, /v1 routes, auth, schemas (the contract)
tokensage/resolve    CA validation, pump.fun PDAs, CreateEvent/BondingCurve decoders
tokensage/engine     the meaning engine: normalize, segment, lexicon, ticker, known_coins, image,
                     aggregate, render_summary, pipeline; xref.py = X link parsing
tokensage/sources    external sources with circuit breakers: X mirrors, pump.fun search,
                     DexScreener, Wikimedia pageviews, Google News RSS, CoinGecko
tokensage/fulldepth.py  cached X content, cached OCR, trend index, news confirmation
tokensage/queue.py   Postgres job queue (single-flight, leases, LISTEN/NOTIFY)
tokensage/worker.py  analyzer worker loop
tokensage/jobs       cron entrypoints (knowledge, maintenance)
migrations/          Alembic
data/                taxonomy, slang, known coins, entities, templates/markers/scoring knobs,
                     generated CLDR emoji + WordNet class files (config, not code)
scripts/             smoke_test.py, export_openapi.py, load_test.py, fake_chain_server.py (dev),
                     build_cldr.py, build_wordnet_classes.py
tokensage/callbacks.py  signed webhook delivery for batch jobs
tests/golden/        hand-written meaning cases the engine must satisfy
```
