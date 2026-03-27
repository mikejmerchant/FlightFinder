# ✈️ AI Flight Finder Suite

> **Find the cheapest flights for groups, in plain English.**
> Powered by [Claude AI](https://anthropic.com) + live Google Flights scraping.

---

## What Is This?

A suite of Python command-line tools that let you search for flights exactly how you'd describe them to a travel agent — no forms, no dropdowns, no fiddling with dates. Claude interprets your request, Playwright scrapes live Google Flights prices, and the results are ranked by a combination of cost and synchronisation quality.

Built for **group travel** where people fly from different cities and need to arrive (and leave) at roughly the same time.

---

## Tools

### `FlightFinderFriends.py` — Group travel optimiser
The main tool. Friends flying from different home cities to a shared destination.

```bash
python FlightFinderFriends.py "Charlie from Exeter and Mike from Manchester want to fly
to Nice on 10 Aug, return from Rome on 17 Aug. Direct flights only. Sociable hours."
```

Finds every combination of direct flights, scores them by total cost + arrival/departure sync gap + time-of-day preferences, and ranks them. Outputs a full trip breakdown with booking links, optional PDF, and an AI prose summary.

**Key features:**
- Natural language query interpretation via Claude
- Live Google Flights scraping (real prices, not cached)
- Sync scoring — configurable penalty per hour of arrival/departure gap
- Direct flights prioritised in search and scoring
- Time-of-day penalties — "sociable hours", "avoid before 7am", "no airport hotel"
- Feasibility check before full search — detects impossible routes early
- Resume/checkpoint — interrupted searches can be continued
- Persistent search store — results saved to `~/.flightfinder/searches/`
- Bike fee lookup with `--bike` flag (or auto-detected from query)
- PDF export with `--pdf`

### `FlightFinderConnections.py` — Hub airport map
Reads your saved searches and produces an interactive map showing which airports work as hubs — places where all travellers can arrive (or depart) on direct flights with good time sync.

```bash
python FlightFinderConnections.py --travellers Mike Charlie --open
```

**Map legend:**
- 🟢 Circle = outbound hub (arrival sync) — green is tight, red is poor
- 🟧 Square = inbound hub (departure sync)
- ⬜ Grey = solo airport (only one traveller has direct flights — ruled out)

Click any pin for a per-date breakdown of best pairings. Stale data (>7 days old) is flagged with a warning banner.

```bash
# Diagnose why an airport appears or doesn't:
python FlightFinderConnections.py --debug-airport FCO
```

### `reanalyse.py` — Re-sort without re-scraping
Re-filter and re-rank a saved search using plain English, without hitting Google Flights again.

```bash
python FlightFinderFriends.py --reanalyse search_011 "show only direct flights under £600, sociable hours"
python FlightFinderFriends.py --reanalyse search_011 "Charlie returns via LGW"
python FlightFinderFriends.py --list-searches
```

Supports: sort by price/sync/score, direct-only, price range, per-traveller airport filters, arrival/departure spread limits, and time-of-day preferences.

### `FlightFinderAdvanced.py` — Open-jaw trips
Fly into one city, return from another (e.g. start in Nice, end in Rome after a cycling trip).

```bash
python FlightFinderAdvanced.py "Fly from London to Nice on 10 Aug, return from Rome on 17 Aug"
```

### `flight_finder.py` — Single traveller
Standard one-way or return search for one person.

```bash
python flight_finder.py "Cheapest flights from Manchester to Barcelona in September"
```

---

## Install

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/ai-flight-finder.git
cd ai-flight-finder

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Install the Playwright browser
playwright install chromium

# 4. Set your Anthropic API key
export ANTHROPIC_API_KEY="sk-ant-..."
# Or pass it per-run: --api-key sk-ant-...
```

---

## Requirements

| Package | Purpose |
|---------|---------|
| `anthropic` | Claude API — query interpretation & AI summaries |
| `playwright` | Headless browser for live Google Flights scraping |
| `rich` | Progress bar & styled terminal output |
| `reportlab` | PDF export |

Python 3.10+ required (uses `match` statements and `|` union types).

---

## CLI Flags — FlightFinderFriends

| Flag | Description |
|------|-------------|
| `--api-key KEY` | Anthropic API key (or set `ANTHROPIC_API_KEY`) |
| `--bike` | Look up bicycle transport fees per airline |
| `--pdf [FILE]` | Export results to PDF |
| `--debug` | Save HTML snapshots; show browser window |
| `--top N` | Show top N results (default: 5) |
| `--max-age DAYS` | Re-scrape if cached result is older than N days (default: 7) |
| `--no-resume` | Ignore checkpoint, start fresh |
| `--no-feasibility` | Skip feasibility check |
| `--reanalyse REF [INSTRUCTION]` | Re-analyse a saved search |
| `--list-searches` | List all saved searches and exit |
| `--yes` / `-y` | Auto-confirm prompts |
| `--interactive` / `-i` | Prompt for query interactively |

---

## How Scoring Works

```
Score = ticket prices
      + (arrival spread hours  × sync_penalty_per_hour)   ← outbound
      + (departure spread hours × sync_penalty_per_hour)  ← inbound
      + (stops per leg × £50)                              ← prefer direct
      + time-of-day penalties per leg per traveller        ← sociable hours
```

`sync_penalty_per_hour` defaults to £10/hr and is tunable from the query:
- "minimise time gaps" → £25/hr
- "cheapest above all" → £3/hr

Time-of-day penalties default to £40 (warn) / £80 (bad) per leg and are only active when the query mentions time preferences.

---

## File Structure

```
├── FlightFinderFriends.py     # Group travel optimiser — main tool
├── FlightFinderConnections.py # Hub airport map from saved searches
├── FlightFinderAdvanced.py    # Open-jaw trip search
├── flight_finder.py           # Single traveller search
├── reanalyse.py               # Re-filter saved searches without re-scraping
├── search_store.py            # Persistent search result database
├── time_preferences.py        # Shared time-of-day scoring module
├── pdf_export.py              # Shared PDF generation
├── bike_fees.py               # Airline bicycle fee lookup
├── requirements.txt           # Python dependencies
├── LICENSE                    # MIT
└── README.md                  # This file
```

Saved searches are stored in `~/.flightfinder/searches/` as JSON files.

---

## Notes

- **Scraping etiquette** — the tools include polite delays between requests; please don't remove them
- **Prices change** — results reflect what Google Flights shows at scrape time; always verify before booking
- **Direct flights** — use "direct flights only" in your query; the feasibility check will warn you if none exist
- **API key security** — never hardcode your key; use the environment variable or `--api-key` flag

---

## Contributing

Pull requests welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

Areas that would benefit from contributions:
- Support for additional currencies
- More airline bike fee data in `bike_fees.py`
- Multi-city itineraries (more than two legs)
- GUI or web interface

---

## Licence

MIT — see [LICENSE](LICENSE)

---

*Built with [Claude](https://anthropic.com) by Anthropic and [Playwright](https://playwright.dev) by Microsoft.*
