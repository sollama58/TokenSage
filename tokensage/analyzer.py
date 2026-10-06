"""The analyzer entry point the worker calls per job.

Phase 1: a STUB that produces a schema-valid Analysis from the CA alone, so the whole
API -> queue -> worker -> API path can be exercised. Phase 2 replaces `analyze` with
on-chain resolution + metadata; Phase 3 adds the engine stages.
"""

from __future__ import annotations

from datetime import UTC, datetime

import asyncpg

from tokensage import __version__
from tokensage.api.schemas import Analysis, Evidence, Versions

RULES_VERSION = "0.0.0-stub"
LEXICON_VERSION = "2026-10-06"


def stub_analysis(mint: str, depth: str) -> Analysis:
    now = datetime.now(UTC)
    return Analysis(
        mint=mint,
        depth=depth,  # type: ignore[arg-type]
        analyzed_at=now,
        summary=(
            "STUB: the analysis engine is not implemented yet. This response proves the "
            "API contract and job pipeline only."
        ),
        evidence=[
            Evidence(
                kind="stub",
                label="none",
                weight=0.0,
                detail="no analysis performed",
                source=f"tokensage:{__version__}",
            )
        ],
        caveats=["stub result; no data was fetched"],
        versions=Versions(rules=RULES_VERSION, lexicon=LEXICON_VERSION),
    )


async def analyze(conn: asyncpg.Connection, mint: str, depth: str) -> int:
    """Run an analysis for `mint` at `depth`, persist it, return the new analysis.version."""
    doc = stub_analysis(mint, depth)
    await conn.execute(
        """insert into token (mint, seen_by) values ($1, '{request}')
           on conflict (mint) do update set seen_by =
             case when 'request' = any(token.seen_by) then token.seen_by
                  else token.seen_by || '{request}' end""",
        mint,
    )
    version = await conn.fetchval(
        """insert into analysis (mint, version, depth, doc, referent, categories, flags)
           values ($1, coalesce((select max(version) from analysis where mint=$1), 0) + 1,
                   $2, $3, $4, $5, $6)
           returning version""",
        mint,
        depth,
        doc.model_dump(mode="json"),
        doc.referent.label if doc.referent else None,
        [c.label for c in doc.categories],
        [f.code for f in doc.flags],
    )
    return int(version)
