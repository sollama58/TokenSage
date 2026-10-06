# TokenSage

An HTTP API that takes a Solana pump.fun token's **Contract Address (CA)** and explains what the token *means*: its name, ticker, image, description and linked X/Twitter content, with categories, flags, confidence scores and evidence. Built for other applications to call. No external AI APIs; deployed on Render via a Blueprint.

**Status:** Phase 1 done. The `/v1` API contract, job queue, worker and deploy pipeline exist and are tested; analysis results are **stubs** until Phase 3. See the build plan in [`PROJECT_GUIDE.md`](PROJECT_GUIDE.md) §9.

- [`docs/API.md`](docs/API.md): integration guide for the consumer application
- [`PROJECT_GUIDE.md`](PROJECT_GUIDE.md): full design; [`FABLE_BRIEF.md`](FABLE_BRIEF.md): kickoff brief
- `docs/research/`: research reports with sources; `docs/reference/`: the original tested reference code (now ported into `tokensage/`)

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

## Layout

```
tokensage/api        FastAPI app, /v1 routes, auth, schemas (the contract)
tokensage/resolve    CA validation, pump.fun PDAs, CreateEvent/BondingCurve decoders
tokensage/engine     analysis stages (Phase 3+); xref.py = X link parsing
tokensage/queue.py   Postgres job queue (single-flight, leases, LISTEN/NOTIFY)
tokensage/worker.py  analyzer worker loop
tokensage/jobs       cron entrypoints (knowledge, maintenance)
migrations/          Alembic
data/                taxonomy, lexicons (config, not code)
scripts/             smoke_test.py, export_openapi.py
```
