# groove-search

Find the best price on a list of albums across online record shops — CDs and vinyl.

You paste a list of albums. groove-search normalizes each line, asks every shop in its
registry, decides which returned products are really that album, and shows you the
cheapest offer with a few alternatives beside it so you can see how much better the
winner is. Every result links straight to the shop's product page, ready for the basket.

```
Pet Fox - A face in your life
Abase - Awakening
Whitest Boy Alive
```

## The idea: shops are data, not code

There is no hand-written scraper per shop. A shop is a **recipe** — a search URL template
and a handful of CSS selectors — and one deterministic runner executes every recipe the
same way. Recipes are *learned*, and can be re-learned whenever a shop redesigns.

```
setup ──► discovery ──► calibration ──► registry.json ──► search ──► results
          rank shops    learn one       shops+recipes     run the     best price
          by measured   shop's search   +health           recipes     + alternatives
          supply        structure
```

### Setup builds the registry

`groove setup` takes the candidate shops for a location (`groove_search/data/seeds/pl.yaml`),
and for each one:

1. **Finds the search endpoint** — reads the shop's own search form (GET or POST), falling
   back to the URL shapes used by WooCommerce, PrestaShop, Magento, OpenCart, Shopify,
   IdoSell, Shoper and friends.
2. **Infers the result structure** — finds every node whose own text is a price, walks up to
   the repeating ancestor that wraps price + link + title, and turns that repetition into
   CSS selectors. No API key, no LLM, fully deterministic.
3. **Proves the endpoint honours the query** — a nonsense search must *not* return the same
   products as a real one. This rejects shops whose "search" quietly falls back to a
   catalogue page, which would otherwise match albums at random.
4. **Measures supply** — asks for a benchmark basket of albums spanning genres and records
   the share it can actually sell.

Shops are then ranked by measured coverage and the best ten are adopted. Nothing about the
list is hand-curated: a dead or unscrapeable shop drops out on its own.

### Search uses only the frozen recipes

Searching re-infers nothing. It fills each recipe's URL template, extracts offers, and puts
every candidate through `matching`, which is deliberately strict — it rejects t-shirts,
cassettes, the wrong artist, the wrong album and the wrong format, because a wrong "best
price" is worse than a missing one.

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/pip install playwright && .venv/bin/playwright install chromium   # optional
```

Playwright is optional but recommended: shops that render results in JavaScript — or that
refuse plain HTTP clients — are retried in a real browser during setup, and only those
shops pay the cost during searches.

## Use

```bash
groove setup                      # build the registry (a few minutes; be patient by design)
groove search "Pet Fox - A face in your life" "Abase - Awakening"
groove search --file albums.txt
groove search --no-web "..."      # calibrated shops only, skip the open web
groove search --shop-price "..."  # rank on the listed price, not delivered cost
groove doctor                     # per-shop health
groove recalibrate                # re-learn every broken shop worth re-learning
groove recalibrate voiceshop      # re-learn one
groove recalibrate voiceshop --force   # ...even if its recipe is younger than 30 days
groove serve                      # web UI on http://127.0.0.1:8000
```

The web UI does everything the CLI does: run setup with live progress, search a pasted
list, and re-learn a shop with one button when its recipe breaks.

### Writing album lines

| You type | It understands |
| --- | --- |
| `Pet Fox - A face in your life` | artist + album |
| `Awakening by Abase` | artist + album |
| `Whitest Boy Alive` | artist only — matches anything they released |
| `Abase - Awakening (vinyl)` | vinyl only; `(CD)` likewise |
| `-> Björk - Homogénic` | list markers and diacritics are handled |

## Two sources, one seam

Shops are searched two ways, and both feed the same ranking.

**Calibrated shops.** The registry's shops, each with a learned recipe for its own
search grid. Fast and precise, but limited to shops that were seeded and that
calibrate — which in practice means domestic generalists.

**The open web.** A search engine is asked where an album is sold; each candidate page
is then opened and read. This reaches shops nobody seeded, which is the only way to
find a record the domestic generalists do not stock. Nothing is trusted on the way in:
a hit becomes an offer only when the page yields a price *with a currency* and the same
strict matcher used by the shop path agrees the page is really that record. Streaming,
review and lyrics sites are never even fetched.

It works out of the box with no account at all, and there are sturdier options. Every
engine you configure is **asked at once** and the results interleaved — engines differ far
more in coverage than in ranking, so the same record comes back found through one and empty
through another, and adding an engine must not cost you what the previous one could see:

| Provider | Account | Rate limit | How |
| --- | --- | --- | --- |
| **DuckDuckGo** (always) | none | throttles after ~10 searches | nothing to do |
| **SearXNG** | none | none — it's your instance | `export GROOVE_SEARXNG_URL=http://127.0.0.1:8888` |
| **Brave Search** | free tier | 2k/month | `export GROOVE_SEARCH_KEY=...` |
| **Serper** | free tier | 2.5k once | `export SERPER_API_KEY=...` |

Merging costs no extra page fetches — the same small budget of candidate pages is opened,
just chosen from a wider pool — and one engine failing never takes down another. DuckDuckGo
needs nothing but will rate-limit a heavy session; when it does, it says so rather than
pretending the record is unavailable. To run SearXNG yourself:

```bash
docker run -d -p 8888:8080 \
  -e SEARXNG_SETTINGS__SEARCH__FORMATS='["html","json"]' searxng/searxng
export GROOVE_SEARXNG_URL=http://127.0.0.1:8888
```

Public SearXNG instances are not a substitute — nearly all disable the JSON format.
`GROOVE_SEARCH_PROVIDER` pins which engines are used instead of all of them — one name, or
several (`searxng,duckduckgo`); `none` switches the open web off entirely. Pinning a single
engine is the quickest way to tell that engine's blind spot from a record nobody sells.

Every offer says which source found it — a calibrated shop or the open web — and each
result carries a "How this was searched" breakdown of what every source did, because a shop
missing a record it should stock and the open web missing one are different faults.

Adding an engine is a class implementing `search()` in `websearch.py`, not a refactor.
The test suite stays fully offline via `FakeSearch`.

## Delivered cost, not listed price

Once foreign shops are in play, the listed price is the wrong number to compare. A
record at $20 in Boston is not cheaper than one at 90 PLN in Warsaw: add transatlantic
postage and 23% import VAT and it lands at about 218 PLN. So every offer — domestic
ones included — is ranked on its **delivered cost**: price + postage + any import tax,
converted to one currency.

Every figure is an estimate and is labelled as one. Postage is a small per-zone table
in `registry.json` (`shipping`), so correcting it is editing data, not code. Rank on
the listed price instead with `--shop-price`.

## Repairing a shop

Shops redesign and recipes go stale. Every search records per-shop health; after three
consecutive failures a shop is marked **broken**, and `groove doctor` (or the web UI) says
so. `groove recalibrate <shop>` re-runs the learning against the live page and replaces the
stored recipe. The search logic itself never changes — only the data it runs on.

**A recipe younger than 30 days is not re-learned.** If a shop broke within the lifetime of
its own recipe, the recipe is almost never the reason — the shop is refusing us (rate limit,
bot block, outage). Re-learning costs minutes of live requests and usually deepens the block,
so such a shop is reported as broken and simply assumed not to be working; `doctor` and the
web UI say why, and `--force` overrides it. The window lives in `RECALIBRATE_AFTER`
(`registry.py`). This only holds back the *repair* path: re-learning a shop that still works
— the usual answer to a redesign — is never blocked.

## Adding a shop

Add it to `groove_search/data/seeds/pl.yaml` and re-run setup:

```yaml
  - id: myshop
    name: My Shop
    base_url: https://myshop.pl
    search_hint: /szukaj?q={query}   # optional: skip endpoint guessing
    needs_browser: false             # optional: force the Playwright path
    currency: PLN                    # optional: if not the location default
```

`search_hint` exists so you can teach the app an endpoint it could not find, without
touching any code.

## Being a good citizen

`robots.txt` is obeyed, requests are rate-limited per host with a real User-Agent, `429`
and `503` are backed off with `Retry-After` honoured, and pages are cached on disk so
repeated runs do not re-hammer a shop. Some shops disallow their search path or block
non-browser clients; those simply drop out of the registry.

## Layout

| Module | Responsibility |
| --- | --- |
| `domain.py` | The shared vocabulary: `AlbumQuery`, `Offer`, `AlbumResult`, `Money`, `Shop` |
| `text.py` | Folding shared by normalization and matching (diacritics, edition noise) |
| `normalize.py` | One user line → an `AlbumQuery` with ordered search terms |
| `pricing.py` | `129,99 zł`, `1 299,00 zł`, `$21.98` → `Money`; cross-currency comparison |
| `matching.py` | Is this scraped product really that album? The strict gatekeeper |
| `fetching.py` | The one real seam: HTTP / browser / caching / routing / fake adapters |
| `recipes.py` | `SearchRecipe` + the deterministic runner; extraction is pure |
| `calibration.py` | Learns a shop's search endpoint and result structure |
| `discovery.py` | Ranks candidate shops by measured supply; browser retry |
| `registry.py` | Shops + recipes + health, persisted as one JSON file |
| `search.py` | Fan-out, per-shop term retries, best-price assembly |
| `web/`, `cli.py` | Thin adapters over the above |

Run the tests with `.venv/bin/python -m pytest`.

## Known limits

- **Prices are read from search-result pages.** A shop that only shows a price on the
  product page cannot be compared, and a shop that hides prices from search (Bandcamp)
  cannot be calibrated at all.
- **Currency conversion uses a static table** in `registry.json` (`rates`), used only to
  rank offers across currencies. Displayed prices are always the shop's own. Edit the
  table if the rates drift.
- **Shipping is not included** in the comparison — only the item price.
- **Some shops will never calibrate**: hard bot protection (Juno and Decks over plain
  HTTP, Bonito's 429s), a robots-disallowed search path (Rush Hour), or a JS-only endpoint
  with no HTML fallback (DVDMax).
