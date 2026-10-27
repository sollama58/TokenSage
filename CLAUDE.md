# Working in TokenSage

TokenSage reads a Solana token's meaning (name, narrative, X account, referents, trend heat) and
serves it over `/v1` to TrenchScanner, its one consumer. `README.md` covers running it and the
layout. `PROJECT_GUIDE.md` is the original design (its §13 open questions are closed) and
`CODE_REVIEW.md` a dated review whose findings shipped in #81.

## Checks before pushing

With Python 3.12 and a Postgres at `DATABASE_URL` (`uv run alembic upgrade head`):

```bash
uv sync --frozen --all-extras --python 3.12
uv run ruff check tokensage tests scripts && uv run ruff format --check tokensage tests scripts
uv run mypy tokensage scripts
uv run python scripts/export_openapi.py --check   # after changing a route or schema: run without --check and commit
TS_REQUIRE_DB=1 uv run pytest -q
```

`TS_REQUIRE_DB=1` makes an unreachable Postgres fail the run; without it the DB-backed tests skip
and a "green" run covers far less than it says. CI sets it.

## Shipping

- `main` is the working branch. Branch from the latest `main`, open the PR into `main`, drive CI
  green, merge. If `main` moves first, merge it into the branch and let CI re-run.
- Never skip or quarantine a test to get green.
- The `/v1` contract is `openapi.v1.json`; TrenchScanner's client
  (`packages/core/src/datasources/tokensage.ts` in that repo) must keep agreeing with it.
- Changes to what is read or fed to TrenchScanner's models, spend (paid X, Helius, CoinGecko), env
  vars and plans go to the owner first.

## Safety

Token metadata, tweets, websites and images are hostile input. Fetch only through
`tokensage/net/safe_fetch.py` (SSRF guard, pinned dial, size caps), and never log or commit a key
or a credentialed URL.
