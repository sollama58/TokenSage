"""Precompute data/wordnet_classes.json offline (never load NLTK/WordNet at runtime).

    NLTK_DATA=/path/with/corpora/wordnet python scripts/build_wordnet_classes.py

For each class we take all hyponym lemmas of the listed WordNet synsets. The result is a
{class: [words]} map, a few hundred KB, used as a gazetteer for the animal/food/object rules.
WordNet licence is permissive (BSD-like). Words that collide with common English or crypto
slang are pruned by the DENY list.

A word joins a class only when one of its most common noun senses is in that class: "world"
is a hyponym of animal.n.01 through a rare sense, "center" of food.n.02 and "right" of
body_part.n.01, and none of them means that in a coin name. The usual sense comes from
WordNet's tagged-use counts ("chicken" is first the meat, then the bird, so animals accept a
30% share).
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
    "bull",
    "bear",  # kept via explicit lexicon with market meaning
    "chad",
    "jack",
    "john",
    "sol",
    "doge",
    "bonk",
    "pump",
    "dump",
    "moon",
    "rocket",
    "cock",
    "ass",
    "pussy",
    "bitch",
    "stud",
    "game",
    "pet",
    "young",
    "adult",
    "male",
    "female",
    "giant",
    "dwarf",
    "common",
    "little",
    "big",
    "great",
    "american",
    "european",
    "african",
    "domestic",
    "wild",
    "water",
    "sea",
    "land",
    "house",
    "field",
    "tree",
    "ground",
    "stock",
    # people are animals to WordNet
    "human",
    "humans",
    "humanity",
    "humankind",
    "mankind",
    "man",
    "homo",
    # animal senses no coin name means
    "head",
    "entire",
    "royal",
    "mount",
    "charger",
    "billy",
    "fisher",
    "soldier",
    "das",
    "jenny",
    "mutant",
    "predator",
    "feeder",
    "prey",
    "bot",
    "tick",
    "chat",
    "redhead",
    "brent",
    "solitaire",
    "weaver",
    "blackburn",
    "merlin",
    "argus",
    "cornish",
    "dominique",
    "creature",
    "beast",
    "livestock",
    "worker",
    "copper",
    "world",
    "blue",
    "bay",
    "kit",
    # food, body-part, emotion and vehicle senses no coin name means
    "feed",
    "sub",
    "mix",
    "vintage",
    "produce",
    "generic",
    "cisco",
    "jonathan",
    "marc",
    "murphy",
    "chuck",
    "msg",
    "provisions",
    "costa",
    "res",
    "lat",
    "sticker",
    "quick",
    "behind",
    "small",
    "despite",
    "technical",
    "electric",
    "sam",
    "launch",
    "gig",
    "apc",
    "clarence",
    "comma",
    "captive",
    "admiral",
    "sawyer",
    "nanny",
    "arab",
    "arabian",
    "hampshire",
    "shire",
    "sierra",
    "brit",
    "britt",
    "argentine",
    "hind",
    "liza",
    "molly",
    "mollie",
    "mademoiselle",
    "margate",
    "durham",
    "devon",
    "springer",
    "galloway",
    "guernsey",
    "ayrshire",
    "cardigan",
    "pembroke",
    "shetland",
    "newfoundland",
    "cairn",
    "lhasa",
    "tom",
    "spam",
    "organs",
    "spirits",
    "hay",
    "sage",
    "duff",
    "gum",
    "ticker",
    "hooks",
    "pathway",
    "receptor",
    "nucleus",
    "vessel",
    "valve",
    "socket",
    "thumbnail",
    "tissue",
    "lap",
    "lid",
    "ala",
    "optic",
    "yen",
    "compatibility",
    "preference",
    "harassment",
    "belonging",
    "carrier",
    "tank",
    "balloon",
    "hoy",
    "galley",
    "pullman",
    "queen",
    "kid",
    "fauna",
}
MAX_WORD_LEN = 20
# a word no corpus tagged: its first noun senses ("taco": a slur, then the food)
UNCOUNTED_SENSES = 2
# Mascot words whose usual WordNet sense is something else but which, in a coin name, are
# the animal ("kitty" is first a pool of money, "chihuahua" a Mexican state).
ALLOW: dict[str, set[str]] = {
    "animal/cat": {"kitty", "kitten", "siamese", "wildcat", "angora", "persian"},
    "animal/dog": {
        "chihuahua",
        "samoyed",
        "maltese",
        "shiba",
        "akita",
        "puppy",
        "pup",
        "husky",
        "pooch",
    },
    "animal/squirrel": {"gopher"},
    "animal/bird": {
        "canary",
        "kiwi",
        "crane",
        "cardinal",
        "kite",
        "swallow",
        "pigeon",
        "duck",
        "crow",
        "gull",
        "quail",
        "thrush",
    },
    "animal/fish": {
        "tuna",
        "cod",
        "pike",
        "perch",
        "snapper",
        "shark",
        "whale",
        "blowfish",
        "seahorse",
        "hammerhead",
        "flounder",
    },
    "animal/other": {"dragon", "monster", "beaver", "coral"},
}


def in_class(word: str, cls: str, members: set[str]) -> bool:
    """The word's usual sense is in the class (members: synset names). Usual = the sense
    with the most tagged uses (WordNet's SemCor counts) over every part of speech; a word no
    corpus tagged falls back to its first two noun senses. "small" and "fit" have noun senses
    under body_part and emotion, but they are adjectives first."""
    name = word.replace(" ", "_")
    family = cls.split("/")[0]
    if word in ALLOW.get(cls, ()):
        return True
    senses = wn.synsets(name)
    counts = [
        sum(lm.count() for lm in s.lemmas() if lm.name().lower() == name.lower()) for s in senses
    ]
    total = sum(counts)
    if total:
        share = sum(c for s, c in zip(senses, counts, strict=True) if s.name() in members)
        # animals: a common second sense counts ("chicken": the meat, then the bird)
        return share / total >= (0.3 if family == "animal" else 0.5)
    return any(s.name() in members for s in wn.synsets(name, pos=wn.NOUN)[:UNCOUNTED_SENSES])


def synsets_of(syn_names: list[str]) -> set[str]:
    out: set[str] = set()
    for name in syn_names:
        s = wn.synset(name)
        out |= {s.name(), *(h.name() for h in s.closure(lambda x: x.hyponyms()))}
    return out


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
        members = synsets_of(syns)
        words = {w for w in lemmas(syns) if in_class(w, cls, members)}
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
