"""Category precision/recall audit against a hand-labelled set of real pump.fun launches.

    uv run python scripts/category_audit.py [tests/golden/category_audit.yaml] [--rows]

Runs the basic-depth engine on each coin's name, ticker and description (no image, no
database, no network) and compares the top-level categories it reports with the labels a
person gave the coin. A category counts as predicted when any label under it reaches the
reported floor (min_category_confidence, 0.2), and separately at 0.5.
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from tokensage.engine.pipeline import EngineInput, run_basic

DEFAULT = Path(__file__).resolve().parents[1] / "tests" / "golden" / "category_audit.yaml"
# derivative is a relation to another coin, decided by the copy and lineage stages, not by
# the coin's own words; it is left out of the theme audit.
SKIP = {"derivative"}


def load(path: Path) -> list[dict[str, Any]]:
    return yaml.safe_load(path.read_text("utf-8"))["coins"]


def predict(c: dict[str, Any]) -> list[tuple[str, float]]:
    out = run_basic(
        EngineInput(
            mint=c.get("mint") or "So11111111111111111111111111111111111111112",
            name=c.get("name"),
            symbol=c.get("symbol"),
            description=c.get("description"),
            image_bytes=None,
            created_at=datetime(2026, 10, 7, 19, 30, tzinfo=UTC),
        )
    )
    return out.agg.categories


def top_level(cats: list[tuple[str, float]], floor: float) -> dict[str, float]:
    out: dict[str, float] = {}
    for lbl, s in cats:
        top = lbl.split("/")[0]
        if top in SKIP or s < floor:
            continue
        out[top] = max(out.get(top, 0.0), s)
    return out


def score(coins: list[dict[str, Any]], floor: float) -> dict[str, Any]:
    tp: Counter[str] = Counter()
    fp: Counter[str] = Counter()
    fn: Counter[str] = Counter()
    none_pred = none_gold = 0
    rows = []
    for c in coins:
        gold = set(c.get("labels") or []) - SKIP
        pred = top_level(predict(c), floor)
        if not pred:
            none_pred += 1
        if not gold:
            none_gold += 1
        for lbl in pred:
            (tp if lbl in gold else fp)[lbl] += 1
        for lbl in gold - set(pred):
            fn[lbl] += 1
        rows.append((c, gold, pred))
    labels = sorted(set(tp) | set(fp) | set(fn))
    per = {}
    for lbl in labels:
        p = tp[lbl] / (tp[lbl] + fp[lbl]) if tp[lbl] + fp[lbl] else None
        r = tp[lbl] / (tp[lbl] + fn[lbl]) if tp[lbl] + fn[lbl] else None
        per[lbl] = {"tp": tp[lbl], "fp": fp[lbl], "fn": fn[lbl], "precision": p, "recall": r}
    T, F, N = sum(tp.values()), sum(fp.values()), sum(fn.values())
    return {
        "n": len(coins),
        "per": per,
        "micro_precision": T / (T + F) if T + F else None,
        "micro_recall": T / (T + N) if T + N else None,
        "no_category": none_pred,
        "no_gold": none_gold,
        "rows": rows,
    }


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.2f}"


def main(argv: list[str]) -> None:
    args = [a for a in argv if not a.startswith("--")]
    coins = load(Path(args[0]) if args else DEFAULT)
    for floor in (0.2, 0.5):
        s = score(coins, floor)
        print(
            f"\n## floor {floor}: {s['n']} coins, no category {s['no_category']} "
            f"(hand labels: {s['no_gold']} have no theme)"
        )
        print("| category | tp | fp | fn | precision | recall |")
        print("|---|---|---|---|---|---|")
        for lbl, v in s["per"].items():
            print(
                f"| {lbl} | {v['tp']} | {v['fp']} | {v['fn']} | {_pct(v['precision'])} "
                f"| {_pct(v['recall'])} |"
            )
        print(
            f"| **all (micro)** | | | | {_pct(s['micro_precision'])} | {_pct(s['micro_recall'])} |"
        )
        if "--rows" in argv and floor == 0.2:
            for c, gold, pred in s["rows"]:
                if gold != set(pred):
                    print(
                        f"  {c['name']!r:34} ${c.get('symbol')!s:10} gold={sorted(gold)} "
                        f"pred={ {k: round(v, 2) for k, v in pred.items()} }"
                    )


if __name__ == "__main__":
    main(sys.argv[1:])
