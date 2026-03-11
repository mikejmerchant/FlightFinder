# Changelog

All notable changes to the AI Flight Finder Suite are documented here.

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
