"""Loads data/taxonomy.yaml (categories and flag codes)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@lru_cache
def load_taxonomy() -> dict[str, Any]:
    with (DATA_DIR / "taxonomy.yaml").open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict) and "categories" in data and "flags" in data
    return data


def category_labels() -> set[str]:
    return {c["label"] for c in load_taxonomy()["categories"]}


def flag_codes() -> set[str]:
    return {f["code"] for f in load_taxonomy()["flags"]}
