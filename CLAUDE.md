# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
export ANTHROPIC_API_KEY="sk-ant-..."   # or pass --api-key per run
```

Python 3.10+ required (uses `match` statements and `|` union types).

## Running the tools

This is a CLI suite — no test runner, no build step. Validation is done by running real queries (the `--debug` flag dumps scraped HTML to `/tmp/` and opens a visible browser).

```bash
# Group trip optimiser (main tool)
python FlightFinderFriends.py "Charlie from Exeter and Mike from Manchester to Nice 10 Aug, return 17 Aug. Direct only."

# Re-filter a saved search without re-scraping
python FlightFinderFriends.py --reanalyse 003 "show cheapest under £600"
python FlightFinderFriends.py --list-searches

# Hub airport map from saved searches
python FlightFinderConnections.py --travellers Mike Charlie --open
python FlightFinderConnections.py --debug-airport FCO    # diagnose why an airport appears or doesn't

# Other entry points
python FlightFinderAdvanced.py "..."   # open-jaw (fly into one city, return from another)
python flight_finder.py "..."          # single traveller
```

Common flags on `FlightFinderFriends.py`: `--top N`, `--debug`, `--no-resume` (ignore checkpoint), `--no-feasibility`, `--max-age DAYS` (cache TTL, default 7), `--bike`, `--pdf [FILE]`, `--yes`/`-y`.

## Architecture

### The pipeline (FlightFinderFriends.py)

1. **Interpret** — `interpret_with_claude()` turns the natural-language query into a `FriendsSearchParams` dataclass (travellers + home airports, dates with flexibility, cabin, `direct_only`, `time_prefs`, `sync_penalty_per_hour`).
2. **Feasibility check** — sample-scrape one route per traveller before the full search; if `direct_only` is set and no direct flights exist, prompt the user. Returns `(feasible, params)` so `direct_only` may be cleared.
3. **Scrape** — `scrape_google_flights()` drives Playwright/Chromium against `google.com/travel/flights`. Selectors are based on `aria-label` and `data-gs` attributes (NOT class names — those are build-hash-generated and change every Google deploy). When the scraper breaks, fix the selectors here and use `--debug` to inspect the dumped HTML.
4. **Combine + score** — `build_and_rank_trips()` enumerates outbound × inbound combinations grouped by shared destination/origin, scoring each `GroupTrip` with `score_group_trip()`:
   ```
   score = sum(leg prices)
         + arrival_spread_hours  × sync_penalty_per_hour   # outbound
         + departure_spread_hours × sync_penalty_per_hour  # inbound
         + (stops per leg × £50)                           # prefer direct
         + time-of-day penalties (£40 warn / £80 bad per leg per traveller)
   ```
   `sync_penalty_per_hour` defaults to £10/hr, tuned from query language ("minimise time gaps" → £25, "cheapest above all" → £3). Direct flights get reserved slots during pruning so cheap indirect options can't crowd them out.
5. **Persist** — every search is saved to `~/.flightfinder/searches/search_NNN_<slug>.json` via `search_store.save()`. The record contains both `raw_results` (full flight dicts) and `trips_digest` (lean version used by `reanalyse.py` and `FlightFinderConnections.py`).
6. **Summarise** — `summarise_with_claude()` writes the final prose summary from the digest.

### Cross-tool shared modules

- **`search_store.py`** — the persistent JSON store. Single source of truth for cache reuse, reanalysis, and hub-map input. `find_matching(key_hash)` enables route-level cache reuse: each individual route is checked before scraping, so partial cache hits work.
- **`time_preferences.py`** — shared time-of-day scoring (warn/bad thresholds for all four legs). Used by both `FlightFinderFriends.py` and `reanalyse.py`. Handles both 12h (AM/PM) and 24h time formats — Google Flights uses both.
- **`pdf_export.py`** — shared PDF generation (used by `--pdf` flag across tools).
- **`bike_fees.py`** — airline bicycle fee lookup, auto-activated when query mentions bike/cycling/bikepacking, or via `--bike`.

### `reanalyse.py` is mostly Claude-free

Filtering and sorting of stored results is done in pure Python (`parse_instruction()` → `FilterSpec` → `apply_filter()`). The only Claude call is `_check_new_search_needed()`, which decides whether the instruction asks for data not in the store (different dates, airports not present) and synthesises a fresh query if so. Don't add Claude calls for filter/rank logic — keep it deterministic.

### `FlightFinderConnections.py` reads only the store

Produces an interactive Leaflet.js map (`hub_connections.html`) showing airports where all named travellers have direct flights. Uses only data from `~/.flightfinder/searches/` — never scrapes. Pairings older than `STALE_DAYS` (default 7) are flagged with a yellow banner. When debugging hub detection, `_time_to_mins` must handle AM/PM correctly — past bugs caused all gaps to come back as -1 and every pairing to be skipped.

### Key data models (FlightFinderFriends.py)

- `FlightResult` — single leg. Carries `arrive_minutes`/`depart_minutes` as absolute minutes since 2000-01-01 for cross-day spread calculation.
- `GroupTrip` — one outbound-leg-per-traveller + one inbound-leg-per-traveller combination with `arrival_spread_mins`, `departure_spread_mins`, `score`.
- `FriendsSearchParams` — the interpreted query: travellers, dates, flexibility, `direct_only`, `time_prefs`, `sync_penalty_per_hour`.

## Conventions

- The scraper includes deliberate inter-request delays — don't remove them.
- Search records are append-only; sequential 3-digit IDs (`search_001_...`). `_next_id()` scans existing files.
- `.gitignore` excludes `*.pdf` and `.env` — don't commit generated reports or keys.
