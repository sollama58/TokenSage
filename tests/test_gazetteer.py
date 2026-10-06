"""The open-world entity source: the Wikidata gazetteer (surface filters, matching, the
engine pass), the Wikipedia lookup fallback, and their database/cron plumbing."""

from __future__ import annotations

from datetime import UTC, datetime

import asyncpg
import httpx
import pytest
import respx

from tokensage import fulldepth, gazetteer_db
from tokensage.engine import gazetteer, lexicon, wikiclass, wikilookup
from tokensage.engine.gazetteer import GazEntry, Gazetteer, surface_form, surfaces_for
from tokensage.engine.knowledge import load_knowledge
from tokensage.engine.normalize import normalize
from tokensage.engine.pipeline import DbContext, EngineInput, run_basic, run_full
from tokensage.sources import wikidata, wikipedia


def _e(
    qid: str,
    label: str,
    aliases: tuple[str, ...] = (),
    desc: str = "",
    kind: str = "person",
    cats: tuple[str, ...] = ("celebrity/other",),
    links: int = 80,
) -> GazEntry:
    return GazEntry(qid, label, aliases, desc, kind, cats, links)


ENTRIES = [
    _e("Q49561909", "Sydney Sweeney", ("Sydney Bernice Sweeney",), "American actress"),
    _e(
        "Q30121972",
        "Charlie Kirk",
        ("Charles James Kirk", "Kirk"),
        "American activist",
        cats=("political",),
    ),  # fmt: skip
    _e("Q114114977", "Ice Spice", (), "American rapper", cats=("celebrity/musician",)),
    _e(
        "Q36844",
        "Rihanna",
        ("Robyn Rihanna Fenty",),
        "Barbadian singer",
        cats=("celebrity/musician",),
    ),  # fmt: skip
    _e("Q23831", "The Office", (), "American sitcom", kind="other", cats=("pop_culture",)),
    _e("Q3700238", "Vine", (), "video-hosting service", kind="other", cats=("pop_culture",)),
    _e(
        "Q134227481",
        "Tung Tung Tung Sahur",
        ("Tung Tung Sahur",),
        "AI-generated internet meme",
        kind="meme",
        cats=("meme_template/other",),
        links=15,
    ),  # fmt: skip
    # a Wikidata copy of a seed entity: the seed keeps its surfaces
    _e(
        "Q22686",
        "Donald Trump",
        ("Trump", "Donald J. Trump"),
        "45th US president",
        cats=("political",),
        links=250,
    ),  # fmt: skip
    # two items share a surface: the better-known one keeps it
    _e(
        "Q23829",
        "The Office (UK)",
        ("The Office",),
        "British sitcom",
        kind="other",
        cats=("pop_culture",),
        links=42,
    ),  # fmt: skip
]


@pytest.fixture(scope="module")
def gaz() -> Gazetteer:
    return Gazetteer(ENTRIES, load_knowledge(), "test")


# ----------------------------------------------------------------- surfaces


def test_surface_form_folds_like_the_lexicon() -> None:
    assert surface_form("Beyoncé") == "beyonce"
    assert surface_form("The Office (American TV series)") == "the office"
    assert surface_form("Donald J. Trump") == "donald j trump"
    assert surface_form("Rock 'n' Roll") == "rock n roll"


def test_surface_filters() -> None:
    common = frozenset({"vine", "office", "kirk", "great", "baby", "shark"})
    s = dict(surfaces_for(ENTRIES[1], common, set()))
    assert "charlie kirk" in s and s["charlie kirk"] is False
    assert "kirk" not in s  # a bare surname alias of a person never matches
    rihanna = dict(surfaces_for(ENTRIES[3], common, set()))
    assert rihanna == {"rihanna": True, "robyn rihanna fenty": False}  # a one-word name
    assert surfaces_for(ENTRIES[5], common, set()) == []  # "vine" is a dictionary word
    office = dict(surfaces_for(ENTRIES[4], common, set()))
    assert office == {"the office": True}  # only dictionary words: name-only
    trump = dict(surfaces_for(ENTRIES[7], common, {"trump", "donald trump"}))
    assert trump == {"donald j trump": False}  # the seed's surfaces are taken
    dog = _e("Q2", "Charlie", (), "dog", kind="famous_animal", links=5)
    assert surfaces_for(dog, common, set(), {"charlie"}) == []  # a personal name
    claude = _e("Q3", "Claude", (), "language model", kind="concept", links=59)
    assert surfaces_for(claude, common, set(), {"claude"}) == [("claude", True)]
    assert gazetteer.person_name_words(ENTRIES[:2]) == {"sydney", "sweeney", "charlie", "kirk"}
    short = _e("Q1", "Xi", ("X Æ A-12",))
    assert surfaces_for(short, common, set()) == []


def test_popularity_scale() -> None:
    assert gazetteer.popularity(10) == 0.4
    assert gazetteer.popularity(1000) == gazetteer.POPULARITY_CAP
    assert gazetteer.popularity(0) == 0.2


def test_shared_surface_goes_to_the_better_known_item(gaz: Gazetteer) -> None:
    hits = gaz.find(" the office ")
    assert [h.entity.label for h in hits] == ["The Office"]


# ----------------------------------------------------------------- lexicon + engine


def test_lexicon_merges_gazetteer_hits(gaz: Gazetteer) -> None:
    k = load_knowledge()
    hits = lexicon.find("ice spice coin", k, gaz)
    assert [(h.surface, h.kind) for h in hits if h.kind != "slang"] == [("ice spice", "entity")]
    # without the gazetteer WordNet reads "spice" as food
    assert any(h.kind == "wordnet" for h in lexicon.find("ice spice coin", k))
    # name-only surfaces are skipped outside the name
    found = lexicon.find("back at the office", k, gaz, name_pass=False)
    assert not [h for h in found if h.kind == "entity"]
    assert lexicon.find("the office", k, gaz)[0].name_only is True


def _basic(name: str, symbol: str, gaz: Gazetteer, desc: str | None = None) -> object:
    return run_basic(EngineInput("m", name, symbol, desc, None, None, ctx=DbContext(gazetteer=gaz)))


def test_engine_resolves_a_wikidata_person(gaz: Gazetteer) -> None:
    out = _basic("Sydney Sweeney Jeans", "JEANS", gaz)
    r = out.agg.referent  # type: ignore[attr-defined]
    assert r is not None and r.label == "Sydney Sweeney"
    assert r.source == "wikidata:Q49561909"
    assert "American actress" in (r.desc or "")
    assert dict(out.agg.categories).get("celebrity/other", 0) >= 0.4  # type: ignore[attr-defined]
    assert "Sydney Sweeney" in out.summary  # type: ignore[attr-defined]


def test_dictionary_sense_loses_to_a_named_entity(gaz: Gazetteer) -> None:
    cats = dict(_basic("ice spice", "SPICE", gaz).agg.categories)  # type: ignore[attr-defined]
    assert "food_object_abstract" not in cats
    assert cats.get("celebrity/musician", 0) >= 0.4


def test_seed_entity_beats_its_wikidata_copy(gaz: Gazetteer) -> None:
    r = _basic("Trump Coin", "TRUMP", gaz).agg.referent  # type: ignore[attr-defined]
    assert r is not None and r.source == "entities:Donald Trump"


def test_name_only_and_ticker_rules(gaz: Gazetteer) -> None:
    assert _basic("The Office", "OFFICE", gaz).agg.referent.label == "The Office"  # type: ignore[attr-defined]
    # an ordinary phrase in a description is not the sitcom
    out = _basic("Dwight", "DWT", gaz, desc="coin made at the office on a friday")
    assert out.agg.referent is None  # type: ignore[attr-defined]
    # tickers never read the gazetteer
    assert _basic("Moon Dog", "RIHANNA", gaz).agg.referent is None  # type: ignore[attr-defined]


def test_multiword_names_count_in_descriptions(gaz: Gazetteer) -> None:
    out = _basic("Turning Point", "TPUSA", gaz, desc="for charlie kirk fans")
    r = out.agg.referent  # type: ignore[attr-defined]
    assert r is not None and r.label == "Charlie Kirk"


def test_packaged_gazetteer_loads() -> None:
    g = gazetteer.packaged()
    assert g.version
    if g.size:  # the snapshot is committed; every surface obeys the filters
        assert g.size > 1000


# ----------------------------------------------------------------- short descriptions


@pytest.mark.parametrize(
    ("desc", "kind", "cat"),
    [
        ("American actress (born 1997)", "person", "celebrity/other"),
        ("American rapper (born 2000)", "person", "celebrity/musician"),
        ("American political activist (1993–2025)", "person", "political"),
        ("Japanese dog and Internet meme celebrity", "famous_animal", "animal/dog"),
        ("Thai pygmy hippopotamus (born 2024)", "famous_animal", "animal/hippo"),
        ("king penguin from Australia", "famous_animal", "animal/bird"),
        ("AI-generated internet meme", "meme", "meme_template/other"),
        ("chatbot developed by xAI", "concept", "ai_agent"),
        ("American sitcom broadcast on NBC", "other", "pop_culture"),
        ("American influencer, musician and online streamer", "person", "celebrity/musician"),
        ("Brazilian footballer (born 1992)", "person", "celebrity/athlete"),
    ],
)
def test_classify_short_descriptions(desc: str, kind: str, cat: str) -> None:
    assert wikiclass.classify(desc) == (kind, [cat])


def test_classify_unknown() -> None:
    assert wikiclass.classify("") == ("other", [])
    assert wikiclass.classify("chemical compound") == ("other", [])
    assert wikiclass.animal_category("American pet (c. 2017 – 2024)") == "animal/other"


# ----------------------------------------------------------------- Wikipedia fallback


def test_spans_cover_unknown_name_words() -> None:
    k = load_knowledge()
    empty = Gazetteer([], k, "empty")
    spans = wikilookup.spans(normalize("Sydney Sweeney Jeans", "JEANS", None), [], k, empty)
    assert [s.text for s in spans] == [
        "sydney sweeney jeans",
        "sydney sweeney",
        "sweeney jeans",
    ]
    assert all(s.where == "name" for s in spans)
    # a "baby" marker belongs to the name it starts; alone it is never looked up
    baby = wikilookup.spans(normalize("Baby Shark", "BS", None), [], k, empty)
    assert [(s.text, s.common) for s in baby] == [("baby shark", True)]
    assert wikilookup.spans(normalize("Baby Pepe Coin", "BP", None), [], k, empty) == []
    # a known entity is not looked up; a lone dictionary word is not a name
    assert wikilookup.spans(normalize("Trump Jeans", "TJ", None), [], k, empty) == []


def test_spans_skip_what_the_gazetteer_knows(gaz: Gazetteer) -> None:
    k = load_knowledge()
    assert wikilookup.spans(normalize("Sydney Sweeney Jeans", "J", None), [], k, gaz) == []


def test_spans_from_capitalised_post_names() -> None:
    k = load_knowledge()
    empty = Gazetteer([], k, "empty")
    spans = wikilookup.spans(
        normalize("Great Jeans", "JEANS", None),
        ["Sydney Sweeney has great jeans. Elon Musk agrees, says The New York Times"],
        k,
        empty,
    )
    texts = [(s.text, s.where) for s in spans]
    assert ("sydney sweeney", "x") in texts
    assert not any("elon" in t for t, _ in texts)  # the seed already knows Elon Musk


def _pages() -> list[wikipedia.WikiPage]:
    return [
        wikipedia.WikiPage("American Eagle Outfitters", "American clothing retailer", "Q1", 1),
        wikipedia.WikiPage("Sydney Sweeney", "American actress (born 1997)", "Q49561909", 2),
        wikipedia.WikiPage(
            "Sweeney (disambiguation)", "Topics referred to by the same term", None, 3, True
        ),  # fmt: skip
    ]


def test_pick_requires_the_title_to_be_the_name() -> None:
    span = wikilookup.Span("sydney sweeney", "name", False)
    ref = wikilookup.pick(span, _pages())
    assert ref is not None and ref.title == "Sydney Sweeney" and ref.qid == "Q49561909"
    assert wikilookup.pick(wikilookup.Span("sydney sweeney jeans", "name", False), _pages()) is None
    tv = [wikipedia.WikiPage("The Office (American TV series)", "American sitcom", "Q23831", 1)]
    assert wikilookup.pick(wikilookup.Span("office", "name", True), tv) is None
    assert gazetteer.is_common("jeans") and not gazetteer.is_common("sweeney")
    assert wikilookup.pick(wikilookup.Span("the office", "name", True), tv) is not None


def test_full_run_uses_wiki_refs() -> None:
    k = load_knowledge()
    ref = wikilookup.WikiRef(
        wikilookup.Span("sydney sweeney", "name", False),
        "Sydney Sweeney",
        "American actress (born 1997)",
        "Q49561909",
    )
    inp = EngineInput(
        "m", "Sydney Sweeney Jeans", "JEANS", None, None, None,
        ctx=DbContext(gazetteer=Gazetteer([], k, "empty")), wiki_refs=[ref],
    )  # fmt: skip
    out = run_full(inp)
    assert out.agg.referent is not None and out.agg.referent.label == "Sydney Sweeney"
    assert dict(out.agg.categories).get("celebrity/other", 0) >= 0.4
    # basic depth never uses them (they are a full-depth lookup)
    assert run_basic(inp).agg.referent is None


def test_wikipedia_parse() -> None:
    data = {
        "query": {
            "pages": [
                {"title": "B", "index": 2, "description": "second"},
                {
                    "title": "A",
                    "index": 1,
                    "description": "first",
                    "pageprops": {"wikibase_item": "Q1"},
                },  # fmt: skip
                {"title": "D", "index": 3, "pageprops": {"disambiguation": ""}},
                {"title": "M", "missing": True},
            ]
        }
    }
    pages = wikipedia.parse(data)
    assert [p.title for p in pages] == ["A", "B", "D"]
    assert pages[0].qid == "Q1" and pages[2].disambiguation
    assert wikipedia.parse({}) == [] and wikipedia.parse(None) == []
    assert wikipedia.WikiPage.from_json(pages[0].to_json()) == pages[0]


@respx.mock
async def test_wikipedia_search_and_failure() -> None:
    route = respx.get(wikipedia.API).mock(
        return_value=httpx.Response(
            200, json={"query": {"pages": [{"title": "Sydney Sweeney", "index": 1}]}}
        )
    )
    async with httpx.AsyncClient() as http:
        pages = await wikipedia.search(http, "sydney sweeney")
        assert pages and pages[0].title == "Sydney Sweeney"
        assert route.calls.last.request.url.params["gsrsearch"] == "sydney sweeney"
        route.mock(return_value=httpx.Response(503))
        assert await wikipedia.search(http, "sydney sweeney") is None


# ----------------------------------------------------------------- Wikidata parsing


def test_wikidata_query_and_parse() -> None:
    animals = next(g for g in wikidata.GROUPS if g.name == "animals")
    q = wikidata.query_for(animals)
    assert "wikibase:sitelinks" in q and "P10241" in q and "FILTER(?links >= 3)" in q
    rows = wikidata.parse(
        animals,
        [
            {
                "item": {"value": "http://www.wikidata.org/entity/Q23486479"},
                "label": {"value": "Kabosu"},
                "desc": {"value": "Japanese dog and Internet meme celebrity"},
                "links": {"value": "13"},
                "aliases": {"value": f"Kabosu-chan{wikidata.SEP}Kabosu"},
                "types": {"value": "http://www.wikidata.org/entity/Q26401003"},
            },
            {
                "item": {"value": "http://www.wikidata.org/entity/Q130288176"},
                "label": {"value": "Moo Deng"},
                "links": {"value": "24"},
                "types": {"value": "http://www.wikidata.org/entity/Q22110899"},
            },
            {"item": {"value": "http://www.wikidata.org/entity/Q9"}, "label": {"value": "Q9"}},
        ],
    )
    assert [(r.label, r.categories) for r in rows] == [
        ("Kabosu", ["animal/dog"]),  # from the description
        ("Moo Deng", ["animal/hippo"]),  # from the taxon
    ]
    assert rows[0].aliases == ["Kabosu-chan"] and rows[0].sitelinks == 13


def test_wikidata_merge() -> None:
    a = wikidata.WikiEntity("Q1", "Ann", ["A"], "", "person", ["political"], 50, ["p"])
    b = wikidata.WikiEntity("Q1", "Ann", ["B"], "", "person", ["celebrity/other"], 50, ["c"])
    m = wikidata.WikiEntity("Q2", "Meme", [], "", "other", ["pop_culture"], 9, ["tv"])
    m2 = wikidata.WikiEntity("Q2", "Meme", [], "", "meme", ["meme_template/other"], 9, ["m"])
    out = wikidata.merge([a, m, b, m2])
    assert [e.qid for e in out] == ["Q1", "Q2"]
    assert out[0].categories == ["political", "celebrity/other"] and out[0].aliases == ["A", "B"]
    assert out[1].kind == "meme"


# ----------------------------------------------------------------- database


@pytest.fixture
async def db(migrated_db: str) -> asyncpg.Connection:
    from tokensage.db import _init_connection

    conn = await asyncpg.connect(migrated_db)
    await _init_connection(conn)
    await conn.execute("delete from entity; delete from lookup_cache")
    gazetteer_db.reset_cache()
    yield conn
    await conn.execute("delete from entity; delete from lookup_cache")
    gazetteer_db.reset_cache()
    await conn.close()


def _wiki(n: int) -> list[wikidata.WikiEntity]:
    out = [
        wikidata.WikiEntity(
            f"Q{i}", f"Person Number{i}x", [], "", "person", ["celebrity/other"], 20, ["g"]
        )  # fmt: skip
        for i in range(n)
    ]
    out.append(
        wikidata.WikiEntity(
            "Q49561909",
            "Sydney Sweeney",
            [],
            "American actress",
            "person",
            ["celebrity/other"],
            83,
            ["actors"],
        )  # fmt: skip
    )
    return out


async def test_store_and_load_from_the_table(db: asyncpg.Connection) -> None:
    # an empty table: the packaged snapshot
    assert await gazetteer_db.current(db) is gazetteer.packaged()
    out = await gazetteer_db.store(db, _wiki(gazetteer_db.MIN_ROWS), complete=True)
    assert out["upserted"] == gazetteer_db.MIN_ROWS + 1
    gazetteer_db.reset_cache()
    g = await gazetteer_db.current(db)
    assert g is not gazetteer.packaged() and g.version.startswith("wikidata-")
    assert [h.entity.label for h in g.find(" sydney sweeney ")] == ["Sydney Sweeney"]
    row = await db.fetchrow("select * from entity where id='wikidata:Q49561909'")
    assert row["categories"] == ["celebrity/other"] and row["sitelinks"] == 83

    # a much smaller refresh (a partial outage) keeps the stored rows
    small = await gazetteer_db.store(db, _wiki(10), complete=True)
    assert small.get("skipped") == 1
    # an incomplete refresh adds and updates but never deletes
    part = await gazetteer_db.store(db, _wiki(gazetteer_db.MIN_ROWS - 100), complete=False)
    assert part["removed"] == 0
    assert await db.fetchval("select count(*) from entity") == gazetteer_db.MIN_ROWS + 1
    # a complete one removes what Wikidata no longer returns
    full = await gazetteer_db.store(db, _wiki(gazetteer_db.MIN_ROWS - 100), complete=True)
    assert full["removed"] == 100


async def test_refresh_gazetteer_job(
    db: asyncpg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokensage.jobs import knowledge as job

    calls = 0

    async def fake_fetch_all(http: object) -> tuple[list[wikidata.WikiEntity], list[str]]:
        nonlocal calls
        calls += 1
        return _wiki(5), []

    monkeypatch.setattr(wikidata, "fetch_all", fake_fetch_all)
    async with httpx.AsyncClient() as http:
        out = await job.refresh_gazetteer(db, http)
        assert out["upserted"] == 6 and calls == 1
        # refreshed less than a month ago: skipped
        assert await job.refresh_gazetteer(db, http) == {"skipped": 1}
        assert calls == 1


async def test_wiki_search_is_cached(db: asyncpg.Connection) -> None:
    with respx.mock(assert_all_called=False) as router:
        route = router.get(wikipedia.API).mock(
            return_value=httpx.Response(
                200,
                json={
                    "query": {
                        "pages": [
                            {
                                "title": "Sydney Sweeney",
                                "index": 1,
                                "description": "American actress",
                            }
                        ]
                    }
                },  # fmt: skip
            )
        )
        async with httpx.AsyncClient() as http:
            first = await fulldepth.wiki_search(db, http, "Sydney Sweeney")
            again = await fulldepth.wiki_search(db, http, "sydney sweeney")
            assert first == again and first and first[0].desc == "American actress"
            assert route.call_count == 1
            # an empty answer expires after a day, a stale copy beats an outage
            await db.execute(
                "update lookup_cache set fetched_at=$1",
                datetime(2020, 1, 1, tzinfo=UTC),
            )
            route.mock(return_value=httpx.Response(503))
            stale = await fulldepth.wiki_search(db, http, "sydney sweeney")
            assert stale == first and route.call_count == 2
