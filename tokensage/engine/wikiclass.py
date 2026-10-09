"""Read a Wikidata/Wikipedia short description ("American actress (born 1997)", "Japanese
dog and Internet meme celebrity") as a referent kind and taxonomy categories. Keyword
rules only: the short description is written to a house style, so a few words carry it."""

from __future__ import annotations

import re

_ANIMALS: tuple[tuple[str, str], ...] = (
    ("animal/dog", r"dog|puppy|shiba|corgi|terrier|retriever|poodle|bulldog|husky|pug|chihuahua"),
    ("animal/cat", r"cat|kitten|kitty"),
    ("animal/frog", r"frog|toad"),
    ("animal/monkey", r"monkey|ape|chimpanzee|gorilla|orangutan|macaque|baboon|bonobo"),
    ("animal/hippo", r"hippo|hippopotamus"),
    ("animal/squirrel", r"squirrel|chipmunk"),
    ("animal/bird", r"bird|penguin|parrot|eagle|owl|duck|goose|crow|pigeon|chicken|rooster"),
    ("animal/bear_bull", r"bear|bull|cow|ox|bison|cattle"),
    ("animal/fish", r"fish|shark|whale|dolphin|octopus|orca"),
)
_ANIMAL_RE = [(cat, re.compile(rf"\b(?:{words})s?\b")) for cat, words in _ANIMALS]
_ANIMAL_ANY = re.compile(
    r"\b(?:animal|pet|mammal|hippopotamus|rodent|reptile|alligator|crocodile|capybara|horse|"
    r"rabbit|raccoon|goat|pig|tortoise|turtle|lizard|snake|penguin|squirrel|dog|cat|bear)s?\b"
)

# (pattern, kind, categories) in priority order: the first match wins
_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (r"internet meme|meme|viral video|copypasta|catchphrase", "meme", ("meme_template/other",)),
    (
        r"chatbot|language model|virtual assistant|artificial intelligence|\bai\b",
        "concept",
        ("ai_agent",),
    ),
    (
        # "president" as a word: a "presidential election" is an event, not a person
        r"politician|\bpresident\b|presidential candidate|prime minister|senator|congressman|"
        r"congresswoman|governor|"
        r"political|activist|minister|monarch|king of|queen of|mayor|diplomat|party leader",
        "person",
        ("political",),
    ),
    (
        r"rapper|singer|musician|songwriter|record producer|\bdj\b|band\b|guitarist|drummer|"
        r"composer",
        "person",
        ("celebrity/musician",),
    ),
    (
        r"footballer|football player|basketball|baseball|tennis|boxer|mixed martial|wrestler|"
        r"racing driver|cricketer|athlete|golfer|ice hockey|quarterback|sprinter|olympic|"
        r"chess player|fighter",
        "person",
        ("celebrity/athlete",),
    ),
    (
        r"youtuber|streamer|influencer|internet personality|internet celebrity|tiktoker|"
        r"podcaster|content creator|vlogger",
        "person",
        ("celebrity/streamer_kol",),
    ),
    (
        r"actor|actress|comedian|model|presenter|television host|entrepreneur|businessman|"
        r"businesswoman|business executive|investor|billionaire|chief executive|ceo|"
        r"founder|programmer|computer scientist|engineer|journalist|author|writer|director|"
        r"filmmaker|socialite|personality",
        "person",
        ("celebrity/other",),
    ),
    (
        r"television series|tv series|sitcom|animated series|film\b|video game|anime|manga|"
        r"cartoon|fictional character|character in|character from|comic|franchise|"
        r"social network|website|mobile app|video-hosting|video hosting|streaming service|"
        r"web series|toy|mascot|brand",
        "other",
        ("pop_culture",),
    ),
    (
        r"(?:cryptocurrency|crypto|bitcoin|blockchain)[- ](?:exchange|company|firm|platform|"
        r"broker|lender|wallet)|stablecoin issuer|bitcoin treasury",
        "concept",
        ("crypto_native/company",),
    ),
    (
        # "crypto" as a word: a cryptographer or cryptozoologist is not a coin
        r"cryptocurrenc|blockchain|memecoin|meme coin|\bcrypto\b|stablecoin",
        "coin",
        ("crypto_native/chain_or_coin",),
    ),
    (
        r"\b(?:incident|attack|shooting|assassination|scandal|protest|election|war|crisis|"
        r"summit|ceasefire|disaster|earthquake|hurricane|controversy|trial)\b",
        "event",
        ("news_event",),
    ),
)
_RULES_RE = [(re.compile(p), kind, cats) for p, kind, cats in _RULES]
# A person whose description is about crypto ("co-founder of Ethereum") is also a crypto figure.
_CRYPTO_PERSON = re.compile(
    r"cryptocurrenc|\bcrypto\b|blockchain|bitcoin|ethereum|solana|binance|memecoin|\bnft"
)


def animal_category(desc: str) -> str:
    d = desc.lower()
    best: tuple[int, str] | None = None
    for cat, rx in _ANIMAL_RE:
        m = rx.search(d)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), cat)  # the first species named is the animal itself
    return best[1] if best else "animal/other"


def is_animal(desc: str) -> bool:
    d = desc.lower()
    return bool(_ANIMAL_ANY.search(d)) and not re.search(r"\b(?:species|genus|family|breed)\b", d)


def classify(desc: str) -> tuple[str, list[str]]:
    """(kind, categories) for a short description; ("other", []) when nothing fits."""
    d = (desc or "").lower()
    if not d:
        return "other", []
    if is_animal(d) and not re.search(r"\b(?:actor|actress|singer|rapper|politician)\b", d):
        return "famous_animal", [animal_category(d)]
    for rx, kind, cats in _RULES_RE:
        if rx.search(d):
            out = list(cats)
            if kind == "person" and _CRYPTO_PERSON.search(d):
                out.append("crypto_native/person")
            return kind, out
    return "other", []
