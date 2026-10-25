# website-watcher

Search several websites for items that match **required** and **optional** criteria and
print a ranked table in the terminal. It started as a used-car finder (Tesla Model Y), but
the engine is generic: sites, item types and criteria are plug-ins and config.

## Setup

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
```

The Tesla source also needs Google Chrome installed (it drives a real Chrome window for a few
seconds; see the Tesla row under "Sites"). The Docker image includes it.

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

# Check Messenger notifications (see "Notifications")
.venv\Scripts\python -m watcher notify ids    # who has messaged your page, with their IDs
.venv\Scripts\python -m watcher notify test   # send a test message
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
| `notify` | send a message about top matches (see "Notifications") |

Rule syntax: `field: value` (equals), `field: [a, b]` (one of), or
`field: {op: value, weight: n}` with ops `eq ne in not_in lt lte gt gte contains regex exists`.
String comparisons ignore case.

### Dealer or private

Car listings get a `seller_type` of `dealer` or `private`, so `seller_type: dealer` under
`required` keeps dealer cars only. Melbourne's Cheapest Cars (and any DealerStudio site),
Cars24, Tesla, Drive and EV Depot only sell dealer stock, so every listing from them is `dealer`.
carsales and Autotrader mix both, and each listing's type comes from the site's own flag. A
listing whose site doesn't say has no `seller_type` and fails the rule.

## Notifications

A profile with a `notify` block sends a message after each run (terminal or the web page's
**Run now**) about matches that score full marks on every optional rule, or at least
`min_score` if given:

```yaml
notify:
  channels: [messenger]
  min_score: 5   # optional
```

Each car is announced once (the `notifications` table records what was sent), with every site
it is listed on in the same message. If a channel isn't set up yet, the run says so and sends
nothing, and those matches are sent on the first run after it is.

**Messenger** sends from a Facebook Page to you through Meta's Send API. Copy `.env.example`
to `.env` (gitignored) and fill in:

1. `MESSENGER_PAGE_TOKEN`: create a Meta app (developers.facebook.com, type Business), add the
   Messenger product, connect your page and generate a Page access token. A development-mode
   app can message anyone with a role on the app, which covers messaging yourself.
2. `MESSENGER_RECIPIENT_ID`: send your page a message from your own Messenger, then run
   `watcher notify ids` and copy your ID.
3. `watcher notify test` to check it.

Meta only delivers ordinary page messages within 24 hours of your last message to the page.
Notifications go out untagged, and if Meta says that window has closed they are retried with
the `ACCOUNT_UPDATE` tag (`MESSENGER_TAG` changes it; empty turns the retry off). A new app
can't use tags until Meta approves it in App Review, so until then message the page now and
then (replying to an alert counts). A refused message isn't recorded, so it is sent again on
the next run after you do.

**Refresh by message**: while `watcher web` is running it checks the page's conversation every
30 seconds (`MESSENGER_POLL_SECONDS`; 0 turns it off). Any message you send the page starts a
run of every profile that notifies through Messenger, which also reopens the 24-hour window.
It replies when it starts, sends any new top matches, then a summary of each profile's run: its match counts, then each site's result
(✅ how many listings were fetched and matched, or ❌ why the fetch failed).
This polls Meta's API instead of using a webhook, so the PC needs no public address.

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
| `notifications` | which listings each notify channel has already sent |
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
- The summary tiles are shortcuts: **New**, **Price drops** and **Lowest price** narrow the list
  to those cars (tap again to undo), and the count tile clears every filter.
- Click a listing for its price history, every site it is on, how it scores against each
  rule and every stored field.
- **Run now** does a `watcher run` in the background and refreshes when it finishes.

There is no login: anyone on your network can open it and start runs. Use `--host 127.0.0.1`
to keep it to this PC, and `--port` to change the port. Photos come from each source's
`image()` method and load straight from the sites.

## Unraid

The web interface runs as a Docker container on the Unraid server (`192.168.0.2`), at
`http://192.168.0.2:8000`. It also runs every profile at 06:00, 12:00 and 18:00 Melbourne time
(`WATCHER_SCHEDULE`, or `watcher web --schedule` anywhere else), as well as on **Run now** and on
Messenger messages. After each scheduled refresh it messages the same summary (matches, how many
are new, and each site's fetched count or failure) to every profile's notify channels, after any new top matches.

```powershell
./deploy.ps1          # ship the last commit: build on the server, recreate the container
./deploy.ps1 -Logs    # follow the container's output
./deploy.ps1 -CopyDb  # also overwrite the server's database with data/watcher.db
```

`deploy.ps1` uploads `git archive HEAD` (so commit first; uncommitted changes are left out) and
your `.env` over SSH (key in `~/.ssh/id_ed25519`, added under Users > root in Unraid), then
runs `unraid/install.sh` on the server. That builds the image from the `Dockerfile` and recreates
the container. It also installs `unraid/website-watcher.xml` as a template, so the container
shows in the Docker tab with a WebUI link and starts with the array. Profiles are baked into
the image, so a profile change needs a deploy too. On the server:

| path | contents |
| --- | --- |
| `/mnt/user/appdata/website-watcher/data` | `watcher.db` |
| `/mnt/user/appdata/website-watcher/.env` | Messenger keys (copied from the PC on each deploy) |
| `/mnt/user/appdata/website-watcher/src` | the deployed code |

Only one copy should run `watcher web`: if the PC runs it too, each Messenger message starts two
runs. The image includes Google Chrome and Xvfb (a virtual screen) for the Tesla source, and the
container runs with `--init` so Chrome's processes are cleaned up after each fetch.

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
    notify.py      pick top matches after a run and send them to the profile's channels
    http.py        shared requests session + bot-wall detection
    nextjs.py      extract data from Next.js pages (RSC rows, __NEXT_DATA__)
    investigate.py site probe used by `watcher investigate`
  web/           `watcher web`: JSON API (server.py) + a no-build page (static/)
  domains/       one file per item type (cars.py normalises state, status, km...)
  sources/       one file per website (auto-discovered via @register_source)
  notifiers/     one file per message channel (auto-discovered via @register_channel)
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
| Tesla | Page and `inventory/api/v4/inventory-results` return Akamai 403/429 to scripts (requests, and curl_cffi with any impersonation, even with a passing browser's cookies), and headless Chrome is denied too. A normal Chrome passes, so the source starts one with `nodriver` (on Xvfb when there's no display, e.g. in Docker), opens the inventory page and calls the API from inside it (~10 s). If that fails it falls back to `data/tesla-snapshot.json` (captured by hand with `watcher/browser/tesla_capture.js`) and flags it when stale (`max_age_hours`, default 24). Prices are per registration state; the API rejects a `RegistrationProvince` option | working |
| carsales | `__NEXT_DATA__` `ListingCard` objects (title, odometer, price, seller/state). Captcha is intermittent, so the request is retried up to 5x with a fresh session. Colour comes from re-running the search once per base colour with carsales' `Colour.<name>.` filter (~12 extra requests; `source_options: carsales: {colours: false}` or a list to limit); only page 1 is read | working |
| Drive | Path-based search (`/cars-for-sale/search/<state>/all/<make>/<model>/`, `/page/<n>/` for more) with `__NEXT_DATA__` `listings.results` (make, model, badge, colour, odometer, price, dealer, state). Cloudflare 403s python-requests by TLS fingerprint, so the source uses `curl_cffi` impersonating Chrome. Dealer stock only | working |
| Autotrader | Nuxt 3 site whose `__NUXT_DATA__` comes from a public JSON API (`listings.platform.autotrader.com.au/api/v3/search?make=&model=&state=`, `paginate` <= 50, `last_page` for more), which the source calls directly. Like Drive, python-requests gets 403, so `curl_cffi` is used. Has colour, odometer, variant, dealer and suburb | working |
| EV Depot | Server-rendered Next.js; `/inventory?q=<make model>&page=<n>` gives the car links (12 per page), and each car's page has a schema.org `Car` JSON-LD block (badge, year, VIN, colour, odometer, price, availability). One dealership in Seaford VIC | working |
