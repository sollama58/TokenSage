# Kickoff Brief: Build TokenSage

**To:** Fable (the AI engineer building this project)
**From:** the project owner
**Repo:** `sollama58/TokenSage`. Research and design are already committed here.
**Date:** 2026-10-06

---

## 1. The mission in one paragraph

Build **TokenSage**, a standalone HTTP API in this repo. **Another project of mine will call it.** That project sends a **pump.fun token Contract Address (CA)**, and TokenSage returns structured JSON explaining **what the token means**. The explanation draws on:
- the token's name, ticker, description and image;
- the X/Twitter link attached to it (whose tweet, what it says, whether it predates the coin, whether the narrative is borrowed).

The JSON contains a referent ("this refers to Peanut the Squirrel"), categories with confidences, a ticker explanation, copycat findings, flags, a template-generated summary, and the **evidence behind every claim**. It must be deployed on **Render** via a **Render Blueprint** (`render.yaml`).

## 2. Read these, in this order

1. **`PROJECT_GUIDE.md`.** This is the full design and your main reference. Read all of it before writing code. The most important sections:
   - §3: the output contract;
   - §4.1: resolving a CA on-chain;
   - §5: the understanding engine;
   - §6.2–6.4: request flow, caching and the API spec;
   - §8: `render.yaml`;
   - §9: build phases with acceptance criteria.
2. **`docs/reference/`.** Small, tested Python you should port (not reinvent). Run `cd docs/reference && python -m pytest -q`; all 8 tests pass.
   - `pump_ca.py`: validate a CA, derive the pump.fun bonding-curve address, decode the bonding-curve account.
   - `pump_event.py`: decode pump.fun `CreateEvent` logs (tested on real 2026 events).
   - `xref.py`: parse any X/Twitter link, decode tweet/community creation time from the ID, compute the X embed-CDN token.
3. **`docs/research/`.** Four detailed reports with sources. Look things up here when the guide's summary isn't enough.

## 3. Decisions already made (do not revisit)

| Decision | Detail |
|---|---|
| **Standalone API** | This repo is an independent service. The other project integrates over HTTP only, with no shared code or database. |
| **Input = CA** | One pump.fun mint address per request, plus a small batch endpoint. Validate it before any network call. |
| **No external AI** | No OpenAI, Anthropic, Gemini, Google Vision or hosted inference of any kind. The core is deterministic: rules, lexicons, gazetteers, fuzzy matching, perceptual hashing, OCR. Local CPU models (CLIP via ONNX) are allowed only as an optional, flag-gated extra (Phase 8). |
| **No NSFW check** | Do not build image moderation or an NSFW classifier. The API returns the image URL; the consumer decides what to show. (The guide has already been updated to match.) |
| **Explainable** | Every label carries evidence records. The summary is filled from templates, never free text. |
| **Hosting** | Render, defined entirely in `render.yaml`. One region. Web API on **Starter** (always on; not free, which sleeps). Analyzer as a background worker. Two cron jobs. Postgres `basic-256mb`. |
| **Stack** | Python 3.12, `uv`, FastAPI, asyncpg (or SQLAlchemy Core), Alembic, httpx, pydantic v2, pytest + respx, ruff, Docker runtime on Render. |
| **API shape** | `/v1/...`, Bearer API keys, OpenAPI schema committed as `openapi.v1.json`, the wait-or-`202` job flow, and a `partial` status when an upstream source fails (§6.4). |

## 4. Defaults for decisions still open

Proceed with these defaults; don't block on them. List them in your first progress report so I can confirm or change them.

| Open question | Default to use |
|---|---|
| Default analysis depth | `full`, with `wait=10` s; the consumer can ask for `basic` |
| Non-pump.fun mints | Analyse best-effort with `launchpad: "unknown"` (`ACCEPT_NON_PUMP=true`) |
| Paid X fallback (twitterapi.io / SocialData) | Built, but **off** (`ENABLE_PAID_X=false`) |
| Corpus ingester (records every new pump.fun coin for copycat context) | Not in v1; Phase 7 |
| CLIP visual labels | Not in v1; Phase 8 |
| Analyzer placement | Separate Starter worker; `INLINE_ANALYZER=true` supported for local dev |
| Request volume | Design for tens of requests per minute; I'll confirm real numbers |

## 5. Your first milestone (Phases 0 + 1 of the guide)

**Goal:** a deployable skeleton that the other project can already call (with stub data), plus a smoke-test script that tells us which data sources actually work from Render.

**Deliverables:**
1. **`scripts/smoke_test.py`.** It checks every external source listed in guide §9 Phase 0, from wherever it runs:
   - Solana RPC `getAccountInfo` for test CAs;
   - the IPFS gateways;
   - pump.fun's `frontend-api-v3` (`/coins-v2`, `/coins/search`);
   - DexScreener search;
   - FxTwitter, vxTwitter, the X syndication endpoint and oEmbed;
   - CoinGecko, Wikimedia pageviews and Google News RSS.

   Each source is reported as `works / flaky / blocked`, with latency. Include a list of ~30 test CAs: legacy and `create_v2` pump coins, graduated coins, a non-pump SPL token, a wallet address and junk strings. **Ask me for real CAs if you can't find them yourself.** The script should run as a one-off on Render (it can use the worker's Docker image), and write its results as markdown to stdout.
2. **Project skeleton** per guide §12:
   - `pyproject.toml` + `uv.lock`, `Dockerfile`, `render.yaml` (guide §8.1);
   - Alembic migrations for the guide §7 schema;
   - `tokensage/config.py` using pydantic-settings.
3. **API contract with stubs:**
   - pydantic models for the §3 Analysis object and the §6.4 envelope and error shape;
   - all `/v1` routes returning schema-valid stub data;
   - API-key auth, per-key rate limiting, CA validation (`400 invalid_ca` before any network call);
   - `/healthz`;
   - `openapi.v1.json` committed.
4. **Worker skeleton:** a job loop on the Postgres `job` table (claim with `FOR UPDATE SKIP LOCKED`, leases, `LISTEN/NOTIFY`) and clean SIGTERM handling. It can process stub jobs end to end.
5. **CI:** GitHub Actions running ruff, mypy (lenient), pytest, and an OpenAPI compatibility diff.
6. **`docs/API.md` (first draft):** for the other project's developer. Auth, the main call, the example request, every status and error code, retry/poll advice, and a plain note that images are not screened.

**Done when:**
- `render.yaml` validates;
- tests pass locally and in CI;
- a `GET /v1/tokens/{ca}` with a valid key returns a schema-valid stub;
- invalid input returns `400`, and a missing key `401`;
- a stub job goes from the API through the worker and back via the wait-or-`202` flow;
- the smoke-test script is ready for me to run on Render.

**After that:** continue with guide §9 Phases 2 → 6 in order. Each phase has its own acceptance criteria. Stop and report at the end of each phase.

## 6. What I (the owner) will do, and when you need it

You probably can't reach Render, Solana or X from your own environment. Tell me when you need any of these:
- Create the Render Blueprint from this repo and set the `sync: false` secrets:
  - `SOLANA_RPC_URL`: a Helius free-tier RPC URL;
  - `API_KEYS`: for the consumer project;
  - `COINGECKO_API_KEY`: a free Demo key.
- Run `scripts/smoke_test.py` on Render and paste the output back to you. **Phase 2 onward depends on those results.** Update guide §4 and §13 with them.
- Provide real test CAs and answer the questions in §4 above.

## 7. Working rules

- **Port the reference code** from `docs/reference/` into `tokensage/` with its tests. Don't rewrite tested logic from scratch.
- **Tolerant parsing everywhere.** On-chain layouts and unofficial APIs change. Validate, degrade to `partial`, log unknown shapes, and never crash on one bad token.
- **Treat all fetched content as hostile:**
  - an SSRF guard on every outbound fetch of a URL that came from token data (guide §10);
  - size and time caps;
  - no `javascript:`/`data:` URLs in responses.
- **No live network in unit tests.** Use recorded fixtures (`tests/fixtures/`) and the golden test set (`tests/golden/`, guide §9 Phase 3).
- **Config, not code,** for thresholds, weights, the taxonomy and lexicons (`data/*.yaml`).
- **Keep `PROJECT_GUIDE.md` current.** When reality differs from the guide (smoke-test results, changed endpoints, a better approach), update it in the same commit.
- **Commits:** small and descriptive, on the working branch I give you. Don't open PRs or push to `main` unless I ask.
- **Report at each milestone:**
  - what was built;
  - what was verified, and how;
  - what is blocked and what you need from me;
  - any default from §4 you think should change.
