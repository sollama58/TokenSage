"""Referent coverage against the hand-labelled category audit sets.

    uv run python scripts/referent_audit.py [file ...] [--rows]

Runs the basic-depth engine on each coin's name, ticker and description (no image, no
database, no network) and reports how many coins get no referent at all, split by whether a
person could read a theme from the coin (its hand labels) or not; the reported kinds,
confidence bands and supported_by counts; and the same coins relaunched as copies an hour
later with the original's read stored, to show how many copies carry a kind.
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from tokensage.engine.context import ReferentCandidate
from tokensage.engine.pipeline import (
    DbContext,
    EngineInput,
    EngineOutput,
    PriorRead,
    SameNameToken,
    run_basic,
)

GOLDEN = Path(__file__).resolve().parents[1] / "tests" / "golden"
DEFAULT = [GOLDEN / "category_audit.yaml", GOLDEN / "category_audit_holdout.yaml"]
NOW = datetime(2026, 10, 7, 19, 30, tzinfo=UTC)
ORIGINAL = "Orig1111111111111111111111111111111111111pump"
COPY = "Copy111111111111111111111111111111111111pump"


def run(
    c: dict[str, Any], mint: str, ctx: DbContext | None = None, when: datetime = NOW
) -> EngineOutput:
    return run_basic(
        EngineInput(
            mint=mint,
            name=c.get("name"),
            symbol=c.get("symbol"),
            description=c.get("description"),
            image_bytes=None,
            created_at=when,
            ctx=ctx or DbContext(),
        )
    )


def band(s: float) -> str:
    return "<0.3" if s < 0.3 else "0.3-0.49" if s < 0.5 else "0.5-0.69" if s < 0.7 else "0.7+"


def audit(path: Path, rows: bool) -> None:
    coins = yaml.safe_load(path.read_text("utf-8"))["coins"]
    null = plain_null = unreadable = 0
    kinds: Counter[str] = Counter()
    bands: Counter[str] = Counter()
    support: Counter[str] = Counter()
    copies_with_kind = 0
    misses = []
    for c in coins:
        out = run(c, c.get("mint") or ORIGINAL)
        rr = out.referent_read
        gold = set(c.get("labels") or []) - {"derivative"}
        unreadable += not gold
        if rr is None:
            null += 1
            if gold:
                plain_null += 1
                misses.append((c.get("name"), c.get("symbol"), sorted(gold)))
        else:
            kinds[rr.kind + (" (generic)" if rr.generic else "")] += 1
            bands[band(rr.confidence)] += 1
            support["2+" if len(rr.supported_by) >= 2 else str(len(rr.supported_by))] += 1
        # the same coin relaunched as a copy an hour later, with the original's read stored
        prior = PriorRead(categories=list(out.agg.categories))
        if rr is not None:
            prior.referent = ReferentCandidate(
                rr.label, rr.kind, rr.desc, "analysis", rr.confidence, generic=rr.generic
            )
        ctx = DbContext(
            same_name=[SameNameToken(ORIGINAL, c.get("name"), c.get("symbol"), NOW, "db")],
            originals={ORIGINAL: prior},
        )
        copies_with_kind += run(c, COPY, ctx, NOW + timedelta(hours=1)).referent_read is not None
    n = len(coins)
    print(f"{path.name}: {n} coins")
    print(
        f"  no referent: {null} ({null / n:.0%}); kind plain by hand {plain_null}, "
        f"no readable theme {null - plain_null} (hand labels: {unreadable} unreadable)"
    )
    print(f"  kinds: {dict(kinds.most_common())}")
    print(f"  confidence: {dict(sorted(bands.items()))}")
    print(f"  supported_by inputs: {dict(sorted(support.items()))}")
    print(f"  copies with a kind: {copies_with_kind}/{n} ({copies_with_kind / n:.0%})")
    if rows:
        for m in misses:
            print("   ", m)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    for path in [Path(a) for a in args] or DEFAULT:
        audit(path, "--rows" in sys.argv)


if __name__ == "__main__":
    main()
