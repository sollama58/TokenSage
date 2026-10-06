# TokenSage: understanding pump.fun memecoins without external AI APIs

Research date: 2026-10-05. Scope: work out what a token's NAME, TICKER, IMAGE, DESCRIPTION and linked tweet *mean* using deterministic or classical methods, free knowledge sources, and optionally small local CPU models, within Render's 512 MB to 2 GB RAM.

## 0. How this was tested (read first)

- **Sandbox network limits.** The research container's egress proxy blocked these hosts: `api.urbandictionary.com`, `*.wikipedia.org`, `wikidata.org`, `wikimedia.org`, `api.coingecko.com`, `api.gdeltproject.org`, `news.google.com`, `reddit.com`, `pump.fun` APIs, `knowyourmeme.com`, `huggingface.co`, `download.pytorch.org` and `openaipublic.azureedge.net`. Only PyPI, GitHub (raw and releases) and some S3 were reachable.
  - **I could not run live tests of the external knowledge APIs from here.** Their behaviour below comes from official docs and recent secondary sources, and is marked **[doc-only]**.
  - These APIs need a 10-minute smoke test from the real Render egress before anything is built on them.
  - CoinGecko, Wikimedia and Reddit all block or throttle some cloud IP ranges, so test from Render specifically.
- **Local libraries were installed and measured** (Python 3.11, Linux x86, CPU only). Those results are marked **[tested]**, and peak RSS was measured in a fresh process each time.
- **CLIP / SigLIP / MobileCLIP weights could not be downloaded** (Hugging Face was blocked, and Lakera's `onnx_clip` S3 bucket now returns 404 for `clip_image_model_vitb32.onnx`). Numbers for those models are **[doc-only]**.

---

## A. Text and ticker understanding without LLMs

### A1. Normalization pipeline [tested]

Order matters. Recommended order:

1. **`unicodedata.normalize("NFKC", s)`**
   - Folds full-width characters (`ＰＮＵＴ` → `PNUT`), ligatures and super/subscripts.
   - Tested: `$Ｐ​ＮＵＴ` → `$P​NUT`. The zero-width character survives, so step 2 is still needed.
2. **Strip zero-width and invisible characters** with the regex `[​-‏⁠﻿­]`.
   - For a complete list, also drop Unicode category `Cf` characters except ZWJ (U+200D) that sits inside emoji sequences.
   - Run emoji extraction before blanket Cf stripping.
3. **Extract emoji first, then remove them from the string.**
   - Use `emoji.emoji_list(s)` (emoji 2.16.0, BSD).
   - Map each emoji to words with CLDR annotations. `emoji.demojize` gives CLDR "tts" names: tested `🥜🐿️` → `:peanuts::chipmunk:`.
   - For richer keywords, ship the CLDR annotations JSON (438 KB, 1,966 entries) from `https://raw.githubusercontent.com/unicode-org/cldr-json/main/cldr-json/cldr-annotations-full/annotations/en/annotations.json` [tested download]. Examples:
     - 🐿 → [animal, chipmunk, **squirrel**]
     - 🥜 → [food, nut, **peanut**]
     - 🐸 → [animal, face, frog]
     - 💎 → [diamond, gem, money…]
   - Note: CLDR calls 🐿 "chipmunk", but its keywords include "squirrel". **Use the keyword list, not just the TTS name.**
4. **Homoglyphs and confusables.**
   - `confusable_homoglyphs` 3.3.1 (MIT). Tested: Cyrillic `Рepe` → `is_dangerous=True`, `is_mixed_script=True`.
   - Use it as a **signal** ("ticker spoofs PEPE with Cyrillic Р"), which is itself a scam/copycat flag. Then fold to ASCII.
   - **Fold with `anyascii` (ISC), not `Unidecode` (GPL-2.0+).**
     - Tested: anyascii folds small caps `ʙᴀʙʏ ᴘɴᴜᴛ` → `baby pnut` and Cyrillic Р → P.
     - Unidecode's GPL license is a known problem for commercial code ([wagtail PR](https://github.com/wagtail/wagtail/pull/6244), [eodag issue](https://github.com/CS-SI/eodag/issues/158)).
   - For full UTS #39 skeletons, the Unicode `confusables.txt` file can be applied directly (about 6k mappings).
5. **Strip `$` and the `#`/`@` prefixes.** Keep the raw form as well, because a `$`-prefixed word in a description is a ticker mention.
6. **Squeeze repeated letters.**
   - `re.sub(r"(.)\1{2,}", r"\1\1", s)`. Tested: `wiiiifhaaat` → `wiifhaat`.
   - Generate both the 2-letter and 1-letter variants and keep whichever segments or looks up better (`moooon` → `moon`, `wiifhaat` → `wifhat`).
7. **Leetspeak.**
   - Map `0→o 1→i/l 3→e 4→a 5→s 7→t @→a $→s`. Tested: `p3anu7` → `peanut`.
   - Apply it only to tokens that mix letters and digits, so real numbers like "2.0", "420" or "69" survive. Those numbers are meaningful in memecoins.
   - When `1` is ambiguous (i vs l), generate both and keep the candidate that hits the lexicon.
8. **Lowercase and keep a cased copy.** Case carries meaning: "CHILLGUY" in camel or upper case helps segmentation.
   - Split camelCase first with `re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+(?:\.\d+)?", s)`.
   - Tested failure: `AIAgentSupercycle` lower-cased then segmented became `a i agent super cycle`. A camelCase split would give `AI Agent Supercycle`.

**Pitfall seen in the prototype:** stripping punctuation turned `2.0` into `20` and lost the "sequel" marker. Detect version and derivative markers (`2.0`, `v2`, `II`, `classic`, `og`, `real`, `new`, `baby`, `mini`, `inu`, `wif`) **before** stripping punctuation.

### A2. Word segmentation and abbreviation expansion [tested]

| input | wordninja 2.0.0 | wordsegment 1.3.1 | SymSpell `word_segmentation` (symspellpy 6.10.0) |
|---|---|---|---|
| dogwifhat | dog w if hat | **dog wif hat** | dog with at |
| catwifhat | catw if hat | **cat wif hat** | catfish at |
| goatseusmaximus | goatse us maximus | goatse us maximus | goatees maximum |
| peanutthesquirrel | peanut the squirrel | peanut the squirrel | peanut the squirrel |
| justachillguy | just a chill guy | just a chill guy | just hill guy |
| moodeng | mood eng | mood eng | mood eng |
| elonsdog | el on s dog | el ons dog | melons dog |
| michimeow | mic him eow | **michi meow** | mich meow |
| notacatcoin | not a cat coin | not a cat coin | notice to in |

After adding a custom vocabulary to wordsegment (`ws.UNIGRAMS["wif"]=5e8`, plus `michi`, `moodeng`, `fwog`, `elons`, `ai`):

- `michimeow` → michi meow
- `elonsdog` → elons dog
- `aiagentsupercycle` → ai agent super cycle
- `babymoodeng` → baby moodeng
- `fwogwifhat` → fwog wif hat

**Recommendation:**

- Use **wordsegment** (Apache-2.0, 12 MB on disk, **~100 MB RSS** once loaded, 0.4 s load), with a **custom crypto/meme unigram list injected**:
  - slang terms
  - all known-coin names
  - celebrity and pet names
  - trending Wikipedia titles, refreshed daily
- **wordninja** (MIT, ~28 MB RSS) is the light fallback. It is worse on slang, but accepts a custom word-list file.
- **SymSpell compound or segmentation actively hurts**, because it "corrects" slang into English (`dog with at`, `catfish at`). Use SymSpell only as a spell-check for single tokens with edit distance ≤1, after a lexicon miss. It costs ~150 MB RSS at max edit distance 2, or ~80 MB at 0.
- Score candidate segmentations with a meme-aware weight: prefer segmentations whose tokens hit the gazetteers (known coins, slang, CLDR, WordNet animals).

**Ticker ↔ name matching** [tested]. `fuzz.WRatio` without a processor is case-sensitive and useless here; it scored PNUT vs Peanut at 36. Use these features together:

- `subsequence(ticker, name_compact)` and the first letter matching. PNUT ⊂ "peanut" → True. WIF ⊂ "dogwifhat" → True. GOAT ⊂ "goatseusmaximus" → True. CHILLGUY ⊂ "justachillguy" → True.
- `fuzz.partial_ratio(ticker.lower(), name_compact)`: PNUT/Peanut 86, WIF/dogwifhat 100, GOAT 100, CHILLGUY 100.
- An acronym of the name's words: "cat in a dogs world" → `ciadw`. That fails for $MEW, which is a lore ticker ("meow"). Lore tickers like this need a gazetteer.
- **Vowel-dropped form of the name**: `peanut` → `pnt`, which fits `pnut` within edit distance 1. This captures the common "drop vowels" ticker style (PNUT, MSTR, BRK).
- Double Metaphone (`metaphone` 0.6 or `jellyfish`): BONK/Bonk → PNK = PNK. FWOG vs FROG → FK vs FRK (no match), but Jaro-Winkler is 0.85.
  - Phonetics help with baby-talk spellings (fwog, smol, chonk, wen). An explicit baby-talk map (`fw→fr`, `w→r` before vowels) is more reliable than phonetics alone.

**Copycat matching against known coins** [tested]:

- Use `process.extractOne(q, known, scorer=fuzz.token_set_ratio, processor=utils.default_process)`, and **also** compare the space-stripped compact forms.
- Tested failure: `m00 deng classic` vs `moo deng` was <80 on token_set_ratio but trivially matches as `moodeng ⊂ moodengclassic`.
- Other tested pairs:
  - `dogwifhat2.0` vs dogwifhat: WRatio 86
  - `popkat` vs popcat: 83
  - `fartcoin classic` vs fartcoin: 90
- Ticker equality, or ticker equality after stripping prefixes (`B`, `BABY`, `MINI`, `2`, `V2`) and suffixes (`INU`, `AI`, `2`), is the strongest copycat signal.
  - The prototype missed `BPNUT` → PNUT because prefix stripping was not implemented. Implement it.

### A3. Lexicons and knowledge sources

| source | access | status | notes |
|---|---|---|---|
| **WordNet** via NLTK 3.10.3 | local data (wordnet.zip 10.8 MB, 35 MB unzipped) | [tested] | Hypernym "is-a" checks work well. corgi/pug → dog.n.01; hamster/capybara/otter/hippo/penguin → animal.n.01. Misses: `shiba`, `pepe`, `wif`. `chad` resolves to "paper chad" and Lake Chad, and `bonk` to sexual or hit senses, so meme senses are absent. **Cost: ~294 MB RSS and 3.2 s to load the full WordNet in NLTK.** **Do not load WordNet at runtime on a 512 MB instance.** Precompute offline into compact JSON gazetteers: all hyponyms of animal.n.01, food.n.01/n.02, person-ish nouns, and so on. That is a few hundred KB. New: `nltk.download()` now refuses proxied fetches by default (`nltk.pathsec.ALLOW_PROXIED_FETCH`). I fetched `https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/corpora/wordnet.zip` directly. Licence: WordNet License (permissive, BSD-like). |
| **Wiktionary** | kaikki.org wiktextract JSONL dumps | [doc-only] | English-only extract is ~2.5 GB ([kaikki raw data](https://kaikki.org/dictionary/rawdata.html), dump dated 2026-09-02). Offline preprocessing only. Filter senses tagged `slang`, `Internet`, `informal` and `Internet slang` into a small lexicon. CC BY-SA. Wiktionary has decent coverage for gm, wagmi, ngmi, based, cope, ratio, rizz, gyatt. |
| **Urban Dictionary** | `https://api.urbandictionary.com/v0/define?term=X` (unofficial, JSON `list[]` with `definition`, `example`, `thumbs_up`, `thumbs_down`, `written_on`) | [doc-only, blocked here] | The undocumented endpoint is widely used ([dev.to](https://dev.to/nhighleysalongenius/comment/epgk), [jaebradley client](https://github.com/jaebradley/urban-dictionary-client)). There is no SLA and no official ToS permission. Content is noisy and often offensive. Use it only as a **low-weight fallback for unknown tokens**, cache aggressively, require `thumbs_up` > 100 and an up/down ratio > 2, and never show raw text without a profanity filter. **Verify from Render.** |
| **Wikipedia Action API** search | `https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch=...&format=json` | [doc-only] | Map an expanded name (e.g. "peanut squirrel") to an article. The REST `/api/rest_v1/page/summary/{title}` endpoint gives a `description` (short description) and `extract`. **New 2026 global rate limits:** about 10 req/min for unidentified clients and 200 req/min with a compliant User-Agent of the form `TokenSage/0.1 (contact@…)` ([MediaWiki rate limits](https://www.mediawiki.org/wiki/Wikimedia_APIs/Rate_limits), [API usage guidelines](https://foundation.wikimedia.org/wiki/Policy:Wikimedia_Foundation_API_Usage_Guidelines)). **Always send a proper User-Agent.** |
| **Wikidata** | `wbsearchentities&search=X&language=en&format=json` returns id, label, description, aliases. `wbgetentities` returns claims such as P31 "instance of" (Q5 human, Q729 animal, Q144 dog, Q2927074 Internet meme, Q19631 meme, etc.) | [doc-only] | Gives structured categories without NLP: if P31 or P279 chains to Q2927074 "Internet meme", the category is meme template. CC0. Same rate limits as Wikipedia. Precompute a **local gazetteer** of entities that are Internet memes, famous animals, celebrities, politicians and AI chatbots, with aliases, via SPARQL offline. Expect 10k–100k labels and a few MB. |
| **Wikimedia pageviews** | top: `https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia/all-access/YYYY/MM/DD` (up to 1000 articles/day). Per-article: `/metrics/pageviews/per-article/en.wikipedia/all-access/user/{Title}/daily/{start}/{end}` | [doc-only] | **Best free "trending" signal.** Pull the daily top-1000 once a day; data lags about 1 day. Compute the per-article spike ratio as views(today) ÷ median(prior 30 days). Inject trending titles into the segmentation vocab and gazetteer. ([Wikimedia Analytics API docs](https://doc.wikimedia.org/generated-data-platform/aqs/analytics-api/examples/page-metrics.html), [Simon Willison TIL](https://til.simonwillison.net/wikipedia/page-stats-api)) |
| **Know Your Meme** | none | [doc-only] | **No public API, and KYM explicitly says "we don't allow scraping"** ([KYM on X](https://x.com/knowyourmeme/status/1504092733568389127)). Owned by Literally Media. **Do not scrape.** Alternatives: Wikidata items for Internet memes, Wikipedia's "List of Internet phenomena", and a hand-curated meme-template list (see B1). |
| **Emoji (CLDR)** | static JSON (above) | [tested] | Unicode licence (permissive). |
| **Crypto slang lexicon** | hand-curated YAML | n/a | Start list below. |
| **Prior famous memecoins** | CoinGecko `GET /api/v3/search?query=X` (coins with id/name/symbol/thumb/market_cap_rank). `GET /api/v3/coins/categories/list` (category ids include `meme-token`, `solana-meme-coins`, `pump-fun` ([pump-fun category page](https://www.coingecko.com/en/categories/pump-fun), [solana-meme-coins](https://www.coingecko.com/en/categories/solana-meme-coins))). `GET /api/v3/coins/markets?vs_currency=usd&category=pump-fun&per_page=250` lists all members with name, symbol and image URL. | [doc-only] | The free **Demo** key allows about 100 calls/min with a 10,000 calls/month cap ([pricing](https://www.coingecko.com/en/api/pricing), [costbench 2026](https://costbench.com/software/blockchain-data-api/coingecko-api/free-plan/)). Monthly-cap arithmetic: a weekly refresh of about 10 category pages, plus `search` only on cache misses, fits easily. **Build a local "famous coins" table** (name, symbol, aliases, lore summary, category, logo pHash) from the `meme-token`, `solana-meme-coins`, `pump-fun`, `ai-meme-coins`, `politifi` and `cat-themed`/`dog-themed` categories. Category ids were not verified live, so verify with `categories/list`. Download each logo once and pHash it for B1. |

**Starter crypto/meme slang lexicon** (curate by hand; about 200 entries is enough):

- **Greetings and outcomes:** gm, gn, wagmi, ngmi, lfg, wen (moon/lambo), ser, fren, anon, degen, ape (in), fomo, fud, hodl, dyor, nfa, iykyk
- **Trading:** jeet (panic seller), paper hands, diamond hands 💎🙌, bag/bagholder, rug/rugpull, honeypot, cto (community takeover), dev, kol, alpha, ath, pump, dump, send it, moon, 100x, lowcap, mcap, bonding curve, migrate/graduate (pump.fun → Raydium/PumpSwap), bundle, sniper, insider, dex paid
- **Meme characters:** pepe, wojak, chad, gigachad, sigma, based, cope, seethe, ratio, rizz, gyatt, skibidi, npc, bobo (bear), doge, shib(a), inu, wif ("with"), fwog, smol, chonk, bonk, mog, mew, michi, ponke, brett, andy, landwolf, boys club
- **AI agent terms:** truth terminal, terminal of truths, eliza/ai16z, virtuals, agent, aixbt, swarm, sentient, agi

Licence note: assemble this yourself from Wiktionary (CC BY-SA, attribution needed) and your own knowledge. Do not copy Urban Dictionary text.

### A4. Fuzzy, phonetic and NER [tested]

- **rapidfuzz** 3.14.6 (MIT, ~12 MB, ~22 MB RSS together with emoji/anyascii/confusables). Use `token_set_ratio`, `partial_ratio` and `WRatio` with `processor=utils.default_process`. Use `process.cdist` for batch matching against 10k known coins; this takes milliseconds.
- **Character n-gram similarity** (3-gram Jaccard or TF-IDF cosine via scikit-learn `TfidfVectorizer(analyzer="char_wb", ngram_range=(2,4))`) suits names like `trumpwifhat` vs `dogwifhat`: it shares the `wifhat` grams, which flags it as a template derivative (X-wif-hat).
- **Phonetic:** `jellyfish` 1.2.1 (MIT) offers Metaphone, NYSIIS, Jaro-Winkler and Levenshtein. Double Metaphone needs the `metaphone` package (BSD) or `pyphonetics`. Use phonetic matching only as a tiebreaker.
- **spaCy `en_core_web_sm` 3.8.0** (MIT; 15 MB model plus 126 MB spaCy package; **~148 MB RSS**, 0.8 s load, **4.4 ms/short doc**) [tested]. Results on memecoin text:
  - "Peanut the Squirrel was seized by New York officials. RIP PNUT" → `Squirrel`=PERSON, `New York`=GPE, `RIP PNUT`=ORG. Partial.
  - "**Elon Musk** just tweeted about his dog Floki" → **no entities at all**.
  - "Trump wins the election, MAGA forever" → Trump=ORG, MAGA=ORG (mislabelled).
  - "Goatseus Maximus… Truth Terminal by Andy Ayrey" → PERSON / ORG / PERSON. OK.
  - **Verdict:** the small statistical NER is unreliable on short, lowercase, slangy crypto text and costs ~150 MB. **Prefer gazetteers** (Wikidata-derived celebrities, politicians, famous animals, memes, AI bots, and your own coin table), matched with a FlashText or Aho-Corasick trie (`pyahocorasick`, BSD; or spaCy's `PhraseMatcher` on a blank `English()` pipeline at ~30 MB). Plain-regex `$TICKER` and `@handle` extraction covers the rest.

### A5. Linking a name to current news without AI

| source | status 2026 | use |
|---|---|---|
| **Wikimedia pageviews top-1000 / per-article** | Free; needs a UA; ~1-day lag [doc-only] | Primary. Daily job → `trending_entities` table with spike ratios. If token tokens match a spiking article title or its Wikidata aliases → "references trending topic X". Example: PNUT on 2024-11-01 would match the spiking "Peanut (squirrel)" article. |
| **Google News RSS** `https://news.google.com/rss/search?q=QUERY+when:1d&hl=en-US&gl=US&ceid=US:en` | Free and undocumented, community-documented ([NewsCatcher](https://www.newscatcherapi.com/blog-posts/google-news-rss-search-parameters-the-missing-documentaiton)); up to 100 items; no published rate limit [doc-only] | Per-token query on the segmented name plus "when:2d". Count recent headlines and keep the top 3 titles as evidence. Parse with `feedparser` (BSD). Cache per query and rate-limit yourself (≤1 req/s). Expect throttling or CAPTCHAs from cloud IPs if abused. |
| **GDELT DOC 2.0** `https://api.gdeltproject.org/api/v2/doc/doc?query=...&mode=artlist&format=json&timespan=24h` | Free; **1 request per 5 s per IP, enforced stricter in practice (8 s spacing, 60 s+ backoff on 429)** ([gdelt-mcp](https://github.com/nadirdev1/gdelt-mcp), [issue #44](https://github.com/cyanheads/gdelt-mcp-server/issues/44)) [doc-only] | Secondary. `mode=timelinevolraw` gives a coverage spike for a term. Too slow for per-token calls at pump.fun launch rates (thousands/day). Use it for batch enrichment of tokens that pass a market filter only. |
| **Google Trends** | **pytrends archived April 2025**; several endpoints 404. The official Trends API is still an application-gated alpha as of Sept 2026 ([dev.to](https://dev.to/esteban_ortega/pytrends-is-dead-heres-how-to-get-google-trends-data-in-2026-1a18), [apiserpent](https://apiserpent.com/blog/pytrends-dead-google-trends-data-2026)) [doc-only] | **Do not depend on it.** The Trending Now RSS (`trends.google.com/trending/rss?geo=US`) may still work but is unverified here. |
| **Reddit JSON** | **Unauthenticated `.json` broadly blocked (403) since ~May 30 2026**; OAuth free tier ~100 QPM ([redditapis.com](https://www.redditapis.com/blogs/reddit-json-endpoint-dead-2026)) [doc-only; secondary source, verify] | Optional, through an OAuth "script" app. Low priority. |
| **Linked tweet text** | X oEmbed `https://publish.x.com/oembed?url=<tweet>` (no key; returns `html` with tweet text; [docs](https://docs.x.com/x-for-websites/oembed-api)). FxTwitter `https://api.fxtwitter.com/status/<id>` (no key; JSON with text, author, media, engagement; [docs](https://docs.fxembed.com/api/introduction/), [wiki](https://github.com/FixTweet/FixTweet/wiki/Status-Fetch-API)) [doc-only] | The **tweet text is often the "meaning"** (the Elon tweet, the news post). Strip HTML from the oEmbed result; check deleted tweets (404) and author handle (a celebrity tweet vs the dev's own account). FxTwitter is a third-party community service, so treat it as best-effort. |

**Trend-matching algorithm** (deterministic):

1. Each day, build `trend_terms`: Wikipedia top-1000 titles with spike ratio > 3, their Wikidata aliases, and Google News top-stories RSS headline n-grams. Each term carries `first_seen` and `score`.
2. For each new token, compute a candidate phrase set: segmented name, ticker expansions, description noun phrases (regex chunks or capitalized spans), tweet text capitalized spans, and emoji keywords.
3. Match with Aho-Corasick for exact matches and rapidfuzz `token_set_ratio` ≥ 90 for fuzzy ones. Weight by term specificity (IDF over token history: "trump" appears in thousands of tokens, so a match on it is weak evidence of a *new* event) and by recency.
4. On a hit, optionally confirm with one Google News RSS query and attach the top headline as evidence.

### A6. Narrative taxonomy and rule-based classification

A practical taxonomy, merging the industry trackers' **Dog / Cat / Frog / AI / Political / Celebrity** themes and **launchpad/chain** groups ([Sharpe narrative tracker](https://www.sharpe.ai/learn/memecoin-narrative-tracker), [KuCoin 2026 outlook](https://www.kucoin.com/blog/q2-2026-memecoin-outlook-top-10)) with what is visible on pump.fun. Multi-label, each label with a confidence:

| top-level | sub-labels | primary signals |
|---|---|---|
| animal | dog, cat, frog, monkey/ape, hippo, squirrel, bird, bear/bull, fish, other | WordNet-derived animal gazetteer, emoji keywords, image CLIP label (optional), famous-animal gazetteer (Moo Deng, Peanut, Doge/Kabosu, Neiro, Floki) |
| meme template / internet culture | pepe/wojak/chad, "X wif hat", chill guy, NPC, brainrot (skibidi, sigma, rizz), copypasta | slang lexicon, Wikidata "Internet meme" gazetteer, image pHash to template DB |
| AI / agent | AI agent coin, AI-launched coin, "terminal"/LLM lore | {ai, agent, gpt, agi, terminal, eliza, swarm, sentient}, known AI-agent X handles in the tweet, description says "launched by agent" |
| political / PolitiFi | US politics, world leaders, elections, policy | politician gazetteer (Wikidata P39/position held), {maga, election, president…} |
| celebrity / influencer | Elon-related, musicians, athletes, streamers/KOLs | celebrity gazetteer; tweet author ∈ celebrity handles; "Elon's X" pattern |
| news / event | breaking news, viral story, sports, disaster, product launch | trend match (A5) with a recent first_seen |
| food / object / abstract | food, vehicles, emotions, "nothing" coins | WordNet food/artifact hyponyms |
| geography / regional | country, city, language-community (CN/KR/ID tickers) | country/demonym gazetteer, non-Latin script detection (Unicode script property; CJK names are a common pump.fun "meta") |
| crypto-native / meta | slang ("WAGMI"), pump.fun self-reference, "the next X", CTO, charity, utility claims | slang lexicon, "cto"/"community takeover" in description |
| derivative / copycat | exact ticker reuse, name + modifier (baby, 2.0, classic, inu, ai, wif), homoglyph spoof, logo reuse | A2 copycat matching, B1 pHash |
| humor / nsfw / offensive | toilet humor, slurs, adult content | blocklists, NudeNet (B), profanity lists (`better-profanity`, MIT) |

**Classification approach:**

- **Model:** weighted rule evidence → per-label scores → `1 - Π(1 - w_i)` noisy-OR combination, capped. Each rule emits `(label, weight, evidence_string, source)`.
- **Signal weights** (tune on a hand-labelled set of about 300 tokens):
  - exact gazetteer hit in the name: 0.6–0.8
  - hit only in the description: 0.3–0.5
  - emoji keyword: 0.3
  - pHash template match: 0.8
  - CLIP zero-shot score: 0.2–0.5
  - known-coin copy: 0.9
- **Precedence:** a known-coin match should *inherit* that coin's labels and lore (e.g. "dogwifhat 2.0" → derivative + animal/dog + meme template).
- **Tested prototype failures to design around:**
  - `wif` was put in the dog keyword set and fired "animal/dog" on "Trump wif Hat". `wif` should be a *template* marker ("X-wif-hat" template derived from WIF), not dog.
  - `BPNUT` was missed because no prefix stripping was done.
  - `2.0` was destroyed by punctuation stripping.
  - "Elons Dog" segmented as `el ons dog`; this needs the custom unigram.

---

## B. Image understanding without external AI

pump.fun images are usually PNG/JPG/GIF/WebP on IPFS, via the `image_uri` field. Coin metadata has `name`, `symbol`, `description`, `image_uri`, `metadata_uri`, `twitter`, `telegram` and `website`; name, symbol and image are immutable ([pump.fun docs](https://pump.fun/docs/create-coin), [Blofin explainer](https://blofin.com/academy/education/pumpfun/how-coins-are-created-on-pump-fun)).

- Fetch through a public IPFS gateway with a timeout and a size cap (e.g. 5 MB).
- Re-encode everything with Pillow to 512 px RGB.
- For animated GIF/WebP, sample frames 0, n/2 and n−1. `Image.open(...).is_animated` / `n_frames` works [tested: 5-frame GIF detected].

### B1. Perceptual hashing [tested]

ImageHash 4.3.2 (BSD-2, 80 KB package, plus numpy/scipy/Pillow; ~30 MB RSS with PIL). Test setup: a synthetic 512×512 logo (coloured shapes, yellow circle, "$PNUT" text). The table gives Hamming distance from the original; 64-bit hashes except colorhash.

| variant | ahash | phash | dhash | whash | colorhash |
|---|---|---|---|---|---|
| resize to 128 | 0 | 0 | 1 | 0 | 0 |
| JPEG q30 | 0 | 0 | 1 | 0 | 0 |
| crop 10% + resize | 5 | 6 | 11 | 4 | 1 |
| brightness +30% | 0 | 2 | 2 | 0 | 0 |
| Gaussian blur 3 | 0 | 0 | 0 | 0 | 1 |
| text changed ($PNUT→$PNUT2) | 1 | 4 | 2 | 0 | 0 |
| mirrored | 10 | **28** | 14 | 8 | 0 |
| rotated 15° | 14 | **22** | 16 | 11 | 1 |
| grayscale | 0 | 0 | 0 | 0 | 6 |
| different logo (same layout) | 10 | 20 | 16 | 14 | 4 |

- **Speed:** pHash plus dHash takes **2.8 ms per image**. `crop_resistant_hash` takes 211 ms, which is too slow for every image, but is fine for top candidates.
- **Takeaways:**
  - pHash ≤ 8 (of 64) means "same image, re-encoded or resized or recoloured". 9–14 means "probably edited, review". Use dHash as a confirm.
  - The images were synthetic, so tune thresholds on real logos. Literature typically uses about 10/64 for pHash.
  - Mirroring and rotation break pHash. Either hash the mirrored image too (cheap), or use **PDQ** with `compute_dihedral()`, which gives all 8 flips and rotations at once. PDQ is a 256-bit hash with a quality score; `pdqhash` on PyPI has MIT bindings, and the Meta ThreatExchange PDQ code has its own BSD-style licence ([pdqhash-python](https://github.com/faustomorales/pdqhash-python)). Threshold ≈ 31/256 per Meta.
  - "Same meme template, different caption" (text changed) stays close (pHash 4), so template detection works. Small-caption variants are indistinguishable from re-uploads; add OCR text diff to tell them apart.
- **Reference DB to build:**
  1. Logos of all famous memecoins (CoinGecko `image` URLs from the category listings).
  2. A curated set of meme template images you have rights to reference. Store **hashes only**, plus Wikidata/Wikipedia references; templates are mostly fair-use images.
  3. **Every pump.fun token you have ever seen.** This is the most valuable item: it detects relaunches and serial-rugger logo reuse.
- **Index:** a 64-bit pHash fits in a BIGINT. For millions of rows, use a BK-tree or multi-index hashing (split into 4×16-bit chunks; by pigeonhole, any match within distance ≤3 shares at least one exact chunk), or Postgres with a `bit_count(a # b)` scan on a candidate set. For 1M hashes an in-memory numpy XOR + popcount scan takes about 5–10 ms. Faiss `IndexBinaryFlat` is also an option.

### B2. OCR [tested]

- **RapidOCR** (`rapidocr_onnxruntime` 1.4.4, Apache-2.0; PaddleOCR PP-OCR models in ONNX bundled in a 16 MB wheel; needs onnxruntime 67 MB plus opencv 72 MB on disk). **~130 MB RSS after init, 0.4 s init.**
  - Results on the synthetic logo:
    - `"$PNUT"` at 1.00 confidence, 305 ms first call
    - rotated 15°: `"$PNUT"` at 0.97, 181 ms
    - downscaled to 128 px: `"$PNUT"` at 0.99, 178 ms
  - Real meme logos with outlines, curved or stylized text will do worse; that is not measured here.
  - Install `opencv-python-headless` instead of `opencv-python` to save size. RapidOCR also has a newer `rapidocr` package (v2/v3) with the same models; check the current name on PyPI.
- **Tesseract** (Apache-2.0) needs the system binary `tesseract-ocr` (about 30 MB plus the eng traineddata at 4–23 MB). On Render native Python environments you cannot apt-install, so a Docker deploy is required. Tesseract is known to struggle with stylized, low-contrast or decorative text compared with PaddleOCR-family models ([Koncile](https://www.koncile.ai/en/ressources/paddleocr-analyse-avantages-alternatives-open-source), [IronSoftware](https://ironsoftware.com/ocr/csharp/blog/compare-to-other-components/paddle-ocr-vs-tesseract/)). Not tested here (binary not installed). **Prefer RapidOCR** for logos.
- **What to use OCR for:** ticker or name in the image (a mismatch with the metadata ticker is a copycat signal), meme captions ("my new character"), URLs or handles, and brand names. Run fuzzy-match on the OCR text with the same A2 pipeline.

### B3. Colour, AI-generation heuristics, GIF, NSFW

- **Dominant colours** [tested]: `img.resize((64,64)).quantize(colors=5, method=MEDIANCUT)` plus `getcolors()` takes about 1 ms. Map RGB to colour names with a small nearest-neighbour table (CSS/XKCD colours). Useful for weak cues ("green frog" plus frog keyword → Pepe-like) and for showing a palette in the UI.
- **AI-generated image heuristics (weak):**
  - Check PNG `tEXt`/`iTXt` chunks: `parameters` (A1111/Stable Diffusion prompt), `prompt`/`workflow` (ComfyUI), `Software`.
  - Check EXIF `Software` and `ImageDescription`, XMP `DigitalSourceType = trainedAlgorithmicMedia`.
  - Check the C2PA manifest: PNG `caBX` chunk, or JPEG APP11 ([fast.io overview](https://fast.io/resources/ai-generated-image-metadata-detection-tools/), [winnow PR](https://github.com/lgtm-hq/winnow/pull/234)).
  - Midjourney, DALL·E and Grok outputs re-uploaded via pump.fun are usually stripped, so **absence of these markers means nothing**. A bonus: an SD `parameters` chunk sometimes *contains the prompt*, which is a free description of the image's meaning.
  - Pixel-level AI detectors are unreliable and not worth it here.
- **NSFW:** **NudeNet** 3.4.2 bundles `320n.onnx` (12 MB) in the wheel, so no download is needed. [tested] **~120 MB RSS, 0.2 s load, 21 ms/image** on CPU.
  - **Licence: AGPL-3.0.** Running it inside a network service triggers the AGPL source-offer obligation. Get legal review, or isolate it as a separately licensed microservice whose source you publish.
  - Alternatives: Yahoo `open_nsfw` (BSD-2; ResNet-50 Caffe weights, about 23 MB, ONNX conversions exist); `Falconsai/nsfw_image_detection` (ViT, Apache-2.0, about 340 MB, too heavy); and CLIP zero-shot "nsfw" prompts if CLIP is already loaded.

### B4. Optional local vision-language models (zero-shot labelling)

None of these weights could be downloaded in this sandbox, so all figures are **[doc-only]**, with caveats.

| model | file size | est. RSS (onnxruntime) | CPU latency/img (1 vCPU-ish) | licence | notes |
|---|---|---|---|---|---|
| CLIP ViT-B/32 vision (fastembed `Qdrant/clip-ViT-B-32-vision`) | 0.34 GB fp32 ([fastembed models](https://qdrant.github.io/fastembed/examples/Supported_Models/), [HF](https://huggingface.co/Qdrant/clip-ViT-B-32-vision/tree/main)) | ~450–600 MB (estimate) | ~70–150 ms on modern x86; slower on shared Render CPUs ([neuralbase](https://theneuralbase.com/clip/learn/beginner/vit-b-32-fastest-model/)) | MIT (OpenAI CLIP) | 512-d embedding. The text encoder (`Qdrant/clip-ViT-B-32-text`, ~0.25 GB) is needed **only offline** to embed the label vocabulary once; at runtime keep only the vision tower plus a precomputed `labels.npy`. |
| CLIP ViT-B/32 INT8 (dynamic quant) | ~90–150 MB ([sayantan47/clip-vit-b32-onnx](https://huggingface.co/sayantan47/clip-vit-b32-onnx)) | ~250–350 MB (estimate) | similar or *slower* on CPUs without VNNI | MIT | Accuracy drops slightly; validate. |
| MobileCLIP-S0 / MobileCLIP2-S0 | image encoder 11.4 M params (~45 MB fp32) | ~150–200 MB (estimate) | ~1.5 ms on iPhone per Apple; probably 15–40 ms on server CPU (estimate) | **Apple sample-code licence / Apple ML research licence (check; not OSI)** ([apple/MobileCLIP2-S0](https://huggingface.co/apple/MobileCLIP2-S0)) | Best accuracy per MB (zero-shot ImageNet about 71.5% for MobileCLIP2-S0, vs about 63% for ViT-B/32). ONNX export via open_clip or ultralytics/mobileclip. Licence review needed. |
| SigLIP base / SigLIP2 base-patch16 | ~370 MB vision | ~500 MB+ | ~150–300 ms | Apache-2.0 | Better zero-shot than CLIP B/32 but heavier. No "small" SigLIP under 100 MB exists. |
| ResNet-50 ImageNet (`Qdrant/resnet50-onnx`) | 0.10 GB | ~200 MB | ~50–100 ms | Apache/BSD | Fixed 1000 ImageNet classes: dog breeds, frogs, hats ("cowboy hat", "sombrero"), but no meme concepts. |
| MobileNetV3-Large ImageNet | ~22 MB | ~80 MB | ~10–20 ms | Apache-2.0 | Cheapest "what animal is it" signal. ImageNet has 120 dog breeds, cats, frogs (tree frog, bullfrog), squirrel (fox squirrel), hippo, etc. |

**Recommendation for images:**

- **On 512 MB:** skip CLIP. Run pHash + PDQ + RapidOCR + palette, and optionally a MobileNetV3 ImageNet classifier. RapidOCR and NudeNet together are about 250 MB, which is tight.
- **On 2 GB:** add CLIP ViT-B/32 vision (or MobileCLIP-S0 if the licence is acceptable) as **one** model, loaded once and shared.
- **Zero-shot labelling:** precompute text embeddings for about 300 prompts. Examples:
  - "a dog wearing a hat"
  - "a cartoon frog", "Pepe the frog meme"
  - "a cat", "a squirrel", "a baby hippo"
  - "Donald Trump", "Elon Musk" (celebrity face recognition by CLIP is weak and ethically sensitive; prefer text evidence)
  - "a robot / AI", "pixel art", "an anime girl", "a photo of food"
  - "a meme with text caption", "a logo with text"
  - "a hand-drawn MS Paint drawing", "a 3D render", "a photograph"
- Score with `softmax(100·cos)` across the vocabulary, but **report only labels above an absolute cosine threshold** (about 0.25–0.28 for B/32; tune it). Label each as "visual guess".
- CLIP embeddings also give **semantic near-duplicate search** (cosine > 0.92), which catches redrawn copies that pHash misses.
- **Run images in a background worker** (RQ, Celery or Dramatiq), not in the web process, so RAM spikes do not kill the API instance.

**Measured baselines** [tested]:

- onnxruntime 1.30.0 import: 44 MB RSS
- RapidOCR loaded: 130 MB
- NudeNet loaded: 121 MB

---

## C. Reverse image search

- **There is no reliable free reverse-image-search API.**
- **Google Lens has no official API.** Third-party SERP scrapers such as SerpApi, BrightData and OpenWebNinja resell it with small free tiers (e.g. 50–5k requests a month), but they are ToS-grey and paid at scale.
- **TinEye's API has no free plan** (from about $200 per 5,000 searches).
- **Google Cloud Vision Web Detection** gives 1,000 free units a month and then about $3.50 per 1,000. It is an external AI API, which violates the constraint.
- Yandex and Bing have no free public reverse-image APIs.
- Sources: [mixpeek list](https://mixpeek.com/curated-lists/best-reverse-image-search-apis), [vecstore guide](https://vecstore.app/blog/reverse-image-search-api), [BrightData](https://brightdata.com/products/serp-api/google-search/reverse-image).
- **Practical substitute:** your own index. Hash and embed every pump.fun image seen, plus famous-coin logos and meme templates. That covers the main question ("is this a copy of a prior coin or template?") better than web reverse search does. Optionally, offer users a **link** to Google Lens or TinEye with the image URL for a manual check; no automation needed.

---

## D. Explainable "meaning" output

### Pipeline

1. Normalize the inputs.
2. Extract candidates: tokens, ticker, emoji, OCR text, tweet text.
3. Run matchers. Each emits an `Evidence` record:

```python
Evidence(
    kind="known_coin_match",
    label="derivative",
    weight=0.9,
    detail="name 'dogwifhat 2.0' ~ dogwifhat (WIF) 100% + version marker '2.0'",
    source="coingecko:dogwifhat",
    url="https://www.coingecko.com/en/coins/dogwifhat",
)
```

4. Aggregate per label with noisy-OR: `conf = 1 - Π(1 - w_i)`.
   - Apply a **source-diversity bonus**: text, image and trend sources agreeing count more than three text rules.
   - Apply **conflict penalties**: a dog keyword with a CLIP "cat" result lowers both.
5. **Pick the referent**, meaning *what it refers to*, separately from the category. The referent is the highest-scoring entity among known coins, Wikidata entities, trend terms and meme templates, with its own confidence.
6. Fill templates. Example:

> **$PNUT2 "Peanut the Squirrel 2.0"** most likely refers to **Peanut (squirrel)**: an Instagram-famous pet squirrel seized and euthanized by New York officials in Oct 2024 (Wikipedia). Confidence 0.86.
> Categories: animal/squirrel 0.80 · news/event 0.55 · **derivative of $PNUT 0.92**.
> Why: ticker PNUT plus suffix "2" equals an existing coin ($PNUT, CoinGecko); name segmented to "peanut the squirrel 2.0"; emoji 🐿 (CLDR: squirrel); OCR read "PNUT" in the logo; logo pHash distance 6 to the $PNUT logo (near-identical image).
> Uncertain: no current news spike for "Peanut" (Wikipedia views flat), so this looks like a revival or copycat, not a new event.

- Template slots: `{ticker} {name} refers_to {referent} ({referent_description}) conf; categories[]; evidence bullets sorted by weight; caveats[]`.
- Generate caveats automatically when:
  - only a single weak source exists
  - the referent is ambiguous (two candidates within 0.1)
  - homoglyphs are present
  - the image is missing or failed to load
  - the tweet was deleted
- **Calibration:** hand-label 300–500 tokens (fast with a small internal UI), fit per-rule weights by logistic regression on the evidence features (scikit-learn, still classical), and check reliability curves so that "0.8" means right about 80% of the time.
- **Store all evidence as JSON** so the UI can show "why", and so you can audit regressions when lexicons change.
- **Output schema** (suggested):

```json
{"mint": "...", "ticker": "PNUT2", "name": "Peanut the Squirrel 2.0",
 "referent": {"label": "Peanut (squirrel)", "kind": "famous_animal|meme|person|coin|event|concept",
              "desc": "...", "source": "wikidata:Q130...", "confidence": 0.86},
 "categories": [{"label": "derivative", "confidence": 0.92}, {"label": "animal/squirrel", "confidence": 0.8}],
 "copy_of": [{"ticker": "PNUT", "mint": "...", "signals": ["ticker", "name", "logo_phash:6"]}],
 "trend": {"matched": false, "terms": []},
 "image": {"phash": "…", "ocr": ["PNUT"], "palette": ["#c87f3a", "…"], "labels": [{"label": "a squirrel", "score": 0.31, "model": "clip-b32"}], "nsfw": false},
 "summary": "…", "evidence": [ ... ], "caveats": [ ... ], "versions": {"lexicon": "2026-10-05", "rules": "0.3"}}
```

---

## E. Library recommendations (versions as installed 2026-10-05)

| purpose | library | version | licence | disk | RSS (measured) | verdict |
|---|---|---|---|---|---|---|
| Unicode folding | **anyascii** | latest | ISC | <1 MB | small | **Use** (not Unidecode 1.4.0, GPL-2+) |
| Confusables | **confusable_homoglyphs** | 3.3.1 | MIT | small | ~20 MB with the others | Use |
| Emoji | **emoji** | 2.16.0 | BSD | 4.5 MB | " | Use, plus CLDR annotations JSON (Unicode licence) |
| Segmentation | **wordsegment** | 1.3.1 | Apache-2.0 | 12 MB | ~100 MB | **Use**, with custom unigrams |
| Segmentation (light) | wordninja | 2.0.0 | MIT | 0.5 MB | ~28 MB | Fallback, or for 512 MB instances |
| Spell / segment | symspellpy | 6.10.0 | MIT | 6.5 MB | 80–150 MB | Single-token correction only; not for compounds |
| Fuzzy | **rapidfuzz** | 3.14.6 | MIT | 12 MB | small | Use |
| Phonetic / distances | **jellyfish** | 1.2.1 | MIT | small | small | Use; `metaphone` 0.6 (BSD) for Double Metaphone |
| Lexical DB | nltk + WordNet | 3.10.3 | Apache-2.0 / WordNet licence | 16 MB + 35 MB data | **~294 MB** | Offline precompute only |
| Gazetteer matching | pyahocorasick | latest | BSD-3 | small | small | Use |
| NER | spaCy + en_core_web_sm | 3.8.16 / 3.8.0 | MIT | 126 + 15 MB | ~148 MB | Optional; weak on this domain |
| Char n-gram TF-IDF | scikit-learn | latest | BSD-3 | ~40 MB | ~60 MB | Optional |
| RSS | feedparser | latest | BSD-2 | small | small | Use for Google News RSS |
| HTTP | httpx | latest | BSD-3 | small | small | Use; async plus a proper User-Agent |
| Images | **Pillow** | 12.3.0 | MIT-CMU (HPND) | 7 MB | ~30 MB | Use |
| Perceptual hash | **ImageHash** | 4.3.2 | BSD-2 | 80 KB (+scipy 113 MB, numpy 45 MB) | small | Use |
| PDQ | **pdqhash** | 0.2.x | MIT bindings + Meta PDQ licence | small | small | Use for dihedral-robust hashing |
| OCR | **rapidocr_onnxruntime** | 1.4.4 | Apache-2.0 | 16 MB + onnxruntime 67 MB + opencv(-headless) ~50–72 MB | **~130 MB** | Use |
| ONNX runtime | onnxruntime | 1.30.0 | MIT | 67 MB | 44 MB base | Use (shared across models) |
| NSFW | nudenet | 3.4.2 | **AGPL-3.0** | 12 MB model in wheel | ~120 MB, 21 ms/img | Legal review needed; else open_nsfw (BSD-2) |
| CLIP embeddings | fastembed (Qdrant) | latest | Apache-2.0 (models: MIT for CLIP) | model 0.34 GB | ~0.5 GB (est.) | Only on ≥2 GB instances or a separate worker |
| Vector / near-dup | numpy popcount / faiss-cpu | — | MIT | — | — | numpy is enough under 1M items |

**Rough memory budgets:**

- **512 MB tier, text only:** python, wordsegment (or wordninja), rapidfuzz, emoji/anyascii, precomputed gazetteers (≤20 MB), httpx and feedparser. About **180–220 MB** total. Image processing should live in a separate worker; pHash + Pillow adds about 60 MB, and RapidOCR adds about 130 MB, giving roughly 350–400 MB. That is feasible but tight on one 512 MB instance.
- **2 GB tier:** all of the above plus CLIP ViT-B/32 vision (~0.5 GB), plus NudeNet or open_nsfw. About **1.0–1.2 GB**.

---

## Open uncertainties to verify from the Render environment

1. Live behaviour and availability of `api.urbandictionary.com/v0/define`; status of the Wikipedia and Wikidata APIs under the 2026 rate limits; the CoinGecko Demo quota (100/min, 10k/month per secondary sources) and exact category ids (`pump-fun`, `solana-meme-coins`, `meme-token`); GDELT throttling; Google News RSS throttling from cloud IPs.
2. Whether Reddit `.json` is really fully blocked. This comes from a single secondary source (redditapis.com); it is plausible but unconfirmed.
3. Whether X oEmbed and FxTwitter still return full tweet text for all tweets; X changes policies often.
4. CLIP, MobileCLIP and SigLIP RAM and latency on Render's actual shared CPUs; my figures are estimates from secondary sources. MobileCLIP licence terms need legal review.
5. The pHash and PDQ thresholds come from synthetic images; tune them on about 1k real pump.fun logos.
6. NudeNet's AGPL-3.0 licence and its consequences for a hosted service.
7. RapidOCR accuracy on real stylized meme logos (only clean synthetic text was tested).

## Test artifacts

The throwaway test scripts used for the measurements above were not kept in the repo; re-measure on Render before relying on the RAM/latency numbers.
