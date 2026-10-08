"""Referent recall: how often the engine says what a coin refers to (guide §5.9).

The golden set only guards against regressions on names the seed was written for; this is
the production number. Every analysis logs its referent status, and the hourly maintenance
job logs the last day's shares per depth. `none` and `guess` rows are the ones to sample
for the labelling set."""

from __future__ import annotations

from typing import Any

import asyncpg

# The reported confidence bands (rules 0.17.0): two or more independent inputs agree at
# `resolved` and above; a named referent from one input between `guess` and `resolved`;
# below `guess` only the kind is known (a generic referent) or a weak guess.
RESOLVED = 0.7
GUESS = 0.5
STATUSES = ("resolved", "weak", "guess", "none")
# the summary runs from the hourly cron, which nobody waits on
SUMMARY_TIMEOUT_S = 120.0


def status(score: float | None) -> str:
    if score is None:
        return "none"
    if score >= RESOLVED:
        return "resolved"
    if score >= GUESS:
        return "weak"
    return "guess"


async def summary(conn: asyncpg.Connection, hours: int = 24) -> dict[str, Any]:
    """Per depth, how many analyses in the last `hours` ended in each referent status, and
    the share that resolved. Only the newest version per mint and depth counts.

    The window is read first (materialized, from analysis_recent_idx alone): left to
    itself, the planner may answer `distinct on (mint, depth)` by walking the whole
    (mint, depth, version) index in order and filtering on created_at, which reads every
    analysis ever written."""
    rows = await conn.fetch(
        """with recent as materialized (
             select mint, depth, version, referent_score, referent
             from analysis
             where created_at >= now() - make_interval(hours => $1)),
           latest as (
             select distinct on (mint, depth) depth, referent_score, referent
             from recent
             order by mint, depth, version desc)
           select depth,
                  count(*) filter (where referent is null) as none,
                  count(*) filter (where referent is not null and referent_score < $2) as guess,
                  count(*) filter (where referent_score >= $2 and referent_score < $3) as weak,
                  count(*) filter (where referent_score >= $3) as resolved,
                  count(*) as total
           from latest group by depth order by depth""",
        hours,
        GUESS,
        RESOLVED,
        timeout=SUMMARY_TIMEOUT_S,
    )
    out: dict[str, Any] = {"hours": hours, "depths": {}}
    for r in rows:
        total = int(r["total"])
        out["depths"][r["depth"]] = {
            "total": total,
            "none": int(r["none"]),
            "guess": int(r["guess"]),
            "weak": int(r["weak"]),
            "resolved": int(r["resolved"]),
            "resolved_share": round(int(r["resolved"]) / total, 3) if total else None,
        }
    return out
