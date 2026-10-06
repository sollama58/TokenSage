# TokenSage: reading X/Twitter content cheaply and reliably

Research date: 2026-10-05. Prepared for TokenSage, a service that analyses Solana pump.fun memecoins. Constraints: no external AI APIs; hosted on Render; small budget.

---

## 0. Testing status (read this first)

**Live endpoint testing was not possible from the research sandbox.** Its egress proxy returns `403` on CONNECT for every X-related host, so none of them could be called. Blocked hosts:
`cdn.syndication.twimg.com`, `publish.twitter.com`, `api.fxtwitter.com`, `api.vxtwitter.com`, `syndication.twitter.com`, `nitter.net`, `x.com`, `twitterapi.io`, `docs.twitterapi.io`, `docs.socialdata.tools`, `docs.x.com`, `devcommunity.x.com`, `docs.fxembed.com` and `status.d420.de`. Even `example.com` was blocked. WebFetch hit the same egress block.

Only GitHub (raw files and anonymous git clone) and web search were reachable. The findings below were therefore checked in three other ways:

- **Source code.** I read the current source of the open-source tools that call these endpoints:
  - FxEmbed/FxTwitter, cloned at commit `b5a890e5` dated 2026-10-05.
  - vxTwitter (`dylanpdx/BetterTwitFix`).
  - `vercel/react-tweet`.
  - Nitter's README.
  - A snapshot of the twitterapi.io docs, scraped 2026-03-16 (repo `dorukardahan/twitterapi-io-mcp`).
- **Web search** results dated 2026.
- **Local code tests.** The URL parser, the syndication token function and the snowflake decoder in this report were run with Node in the sandbox. Output is shown in the relevant sections.

The sample JSON below comes from upstream docs and source schemas, not from my own live calls. Each sample is labelled with where it came from. **Before relying on any free endpoint, run the smoke test in §8 from a Render instance.** Render egress IPs are datacenter IPs, and several free endpoints treat datacenter IPs differently from home IPs.

---

## 1. Official X API (v2): tiers and pricing, 2025–2026

| Period | Model |
|---|---|
| 2023 to early 2026 | **Free** tier (write-only: about 500 posts/month, roughly 1 read endpoint call per 15 min); **Basic** $200/mo (about 10–15k post reads/mo); **Pro** $5,000/mo (about 1M reads/mo, full-archive search); **Enterprise** about $42k+/mo. |
| **6 Feb 2026** | X **closed the Free tier to new developers** and made **pay-per-use** the default. Basic and Pro stay only for existing subscribers ("legacy"). New developers can only choose pay-per-use or Enterprise. |
| **20 Apr 2026** pricing update | "Owned reads" (your own app's data) repriced to $0.001 per resource; writes $0.015 per post; a post containing a URL costs $0.20. |

**Pay-per-use rates as of Sept 2026** (prepaid credits bought in the Developer Console, with a spending limit):

| Operation | Price |
|---|---|
| Post read | **$0.005 per post** ($5 per 1k) |
| User read | **$0.010 per user** ($10 per 1k) |
| Owned read | $0.001 per resource |
| Create post | $0.015 ($0.20 if it contains a link) |

- **Deduplication:** the same resource read again within the same UTC day is not charged twice. This is a "soft guarantee".
- **Monthly cap:** about 2–3M post reads per month on pay-per-use, then Enterprise. Sources disagree on the exact figure, so it is flagged.
- **Rate limits still apply on top of price.** They use 15-minute windows. For example, `GET /2/tweets` (lookup by IDs, up to 100 IDs per request) is cited at 3,500 requests per 15 min per app (unverified third-party figure).
- **Readable endpoints:** tweet lookup (`/2/tweets`, `/2/tweets/:id`), user lookup (`/2/users/by/username/:u`), recent search (`/2/tweets/search/recent`), user timelines, quote tweets and so on, all billed per resource returned.
- **Communities:** **flagged, not confirmed.** X v2 has added community lookup (`GET /2/communities/:id`) and community search for some access levels. I could not reach docs.x.com to confirm availability or pricing on pay-per-use. Treat it as "check in the Developer Console".

Sources:
- https://devcommunity.x.com/t/x-api-pricing-update-owned-reads-now-0-001-other-changes-effective-april-20-2026/263025 (title only; page blocked)
- https://docs.x.com/x-api/getting-started/pricing (from search snippets)
- https://www.outstand.so/blog/x-api-pricing
- https://postproxy.dev/blog/x-api-pricing-2026/
- https://bundle.social/blog/x-api-pricing-2026-costs-limits
- https://docs.x.com/x-api/fundamentals/rate-limits
- https://api.sorsa.io/blog/twitter-api-rate-limits-2026

**What this means for TokenSage:** the official API is now affordable for low volume. 1,000 new tokens a day × 1 tweet plus 1 user ≈ $15/day ≈ $450/month, so it is still too expensive as the primary path. It is the only ToS-clean option, so keep it as an optional "compliance" fallback.

---

## 2. Unofficial no-key options

### 2.1 `cdn.syndication.twimg.com/tweet-result` (X's own embed backend)

```
GET https://cdn.syndication.twimg.com/tweet-result?id=<TWEET_ID>&lang=en&token=<TOKEN>
```

- **Token algorithm.** This is identical in `vercel/react-tweet` (`packages/react-tweet/src/api/fetch-tweet.ts`) and vxTwitter (`twExtract/twUtils.py: calcSyndicationToken`):

```js
const token = id => ((Number(id) / 1e15) * Math.PI).toString(36).replace(/(0+|\.)/g, '') || '0';
```

  Tested locally: `20 -> 6dq1a2xwd93`, `1577730467436138524 -> 3tol417ti8o`, `1791351500217754008 -> 4cbp2xufsb5`.

  The result loses float precision. That is fine because the server checks it loosely. react-tweet also sends a long `features=` parameter (tfw_* flags). That parameter is optional in practice. **Flagged: I could not test it.**

- **Responses** (from react-tweet's handling):
  - Normal tweet: `200` with JSON whose `__typename` is `"Tweet"`.
  - Deleted, withheld or age-gated tweet: `__typename: "TweetTombstone"`.
  - Unknown tweet: `{}` (empty object) or `404`.
  - Known reliability problems: random empty bodies and 404s from cloud build IPs (Netlify), reported as possible IP blocking. Sources: https://samwize.com/2025/08/10/the-x-com-embed-disaster-still-broken-but-we-have-a-reverse-engineered-solution/ and https://www.stefanjudis.com/blog/how-to-prerender-tweets-without-using-the-official-twitter-apis/
- **Schema** (from `react-tweet/src/api/types/tweet.ts` and `user.ts`):

```jsonc
{
  "__typename": "Tweet",
  "id_str": "…", "lang": "en", "created_at": "2022-10-05T18:40:30.000Z",
  "text": "…", "display_text_range": [0, 140],
  "entities": { "hashtags": [], "urls": [], "user_mentions": [], "symbols": [] },
  "user": { "id_str": "…", "name": "…", "screen_name": "…",
            "profile_image_url_https": "…", "profile_image_shape": "Circle",
            "verified": false, "is_blue_verified": true,
            "verified_type": "Business|Government (optional)",
            "highlighted_label": { "description": "affiliate org", "badge": {"url": "…"} } },
  "favorite_count": 123, "conversation_count": 4,
  "mediaDetails": [ … ], "photos": [ … ], "video": { … },
  "quoted_tweet": { …TweetBase, "reply_count", "retweet_count", "favorite_count" },
  "in_reply_to_screen_name": "…", "in_reply_to_status_id_str": "…",
  "parent": { … }, "possibly_sensitive": false,
  "edit_control": { … }, "isEdited": false, "note_tweet": { "id": "…" }
}
```

- **Gaps:**
  - **No follower count.** The `tfw_follower_count_sunset` flag removed it.
  - No retweet count on the main tweet.
  - Long "note tweets" (over 280 characters) come back truncated; only `note_tweet.id` is given.
  - No profile endpoint.
  - No communities.
- **Best use:** cheapest first try for a tweet's text, author, verification, media, quote and date. It is X's own CDN, so it is usually fast.

### 2.2 `publish.twitter.com/oembed` (now `publish.x.com/oembed`)

```
GET https://publish.x.com/oembed?url=https://x.com/<h>/status/<id>&omit_script=1&dnt=true
```

- Documented and unauthenticated. `publish.twitter.com` redirects to `publish.x.com`.
- Returns `{url, author_name, author_url, html, width, type:"rich", cache_age, provider_name:"X", provider_url, version:"1.0"}`. The tweet text and date are only inside the `html` blockquote (`<p>…</p>&mdash; Name (@handle) <a href="…">Month D, YYYY</a>`).
- No metrics, no verification, no media URLs.
- Reported as rate-limited for bulk use in 2026.
- Also accepts **profile URLs**, which return timeline embed HTML with no data. Not useful for profiles.
- **Use:** last-resort "does this tweet exist, and what is its text and author" check. It is the most "official" free option.
- Sources: https://docs.x.com/x-for-websites/oembed-api and search snippet (socialrails, smashballoon 2026).

### 2.3 FxTwitter / FixupX API (`api.fxtwitter.com`, also `api.fixupx.com`)

The richest free source. The source code (FxEmbed, commit of 2026-10-05) shows two API versions:

**v1 (legacy, still routed):**
- `GET /status/:id`
- `GET /:handle/status/:id[/:lang]`
- `GET /:handle` (profile)

**v2 (current; OpenAPI spec at `https://api.fxtwitter.com/2/openapi.json`):**

| Route | Purpose |
|---|---|
| `GET /2/status/{id}` (`?about_account=true`, `?lang=`) | single post |
| `GET /2/thread/{id}`, `/2/conversation/{id}` | thread / replies |
| `GET /2/status/{id}/quotes`, `/2/status/{id}/reposts` | who quoted / reposted |
| `GET /2/profile/{handle}` | full profile |
| `GET /2/profile/{handle}/about` | "About this account": country, **username change count** |
| `GET /2/profile/{handle}/statuses`, `/media`, `/articles`, `/followers`, `/following` | timelines / graph |
| `GET /2/search?q=&feed=latest\|top\|media&count=1..100&cursor=` | **post search** |
| `GET /2/search/users`, `/2/typeahead`, `/2/trends` | discovery |

- **Rules:**
  - **A `User-Agent` header is required.** Without one you get `401` with a message asking for a descriptive UA such as `MyAwesomeBot/1.0 (+http://example.com)` (see `src/realms/api/router.ts`).
  - Rate limit cited as about **1000 req/min per IP** for v2 (search snippet; flagged).
  - A GitHub issue opened 2026-10-04 asks about *commercial* fallback use of the hosted v2 API (FxEmbed issue #2550), so commercial use is not clearly permitted. Ask the maintainers or self-host. The project is MIT-licensed and runs as a Cloudflare Worker; Docker is supported.
  - Upstream, FxTwitter uses X's GraphQL with guest tokens plus a pool of encrypted account credentials (`CREDENTIAL_KEY`). That makes search and timelines possible, but it also means the service can be degraded if X bans those accounts.
- **v2 status schema** (`packages/atmosphere/src/types/api-schemas.ts`, abridged):

```jsonc
{ "code": 200, "message": "OK",
  "status": {
    "type": "status", "id": "…", "url": "…", "text": "…",
    "created_at": "…", "created_timestamp": 1664995230,
    "likes": 0, "reposts": 0, "quotes": 0, "replies": 0, "views": 0, "bookmarks": 0,
    "lang": "en", "possibly_sensitive": false, "is_note_tweet": false, "source": "…",
    "replying_to": null, "reposted_by": null,
    "quote": { /* nested status or tombstone */ },
    "media": { "photos": [], "videos": [], "mosaic": {} },
    "poll": { }, "card": { }, "article": { },
    "community_note": null,
    "community": {                     // ONLY when the post was made inside an X Community
      "id": "…", "name": "…", "description": "…", "created_at": "ISO",
      "search_tags": [], "is_nsfw": false, "topic": "…|null",
      "join_policy": "Open|Closed", "invites_policy": "MemberInvitesAllowed|MemberInvitesDisabled",
      "is_pinned": false, "admin": { /*APIUser*/ }, "creator": { /*APIUser*/ } },
    "author": { /* APIUser, see below */ },
    "provider": "twitter" } }
```

  APIUser (from the profile endpoint; post authors may be a stub marked `profile_embed: true`):

```jsonc
{ "type": "profile", "id": "…", "name": "…", "screen_name": "…",
  "avatar_url": "…", "banner_url": "…", "description": "…", "location": "…", "url": "…",
  "protected": false, "followers": 0, "following": 0, "statuses": 0, "media_count": 0, "likes": 0,
  "joined": "…", "website": { "url": "…", "display_url": "…" },
  "verification": { "verified": true, "type": "organization|government|individual|null",
                    "verified_at": "…", "identity_verified": false },
  "about_account": { "based_in": "…", "location_accurate": true,
                     "username_changes": { "count": 3, "last_changed_at": "…" } } }
```

  The profile response is wrapped as `{code, message, user, reason?: "suspended", id?}`.

- **Communities:** FxTwitter has **no community-by-id endpoint**. It only exposes `status.community` for posts *made inside* a community. See `processor.ts:651`, which reads `author_community_relationship.community_results`.

### 2.4 vxTwitter API (`api.vxtwitter.com`)

- **Routes:**
  - `GET https://api.vxtwitter.com/<handle>/status/<id>`. The handle can be anything; `/status/<id>` and `/i/status/<id>` also work.
  - `GET https://api.vxtwitter.com/<handle>` for a profile.
  - Add `?with_tweets=true` to include `latest_tweets`.
- **Sample JSON** (from the project's own docs, https://github.com/dylanpdx/BetterTwitFix/blob/main/readme.md; I checked locally that ID `1577730467436138524` decodes to 2022-10-05T18:40:30Z, which matches `date`):

```json
{
  "date": "Wed Oct 05 18:40:30 +0000 2022",
  "date_epoch": 1664995230,
  "hashtags": [],
  "likes": 21664,
  "mediaURLs": ["https://video.twimg.com/tweet_video/FeU5fh1XkA0vDAE.mp4","https://pbs.twimg.com/media/FeU5fhPXkCoZXZB.jpg"],
  "replies": 2911,
  "retweets": 3229,
  "text": "whoa, it works\n\nnow everyone can mix GIFs, videos, and images in one Tweet, available on iOS and Android https://t.co/LVVolAQPZi",
  "tweetID": "1577730467436138524",
  "tweetURL": "https://twitter.com/Twitter/status/1577730467436138524",
  "user_name": "Twitter",
  "user_screen_name": "Twitter"
}
```

- **Current extra fields** (from `vxApi.py`):
  - On tweets: `user_profile_image_url`, `conversationID`, `possibly_sensitive`, `qrtURL` (and `qrt`, the nested quoted tweet), `communityNote`, `pollData`, `article`, `lang`, `replyingTo`, `replyingToID`, `retweetURL`, `media_extended[]`, `fetched_on`.
  - **User object:** `{id, screen_name, name, profile_image_url, description, location, followers_count, following_count, tweet_count, created_at, protected, fetched_on}`.
  - There is **no verification flag** on the user object.
- **Upstream method order** (`twExtract.extractStatus`): guest-token GraphQL `TweetResultByRestId`, then authenticated GraphQL variants. Syndication code exists but is no longer in the default chain. User lookups need configured account tokens (`workaroundTokens`).
- **Communities:** none.

### 2.5 Syndication timeline-profile (`syndication.twitter.com/srv/timeline-profile/screen-name/<h>`)

- Returns HTML containing a `__NEXT_DATA__` JSON with about 12–20 recent tweets *and* the user object. That has historically included `followers_count`; flagged, may now be sunset.
- **Status 2026:** works intermittently. Reports in late 2026 describe frequent `429 Rate limit exceeded`. One scraping guide said it was "confirmed against live targets this month". Not a dependable source.
- Sources: https://twitterscraperapi.com/blog/how-to-scrape-twitter and https://devcommunity.x.com/t/keep-getting-rate-limit-exceeded-with-no-reason/241316

### 2.6 Nitter

- Needs real X account session tokens since X removed guest accounts (Jan 2024). Development resumed in Feb 2025.
- **24 Aug 2026:** X Corp sent cease-and-desist letters to Nitter and XCancel. The repo README (fetched today) says: *"Following legal advice, the Nitter project will continue."* Some articles say the repo was archived. This conflicts with the README, so it is **flagged**.
- Public instance health in Sept 2026: of about 42 tracked instances, 1 served real pages to plain requests, 8 showed verification walls, and 28 were gone (https://peekvault.com/nitter-instances, https://status.d420.de/).
- **Verdict: do not use public Nitter.** Self-hosting needs burner X accounts, which is a ToS risk and has ban churn.

### 2.7 Summary table (free sources)

| Source | Tweet | Profile (followers) | Search | Community | Key/UA | Reliability 2026 |
|---|---|---|---|---|---|---|
| syndication tweet-result | yes (no follower count) | no | no | no | token (computed) | medium; IP-sensitive |
| publish.x.com oEmbed | text/author/date in HTML | no | no | no | none | medium; bulk-limited |
| FxTwitter API v2 | **yes, rich** | **yes + about/username changes** | **yes** | only on community posts | UA header | good; third-party, commercial use unclear |
| vxTwitter API | yes | yes (no verified flag) | no | no | none | good; third-party |
| timeline-profile | last ~12–20 tweets | maybe | no | no | none | poor (429s) |
| Nitter | yes | yes | yes | no | n/a | poor / legal cloud |

---

## 3. Third-party paid scraper APIs

| Provider | Price | Notes | Communities |
|---|---|---|---|
| **twitterapi.io** | $1 = 100k credits. Tweet = 15 credits (**$0.15/1k**). Profile = 18 credits (**$0.18/1k**). Follower = 15. List calls = 150. **Min 15 credits ($0.00015) per request.** Community info = **20 credits per call**. | QPS: free accounts 1 req per 5 s; paid 3 QPS at 1k credits up to 20 QPS at 50k. Endpoints include `/twitter/tweets?tweet_ids=a,b,c` (batch), `/twitter/user/info?userName=`, `/twitter/user_about`, `/twitter/tweet/advanced_search?query=&queryType=Latest\|Top` (20 per page), quotes, replies, thread context, webhooks / user monitoring. Note: X's `since:`/`until:` operators are degraded; use `since_time:UNIX`. | **Yes:** `GET /twitter/community/info?community_id=` (described as "a bit slow"), `/community/members`, `/community/moderators`, `/community/tweets` (20 per page), `/community/get_tweets_from_all_community` (search across communities). |
| **SocialData.tools** | **$0.0002 per tweet or profile ($0.20/1k)**. Failed requests free. A free allowance of about 3 req/min is cited (flagged). Extended bio $0.001. | Tweet lookup, user lookup, search, user tweets. | **Yes:** community details (identity, description, creator, join_policy, created_at, is_nsfw, member_count, topic, tags, **rules**), `/twitter/community/{id}/members`, `/twitter/community/{id}/tweets`, community search. |
| **Apify actors** | apidojo Tweet Scraper V2 about **$0.40/1k** (minimum 50 tweets per query). kaitoeasyapi about $0.25/1k (free plan) to $0.18/1k (Business). Some actors $0.15/1k. Apify free plan includes $5/mo platform credit. | Batch, run-based; latency from seconds to minutes. Poor fit for per-token real-time lookups. | Community actors exist (e.g. `datamagnet/x-twitter-community-info-scraper`, `agentx/x-twitter-community-api`, `igview-owner/twitter-x-communities-search`). Quality varies. |
| **RapidAPI** | `twitter-api45`: $0 / $9 / $99 / $490 per month tiers. `twttrapi`: $59/mo for 2M req, $119/mo for 4M req. `twitter241`. | Flat monthly quotas. Fine for steady volume. Vendors come and go. | Some list community endpoints; unverified. |
| Others seen in 2026 roundups | twitterapis.com claims $0.04/1k; GetXAPI, Sorsa, SocialCrawl, ScrapeCreators. | Unverified; mostly from vendors' own comparison posts. | ? |

Sources:
- twitterapi.io: docs snapshot `data/docs.json` in https://github.com/dorukardahan/twitterapi-io-mcp (pricing / qps_limits / endpoints sections), https://docs.twitterapi.io/api-reference/endpoint/get_community_by_id, https://twitterapi.io/blog/x-api-cost-breakdown-2026
- SocialData: https://docs.socialdata.tools/getting-started/pricing/, https://docs.socialdata.tools/reference/get-community-tweets/, https://socialdata.gitbook.io/docs/twitter-x-communities/retrieve-community-members
- Apify: https://apify.com/apidojo/tweet-scraper, https://use-apify.com/docs/best-apify-actors/best-twitter-scrapers
- RapidAPI: https://rapidapi.com/alexanderxbx/api/twitter-api45, https://twttrapi.com/

All of these vendors scrape X with pools of accounts. They are as fragile as X's anti-scraping measures, and using them is a ToS grey area (§6).

---

## 4. X Communities (`x.com/i/communities/<id>`)

Facts:

1. **The community ID is a snowflake, so its creation time is free.** Tested locally:
   `1804846498066116981 -> 2024-06-23T11:58:32Z` ("Pump.Fun: Shill your coin", 8.1K members per search-result title).
   `1680241856523771907 -> 2023-07-15T15:44:10Z` ("Memecoins", 5.0K members).
   A community created minutes before the token is a strong "fresh, purpose-built" signal.
2. **Community pages need login** for full content. No public, unauthenticated JSON endpoint exists for name, description, member count or rules. The data lives in X GraphQL (`CommunityQuery` / `CommunityByRestId` and similar). That data includes `name, description, member_count, moderator_count, rules[{name, description}], join_policy, is_nsfw, created_at, primary_community_topic, search_tags, custom_banner_media, admin_results, creator_results`. Field names are inferred from FxEmbed's processor and SocialData's field list; **flagged**.
3. **Free partial route:** if any tweet *posted inside* the community is known, FxTwitter `/2/status/{id}` returns `status.community` with name, description, created_at, tags, join_policy, admin and creator (no member count, no rules). A token's metadata almost never carries such a tweet ID, so this is rarely useful.
4. **Possible free route, untested and flagged:** X serves server-rendered OpenGraph `<title>` / `og:description` to link-preview crawlers. Search engines index community titles as "<Name> Community on X - 8.1K Members". Fetching `https://x.com/i/communities/<id>` with a crawler UA (e.g. `Twitterbot/1.0`, `facebookexternalhit/1.1`, `Discordbot/2.0`) *may* return the name and member count in meta tags. Run the smoke test in §8 from Render before relying on this. It is fragile and may violate the ToS.
5. **Paid, reliable:** twitterapi.io `GET https://api.twitterapi.io/twitter/community/info?community_id=<id>` (20 credits = $0.0002 per call; the response schema is an untyped `community_info` object in the docs) or SocialData community details (includes `member_count` and `community_rules`). Community tweets (twitterapi.io `/twitter/community/tweets`, SocialData `/twitter/community/{id}/tweets`) show whether the community is active or just a shell.

Memecoin context: pump.fun devs increasingly link an X Community instead of a profile. Communities are free to create, anonymous, and do not need a following. Treat them as **neutral to weak**, unless member count and recent activity are real or the community predates the token by a long time.

---

## 5. URL parsing

### Shapes seen in the wild

- **Hosts:**
  - `twitter.com`, `x.com`, with or without `www.`
  - `mobile.twitter.com`, `mobile.x.com`, `m.twitter.com`
  - Embed-fixers: `fxtwitter.com`, `fixupx.com`, `vxtwitter.com`, `fixvx.com`, `twittpr.com`, plus `d.` / `api.` subdomains
  - Mirrors: `nitter.*`, `xcancel.com`
  - Shortener: `t.co`
- **Tweets:**
  - `/<handle>/status/<id>`
  - `/<handle>/statuses/<id>`
  - `/i/web/status/<id>`, `/i/status/<id>`, `/status/<id>`
  - Suffixes such as `/photo/1`, `/video/1`, `/analytics`, `/quotes`, `/retweets`, or a language code (fxtwitter `/en`)
- **Query noise:** `?s=20`, `?s=21`, `?s=46`, `&t=<token>`, `?ref_src=…`, `?lang=`.
- **Communities:** `/i/communities/<id>`, plus suffixes like `/about` or `/members`.
- **Profiles:**
  - `/<handle>`, optionally with `/with_replies`, `/media`, `/likes` or `/highlights`
  - `/intent/user?screen_name=` or `/intent/follow?screen_name=`
  - `/i/user/<numeric id>`
  - Bare `@handle` or `handle`
- **Search:** `/search?q=%24TICKER&src=typed_query&f=live`, `/hashtag/<tag>`
- **Lists:** `/i/lists/<id>`
- **Garbage:**
  - Non-X domains (pump.fun, Telegram, websites)
  - Reserved paths (`/home`, `/explore`)
  - Quoted, angle-bracketed or trailing-punctuation strings
  - Plain text
  - Numeric-only strings, which are not valid handles

### Strategy

1. Trim and strip wrapping characters. A bare `@handle` or handle (`^[A-Za-z0-9_]{1,15}$`, not all digits) becomes a profile.
2. If there is no scheme, prepend `https://`, then parse with `new URL`. If parsing fails, classify as `invalid`.
3. Normalise the host (lowercase, strip `www.`), then route: `t.co` means resolve the redirect; unknown hosts are `foreign`.
4. Match path segments in priority order: community, then status (find `status` or `statuses` followed by an all-digit ID; take the preceding handle if it is valid and not reserved), then lists, `i/user`, intent, search/hashtag, then profile (first segment is a valid handle and not reserved).
5. **Never trust the handle in a status URL.** X ignores it (`/anything/status/<id>` resolves). Scammers paste `x.com/elonmusk/status/<id of their own tweet>`. Always use the *fetched* author.
6. Resolve `t.co` with a `HEAD` request without following redirects, read `Location`, and re-parse. Cap at 3 hops.

A tested implementation is in `docs/reference/xurl.reference.mjs` (Python port: `docs/reference/xref.py`). Test output:

```
"https://x.com/elonmusk/status/1791351500217754008?s=20&t=abc" {"kind":"tweet","tweetId":"1791351500217754008","handle":"elonmusk"}
"x.com/i/web/status/1234567890123456789"   {"kind":"tweet","tweetId":"1234567890123456789","handle":null}
"https://mobile.twitter.com/jack/status/20/photo/1" {"kind":"tweet","tweetId":"20","handle":"jack"}
"https://fixupx.com/jack/status/20/en"     {"kind":"tweet","tweetId":"20","handle":"jack"}
"https://vxtwitter.com/Twitter/statuses/1577730467436138524" {"kind":"tweet",…}
"https://x.com/i/communities/1804846498066116981/about" {"kind":"community","communityId":"1804846498066116981"}
"https://x.com/SomeCoin_?s=21"             {"kind":"profile","handle":"SomeCoin_"}
"@pumpdotfun"                              {"kind":"profile","handle":"pumpdotfun"}
"https://x.com/search?q=%24WIF&src=typed_query" {"kind":"search","query":"$WIF"}
"https://t.co/AbCdEf123"                   {"kind":"shortlink","url":"https://t.co/AbCdEf123","needsResolve":true}
"https://twitter.com/intent/user?screen_name=jack" {"kind":"profile","handle":"jack"}
"https://x.com/i/user/12"                  {"kind":"profile","userId":"12"}
"https://pump.fun/coin/abc"                {"kind":"foreign","host":"pump.fun",…}
"https://x.com/home"                       {"kind":"unknown",…}
"not a url at all"                         {"kind":"invalid"}
```

Core of the implementation:

```js
const si = lower.findIndex(x => x === 'status' || x === 'statuses');
if (si >= 0 && /^\d{1,20}$/.test(seg[si+1]||'')) return {kind:'tweet', tweetId: seg[si+1], handle: validHandle(seg[si-1])};
if (lower[0]==='i' && lower[1]==='communities' && /^\d+$/.test(seg[2]||'')) return {kind:'community', communityId: seg[2]};
```

**Snowflake decoding (no network needed):**

```js
ts = new Date(Number((BigInt(id) >> 22n) + 1288834974657n))
```

This works for IDs from Nov 2010 onward. Test: `1577730467436138524 -> 2022-10-05T18:40:30.908Z`, which matches the API's `date`. Use it to get the tweet or community creation time *before* any fetch, and to detect fake IDs. Examples: an ID that decodes to a future date, or a tweet whose decoded time differs from the fetched `created_at`.

---

## 6. Signals for memecoin understanding

Let `T_token` be the token's on-chain creation time and `T_tweet` the tweet's time (from the snowflake).

| Signal | Source | Interpretation |
|---|---|---|
| **Tweet predates the token** (`T_tweet < T_token`) | snowflake | The tweet is a *narrative source*: the coin was minted *about* this tweet (news, a celebrity post, a viral meme). A gap of minutes to hours is typical of "launch on a trending tweet". |
| **Tweet postdates the token**, and the author is the token account itself | snowflake + author | A launch-announcement tweet. Weight depends on the account's age and followers. |
| **Hijacked celebrity tweet** | author handle, verification, followers vs the token's own socials | The linked tweet's author is a big or verified account (e.g. a politician or Musk) with no relationship to the deployer. The token *borrows* a narrative. This is very common. Classify it as `narrative_reference`, not `official_account`. The risk: the token looks endorsed but is not. |
| Handle in URL ≠ fetched author | parser vs API | **Spoof attempt.** Strong red flag. |
| Tweet text mentions the token's CA / ticker / pump.fun link | text, entities.urls, symbols (`$TICKER`) | Confirms a direct link. A celebrity tweet mentioning the ticker is very rare and very strong. Otherwise it is usually the dev's own account. |
| Author verification: `is_blue_verified` vs `verified_type` (Business/Government) vs `highlighted_label` (affiliate) | syndication / FxTwitter | Blue checks are paid and weak. Gold or grey checks are strong. Note that FxTwitter `verification.type` maps to organization/government/individual. |
| Follower count, account `joined`, statuses count | FxTwitter / vx profile, twitterapi.io | Accounts days old with few tweets point to a disposable dev account. |
| **Username change count / last change** | FxTwitter `/2/profile/{h}/about` (`about_account.username_changes`), twitterapi.io `user_about` | Recycled or renamed accounts (a bought account with followers renamed to the coin) are a **strong rug signal**. `based_in` (country) is also available. |
| Media (images/video), quoted tweet | all tweet sources | The meme image is often the real "narrative". A quoted celebrity tweet inside the dev's tweet is the same hijack pattern. |
| Engagement (likes / replies / reposts / views) relative to follower count | FxTwitter | Inflated likes on low views suggests bots. |
| Tombstone / 404 / suspended | syndication `TweetTombstone`, FxTwitter `code:404`, `reason:"suspended"` | Deleted narrative tweet or banned account. Record it and keep cached data. |
| Community: creation time vs `T_token`, member count, rules, recent posts | snowflake + paid API | Brand-new, empty community: no signal or weakly negative. |
| Search URL (`/search?q=$TICKER`) | parser | No account at all; the dev is outsourcing "socials". Weak or negative. |
| Same Twitter link reused across many tokens | your own DB | **Copy-paste / narrative farming.** Count distinct mints per tweet ID or handle. Very cheap and very useful. |

**Risks:**

- **Fake or spoofed links:** wrong handle in a status URL, look-alike handles (`elonmusk_` / `eIonmusk`), and fxtwitter-style mirrors. Always compare the fetched author.
- **Deleted tweets:** narrative tweets get deleted, so cache the first successful fetch permanently.
- **Rate limits and IP blocks:** Render shares egress IPs, so free endpoints may 429 or return empty bodies. Use backoff and a circuit breaker per source.
- **Schema drift:** unofficial sources change without notice. Validate responses (e.g. zod) and degrade gracefully.
- **ToS and legal:**
  - X's ToS forbids scraping or crawling without consent and includes liquidated damages of **$15,000 per 1M posts accessed in 24 h** (effective Nov 2024 terms; https://knightcolumbia.org/content/knight-institute-says-xs-new-terms-of-service-will-stifle-independent-research).
  - X sent cease-and-desist letters to Nitter and XCancel in Aug 2026.
  - At TokenSage's volume (thousands of posts per day), the practical risk is IP blocks, not lawsuits. But using third-party scrapers or FxTwitter commercially is a grey area.
  - The official API (pay-per-use) and oEmbed are the only clearly sanctioned paths.

---

## 7. Recommended tiered strategy

```
parseXRef(metadata.twitter)
  ├─ empty/invalid/foreign/homepage → signal "no_x_link" (done, $0)
  ├─ t.co → HEAD resolve (≤3 hops), re-parse
  ├─ search/hashtag → signal "search_link" (no fetch)
  ├─ tweet(id) → decode snowflake time FIRST (free) → fetch chain:
  │      1. FxTwitter  GET api.fxtwitter.com/2/status/{id}   (UA header; rich incl. author followers)
  │      2. vxTwitter  GET api.vxtwitter.com/i/status/{id}
  │      3. Syndication tweet-result (computed token)        (no followers)
  │      4. oEmbed publish.x.com                             (text/author only)
  │      5. PAID: twitterapi.io /twitter/tweets?tweet_ids=… (batch!)  or SocialData
  │      6. (optional) official X API GET /2/tweets?ids=… ($0.005/post, dedup per UTC day)
  ├─ profile(handle) →
  │      1. FxTwitter /2/profile/{h} (+ /about for username_changes, sampled)
  │      2. vxTwitter api.vxtwitter.com/{h}
  │      3. PAID: twitterapi.io /twitter/user/info (+ /twitter/user_about)
  └─ community(id) → decode snowflake (free) →
         1. (experimental) crawler-UA OG meta on x.com/i/communities/{id}
         2. PAID: twitterapi.io /twitter/community/info  or SocialData community details
```

- **Do not fetch everything.** pump.fun mints tens of thousands of tokens a day, and most die in minutes. Do the free work for every token: parse the link, decode the snowflake, count reuse in the DB. Fetch from X only for tokens that pass a cheap on-chain filter, such as surviving N minutes, reaching a bonding-curve %, or being requested by a user.
- **Cost estimate for the paid fallback:**
  - 5,000 fetched tokens/day × (1 tweet + 1 profile) on twitterapi.io ≈ 5,000 × $0.00033 ≈ **$1.65/day ≈ $50/month**.
  - SocialData is about $2/day.
  - Communities at $0.0002 each are negligible.
  - The same load on the official X API: about $75/day.
- **Caching** (Render Postgres or Redis; key by tweet ID, lowercased handle, community ID):
  - Tweet content: immutable apart from edits, so **store forever**, including the *first-seen* snapshot. Engagement counts can be refreshed with a TTL of about 1–6 h only for "hot" tokens.
  - Tombstones / 404: cache with a short TTL (about 1 h, up to 3 retries), then permanently. Deleted is a signal in itself.
  - Profiles: TTL about 6–24 h. Keep a history table of (handle → user ID, followers, name) to detect renames. Key by **user ID**, not handle.
  - Communities: TTL about 24 h.
  - Negative cache for parse failures: forever, since it is deterministic.
  - **Dedupe in flight:** many tokens link the same celebrity tweet, so use a single-flight lock per tweet ID.
  - **Per-source circuit breaker:** after N consecutive failures or empty bodies from one source, skip it for M minutes. Record which source answered in each row.
- **Being a good citizen:** send a descriptive `User-Agent` to FxTwitter and vxTwitter (`TokenSage/1.0 (+https://your.site)`). Keep to ≤ a few req/s per source. Consider self-hosting FxEmbed (MIT, Cloudflare Worker free tier) if volume grows or commercial use is a concern; that still needs your own X credentials for some routes.

---

## 8. Smoke test to run from Render (not run in this sandbox; egress was blocked)

```bash
UA='TokenSage/0.1 (+https://example.org)'
TOK=$(node -e 'const id="20";console.log(((Number(id)/1e15)*Math.PI).toString(36).replace(/(0+|\.)/g,""))')
curl -s -A "$UA" "https://cdn.syndication.twimg.com/tweet-result?id=20&lang=en&token=$TOK" | head -c 600; echo
curl -s -A "$UA" "https://publish.x.com/oembed?url=https://x.com/jack/status/20&omit_script=1" | head -c 600; echo
curl -s -A "$UA" "https://api.fxtwitter.com/2/status/20" | head -c 600; echo
curl -s -A "$UA" "https://api.fxtwitter.com/2/profile/jack" | head -c 600; echo
curl -s -A "$UA" "https://api.fxtwitter.com/2/profile/jack/about" | head -c 600; echo
curl -s -A "$UA" "https://api.vxtwitter.com/jack/status/20" | head -c 600; echo
curl -s -A "$UA" "https://api.vxtwitter.com/jack" | head -c 600; echo
curl -s -A "$UA" "https://syndication.twitter.com/srv/timeline-profile/screen-name/jack" -o /dev/null -w "%{http_code}\n"
curl -s -A "Twitterbot/1.0" "https://x.com/i/communities/1804846498066116981" | grep -io '<title>[^<]*\|og:[a-z]*" content="[^"]*' | head
curl -sI "https://t.co/xxxxxxxx" | grep -i location
```

Expected: tweet 20 ("just setting up my twttr", 2006-03-21) from the first six commands. Note that ID 20 predates snowflakes, so do not snowflake-decode it.

---

## 9. Uncertain or flagged claims

- **Pay-per-use monthly read cap:** reported as both 2M and 3M. The exact `GET /2/tweets` rate limits on pay-per-use come from third-party pages.
- **Official v2 Communities endpoints:** availability and price on pay-per-use not confirmed.
- **FxTwitter:** the ~1000 req/min per IP limit comes from a search snippet; commercial-use permission is unclear (issue #2550).
- **Syndication `tweet-result`:** behaviour from Render IPs is untested. The `features` parameter may be needed.
- **timeline-profile still returning `followers_count`:** unknown.
- **Crawler-UA OG meta for community pages:** an untested idea.
- **Nitter:** whether the repo is archived conflicts between sources (README says it continues).
- **SocialData free allowance (3 req/min) and community-details path/fields:** from search snippets. The exact path for community details was not seen.
- **twitterapi.io pricing:** from a docs snapshot dated 2026-03-16; prices may have changed. The `community_info` response fields are undocumented in that snapshot.
- **RapidAPI and Apify pricing:** changes often; numbers are from 2026 roundup and vendor pages.
