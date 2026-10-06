"""X/Twitter link parsing, snowflake timing and the syndication token."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, unquote, urlsplit

X_HOSTS = {
    "twitter.com",
    "x.com",
    "mobile.twitter.com",
    "mobile.x.com",
    "m.twitter.com",
    "fxtwitter.com",
    "fixupx.com",
    "vxtwitter.com",
    "fixvx.com",
    "twittpr.com",
    "nitter.net",
    "xcancel.com",
    "api.fxtwitter.com",
    "api.vxtwitter.com",
    "d.fxtwitter.com",
    "d.fixupx.com",
}
RESERVED = {
    "i",
    "home",
    "explore",
    "search",
    "hashtag",
    "settings",
    "notifications",
    "messages",
    "intent",
    "share",
    "login",
    "signup",
    "tos",
    "privacy",
    "compose",
    "communities",
    "lists",
    "status",
    "web",
}
# \Z, not $: "$" also matches before a trailing newline; [0-9], not \d: \d is any Unicode digit
HANDLE = re.compile(r"\A[A-Za-z0-9_]{1,15}\Z")
ID = re.compile(r"\A[0-9]{1,20}\Z")
TWITTER_EPOCH_MS = 1288834974657


def parse_x_ref(raw: str | None) -> dict:
    """Classify a pump.fun `twitter` metadata value into a typed X reference.

    NOTE: the handle in a status URL is NOT trustworthy (X ignores it). Always use
    the author returned by the fetch.
    """
    if raw is None:
        return {"kind": "empty"}
    s = re.sub(r"""^[<"'(\[]+|[>"')\],.]+$""", "", str(raw).strip())
    if not s:
        return {"kind": "empty"}
    if re.fullmatch(r"@?[A-Za-z0-9_]{1,15}", s) and not s.lstrip("@").isdigit():
        return {"kind": "profile", "handle": s.lstrip("@")}
    if not re.match(r"^[a-z]+://", s, re.I):
        s = "https://" + s
    try:
        u = urlsplit(s)
        host = (u.hostname or "").lower()
    except ValueError:
        return {"kind": "invalid", "raw": str(raw)}
    if not host or " " in s:
        return {"kind": "invalid", "raw": str(raw)}
    host = host.removeprefix("www.")
    if host == "t.co":
        return {"kind": "shortlink", "url": s, "needs_resolve": True}
    if host not in X_HOSTS:
        return {"kind": "foreign", "host": host, "url": s}
    seg = [unquote(p) for p in u.path.split("/") if p]
    low = [p.lower() for p in seg]
    q = parse_qs(u.query)
    get = lambda k: (q.get(k) or [""])[0] or None  # noqa: E731
    at = lambda i: seg[i] if len(seg) > i else ""  # noqa: E731

    if low[:2] == ["i", "communities"] and ID.match(at(2)):
        return {"kind": "community", "community_id": seg[2]}
    if low[:1] == ["communities"] and ID.match(at(1)):
        return {"kind": "community", "community_id": seg[1]}
    for si, p in enumerate(low):
        if p in ("status", "statuses") and ID.match(at(si + 1)):
            h = (
                seg[si - 1]
                if si > 0 and HANDLE.match(seg[si - 1]) and low[si - 1] not in RESERVED
                else None
            )
            return {"kind": "tweet", "tweet_id": seg[si + 1], "url_handle": h}
    if low[:2] == ["i", "lists"] and ID.match(at(2)):
        return {"kind": "list", "list_id": seg[2]}
    if low[:2] == ["i", "user"] and ID.match(at(2)):
        return {"kind": "profile", "user_id": seg[2]}
    if low[:1] == ["intent"] and len(low) > 1 and low[1] in ("user", "follow"):
        h, uid = get("screen_name"), get("user_id")
        if h and HANDLE.match(h):
            return {"kind": "profile", "handle": h}
        if uid and ID.match(uid):
            return {"kind": "profile", "user_id": uid}
    if low[:1] == ["search"]:
        return {"kind": "search", "query": get("q") or ""}
    if low[:1] == ["hashtag"] and len(seg) > 1:
        return {"kind": "search", "query": "#" + seg[1]}
    if seg and HANDLE.match(seg[0]) and low[0] not in RESERVED:
        return {"kind": "profile", "handle": seg[0]}
    if not seg:
        return {"kind": "homepage"}
    return {"kind": "unknown", "url": s}


def snowflake_time(id_: str | int) -> datetime | None:
    """Creation time of a tweet / community / user id (snowflakes, Nov 2010+)."""
    n = int(id_)
    if n < 2**22 * 1000:  # pre-snowflake ids (e.g. tweet 20)
        return None
    return datetime.fromtimestamp(((n >> 22) + TWITTER_EPOCH_MS) / 1000, tz=UTC)


def _js_float_to_base36(x: float) -> str:
    """Port of V8's Number.prototype.toString(36) (DoubleToRadixCString)."""
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    integer = math.floor(x)
    fraction = x - integer
    delta = max(0.5 * (math.nextafter(x, math.inf) - x), math.nextafter(0.0, 1.0))
    frac_digits = []
    if fraction >= delta:
        while True:
            fraction *= 36
            delta *= 36
            digit = int(fraction)
            frac_digits.append(digit)
            fraction -= digit
            if fraction > 0.5 or (fraction == 0.5 and (digit & 1)):
                if fraction + delta > 1:
                    # round up and propagate carry
                    i = len(frac_digits) - 1
                    while True:
                        if i < 0:
                            integer += 1
                            break
                        frac_digits[i] += 1
                        if frac_digits[i] < 36:
                            break
                        frac_digits[i] = 0
                        i -= 1
                    break
            if fraction < delta:
                break
    int_digits = ""
    n = int(integer)
    while True:
        n, r = divmod(n, 36)
        int_digits = chars[r] + int_digits
        if n == 0:
            break
    out = int_digits
    if frac_digits:
        # strip trailing zeros produced by carry
        while frac_digits and frac_digits[-1] == 0:
            frac_digits.pop()
        if frac_digits:
            out += "." + "".join(chars[d] for d in frac_digits)
    return out


def syndication_token(tweet_id: str | int) -> str:
    """Token for cdn.syndication.twimg.com/tweet-result (same as vercel/react-tweet)."""
    s = _js_float_to_base36((float(int(tweet_id)) / 1e15) * math.pi)
    return re.sub(r"(0+|\.)", "", s) or "0"
