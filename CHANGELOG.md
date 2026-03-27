# Changelog

All notable changes to the AI Flight Finder Suite are documented here.

## [2.0.0] — 2026-03-27

### Added
- `FlightFinderConnections.py` — new hub airport analyser. Reads the search store and produces an interactive Leaflet.js map showing airports where all travellers have direct flights, coloured by arrival/departure sync gap. Circles = outbound hubs, squares = inbound/return hubs, grey markers = solo airports (ruled out). Click any pin for a per-date breakdown of best pairings.
- `time_preferences.py` — new shared module for time-of-day scoring. Encodes warn/bad thresholds for all four legs (outbound depart, outbound arrive, inbound depart, inbound arrive). Used by both FlightFinderFriends and reanalyse. Handles both 12h (AM/PM) and 24h time formats.
- `search_store.py` — persistent search result database. Saves every search as a named JSON file in `~/.flightfinder/searches/`. Enables cache reuse, reanalysis, and connection mapping across sessions.
- `reanalyse.py` — reanalysis tool for stored searches. Pure-Python filtering and re-sorting without re-scraping. Supports sort by price, arrival gap, departure gap, or score; direct-only filter; price range; per-traveller airport constraints; and time-of-day preferences.
- Time-of-day scoring in `FlightFinderFriends` — query language like "sociable hours", "avoid before 7am", "no airport hotel" activates graduated penalties (£40 warn / £80 bad per leg per traveller). Claude interprets thresholds from the query.
- `direct_only` mode — "direct flights only" in the query sets a flag preserved through the full pipeline. The feasibility check now detects when no direct flights exist on sample routes and prompts yes/no before falling back to indirect.
- Stop penalty in scoring — each stop adds £50 to a trip's score so direct flights are preferred even at slightly higher price.
- Direct-aware flight pruning — when the search space is capped, up to 3 direct flights per traveller per route are always preserved before indirect flights fill remaining slots.
- `--travellers` filter for `FlightFinderConnections` — restrict hub analysis to named travellers, e.g. `--travellers Mike Charlie`.
- `--debug-airport` flag for `FlightFinderConnections` — dumps every stored flight involving a given IATA code with stops value, useful for diagnosing unexpected hub results.
- Staleness warnings in `FlightFinderConnections` map — pairings based on data older than 7 days show a yellow banner and per-row age label. Configurable via `STALE_DAYS` constant.
- Flight deduplication in `FlightFinderConnections` — identical flights scraped across multiple searches are collapsed to one entry (keeping the freshest), preventing inflated gap counts.
- Per-date pairing table in hub map popups — clicking a hub pin shows each date where both travellers have direct flights, with arrival times, gap, and total price per row.
- Route-level cache reuse — `FlightFinderFriends` checks the store for each individual route before scraping; reuses any result fresher than `--max-age` days.
- `--yes` / `-y` flag for non-interactive confirmation prompts.
- Bike fee auto-detection — queries containing "bike", "bicycle", "cycling", or "bikepacking" automatically activate bike fee lookup without needing `--bike`.

### Changed
- `FlightFinderFriends` feasibility check now returns `(feasible, params)` tuple so `direct_only` can be cleared if the user chooses to continue without the restriction.
- `FlightFinderConnections` popup route display fixed — inbound legs now correctly show `origin → home` rather than `origin → origin`.
- Time parser updated across all tools to handle Google Flights' 12h AM/PM format (`7:55 PM`) in addition to 24h format.
- `FlightFinderConnections` now plots all airports from the store, not just hubs — solo airports (only one traveller has direct flights) appear as small grey markers so ruled-out options are visible.

### Fixed
- `FlightFinderConnections` traveller filter — inner flight dict field was double-checked against the filter, causing all flights to be silently dropped. Now uses the dict key as canonical traveller name.
- `FlightFinderConnections` hub detection — `_time_to_mins` returned -1 for AM/PM times, so all arrival gaps were -1 and every pairing was skipped.
- `FlightFinderFriends` direct flight prioritisation — cheap indirect flights were crowding out direct options during the combinatorics pruning step. Direct flights now get reserved slots.

## [1.0.0] — 2026-03-11

### Added
- `flight_finder.py` — standard one-way/return flight search with natural language queries
- `FlightFinderAdvanced.py` — open-jaw trip search (fly into one city, return from another)
- `FlightFinderFriends.py` — group travel optimiser, minimising arrival/departure time gaps across travellers flying from different cities
- `pdf_export.py` — shared PDF generation module producing print-ready reports with flight cards, AI summary, booking links, traveller colour-coding, and sync gap badges
- AI-powered query interpretation via Claude (claude-sonnet) — expands vague destinations, parses flexible dates, infers cabin class and budget
- AI-generated prose summary after each search — highlights patterns, cheapest days, honest comparisons against what was requested
- Live Google Flights scraping via Playwright/Chromium — real prices, not cached data
- No-results detection — cleanly skips routes with no Google Flights data rather than returning phantom results
- Sync scoring system (Friends mode) — configurable cost-per-hour penalty for arrival/departure gaps
- `--pdf` flag on all three tools for PDF export
- `--debug` flag for HTML inspection and visible browser mode
- `--interactive` / `-i` mode for prompted input
- Rich progress bar with elapsed time during searches
- Per-flight Google Flights booking URLs in all output
