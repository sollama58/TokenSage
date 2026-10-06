"""Build the packaged Wikidata gazetteer (guide §4.4) and its common-word list.

    python scripts/build_gazetteer.py            # data/gazetteer_wikidata.json.gz
    NLTK_DATA=/path/with/corpora/wordnet python scripts/build_gazetteer.py --words
                                                 # data/common_words.txt.gz

The snapshot is what a fresh deploy (and the test suite) matches against; the knowledge
cron refreshes the same entities monthly into the `entity` table. The Wikidata query
service asks for one query at a time and a descriptive User-Agent; this takes a few
minutes. Wikidata data is CC0.

The common-word list is every WordNet lemma with an ordinary (non proper-noun) sense: a
one-word Wikidata label that is also a dictionary word ("Vine", "Office") is too ambiguous
to match on its own.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tokensage.engine.gazetteer import COMMON_WORDS, SNAPSHOT  # noqa: E402
from tokensage.sources import wikidata  # noqa: E402

DATA = ROOT / "data"
UA = "TokenSage/0.1 (+https://github.com/sollama58/TokenSage) gazetteer build"


async def build_snapshot(out: Path) -> None:
    async with httpx.AsyncClient(headers={"User-Agent": UA}) as http:
        entities, failed = await wikidata.fetch_all(http)
    if failed:
        print(f"groups that failed: {', '.join(failed)}", file=sys.stderr)
    doc = {
        "version": "wikidata-" + datetime.now(UTC).strftime("%Y-%m-%d"),
        "source": "Wikidata (CC0), query.wikidata.org",
        "entities": [e.to_json() for e in entities],
    }
    with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(entities)} entities -> {out} ({out.stat().st_size // 1024} KB)")


def build_words(out: Path) -> None:
    from nltk.corpus import wordnet as wn

    words = set()
    for lemma in wn.all_lemma_names():
        if not lemma.isalpha() or len(lemma) < 3:
            continue
        # proper nouns ("sydney", "madonna") are WordNet instances, not dictionary words
        if any(not s.instance_hypernyms() for s in wn.synsets(lemma)):
            words.add(lemma.lower())
    with gzip.open(out, "wt", encoding="utf-8", compresslevel=9) as f:
        f.write("\n".join(sorted(words)) + "\n")
    print(f"{len(words)} words -> {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--words", action="store_true", help="build the common-word list instead")
    args = ap.parse_args()
    if args.words:
        build_words(DATA / COMMON_WORDS)
    else:
        asyncio.run(build_snapshot(DATA / SNAPSHOT))


if __name__ == "__main__":
    main()
