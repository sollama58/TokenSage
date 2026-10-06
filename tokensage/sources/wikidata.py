"""The Wikidata entity gazetteer (guide §4.4): people, famous animals, Internet memes, AI
chatbots and pop-culture items a coin can be named after, with their English labels,
aliases and short descriptions.

Fetched offline (scripts/build_gazetteer.py writes the packaged snapshot) and monthly by
the knowledge cron (into the `entity` table). Never called while analysing a token.

Each query group is a class of items plus a sitelink floor: the number of Wikipedia
language editions with an article is a cheap, language-neutral notability signal. The
P31/P106 values of a group give the taxonomy categories, so no NLP is needed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx
import structlog

from tokensage.engine import wikiclass

log = structlog.get_logger("wikidata")

SPARQL = "https://query.wikidata.org/sparql"
SEP = "\u001f"  # alias separator: never inside a label


@dataclass(frozen=True)
class Group:
    name: str
    where: str  # SPARQL triple patterns binding ?item
    min_links: int
    kind: str  # Entity.kind: person | famous_animal | meme | concept | other
    categories: tuple[str, ...]


# (name, occupation Q-ids, sitelink floor, categories, born after). One query per
# occupation: a VALUES list over all humans times out on the public endpoint.
_PEOPLE: tuple[tuple[str, str, int, tuple[str, ...], int], ...] = (
    ("politicians", "Q82955 Q372436", 45, ("political",), 1930),
    (
        "musicians",
        "Q177220 Q639669 Q2252262 Q488205 Q183945 Q130857 Q753110",
        40,
        ("celebrity/musician",),
        1930,
    ),
    (
        "actors",
        "Q33999 Q10800557 Q10798782 Q4610556 Q245068 Q947873",
        45,
        ("celebrity/other",),
        1930,
    ),
    # football, basketball, American football, tennis, boxing, MMA, motor sport, baseball,
    # cricket, wrestling, chess, athletics, golf, ice hockey
    (
        "athletes",
        "Q937857 Q3665646 Q19204627 Q10833314 Q11338576 Q11607585 Q378622 Q10871364 "
        "Q12299841 Q13474373 Q10873124 Q11513337 Q11303721 Q11774891",
        45,
        ("celebrity/athlete",),
        1930,
    ),
    # founders, businesspeople, investors, CEOs, tech figures
    (
        "business",
        "Q131524 Q43845 Q557880 Q484876 Q82594 Q5482740",
        35,
        ("celebrity/other",),
        1930,
    ),
    # YouTubers, streamers, Internet celebrities, podcasters, TikTokers, influencers
    (
        "online",
        "Q17125263 Q57414145 Q2045208 Q15077007 Q94791573 Q2906862",
        8,
        ("celebrity/streamer_kol",),
        1960,
    ),
)


def _people_groups() -> tuple[Group, ...]:
    out: list[Group] = []
    for name, occs, links, cats, born in _PEOPLE:
        for occ in occs.split():
            where = (
                f"?item wdt:P106 wd:{occ} ; wdt:P31 wd:Q5 ; wdt:P569 ?born . "
                f"FILTER(YEAR(?born) >= {born})"
            )
            out.append(Group(f"{name}:{occ}", where, links, "person", cats))
    return tuple(out)


def _instances(classes: str) -> str:
    values = " ".join("wd:" + q for q in classes.split())
    return f"VALUES ?cls {{ {values} }} ?item wdt:P31 ?cls ."


GROUPS: tuple[Group, ...] = (
    *_people_groups(),
    # Internet memes, including their subclasses (viral videos, image macros, ...)
    Group("memes", "?item wdt:P31/wdt:P279* wd:Q2927074 .", 2, "meme", ("meme_template/other",)),
    # individual animals (Kabosu, Moo Deng, Peanut, Pesto)
    Group("animals", "?item wdt:P31/wdt:P279* wd:Q26401003 .", 3, "famous_animal", ("animal",)),
    # chatbots, large language models, generative AI chatbots, virtual assistants
    Group(
        "ai",
        _instances("Q870780 Q115305900 Q133284163 Q3467906"),
        5,
        "concept",
        ("ai_agent",),
    ),
    # fictional characters people launch coins about (Pikachu, SpongeBob, Shrek)
    Group(
        "characters",
        _instances("Q15711870 Q95074 Q1569167 Q15632617 Q1114461 Q80447738 Q15773347"),
        25,
        "other",
        ("pop_culture",),
    ),
    # TV series, animated series, video games, films, online services
    Group("tv", _instances("Q5398426 Q581714 Q117467246"), 40, "other", ("pop_culture",)),
    Group("games", _instances("Q7889"), 50, "other", ("pop_culture",)),
    Group("films", _instances("Q11424"), 80, "other", ("pop_culture",)),
    Group(
        "online_services",
        _instances("Q35127 Q1668024 Q3220391 Q166142"),
        60,
        "other",
        ("pop_culture",),
    ),
)

# P31 / P10241 (individual of taxon) values of an individual animal -> its species category.
# Descriptions fill the gaps ("Japanese dog and Internet meme celebrity"); see wikiclass.
SPECIES = {
    "Q144": "animal/dog",
    "Q146": "animal/cat",
    "Q34505": "animal/hippo",
    "Q629680": "animal/hippo",
    "Q22110899": "animal/hippo",  # pygmy hippopotamus
    "Q9482": "animal/squirrel",
    "Q5113": "animal/bird",
    "Q9147": "animal/bird",  # penguin
    "Q3736439": "animal/bird",  # duck
    "Q1367": "animal/monkey",
    "Q4126704": "animal/monkey",  # chimpanzee
    "Q41050": "animal/monkey",  # orangutan
    "Q36611": "animal/monkey",  # gorilla
    "Q830": "animal/bear_bull",  # cattle
    "Q11788": "animal/bear_bull",  # bears
    "Q152": "animal/fish",
    "Q7372": "animal/fish",  # shark
    "Q1865281": "animal/fish",  # whale
}


@dataclass
class WikiEntity:
    qid: str
    label: str
    aliases: list[str]
    desc: str
    kind: str
    categories: list[str]
    sitelinks: int
    groups: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "id": self.qid,
            "label": self.label,
            "aliases": self.aliases,
            "desc": self.desc,
            "kind": self.kind,
            "categories": self.categories,
            "sitelinks": self.sitelinks,
        }


def query_for(g: Group) -> str:
    types = "OPTIONAL { ?item wdt:P31|wdt:P10241 ?type . }" if g.kind == "famous_animal" else ""
    return f"""SELECT ?item ?label ?desc ?links
  (GROUP_CONCAT(DISTINCT ?alias; separator="{SEP}") AS ?aliases)
  (GROUP_CONCAT(DISTINCT ?type; separator=" ") AS ?types) WHERE {{
  {g.where}
  ?item wikibase:sitelinks ?links . FILTER(?links >= {g.min_links})
  ?item rdfs:label ?label . FILTER(LANG(?label) = "en")
  OPTIONAL {{ ?item schema:description ?desc . FILTER(LANG(?desc) = "en") }}
  OPTIONAL {{ ?item skos:altLabel ?alias . FILTER(LANG(?alias) = "en") }}
  {types}
}} GROUP BY ?item ?label ?desc ?links"""


def parse(g: Group, bindings: list[dict]) -> list[WikiEntity]:
    out: list[WikiEntity] = []
    for b in bindings:

        def v(key: str, b: dict = b) -> str:
            return str((b.get(key) or {}).get("value") or "")

        qid = v("item").rsplit("/", 1)[-1]
        label = v("label").strip()
        if not qid.startswith("Q") or not label or label == qid:
            continue
        cats = list(g.categories)
        if g.kind == "famous_animal":
            species = [SPECIES[t.rsplit("/", 1)[-1]] for t in v("types").split()
                       if t.rsplit("/", 1)[-1] in SPECIES]  # fmt: skip
            cats = [species[0]] if species else [wikiclass.animal_category(v("desc"))]
        try:
            links = int(v("links") or 0)
        except ValueError:
            links = 0
        aliases = [a.strip() for a in v("aliases").split(SEP) if a.strip() and a.strip() != label]
        out.append(
            WikiEntity(qid, label, aliases[:30], v("desc")[:200], g.kind, cats, links, [g.name])
        )
    return out


async def fetch_group(
    http: httpx.AsyncClient, g: Group, timeout: float = 70.0
) -> list[WikiEntity] | None:
    """One group's entities, or None when the endpoint failed (timeouts are common for the
    big classes; the caller keeps the previous rows for that group)."""
    for attempt in range(3):
        try:
            r = await http.post(
                SPARQL,
                data={"query": query_for(g)},
                headers={"Accept": "application/sparql-results+json"},
                timeout=timeout,
            )
        except httpx.HTTPError as e:
            log.info("wikidata.error", group=g.name, error=str(e)[:120])
            r = None
        if r is not None and r.status_code == 200:
            try:
                return parse(g, r.json()["results"]["bindings"])
            except (ValueError, KeyError, TypeError) as e:
                log.info("wikidata.bad_json", group=g.name, error=str(e)[:120])
                return None
        if r is not None:
            log.info("wikidata.status", group=g.name, status=r.status_code)
            if r.status_code not in (429, 500, 502, 503, 504):
                return None
        await asyncio.sleep(5 * (attempt + 1))
    return None


_KIND_RANK = {"famous_animal": 0, "meme": 1, "concept": 2, "person": 3, "other": 4}


def merge(entities: list[WikiEntity]) -> list[WikiEntity]:
    """One row per item: an actor who is also a politician gets both categories."""
    by_id: dict[str, WikiEntity] = {}
    for e in entities:
        have = by_id.get(e.qid)
        if have is None:
            by_id[e.qid] = WikiEntity(
                e.qid, e.label, list(e.aliases), e.desc, e.kind, list(e.categories),
                e.sitelinks, list(e.groups),
            )  # fmt: skip
            continue
        have.categories = list(dict.fromkeys([*have.categories, *e.categories]))
        have.aliases = list(dict.fromkeys([*have.aliases, *e.aliases]))
        have.groups = list(dict.fromkeys([*have.groups, *e.groups]))
        if _KIND_RANK.get(e.kind, 9) < _KIND_RANK.get(have.kind, 9):
            have.kind = e.kind  # a meme that is also a TV series is a meme
    return sorted(by_id.values(), key=lambda e: (-e.sitelinks, e.qid))


async def fetch_all(
    http: httpx.AsyncClient, pause_s: float = 2.0
) -> tuple[list[WikiEntity], list[str]]:
    """All groups, merged; and the names of the groups that failed."""
    got: list[WikiEntity] = []
    failed: list[str] = []
    for g in GROUPS:
        rows = await fetch_group(http, g)
        if rows is None:
            failed.append(g.name)
        else:
            log.info("wikidata.group", group=g.name, rows=len(rows))
            got += rows
        await asyncio.sleep(pause_s)  # the query service asks for one query at a time
    return merge(got), failed
