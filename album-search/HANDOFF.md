# groove-search — session handoff

Status as of 2026-09-20 (third session). Read `README.md` first for what the app is and how the pieces fit;
this file is only what a next session needs that the code does not already say.

## Where things stand

Working end to end: `groove setup` → `groove search` → `groove serve`, plus `doctor` and
`recalibrate`. 185 tests pass (`.venv/bin/python -m pytest`), all offline — the fake fetcher
and `FakeSearch` model a real query-sensitive shop and a real engine, so nothing in the
suite touches the network.

**The headline change this session: three bugs in the matcher were making real records
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

**A SearXNG container is running on this machine** as `groove-searxng` (port 8888), left up
deliberately so the next session can use it. Two things to know: its JSON format was enabled
by appending to `/etc/searxng/settings.yml` *inside the container*, so recreating the
container loses it (mount a settings file to make it durable), and nothing depends on the
container — without `GROOVE_SEARXNG_URL` the app simply searches through DuckDuckGo alone.

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

## What a next session should probably do first

1. **Make SearXNG durable, or drop it.** It is the only keyless provider that does not
   throttle, and right now it depends on a hand-edited file inside a container. A
   `docker-compose.yml` with a mounted `settings.yml` in the repo would make it a real
   option rather than a demo. This is now the most valuable item on the list: the merge
   always includes DuckDuckGo, so without SearXNG every search spends DuckDuckGo's throttle
   budget.
2. **A real source, not another engine.** Merging engines (done this session) was the cheap
   win and it is spent. The next step change is an adapter that produces `Offer`s directly,
   which `openweb.py` has already proven as a seam. **Discogs** is the cheapest — a free
   token, and the obvious source for exactly the records this struggles with. **Allegro** is
   the highest value for a Polish buyer: the dominant domestic marketplace, huge second-hand
   stock, and *domestic*, so its offers would land without the postage-and-VAT penalty that
   makes every open-web winner so far expensive. Both need their APIs; neither can be
   scraped (finding 16). Note that `search_albums` calls `_search_open_web` directly — when
   you add the *second* API source, that is the moment to turn the two hardcoded sources
   into a small registry of adapters, not before.
3. **Leave VoiceShop alone** until its recipe is 30 days old (it is the only broken shop).
   If you want it back sooner, the real fix is an adaptive per-host delay, not recalibration.
4. **Consider per-session search state.** `web/app.py` now holds a `SearchJob` global beside
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
  unavailable, and do not tune matching thresholds on a single provider's output.
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
- VoiceShop starts returning 429 under a multi-album search and gets marked broken; the
  per-host delay (`HttpFetcher.delay`, 1s) is too aggressive for it. An adaptive per-host
  delay that widens after a 429 is the obvious next improvement — and it is now the *only*
  route back for VoiceShop, since the cooldown deliberately stops us re-calibrating it.
  Confirmed on 2026-09-20 that its 429 covers even the homepage, so recalibration could not
  have succeeded anyway; the cooldown just stops us paying ten minutes to learn that.
