"""Golden meaning cases: run the pure engine on hand-written tokens. No DB, no network."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from tokensage.engine.knowledge import load_knowledge
from tokensage.engine.pipeline import EngineInput, run_basic
from tokensage.engine.ticker import explain

CASES = yaml.safe_load((Path(__file__).parent / "golden" / "cases.yaml").read_text("utf-8"))[
    "cases"
]
assert len(CASES) >= 60, f"golden set has {len(CASES)} cases; guide requires >= 60"


def _ids() -> list[str]:
    return [f"{c.get('name', '')!r}/{c.get('symbol', '')}" for c in CASES]


def _run(case: dict[str, Any]):  # type: ignore[no-untyped-def]
    inp = EngineInput(
        mint="So11111111111111111111111111111111111111112",
        name=case.get("name"),
        symbol=case.get("symbol"),
        description=case.get("description"),
        image_bytes=None,
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    return run_basic(inp)


@pytest.mark.parametrize("case", CASES, ids=_ids())
def test_golden(case: dict[str, Any]) -> None:
    out = _run(case)
    cats = dict(out.agg.categories)
    flags = {f.code for f in out.flags}
    copies = {c.get("ticker") for c in out.copy_of}
    ctx = (
        f"\n  categories={cats}\n  referent={out.agg.referent}\n  flags={flags}\n  copies={copies}"
    )
    for lbl in case.get("categories_include", []):
        assert lbl in cats, f"missing category {lbl}{ctx}"
    for lbl in case.get("categories_exclude", []):
        assert lbl not in cats, f"unexpected category {lbl}{ctx}"
    if "top_category" in case:
        assert out.agg.categories, f"no categories{ctx}"
        assert out.agg.categories[0][0].startswith(case["top_category"]), ctx
    if "referent" in case:
        assert out.agg.referent is not None, f"no referent{ctx}"
        want = case["referent"].lower().replace(" ", "")
        assert want in out.agg.referent.label.lower().replace(" ", ""), ctx
        assert out.agg.referent.score >= 0.45, ctx
    if case.get("no_referent"):
        assert out.agg.referent is None or out.agg.referent.score < 0.45, ctx
    for code in case.get("flags_include", []):
        assert code in flags, f"missing flag {code}{ctx}"
    for code in case.get("flags_exclude", []):
        assert code not in flags, f"unexpected flag {code}{ctx}"
    if "ticker_method" in case:
        method = explain(out.normalized, load_knowledge()).method
        assert method == case["ticker_method"], f"ticker method {method}{ctx}"
    for t in case.get("copy_of_includes", []):
        assert t in copies, f"copy_of lacks {t}{ctx}"
    for t in case.get("copy_of_excludes", []):
        assert t not in copies, f"copy_of should not list {t}{ctx}"
    if "tokens" in case:
        assert out.normalized.name_tokens == case["tokens"], ctx
    for o in case.get("obfuscation_include", []):
        assert o in out.normalized.obfuscation, ctx
    for c in case.get("caveats_include", []):
        assert any(c in cv for cv in out.caveats), f"caveat {c!r} missing: {out.caveats}"
    # every result must be explainable and summarised
    assert out.summary and len(out.summary) > 20
    for ev in out.evidence:
        assert ev.detail and ev.source


def test_engine_speed_budget() -> None:
    """p95 of basic analysis (text only) must stay well under the 300 ms CPU budget."""
    _run(CASES[0])  # warm caches
    times: list[float] = []
    for case in CASES:
        t0 = time.perf_counter()
        _run(case)
        times.append(time.perf_counter() - t0)
    times.sort()
    p95 = times[int(len(times) * 0.95) - 1]
    assert p95 < 0.3, f"p95 {p95 * 1000:.0f} ms"
