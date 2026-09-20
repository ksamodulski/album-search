# groove-search — session handoff

Status as of 2026-09-20 (fifth session, the same day as the fourth). Read `README.md` first for what the app is and how the pieces fit;
this file is only what a next session needs that the code does not already say.

## Where things stand

Working end to end: `groove setup` → `groove search` → `groove serve`, plus `doctor`,
`recalibrate` and now `library`. 267 tests pass (`.venv/bin/python -m pytest`), all offline —
the fake fetcher, `FakeSearch` and `FakeLibrary` model a real query-sensitive shop, a real
engine and a real streaming account, so nothing in the suite touches the network.

**The headline change this session: the want-list no longer has to be typed.**
`groove library spotify` lists the last X albums added to a streaming account and
`--pick 1,3,5-8` prices exactly those; the web UI does the same with checkboxes that fill
the search box. Two adapters (`library.py`) behind one protocol, one PKCE login shared by
both surfaces (`oauth.py`). **Spotify's path is the one to trust; TIDAL's endpoints are
written from documentation that could not be read end to end — see finding 23.**

**Last session: the open web now says which engine answered, blocked sellers are named
instead of dropped, and the engine roster moved into a mounted SearXNG config.** The concrete win is measurable — `Simon & Garfunkel - Bookends` now wins at
99.68 PLN from muziker.pl, found via *Yep*, an engine the app could not reach that
morning. Details under *Hard-won findings* 17–20.

**The session before that: three bugs in the matcher were making real records
invisible, and two of them had nothing to do with coverage.** `Simon & Garfunkel -
Bookends` returned nothing while four shops — three of them in the registry — had it in
stock. Details under *Hard-won findings* 13–15; all three are the same lesson, which is
that a gate written as a substring test is a gate that silently deletes records.

The session before it lifted a different ceiling: albums the app used to call "Not
available" started returning real offers from shops nobody seeded, via the open-web source
and the delivered-cost model described under *Decisions* below.

The live registry holds 7 shops for PL: TonyMuzy (73%), VoiceShop (73%), Przeto (64%),
Master Disc (45%), VinylMusic (36%), Gramofonia (0%), Gandalf (0%). VoiceShop is broken
(HTTP 429) and deliberately not repairable — see the 30-day rule below. `The Beatles -
Rubber Soul` returns four offers through both the CLI and the web UI, all domestic, which is
the correct answer for a mainstream record.

**Search providers are no longer picked — they are merged.** `from_env` now builds *every*
engine this machine can run and wraps them in `MergedSearch`, which asks them concurrently
and interleaves the results round-robin. This replaced a precedence list, and the change
matters: configuring SearXNG used to *replace* DuckDuckGo, silently costing you the records
only DuckDuckGo could see.

| Provider | Account | Rate limit | In the merge when |
| --- | --- | --- | --- |
| SearXNG (self-hosted) | none | none — it's yours | `GROOVE_SEARXNG_URL` is set |
| Serper | free tier | 2.5k once | `SERPER_API_KEY` is set |
| Brave | free tier | 2k/month | `BRAVE_API_KEY` / `GROOVE_SEARCH_KEY` is set |
| **DuckDuckGo** | none | throttles fast | always — it needs no account |

**Four adapters, but a dozen engines** — because SearXNG is itself a front end onto
many. `docker-compose.yml` and `searxng/settings.yml` now live in the repo (item 1 of
the old next-steps list, done), and the mounted settings enable Bing, Yahoo, Yandex,
Seznam, Yep, Qwant, Mojeek, Startpage, Google, DuckDuckGo and Brave. Measured on one
query the day it was written: 85 results from 7 engines, against 20 from one before.
Adding an engine is now a line in a YAML file, not a class.

**Every engine states its own case.** `EngineReport` (in `domain.py`) carries a name, a
hit count and a note; `SearchProvider.reports()` returns one per engine, SearXNG returns
one per *upstream* engine including its `unresponsive_engines`, and `MergedSearch`
concatenates them. It reaches the user three ways: `engines:` under each album in the
CLI, indented rows under "web search" in the web trace, and a `via <engine>` badge on
every open-web offer. This was worth doing because a merged search shows a union, and a
union hides the difference between "nobody sells this" and "Google captcha'd, Brave
suspended us and DuckDuckGo was throttled".

`GROOVE_SEARCH_PROVIDER` still pins the set by name and now accepts a list
(`searxng`, or `searxng,duckduckgo`); `none` switches the open web off. Pinning one engine
is how you tell that engine's blind spot from a record nobody sells.

Three things about the merge worth not rediscovering: a failing engine cannot take down a
working one (exceptions are contained, and `last_error` is only set when *every* engine came
back empty, so a throttled DuckDuckGo never prints "rate-limiting us" beside good SearXNG
results); a straggler is abandoned after `MergedSearch.patience` seconds, because a throttled
DuckDuckGo sleeps 15s before each call and then returns nothing anyway and would otherwise
pace the whole search; and each keyed engine now reads its own key, since building Brave and
Serper together could previously hand one the other's.

Only DuckDuckGo and SearXNG have been exercised against a live engine. **The Brave and
Serper code paths have never run against a real key** — the user could not complete Brave's
registration, which is why the keyless providers exist at all. The merge itself was verified
live against two SearXNG instances configured with different engine sets: 2 hits + 12 hits
came back as 14, the exact union, alternating, nothing dropped.

Live registry lives in `var/registry.json` (gitignored), built from
`groove_search/data/seeds/pl.yaml`. It now also carries the `shipping` table; an older
registry without that key loads on defaults. Page cache in `var/cache/` — delete it to
force fresh fetches.

**The SearXNG container is now `docker compose up -d`**, bound to 127.0.0.1:8888, with its
engine list in `searxng/settings.yml` mounted from the repo. The old hand-configured
container has been removed. Nothing depends on it — without `GROOVE_SEARXNG_URL` the app
searches through DuckDuckGo alone — but with it, the open web sees roughly four times as
many candidates.

## Decisions worth not re-litigating

- **Shops are data, not code.** A shop is a `SearchRecipe` executed by one runner. Adding or
  repairing a shop never adds a module. This is the core bet of the design; keep it.
- **Calibration is heuristic, not LLM.** Chosen deliberately: no API key, deterministic,
  re-runnable offline. It infers structure from repeated DOM signatures around price nodes.
- **Matching is strict on purpose.** A wrong "best price" is worse than a missing one. When
  tuning, prefer false negatives. Thresholds live at the top of `matching.py`.
- **A recipe younger than 30 days is never re-learned** (`Registry.repair_blocked`,
  `RECALIBRATE_AFTER`). A shop that broke inside its own recipe's lifetime is being refused
  by the shop, not misread; re-probing burns minutes of live requests and deepens the block.
  Such a shop is reported broken and assumed dead until the window passes; `--force`
  overrides. It gates only the *repair* path — deliberately re-learning a still-working shop
  (the answer to a redesign) must stay available, or the web Re-learn button is dead for a
  month after every setup.
- **There are two sources, not one.** Calibrated shop recipes *and* the open web
  (`openweb.py`): search engine → candidate URLs → fetch each page → `product.extract_product`
  → the same strict `matching.score_offer`. The recipe engine was never going to reach a
  record no Polish generalist stocks; this is what lifted that ceiling. Both feed one
  ranking, and a shop already in the registry is excluded from the web path so it cannot
  appear twice.
- **Ranking is on delivered cost, not listed price** (`shipping.py`). Non-negotiable once
  foreign shops are in play: $20 in Boston lands at ~218 PLN after postage and 23% import
  VAT, losing to a 90 PLN copy in Warsaw. Domestic postage is costed too, so the comparison
  is like-for-like. An unknown origin is costed as worst case *on purpose* - guessing cheap
  would let an unidentified foreign shop win on a domestic estimate.
- **Coverage ranks shops; it no longer excludes them.** A specialist that stocks none of the
  mainstream benchmark may still be the only source for a niche record, so any shop that
  calibrates is adopted and simply ranked low.
- **Every engine is asked, not the best one.** Engines differ far more in coverage than in
  ranking, so picking one makes "is this record for sale" depend on which engine happens to
  be configured. Merging costs no extra page fetches — `openweb` still opens the same small
  budget of candidates, just chosen from a wider pool.
- **The reader is told which source found an offer** (`Offer.from_open_web`, and the "How
  this was searched" panel in the web UI). A calibrated shop missing a record it should
  stock is a broken recipe; the open web missing one is engine coverage. They are different
  faults with different fixes, and a result that does not say which cannot be diagnosed.

- **A streaming library is an input, not a third source.** It produces `Artist - Title`
  lines that go through `normalize_lines` and the unchanged `search_albums`, so nothing
  downstream knows streaming exists and no ranking, matching or reporting code had to
  change. Resisting the pull to make it a source is what kept this feature small.
- **Ticking albums fills the search box; it never starts a search.** The user still reads
  the lines, edits them and presses the button. An import that searched on its own would
  spend a DuckDuckGo throttle budget and minutes of live fetches on albums nobody chose.
- **The titles keep their edition noise on purpose.** Spotify hands back "In Rainbows
  (Deluxe Edition)"; cleaning it here would fold differently from the shop's side, which
  is finding 15 all over again. `text.significant()` already strips it from both sides.
- **One redirect URI, not one per surface.** The consent page redirects to a one-shot
  loopback listener rather than into the web app, so the CLI and `groove serve` share a
  single registered URI. The cost is a page that has to poll until the listener catches
  the code; the benefit is one line to register per service instead of two.

## Hard-won findings (do not rediscover these)

1. **A "search" endpoint that ignores the query is the main trap.** Several shops answer any
   query with the same catalogue grid; a recipe learned from one calibrates perfectly and
   then matches albums at random. `calibration._ignores_query` catches this by searching for
   nonsense and comparing result sets. Combat Rock's `/?s=` is the live example.
2. **Price nodes usually have no class of their own** (`<span>45,00 zł</span>` inside
   `div.price`). `_nearest_signature` climbs to the closest classed ancestor; without it
   most shops yield no price selector at all.
3. **Never accept a fallback price without an explicit currency** — a footer's "2024" parses
   as a 2024 zł price otherwise.
4. **A block's first anchor is often the artist or label**, not the product. Prefer the title
   node's anchor and reject taxonomy paths (`_NON_PRODUCT_PATH` in `recipes.py`).
5. **A 404 on a search URL means "no results"**, not a broken shop; several shops do this.
6. **Probe genres matter.** An all-rock probe set makes a hip-hop shop look broken. Probes
   live in `calibration.DEFAULT_PROBES`, the ranking basket in `discovery.BENCHMARK`.
7. **Polish shop engines commonly use POST search forms and path-style search**
   (IdoSell: `/pl/searchquery/{query}`). Both are handled; keep them.
8. **A product page is far easier to read than a search grid**, because Google Shopping
   rewards structured markup. Strategies in `product.py` run most-trustworthy first:
   JSON-LD → microdata → OpenGraph → embedded JSON → visible text. Measured on five live
   shops: 2 had machine-readable JSON, 1 visible text only, 1 nothing, 1 Cloudflare 403.
   All five strategies earn their place and `None` is a normal answer.
9. **"Cena netto" is a live trap.** Polish shops print the ex-VAT price beside the real
   one; taking it undercuts the truth by the VAT rate and invents an offer the shop never
   made. The label is a *sibling* of the amount, so the amount's own text is clean - it is
   only caught by adjacency (`_NET_LABEL_WINDOW` in `product.py`). A blanket "is 'netto'
   anywhere above?" test throws away the real price too; that was tried and it fails.
10. **A related-products rail can render before the product's own block.** VinylMusic
   leads with U2 and George Harrison before the album you asked for, so document order
   picks the wrong price. Extraction anchors on the node holding the title and climbs
   until an ancestor has a price. VinylMusic has no `<h1>` at all, which is why the
   anchor falls back to finding the title *text*.
11. **A release page sells more than the record.** Bandcamp's JSON-LD is one MusicAlbum
   holding several Products - download first at $8, the LP at $20, then a hat and two
   t-shirts. Taking the first quotes the download; taking the cheapest sells a t-shirt.
   `_ranked_products` prefers a physical `musicReleaseFormat`, and the artist has to be
   lifted from the parent's `byArtist` or the matcher rejects the release as a different
   artist. Digital carriers are now in `_WRONG_CARRIER` - a download is always cheaper
   than the record, so without that gate it wins every comparison it enters.
12. **Playwright must reuse one browser.** Launching Chromium per request made calibration
   unusable. It also defeats some 403 bot-blocks (Juno calibrates via browser, not HTTP).
13. **A gate written as a substring test silently deletes records.** `_MERCH` and
   `_WRONG_CARRIER` were matched with `word in folded`, so "book" inside *Bookends*, "pin"
   inside *Pink* and "mc" as a token in *MC Solaar* each rejected a real album as
   merchandise or a cassette. `Simon & Garfunkel - Bookends` returned nothing while four
   shops had it in stock — **three of them in the registry**, so this was suppressing the
   recipe path too, not just the open web. `matching._mentions` now matches runs of whole
   tokens (folding splits "t-shirt" into two, so phrases must be sequences). "mc" was
   dropped from the carrier list outright: it is a whole token in a great many artist
   names, and shops that sell cassettes say "kaseta" or "cassette".
14. **Shops keep the artist in a field of its own, and the matcher only sees the title.**
   Mr Bongo names the product "Akhenaten – Vinyl LP" and files the performer under
   `itemprop="brand"`; the offer was in stock at $34.99 and was thrown away as "different
   artist". schema.org's `byArtist` was already folded into the title for JSON-LD release
   pages — `product._page_artist` now does the same for every strategy, reading
   `byArtist` → `music:musician` → `brand` → `author` → `product:brand` → Shopify's
   `vendor`. The page's `<h1>` is deliberately *not* in that list: it is the artist on some
   shops and the shop's own name on others, with nothing in the markup to tell them apart.
   Adding an artist can only help — the strict title gate still has to pass separately — so
   a page that names its label rather than its performer costs nothing.
15. **Both sides of a comparison must fold identically.** `matching` scores against
   `significant()`, which strips edition noise, but `normalize_query` kept the user's words
   intact — so typing "Burial - Untrue EP" asked for an `ep` token no offer could ever
   supply, scored 50% on a two-word title and was rejected as a different record, while
   "Burial - Untrue" matched. `text.py`'s own docstring warns about exactly this. A title
   that is *entirely* edition noise keeps its raw words, because an empty token set matches
   every record there is.
16. **Allegro cannot be scraped — it has to be the API.** Its `robots.txt` permits the pages
   that matter (`/offer/` is disallowed but the Polish `/oferta/` and `/produkt/` forms are
   not), and yet a product page answers **403 to plain HTTP *and* to headless Chromium** —
   so the browser trick that rescued Juno does not help. Engines return Allegro constantly,
   but almost always as `/listing?string=...` or `/kategoria/...?string=...` search pages,
   which `_is_listing` had to be taught to reject (neither the path nor a `q` parameter gave
   them away). Same class as Discogs: robots says yes, bot protection says no.

17. **Allegro is closed, and now we know at which door.** Not scrapeable (403 to HTTP and
   to headless Chromium, finding 16) *and* not reachable by API: a personal app
   registered at apps.developer.allegro.pl gets a valid `client_credentials` token -
   `GET /sale/categories` answers 200 - but **both** offer-search resources answer 403
   `AccessDenied`: `/offers/listing` and `/sale/products`. The token, the required
   `User-Agent` (Allegro issues one per app, e.g. `groove-search/1.0 (+example.com)`) and
   the `Accept: application/vnd.allegro.public.v1+json` header were all correct - the
   categories call proves it. Offer search is a partner/affiliate grant, so do not spend
   another session building an Allegro adapter; the only remaining route is the affiliate
   programme. **Do not delete this finding: registering the app takes ten minutes and
   looks like it should work right up to the 403.**
18. **Ceneo is the way to Polish marketplace prices.** `ceneo.pl` product pages
   (`/<id>`) publish JSON-LD, are allowed by robots (only its *search* paths are
   disallowed), read cleanly through the existing open-web path, and match at full
   confidence with the right carrier - `Daft Punk - Discovery (CD)` at 42.40 PLN,
   `Radiohead - In Rainbows (Winyl)` at 112.80. It aggregates Allegro's own sellers
   among others, so it recovers most of what Allegro's block costs. Nothing was built
   for it: it simply stopped being crowded out once domestic hosts got the fetch budget
   first. Note it is a comparison site - its price is the cheapest across shops, and the
   buyer clicks through - which is why the host name is shown as-is rather than dressed
   up as a shop.
19. **Amazon was never blocked; it was being filtered and outranked.** Product pages
   answer 200 to the plain HTTP fetcher and `product.extract_product` reads them via
   visible text (`Bookends` at 68.35 PLN). Two things kept them out: `_is_listing` let
   Amazon's `/s?rh=...` and `/clp/...` grids through (a "price" from whichever record
   sat first) while `/dp/` was not recognised as a product path, and the eight-page
   budget was spent before Amazon's rank. Both are fixed - `_GRID_PATH`, the `rh`,
   `_nkw` and `field-keywords` parameters, `/dp/` and `/gp/product` in `_PRODUCT_PATH`.
   Amazon also localises to PLN for a Polish visitor, so the currency says nothing about
   origin; `_HOST_COUNTRY` maps `amazon.com` to US. eBay, Discogs and Bandcamp are
   deliberately *not* in that map - their sellers are worldwide, and an invented country
   shown as a fact is worse than an honest blank costed as worst case.
20. **Scraping search engines directly is a dead end; SearXNG is the way in.** Tried live
   from this machine: Mojeek captcha, Startpage "Blocked", Ecosia 403 firewall, Yep
   Cloudflare 403, Brave HTML 429, and Bing 200 *but serving a generic degraded page that
   ignores the query* - the sly one, since it looks like success. Marginalia answers
   honestly but indexes non-commercial sites by design, so it is useless for shopping.
   Every one of those engines is reachable through SearXNG, which handles their quirks
   server-side. Expanding coverage belongs in `searxng/settings.yml`, not in new adapter
   classes.
21. **The fetch budget should go domestic first.** Postage and 23% import VAT add ~100 PLN
   to a non-EU parcel, so a foreign candidate has to be far cheaper to win on delivered
   cost. Spending eight page-fetches on foreign shops while a Polish one sits at rank
   nine buys pages that were never going to place. `_spread` now stable-sorts hits whose
   host resolves to the buyer's country to the front; engine order is otherwise intact.
   This, not any new source, is what let Ceneo and muziker.pl start winning.

22. **Spotify's redirect URI must be the `127.0.0.1` literal.** `localhost` is refused at
   *registration* time, which is the good case; the bad case is registering something that
   does not match the `redirect_uri` sent at both the authorize *and* the token call, where
   the symptom is `invalid_grant` one step after the actual mistake. Same for the PKCE
   challenge: base64url with the padding stripped, or the failure again surfaces at the
   token call. The RFC 7636 vector is pinned in `tests/test_oauth.py` and was checked
   against `openssl` rather than memory — a recalled vector was wrong in its last
   character, which is exactly the kind of error this class of bug hides behind.
23. **TIDAL's collection API is written from thin documentation and is UNVERIFIED.**
   `developer.tidal.com` and `tidal-music.github.io/tidal-api-reference` are both
   JavaScript-rendered, so neither could be read; the shape used here
   (`GET /v2/users/me`, then `/v2/userCollections/{id}/relationships/albums?include=albums,albums.artists`,
   `Accept: application/vnd.api+json`, scopes `user.read collection.read`, cursor in
   `links.next`) comes from the maintainers' own GitHub discussions plus a third-party Go
   client. Collection read access rolled out recently and reportedly covers albums,
   artists and playlists but not tracks. **Every base URL is an env override
   (`GROOVE_TIDAL_API`, `GROOVE_TIDAL_AUTHORIZE`, `GROOVE_TIDAL_TOKEN`) precisely so
   correcting it is configuration, not code.** First live run should record what is
   actually true here.
24. **A pending login kept `groove serve` alive for its full timeout on Ctrl-C.** The
   login was a FastAPI `BackgroundTask`, and uvicorn waits for those at shutdown, so a
   five-minute consent window looked exactly like a hang. It is an `asyncio.Task` held on
   the `LoginJob` now, cancelled on shutdown, on sign-out, and by a second Connect click
   (which would otherwise fail to bind the loopback port the first one still held).

## What a next session should probably do first

1. ~~**Make SearXNG durable, or drop it.**~~ Done — `docker-compose.yml` plus
   `searxng/settings.yml`, both in the repo, both mounted.
2. **Run the TIDAL path against a real account, once there is one.** Finding 23 says why:
   the endpoints are the best reading of documentation that could not be read. Spotify
   needs no such caveat. If TIDAL turns out to be wrong, the fix is almost certainly a URL
   in the environment rather than a change in `library.py`.
3. **Discogs is the remaining real source, and Allegro is not.** Finding 17 closed the
   Allegro API route for good: a personal app is refused offer search, so stop there
   unless you join the affiliate programme. **Discogs** still has a free token, is
   exactly the catalogue for the records this struggles with, and its pages answer
   Cloudflare 403 to the open-web path — so an adapter producing `Offer`s directly is
   the only way in. `openweb.py` has proven that seam. Note that `search_albums` calls
   `_search_open_web` directly — when you add the *second* API source, that is the moment
   to turn the hardcoded sources into a small registry of adapters, not before.
4. **Watch what the leads panel teaches you.** Every blocked-but-matching seller is now
   named per search (`AlbumResult.leads`). If one host keeps appearing, that is the
   evidence for which adapter to write next — it is a measurement rather than a guess,
   which is how Discogs earned its place on this list.
5. **Leave VoiceShop alone** until its recipe is 30 days old (it is the only broken shop).
   If you want it back sooner, the real fix is an adaptive per-host delay, not recalibration.
6. **Consider per-session search state.** `web/app.py` now holds a `SearchJob` global beside
   the setup one, so only one search runs at a time and a second submission is told so
   rather than silently shown the first's progress. Fine for one local user; wrong for
   anything else.

## Known gaps / next steps

- **The open-web source is proven live, keyless.** Verified results worth trusting as a
  baseline: Pet Fox → Bandcamp's LP at $20, 217.87 PLN delivered; Whitest Boy Alive →
  musicbundles.com at 183.38 PLN delivered; Abase → an eBay CD at 222.91 PLN delivered.
  Note all three winners are foreign and all three are dominated by postage and VAT, which
  is exactly why the shipping model had to exist before this source was any use.
- **Results vary by engine, more than expected.** The same album can come back found through
  DuckDuckGo and empty through SearXNG. This is what `MergedSearch` exists for, but it does
  not make the variance go away: do not treat one empty result as proof a record is
  unavailable, and do not tune matching thresholds on a single provider's output. The
  per-engine reporting added this session makes the variance visible rather than smaller —
  a Bookends search showed google cse 7, bing 4, yep 3, qwant 3, seznam 2, yandex 2,
  yahoo 1, with brave suspended and duckduckgo captcha'd, all in one search.
- **Leads are not offers and must not become them.** A `Lead` has no price, is never
  costed, never ranked and never "best". The temptation will be to read a price out of an
  engine snippet for Allegro — it is often right there in the text. Do not: snippets are
  stale, frequently show "od 49 zł" for a listing rather than the record, and a wrong best
  price is the one failure this app is built to avoid.
- **DuckDuckGo throttles after roughly ten searches in quick succession**, and it signals
  this with HTTP *202* plus an "anomaly" notice rather than a 429 - so the status code alone
  reads as success and the result silently looks like "nothing is for sale". `_is_throttled`
  sniffs the body; the reason then reaches the user through `ShopReport.error`. If you add
  another scraped engine, assume it does something equally sly.
- **Self-hosted SearXNG is the upgrade that keeps the "no account" property**
  (`GROOVE_SEARXNG_URL`). Public instances do not work: of four tried, one returned HTML
  instead of JSON, one 403, one 429 and one would not connect. Nearly all disable the JSON
  format, so this only pays off if you run the container yourself.
- **Shipping rates are rough round numbers**, not quotes: per-zone, per-carrier, in
  `registry.json`. They are good enough to order offers correctly, which is their job.
  Free-shipping thresholds beyond the domestic one are not modelled, and neither is the
  EU's €150 duty threshold (only VAT).
- **Bandcamp is solved, but not by calibration.** Its *search* carries no prices, which is
  why a recipe could never work; its *album page* carries full JSON-LD, which the open-web
  path reads. Verified live: Pet Fox returns the $20 LP, in stock, correctly formatted.
- **Discogs is still the obvious next source.** Its product pages answer the open-web path
  with a Cloudflare 403, so scraping will not reach it — but it has a real API, and the
  second-adapter seam that the open-web source proved out is exactly where it belongs.
- **The streaming tokens live in `var/tokens/<service>.json`, mode 0600.** `var/` was
  already gitignored for the registry and the page cache; unlike those, these grant access
  to somebody's account. A refresh token can be *rotated* by either service, and the store
  keeps the new one — dropping it silently signs the user out a week later, which is the
  kind of bug that gets blamed on the service.
- **Spotify does not document the order of `/me/albums`**, so "the last 20 I added" is
  sorted here by `added_at` rather than trusted. TIDAL's collection carries `addedAt` in
  the relationship's `meta`; when it is absent the API's own order is kept rather than an
  invented one.
- **FX rates are a static table** in `registry.json`.
- `web/app.py` keeps the setup job *and* the search job in module-level globals — fine for
  one local user, wrong for anything multi-user. The search job is what drives live
  per-source progress: `search_albums` takes an `on_progress(query, report)` callback fired
  the moment each source finishes (the same convention `discover` uses), the page polls
  `/search/progress`, and a callback that raises is swallowed because a reporting bug must
  not cost a search that otherwise worked.
- **The open web is essentially all of a search's wall clock.** The seven shops answer in a
  few seconds; the open web then runs two engine queries and up to eight live page fetches.
  Before the progress list existed this looked like the whole app hanging, which is most of
  why it was built.
- **Juno is flaky, not impossible.** Calibrated on its own via the browser it produces good
  selectors (`div.dv-item.dv-item-music` / `a.text-md` / `span.price_lrg.text-cta`, endpoint
  `/search/?hide_forthcoming=0&q[all][]={query}`), but under setup's concurrency the browser
  pass times out and it is skipped. Lower `BrowserFetcher.max_concurrency` or raise its
  timeout if you want it in. Note its search answers unknown queries with *artist* pages,
  which `_NON_PRODUCT_PATH` now filters, so it still yields nothing for the three test albums.
- Shops that resist: Juno/Decks (403 on HTTP, Juno works via browser), Bonito (429),
  Rush Hour (search path robots-disallowed — we obey), DVDMax/Empik (JS-only, browser retry
  still found no blocks), Gandalf (calibrates cleanly but is a bookstore: 0% music coverage).

## Things fixed late — do not regress them

- **`RobotsGate` is shared by the HTTP *and* browser adapters.** The browser path originally
  skipped robots.txt, which quietly admitted Rush Hour (whose search path is disallowed).
  A headless browser is still a robot; there are tests for this.
- **Fetchers must be closed** (`close_fetcher`). Without it Playwright's subprocess dangles
  and `groove setup` hangs after printing its summary.
- **Titles are tidied** in `recipes._tidy_title`, and `<script>`/`<style>` bodies are stripped
  before extraction — otherwise inline jQuery ends up inside a product title.
- **A shop that localises its prices is a trap for origin inference.** Reading "PLN" as
  "ships from Poland" gave a Los Angeles shop free domestic delivery and no import VAT -
  the exact error the shipping model exists to prevent. `_CURRENCY_COUNTRY` therefore has
  no entry for the destination's own currency: a currency guess may make an offer dearer,
  never cheaper, and an unknown origin is costed as worst case.
- **Engines return listing pages, and they price whatever is on them.**
  `us.rarevinyl.com/collections/the-whitest-boy-alive` yielded a "price" of 18,200 USD, and
  Amazon answers `/whitest-boy-alive/s?k=...`, whose *path* looks like a product. `_is_listing`
  rejects taxonomy paths (unless a product segment overrides, as on Shopify) and any URL
  carrying a search parameter. Both were live false positives, not hypotheticals.
- **Cloudflare 403s are normal on the open web**, not a bug to chase: Norman Records and
  Discogs both answer that way. They are reported per candidate rather than swallowed, so
  an empty result says *why* it was empty.
- **`savings_vs_worst` is a share of the priciest offer** (0–100%). It previously reported the
  markup, which printed nonsense like "232% below".

## Gotchas

- `groove setup` is slow *by design* (rate limiting + browser retries): 10–25 minutes.
  Use `--no-browser` for a fast HTTP-only pass while iterating.
- Run it unbuffered (`python -u -m groove_search.cli setup`) if you want live progress in a
  log file; `print` block-buffers when stdout is not a tty.
- `playwright install chromium` — *without* `--with-deps`, which needs sudo.
- The disk cache can mask changes during calibration work; clear `var/cache/<host>/`.
- **Live-testing the open web is self-limiting.** Roughly ten DuckDuckGo searches in quick
  succession earn a throttle that outlasts "a few minutes" — it was still refusing 20+
  minutes later. Budget for that when iterating: use SearXNG, or `--no-web`, or the fake
  provider in tests. Every live check in this session cost 1–4 minutes of wall clock.
- **`GROOVE_SEARCH_PROVIDER=none`** turns the open web off entirely, which is the quickest
  way to tell a shop-recipe bug from an open-web one. Naming a single engine
  (`GROOVE_SEARCH_PROVIDER=searxng`) is the quickest way to tell one engine's blind spot
  from a record nobody sells.
- **DuckDuckGo is now in every merged search**, so its throttle budget is spent faster than
  it used to be — roughly two searches per album. It degrades quietly (empty results, an
  error only if *every* engine came back empty), so a search that suddenly finds less may
  just be DuckDuckGo cooling off rather than anything you changed.
- **"Not available" has three distinct meanings** and the result says which: nothing
  matched, candidate pages were found but blocked (403 — Discogs, Boomkat, Juno, Allegro
  all do this), or it matched and was sold out. `Abase - Awakening` is the third kind:
  Bandcamp matches it at full confidence and says Sold Out.
- **Do not `pkill -f` on a pattern that appears in your own command line** — it kills the
  tool's shell (exit 144). The bracket trick does *not* save you when the pattern is piped
  into `xargs kill` from the same command line; this was hit again on 2026-09-20. Find the
  PID first (`ss -lntp | grep <port>`), then kill that number alone.
- **A `serve` process does not pick up code edits** (uvicorn runs without `--reload`).
  Restart it after changing anything under `groove_search/`, or you will test stale code.
  **Worse than stale: it 500s.** Jinja re-reads templates from disk on every request while
  the Python module stays as it was loaded, so a template that uses a filter or a context
  variable added in the same change explodes with `no filter named ...` in a process that
  predates it. Hit live on 2026-09-20: a server started at 16:59 served Internal Server
  Error on `/` for edits made at 17:20. The symptom points at the template, the cause is
  the process. Check `ps -p <pid> -o lstart=` against the file's mtime before debugging
  anything else.
- VoiceShop starts returning 429 under a multi-album search and gets marked broken; the
  per-host delay (`HttpFetcher.delay`, 1s) is too aggressive for it. An adaptive per-host
  delay that widens after a 429 is the obvious next improvement — and it is now the *only*
  route back for VoiceShop, since the cooldown deliberately stops us re-calibrating it.
  Confirmed on 2026-09-20 that its 429 covers even the homepage, so recalibration could not
  have succeeded anyway; the cooldown just stops us paying ten minutes to learn that.
