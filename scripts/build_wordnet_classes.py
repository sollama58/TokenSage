"""Precompute data/wordnet_classes.json offline (never load NLTK/WordNet at runtime).

    NLTK_DATA=/path/with/corpora/wordnet python scripts/build_wordnet_classes.py

For each class we take all hyponym lemmas of the listed WordNet synsets. The result is a
{class: [words]} map, a few hundred KB, used as a gazetteer for the animal/food/object rules.
WordNet licence is permissive (BSD-like). Words that collide with common English or crypto
slang are pruned by the DENY list.
"""

from __future__ import annotations

import json
from pathlib import Path

from nltk.corpus import wordnet as wn

OUT = Path(__file__).resolve().parent.parent / "data" / "wordnet_classes.json"

CLASSES: dict[str, list[str]] = {
    "animal/dog": ["dog.n.01", "wolf.n.01", "fox.n.01"],
    "animal/cat": ["cat.n.01", "big_cat.n.01"],
    "animal/frog": ["frog.n.01", "toad.n.01"],
    "animal/monkey": ["monkey.n.01", "ape.n.01"],
    "animal/hippo": ["hippopotamus.n.01"],
    "animal/squirrel": ["squirrel.n.01", "chipmunk.n.01"],
    "animal/bird": ["bird.n.01"],
    "animal/bear_bull": ["bear.n.01", "bull.n.01", "cattle.n.01"],
    "animal/fish": ["fish.n.01", "shark.n.01", "whale.n.02", "dolphin.n.02", "octopus.n.01"],
    "animal/other": ["animal.n.01"],
    "food": ["food.n.01", "food.n.02", "beverage.n.01"],
    "vehicle": ["vehicle.n.01"],
    "body_part": ["body_part.n.01"],
    "emotion": ["emotion.n.01", "feeling.n.01"],
}
DENY = {
    "bull", "bear",  # kept via explicit lexicon with market meaning
    "chad", "jack", "john", "sol", "doge", "bonk", "pump", "dump", "moon", "rocket",
    "cock", "ass", "pussy", "bitch", "stud", "game", "pet", "young", "adult", "male", "female",
    "giant", "dwarf", "common", "little", "big", "great", "american", "european", "african",
    "domestic", "wild", "water", "sea", "land", "house", "field", "tree", "ground", "stock",
}
MAX_WORD_LEN = 20


def lemmas(syn_names: list[str]) -> set[str]:
    out: set[str] = set()
    for name in syn_names:
        try:
            s = wn.synset(name)
        except Exception:  # noqa: BLE001
            print("missing synset", name)
            continue
        for h in [s, *s.closure(lambda x: x.hyponyms())]:
            for lemma in h.lemma_names():
                w = lemma.replace("_", " ").lower()
                if 2 < len(w) <= MAX_WORD_LEN and w not in DENY and w.isascii():
                    out.add(w)
    return out


def main() -> int:
    data: dict[str, list[str]] = {}
    seen: set[str] = set()
    # specific classes first so "animal/other" does not swallow them
    for cls, syns in CLASSES.items():
        words = lemmas(syns)
        if cls == "animal/other":
            words -= seen
        else:
            seen |= words
        data[cls] = sorted(words)
    OUT.write_text(json.dumps(data, separators=(",", ":")) + "\n")
    print(f"wrote {OUT}: " + ", ".join(f"{k}={len(v)}" for k, v in data.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
