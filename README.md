# ✈️ AI Flight Finder Suite

> **Find the cheapest flights, in plain English.**
> Powered by [Claude AI](https://anthropic.com) + live Google Flights scraping.

---

## What Is This?

A suite of three Python command-line tools that let you search for flights exactly how you'd describe them to a friend. No dropdowns, no date pickers — just type what you want.

```bash
python FlightFinderFriends.py "Charlie lives in Exeter and I live in Manchester.
  We want to fly to the Genoa region in late June for a 7-day cycling holiday,
  then fly home from somewhere in southern Italy."
```

Claude interprets your query → a real browser scrapes Google Flights live → results are ranked, summarised by AI, and optionally exported to a beautiful PDF.

---

## The Three Tools

| Tool | Best For |
|------|----------|
| `flight_finder.py` | Solo or return travel from one city |
| `FlightFinderAdvanced.py` | Open-jaw trips — fly into one city, back from another |
| `FlightFinderFriends.py` | **Groups flying from different home cities to meet up**, minimising arrival time gaps |

---

## How It Works

```
Your plain-English query
        │
        ▼
  🤖  Claude (claude-sonnet)
      ├─ Identifies travellers & home airports
      ├─ Expands vague destinations → nearby IATA codes
      ├─ Parses flexible dates ("late June", "7 days later")
      ├─ Infers cabin class, passengers, budget
      └─ Returns structured search parameters
        │
        ▼
  🌐  Playwright (headless Chromium)
      └─ Scrapes Google Flights live — real prices, right now
        │
        ▼
  📊  Scoring & Ranking
      ├─ Ranked by price (+ stop penalty)
      ├─ Friends mode: scored on cost + arrival/departure sync
      └─ AI summary: patterns, cheapest days, honest caveats
        │
        ▼
  📄  Optional PDF Export
      └─ Print-ready report for sharing via email or WhatsApp
```

---

## Demo Output

```
╔══════════════════════════════════════════════════════════════╗
║   ✈️   Flight Finder Friends  •  Group trip optimiser         ║
╚══════════════════════════════════════════════════════════════╝

📋  Claude's interpretation:
    👤 Mike          home airports: MAN, LPL
    👤 Charlie       home airports: EXT, EXM
    🛬 Shared destinations  : GOA, NCE, MXP
    🛫 Shared return origins: PMO, BRI, CTA, AHO, CAG
    📅 Outbound: 2026-06-26   Return: 2026-07-03

══════════════════════════════════════════════════════════════════
  #1  💰 TOTAL GBP 253  (GBP 126/person)
       🕐 Arrival gap: 45m gap   |   Departure gap: 30m gap
       📊 Score: 262.5  (lower = better balance of cost & sync)

  ── OUTBOUND  (→ NCE) ─────────────────────────────────────────

      👤 Mike          ✈️  easyJet
         MAN → NCE   📅 Fri 26 Jun 2026
         🛫 06:30  →  🛬 09:45   ⏱ 3 hr 15 min   ✈ Direct
         💰 GBP 67

      👤 Charlie       ✈️  Flybe
         EXT → NCE   📅 Fri 26 Jun 2026
         🛫 07:15  →  🛬 10:30   ⏱ 3 hr 15 min   ✈ Direct
         💰 GBP 89

════════════════════════════════════════════════════════════════
  🤖  AI TRAVEL SUMMARY

  The best-value combination for Mike and Charlie is trip #1,
  totalling £253 (£126.50/person). Both fly into Nice (NCE) on
  26 June — Mike direct from Manchester with easyJet at £67,
  and Charlie direct from Exeter at £89. The 45-minute arrival
  gap is very manageable. Charlie's Exeter options are
  consistently £15-25 more expensive than Mike's Manchester
  flights. Flying back on 4 July saves ~£20 if your itinerary
  allows an extra day.
```

---

## Installation

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Get an Anthropic API key

Create a free account at [console.anthropic.com](https://console.anthropic.com) and generate an API key.

### 3. Set your API key

```bash
# macOS / Linux — add to your ~/.zshrc or ~/.bashrc:
export ANTHROPIC_API_KEY="sk-ant-your-key-here"

# Windows (PowerShell):
$env:ANTHROPIC_API_KEY = "sk-ant-your-key-here"
```

### 4. Run!

```bash
python flight_finder.py "Manchester to Barcelona in July, return after a week"
```

---

## Usage

### `flight_finder.py` — Standard search

```bash
# Simple return trip
python flight_finder.py "Fly from Manchester to Barcelona, return after a week in July"

# Flexible dates — Claude searches ±days
python flight_finder.py "Cheapest flight from London to Tokyo any time in March"

# Business class with budget cap
python flight_finder.py "NYC to Singapore business class under £2000 in June"

# Save results as a PDF
python flight_finder.py "Edinburgh to Rome in August" --pdf my_trip.pdf

# Interactive mode
python flight_finder.py --interactive
```

### `FlightFinderAdvanced.py` — Open-jaw trips

```bash
# Fly into one city, back from another
python FlightFinderAdvanced.py "Fly from Manchester to Nice on 26th June,
  return 7 days later from Rome. No flexibility."

# Vague end destination — Claude uses geography
python FlightFinderAdvanced.py "London to Lisbon in July, cycling back,
  fly home from somewhere on the Spanish coast 10 days later"

# With PDF export
python FlightFinderAdvanced.py "MAN to NCE 26 June, return from Rome 3 July" \
  --pdf italy_trip.pdf
```

### `FlightFinderFriends.py` — Group travel

```bash
# Two people, different home cities, open-jaw cycling trip
python FlightFinderFriends.py "Charlie lives in Exeter and I (Mike) live in
  Manchester. We want to fly to the Genoa region in late June for a 7-day
  cycling holiday, then fly home from southern Italy." --pdf cycling_june.pdf

# Three people meeting in one city
python FlightFinderFriends.py "Alice in London, Bob in Edinburgh, Carol in
  Bristol — we all want to meet in Barcelona for a long weekend in July,
  flying home Sunday evening"

# Prioritise sync over cost
python FlightFinderFriends.py "Tom (Glasgow) and Sarah (Bristol) want to fly
  to Lisbon in August — really minimise the arrival time gap"
```

---

## All Options

```
-i, --interactive      Prompt for query interactively
-n, --top N            Show top N results (default: 10)
--pdf [FILENAME]       Export to PDF (optional custom filename)
--debug                Save raw HTML, open browser visibly
--api-key KEY          Pass API key directly
```

---

## What Claude Understands

| You say | Claude does |
|---------|-------------|
| `"near Manchester"` | Adds MAN, LPL |
| `"London airports"` | Adds LHR, LGW, STN, LTN, LCY |
| `"Genoa region"` | Adds GOA, NCE, MXP |
| `"late June"` | Picks a date in the last week of June |
| `"7 days later"` | Calculates return date automatically |
| `"cheapest time in March"` | Sets flexible_days: 7 |
| `"no flexibility"` | Sets flexible_days: 0 |
| `"business class"` | Sets cabin: business |
| `"under £300"` | Sets max_price: 300 |
| `"2 passengers"` | Sets passengers: 2 |
| `"minimise the time gap"` | Raises sync penalty in Friends mode |
| `"cost is the priority"` | Lowers sync penalty in Friends mode |

---

## The Sync Score (Friends Mode)

The Friends tool scores group trips on **cost + synchronisation**:

```
Score = total cost
      + (arrival gap in hours  × penalty per hour)
      + (departure gap in hours × penalty per hour)
```

The penalty defaults to **£10/hour**. Say *"really minimise the gap"* to raise it to ~£25/hr, or *"cheapest above all"* to drop it to ~£3/hr.

---

## PDF Export

Add `--pdf` to any command to generate a print-ready PDF:

- Navy header with your search query (word-wrapped, never cramped)
- AI prose summary at the top
- Flight cards with airline, route, times, stops badge, and price
- Clickable Google Flights booking links on every card
- Traveller colour-coding and sync gap badges (Friends mode)
- Page numbers and footer on every page

```bash
python FlightFinderFriends.py "..." --pdf results.pdf
```

---

## Project Structure

```
flight_finder/
├── flight_finder.py          # Standard single-origin search
├── FlightFinderAdvanced.py   # Open-jaw trip search
├── FlightFinderFriends.py    # Group travel optimiser
├── pdf_export.py             # Shared PDF generation module
├── requirements.txt          # Python dependencies
├── LICENSE                   # MIT
└── README.md                 # This file
```

---

## Requirements

| Package | Purpose |
|---------|---------|
| `anthropic` | Claude API — query interpretation & AI summaries |
| `playwright` | Headless browser for live Google Flights scraping |
| `rich` | Progress bar & styled terminal output |
| `reportlab` | PDF generation (`--pdf` flag) |

Python 3.10+ required.

---

## Notes & Caveats

- **Prices are live** — scraped from Google Flights in real time, not cached or AI-generated
- **Each search opens a real browser** — searches take 1–2 minutes per combination; Friends mode with multiple travellers can take longer, this is expected
- **Google occasionally changes their page structure** — if results look wrong, try `--debug` to inspect the raw HTML
- **No route = no results** — if Google Flights has no flights for a route, the tool logs it and skips it cleanly rather than returning bad data
- **Be respectful** — the tools include polite delays between requests; please don't remove them

---

## Contributing

Pull requests welcome! Areas that would particularly benefit:

- Support for additional currencies
- Caching layer to avoid re-scraping identical routes
- Multi-city itineraries (more than two legs)
- GUI or web interface

---

## Licence

MIT — see [LICENSE](LICENSE). Use freely, modify freely, share freely.

---

*Built with [Claude](https://anthropic.com) by Anthropic and [Playwright](https://playwright.dev) by Microsoft.*
