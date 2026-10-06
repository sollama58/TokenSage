from tokensage.engine.xref import parse_x_ref, snowflake_time, syndication_token

CASES = {
    "https://x.com/elonmusk/status/1791351500217754008?s=20&t=abc": (
        "tweet",
        "1791351500217754008",
    ),
    "x.com/i/web/status/1234567890123456789": ("tweet", "1234567890123456789"),
    "https://mobile.twitter.com/jack/status/20/photo/1": ("tweet", "20"),
    "https://fixupx.com/jack/status/20/en": ("tweet", "20"),
    "https://vxtwitter.com/Twitter/statuses/1577730467436138524": ("tweet", "1577730467436138524"),
    "https://x.com/i/communities/1804846498066116981/about": ("community", "1804846498066116981"),
    "https://x.com/SomeCoin_?s=21": ("profile", "SomeCoin_"),
    "@pumpdotfun": ("profile", "pumpdotfun"),
    "https://x.com/search?q=%24WIF&src=typed_query": ("search", "$WIF"),
    "https://t.co/AbCdEf123": ("shortlink", None),
    "https://twitter.com/intent/user?screen_name=jack": ("profile", "jack"),
    "https://x.com/i/user/12": ("profile", "12"),
    "https://pump.fun/coin/abc": ("foreign", None),
    "https://x.com/home": ("unknown", None),
    "https://x.com": ("homepage", None),
    "not a url at all": ("invalid", None),
    "": ("empty", None),
    None: ("empty", None),
}


def test_parse():
    for raw, (kind, val) in CASES.items():
        r = parse_x_ref(raw)
        assert r["kind"] == kind, (raw, r)
        if val is not None:
            assert val in r.values(), (raw, r)


def test_snowflake():
    assert snowflake_time("1577730467436138524").isoformat().startswith("2022-10-05T18:40:30")
    assert snowflake_time("1804846498066116981").isoformat().startswith("2024-06-23T11:58:32")
    assert snowflake_time("20") is None


def test_syndication_token():
    assert syndication_token("20") == "6dq1a2xwd93"
    assert syndication_token("1577730467436138524") == "3tol417ti8o"
    assert syndication_token("1791351500217754008") == "4cbp2xufsb5"
