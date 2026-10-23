# website-watcher

Search several websites for items that match **required** and **optional** criteria and
print a ranked table in the terminal. It started as a used-car finder (Tesla Model Y), but
the engine is generic: sites, item types and criteria are plug-ins and config.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

## Usage

```powershell
# Run a search profile
.venv\Scripts\python -m watcher run profiles\tesla-model-y-lr.yaml
.venv\Scripts\python -m watcher run profiles\tesla-model-y-lr.yaml -v      # also show rejected + why
.venv\Scripts\python -m watcher run profiles\tesla-model-y-lr.yaml --json  # machine-readable
.venv\Scripts\python -m watcher run profiles\tesla-model-y-lr.yaml --no-save  # don't record in the database

# Re-apply a profile's rules to everything ever found (no scraping)
.venv\Scripts\python -m watcher db profiles\tesla-model-y-lr.yaml

# Browse the database in a web page (phone-friendly; see "Web interface")
.venv\Scripts\python -m watcher web

# Investigate how sites expose their data (bot walls, embedded JSON, API endpoints)
.venv\Scripts\python -m watcher investigate --profile profiles\tesla-model-y-lr.yaml --deep
.venv\Scripts\python -m watcher investigate https://example.com/search --item-keys price title

# List available sources
.venv\Scripts\python -m watcher sources
```

## Profiles

A profile (YAML in `profiles/`) says what to search for, where, and how to judge results:

| key | purpose |
| --- | --- |
| `domain` | item type that normalises fields (`cars`, `generic`) |
| `sources` | site plug-ins to query |
| `search` | hints passed to every source (make, model, state, ...) |
| `source_options` | per-site extras, keyed by source name |
| `aliases` | regexes that map messy values onto one canonical value |
| `required` | rules that must all pass |
| `optional` | rules that add `weight` (default 1) to the score |
| `display`, `sort` | table columns and ordering (`-score`, `price`, ...) |

Rule syntax: `field: value` (equals), `field: [a, b]` (one of), or
`field: {op: value, weight: n}` with ops `eq ne in not_in lt lte gt gte contains regex exists`.
String comparisons ignore case.

## Duplicates

The same item is often listed on several sites. Matches from different sites whose domain's
`dedupe_keys` all agree are shown as one row, listing every source and link and the earliest
`found` date. For cars the keys are colour, year and odometer; a listing missing any of them is
never merged. The database still keeps one row per site listing.

## Database

Every `run` records **all** fetched listings, including the ones the rules reject, in
`data/watcher.db` (SQLite, gitignored; use `--db` to pick another file). The filters only
decide what is shown. The results table gains a `found` column with the date a listing was
first seen, marks listings that are new this run, and shows a price as
`$52,500 (was $54,000)` when it has changed since then.

| table | contents |
| --- | --- |
| `listings` | latest fields/raw JSON per (profile, source, id), whether it last passed, `first_seen`, `last_seen` |
| `price_history` | a row each time a listing's price is first seen or changes |
| `runs`, `run_sources` | when each run happened and how each source did |

`watcher db` re-judges the stored listings against the profile's *current* rules and
aliases, so you can tighten or loosen a filter without scraping again.

When a run fetches a source successfully and a stored listing is no longer there, the listing
is assumed **sold**: its `status` is set to `sold` in the database, so a `status: available`
rule hides it from both `run` and `db` (it still appears under `-v`). A blocked or failing
source never marks its listings sold, and a relisted car gets its real status back on the next
run. History is keyed by the profile's `name`, so
renaming a profile starts a fresh history.

## Web interface

`watcher web` serves a page for browsing the database on your PC and on your phone (open the
`http://<pc-ip>:8000` address it prints while on the same Wi-Fi). It serves every profile in
`profiles/` unless you name some, and re-reads the YAML on each refresh.

- **Profile matches** (the default) shows what the terminal shows: listings passing the
  profile's `required` rules, ranked by its `sort`, duplicates merged, with the rules listed
  above the results.
- **Everything stored** shows every listing, including sold ones and ones that fail the rules
  (each card says which rule it misses).
- Filters for every field (ranges for numbers, choices for the rest), search, sorting, and a
  card or table layout narrow down either view. They are only a view - the YAML is never
  changed - and they live in the URL, so a filtered view can be bookmarked.
- Click a listing for its price history, every site it is on, how it scores against each
  rule and every stored field.
- **Run now** does a `watcher run` in the background and refreshes when it finishes.

There is no login: anyone on your network can open it and start runs. Use `--host 127.0.0.1`
to keep it to this PC, and `--port` to change the port. Photos come from each source's
`image()` method and load straight from the sites.

## Layout

```
watcher/
  core/          generic engine - knows nothing about cars or specific sites
    models.py      Listing, FetchContext, SourceBlocked/SourceNotImplemented
    criteria.py    required/optional rule engine
    profile.py     YAML profile loading + aliases
    runner.py      fetch all sources in parallel, normalise, filter, rank
    output.py      rich terminal tables / JSON
    store.py       SQLite history of everything found
    http.py        shared requests session + bot-wall detection
    nextjs.py      extract data from Next.js pages (RSC rows, __NEXT_DATA__)
    investigate.py site probe used by `watcher investigate`
  web/           `watcher web`: JSON API (server.py) + a no-build page (static/)
  domains/       one file per item type (cars.py normalises state, status, km...)
  sources/       one file per website (auto-discovered via @register_source)
profiles/        search definitions
```

### Adding a site

Create `watcher/sources/<site>.py`, subclass `Source`, set `name` / `domain`, implement
`search_urls()` and `fetch()` returning `Listing`s with normalised `fields`, and decorate it
with `@register_source`. Optionally implement `image(raw)` to give the web page a photo. Start by running `watcher investigate <url> --deep` to see what is
available.

### Adding a new kind of item

Create `watcher/domains/<thing>.py` with a `Domain` subclass (field normalisation, default
columns), then point a new profile's `domain:` at it.

## Site findings (Oct 2026)

| site | approach | status |
| --- | --- | --- |
| Melbourne's Cheapest Cars | DealerStudio platform. Public Meilisearch API (`search4.dealerstudio.com.au/multi-search`, scoped tenant token read from site JS); falls back to the Next.js RSC page data | working |
| Cars24 | Next.js RSC page data on `/buy-used-<make>-<model>/`. The JSON API (`listing-service-au.c24.tech`) returns 401 without browser session auth | working |
| Tesla | Page and `inventory/api/v4/inventory-results` return Akamai 403 to scripts, but the page's own same-origin `fetch` works in real Chrome. Data is captured with Claude-in-Chrome (`watcher/browser/tesla_capture.js` run on a tesla.com tab) into `data/tesla-snapshot.json`, which the source reads and flags when stale (`max_age_hours`, default 24). Prices are per registration state; the API rejects a `RegistrationProvince` option | working (needs a snapshot refresh) |
| carsales | `__NEXT_DATA__` `ListingCard` objects (title, odometer, price, seller/state). Captcha is intermittent, so the request is retried up to 5x with a fresh session. Colour comes from re-running the search once per base colour with carsales' `Colour.<name>.` filter (~12 extra requests; `source_options: carsales: {colours: false}` or a list to limit); only page 1 is read | working |
| Drive | Path-based search (`/cars-for-sale/search/<state>/all/<make>/<model>/`, `/page/<n>/` for more) with `__NEXT_DATA__` `listings.results` (make, model, badge, colour, odometer, price, dealer, state). Cloudflare 403s python-requests by TLS fingerprint, so the source uses `curl_cffi` impersonating Chrome. Dealer stock only | working |
| Autotrader | Nuxt 3 site whose `__NUXT_DATA__` comes from a public JSON API (`listings.platform.autotrader.com.au/api/v3/search?make=&model=&state=`, `paginate` <= 50, `last_page` for more), which the source calls directly. Like Drive, python-requests gets 403, so `curl_cffi` is used. Has colour, odometer, variant, dealer and suburb | working |
