#!/usr/bin/env python3
"""
✈️  Flight Finder — AI-powered Google Flights search
Uses Claude to interpret natural language queries, then scrapes Google Flights live.
"""

import os
import sys
import json
import time
import argparse
import re
from datetime import datetime, timedelta
from typing import Optional
from dataclasses import dataclass, field

try:
    import anthropic
except ImportError:
    print("❌  Please install: pip install anthropic")
    sys.exit(1)

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
except ImportError:
    print("❌  Please install: pip install playwright && playwright install chromium")
    sys.exit(1)

try:
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
    from rich.console import Console
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

console = Console() if RICH_AVAILABLE else None

try:
    from bike_fees import lookup_bike_fees, attach_bike_fees, format_price_with_bike, BikeFee
    BIKE_AVAILABLE = True
except ImportError:
    BIKE_AVAILABLE = False

try:
    from pdf_export import export_simple as _pdf_export_simple
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False


# ─────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────

@dataclass
class FlightResult:
    airline: str
    origin: str
    destination: str
    depart_date: str       # YYYY-MM-DD
    depart_time: str
    arrive_time: str
    duration: str
    total_travel_time: str    # depart → arrive wall-clock time incl. layovers
    stops: str
    price: str
    currency: str = "GBP"
    co2: str = ""
    score: float = 0.0

    def booking_url(self) -> str:
        """Build a Google Flights search URL pre-filled for this specific flight."""
        q = f"flights+from+{self.origin}+to+{self.destination}+on+{self.depart_date}"
        return f"https://www.google.com/travel/flights?q={q}&curr=GBP&hl=en"

    def display(self, rank: int):
        stops_label = "✈ Direct" if self.stops == "0" else f"↩ {self.stops} stop(s)"
        co2_label = f"  🌿 {self.co2}" if self.co2 else ""
        price_str = f"{self.currency} {self.price}" if self.price else "Price N/A"
        try:
            dt = datetime.strptime(self.depart_date, "%Y-%m-%d")
            date_label = dt.strftime("%a %d %b %Y")
        except ValueError:
            date_label = self.depart_date
        print(f"""
  {'─'*58}
  #{rank}  💰 {price_str}   {stops_label}{co2_label}
      ✈️  {self.airline}
      {self.origin} → {self.destination}   📅 {date_label}
      🛫 {self.depart_time}  →  🛬 {self.arrive_time}   ⏱ {self.total_travel_time if self.total_travel_time else self.duration}{"  (flight: " + self.duration + ")" if self.total_travel_time and self.duration and self.total_travel_time != self.duration else ""}
      🔗 {self.booking_url()}
""")


@dataclass
class SearchParams:
    origins: list[str] = field(default_factory=list)        # IATA codes
    destinations: list[str] = field(default_factory=list)   # IATA codes
    depart_date: str = ""                                     # YYYY-MM-DD
    return_date: str = ""                                     # YYYY-MM-DD or ""
    passengers: int = 1
    cabin: str = "economy"                                    # economy/business/first
    flexible_days: int = 0                                    # ±days around date
    max_price: Optional[int] = None
    reasoning: str = ""                                       # Claude's explanation


# ─────────────────────────────────────────────
# Claude: interpret natural language input
# ─────────────────────────────────────────────

CLAUDE_SYSTEM = """You are a flight search assistant. Your job is to parse a user's natural-language flight request into structured JSON search parameters.

CRITICAL — Airport selection for home/origin cities:
Do NOT simply find the geographically nearest airport. Instead, reason about which airports
people from that location ACTUALLY use in practice for the type of trip described.

For each home location ask yourself: "Which airports do people from [CITY] typically use
for this kind of travel?" Then consider:
- The local airport (may have very limited routes — include but do not rely on it alone)
- Larger regional airports within 1–2 hours' drive that locals commonly prefer
- Major hub airports 2–3 hours away that locals regularly travel to for better connections,
  cheaper fares, or direct international routes unavailable locally
- Rail links to distant hubs (e.g. Exeter → London Paddington → Heathrow is a common
  journey for southwest England residents flying internationally)

Real-world examples of correct reasoning:
- Exeter, international trip → EXT (local, very limited), BRS (Bristol, 1hr drive, far
  better connected), LHR + LGW (London, ~2.5hrs, but Exeter residents routinely use these
  for international flights with no viable local alternative)
- Cambridge → STN (Stansted, 30 min), LHR (1hr), LGW (1.5hrs) — no local airport at all
- Cardiff → CWL (local), BRS (Bristol, 45 min drive, often more routes and cheaper)
- Inverness, long-haul → INV (local, domestic only), ABZ (Aberdeen, 1.5hrs),
  EDI (Edinburgh, 3hrs but used for long-haul connections)
- Norwich → NWI (local, very limited), STN (Stansted, 1.5hrs, commonly used)

Apply the same logic to destinations: expand vague destination descriptions to the airports
people actually fly INTO for that region, not just the closest dot on the map.

Other rules:
- If the user says "cheapest time" or "flexible", set flexible_days to 3–7
- If the user says "no nearby airports" or "exact airport only", respect that strictly
- Map all city/region names to IATA codes
- If no year is given, assume the next upcoming date
- Output ONLY valid JSON, no prose, no markdown fences

Output schema (all fields required):
{
  "origins": ["EXT","BRS","LHR","LGW"],  // all airports this traveller would realistically use
  "destinations": ["GOA","MXP","NCE"],   // airports serving the destination region
  "depart_date": "2024-06-15",           // YYYY-MM-DD
  "return_date": "",                     // YYYY-MM-DD or "" for one-way
  "passengers": 1,
  "cabin": "economy",                    // economy | premium_economy | business | first
  "flexible_days": 0,                    // search ±N days around depart_date
  "max_price": null,                     // integer or null
  "reasoning": "For Exeter: EXT has almost no international routes so included BRS (Bristol, 1hr) and LHR/LGW (London, 2.5hrs) which Exeter residents routinely use for international travel. Expanded Genoa to MXP and NCE as these are the realistic flying-in options for the Ligurian coast..."
}"""

def interpret_with_claude(user_query: str, api_key: str) -> SearchParams:
    """Use Claude to parse the user's natural-language query into SearchParams."""
    client = anthropic.Anthropic(api_key=api_key)

    today = datetime.today().strftime("%Y-%m-%d")
    prompt = f"Today is {today}.\n\nUser request: {user_query}"

    print("\n🤖  Asking Claude to interpret your request...")
    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        system=CLAUDE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    # Strip markdown fences if Claude added them
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"⚠️  Claude returned unexpected output:\n{raw}")
        raise ValueError(f"JSON parse error: {e}")

    params = SearchParams(
        origins=data.get("origins", []),
        destinations=data.get("destinations", []),
        depart_date=data.get("depart_date", ""),
        return_date=data.get("return_date", ""),
        passengers=int(data.get("passengers", 1)),
        cabin=data.get("cabin", "economy"),
        flexible_days=int(data.get("flexible_days", 0)),
        max_price=data.get("max_price"),
        reasoning=data.get("reasoning", ""),
    )

    print(f"\n📋  Claude's interpretation:")
    print(f"    Origins      : {', '.join(params.origins)}")
    print(f"    Destinations : {', '.join(params.destinations)}")
    print(f"    Depart date  : {params.depart_date}" + (f" ±{params.flexible_days} days" if params.flexible_days else ""))
    if params.return_date:
        print(f"    Return date  : {params.return_date}")
    print(f"    Passengers   : {params.passengers}  |  Cabin: {params.cabin}")
    if params.max_price:
        print(f"    Max price    : {params.max_price}")
    print(f"\n    💬 {params.reasoning}")

    return params


# ─────────────────────────────────────────────
# Google Flights scraper (Playwright)
# ─────────────────────────────────────────────

CABIN_MAP = {
    "economy": "1",
    "premium_economy": "2",
    "business": "3",
    "first": "4",
}

# ── Google Flights selectors (confirmed from live DOM, March 2026) ─────────────
#
# KEY FINDING from debug HTML:
#   Price span: <span data-gs="..." aria-label="84 British pounds" role="text">£84</span>
#
# The price element is identified by:
#   • presence of the `data-gs` attribute  (unique to fare spans)
#   • aria-label matching "N British/US/Euro pounds/dollars/euros"
#   • role="text"
#
# CSS class names (YMlIz, pIav2d, etc.) are build-hash generated and change
# with every Google Flights deploy — we no longer rely on them.

# Flight result list-item selectors — tried in order, first match wins.
# Google wraps each flight in an <li> or role="listitem" inside the results list.
CARD_SELECTORS = [
    "li[data-gs]",                    # li that itself carries a data-gs fare token
    "li:has(span[data-gs])",          # li containing a data-gs price span
    '[role="listitem"]:has(span[data-gs])',
    "ul[role='list'] > li",           # generic: every li in the results list
]

# Within a card, field selectors ordered most-specific → most-general.
# All time/airline/duration values are exposed via aria-label on Google Flights,
# making them robust to class-name changes.
FIELD_SELECTORS = {
    "airline": [
        # Not used directly — airline is extracted from the card's top-level aria-label
        # (see _extract_airline_from_card). These are kept as last-resort fallbacks only.
        "span[aria-label*='Operated by']",
        "[data-testid='airline-name']",
    ],
    "depart": [
        "span[aria-label*='Departure time']",
        "span[aria-label*='departs']",
    ],
    "arrive": [
        "span[aria-label*='Arrival time']",
        "span[aria-label*='arrives']",
    ],
    "duration": [
        "span[aria-label*='Total duration']",
        "span[aria-label*='duration']",
        "[data-testid='duration']",
    ],
    "stops": [
        "span[aria-label*='stop']",
        "span[aria-label*='Nonstop']",
        "span[aria-label*='nonstop']",
        "[data-testid='stops']",
    ],
    "co2": [
        "span[aria-label*='carbon']",
        "span[aria-label*='CO2']",
        "span[aria-label*='emissions']",
    ],
}


def _dismiss_consent(page) -> None:
    """Dismiss cookie/GDPR consent dialogs if present."""
    for selector in [
        'button:has-text("Accept all")',
        'button:has-text("Reject all")',
        '[aria-label="Accept all"]',
        'button:has-text("I agree")',
        'button:has-text("Agree")',
    ]:
        try:
            btn = page.query_selector(selector)
            if btn and btn.is_visible():
                btn.click()
                time.sleep(1)
                return
        except Exception:
            pass


def _extract_price_from_card(card) -> str:
    """
    Extract the fare from a flight card.

    Google Flights marks price spans with:
      • data-gs attribute  (a base64 fare token — present ONLY on price spans)
      • aria-label="N British pounds" / "N US dollars" / "N euros"
      • role="text"

    We use data-gs as the anchor — it is the single most reliable signal
    that an element is a price, since no other element on the page carries it.
    We then read the value from the aria-label rather than inner text, because
    the aria-label always contains the plain integer ("84 British pounds")
    whereas inner text may include currency symbols that vary by locale.
    """
    # Strategy 1: span with data-gs + aria-label containing a currency word
    CURRENCY_WORDS = ("pounds", "dollars", "euros", "pound", "dollar", "euro")
    try:
        price_spans = card.query_selector_all("span[data-gs]")
        for span in price_spans:
            label = (span.get_attribute("aria-label") or "").lower()
            if any(word in label for word in CURRENCY_WORDS):
                # aria-label is e.g. "84 British pounds" — grab the leading integer
                m = re.match(r'([\d,]+)', label.strip())
                if m:
                    return m.group(1).replace(',', '')
    except Exception:
        pass

    # Strategy 2: any element with role="text" whose aria-label is "N <currency>"
    try:
        text_roles = card.query_selector_all('[role="text"]')
        for el in text_roles:
            label = (el.get_attribute("aria-label") or "").lower()
            if any(word in label for word in CURRENCY_WORDS):
                m = re.match(r'([\d,]+)', label.strip())
                if m:
                    val = int(m.group(1).replace(',', ''))
                    if 5 <= val <= 15000:   # realistic fare range
                        return str(val)
    except Exception:
        pass

    # Strategy 3: inner text of a data-gs span as last resort (strip £/$)
    try:
        price_spans = card.query_selector_all("span[data-gs]")
        for span in price_spans:
            txt = re.sub(r'[£$€,\s]', '', span.inner_text().strip())
            if txt.isdigit() and 5 <= int(txt) <= 15000:
                return txt
    except Exception:
        pass

    return ""


def _extract_airline_from_card(card) -> str:
    """
    Extract the airline name from a flight card.

    Confirmed from live DOM (debug HTML):
      <div class="sSHqwe tPgKwe ogfYpf"><span>easyJet</span></div>

    The airline is plain inner text inside a <span> within div.sSHqwe.
    There is no aria-label on this element — inner text is the only signal.

    Fallbacks in order:
      1. div.sSHqwe span  (confirmed live selector)
      2. img[alt]         (airline logo alt text)
      3. FIELD_SELECTORS  (legacy aria-label attempts)
    """
    # Strategy 1: confirmed live selector — div.sSHqwe span
    try:
        el = card.query_selector("div.sSHqwe span")
        if el:
            txt = el.inner_text().strip()
            if 2 <= len(txt) <= 60:
                return txt
    except Exception:
        pass

    # Strategy 2: airline logo img alt text (e.g. <img alt="Ryanair">)
    try:
        img = card.query_selector("img[alt]")
        if img:
            alt = (img.get_attribute("alt") or "").strip()
            if 2 <= len(alt) <= 60 and "logo" not in alt.lower():
                return alt
    except Exception:
        pass

    # Strategy 3: fall back to FIELD_SELECTORS aria-label attempts
    return _extract_field(card, "airline")


def _extract_field(card, field: str) -> str:
    """
    Extract a named field from a card using its known selectors.
    Prefers aria-label values over inner text for all fields, since
    aria-labels are stable even when CSS class names change.
    """
    for sel in FIELD_SELECTORS.get(field, []):
        try:
            el = card.query_selector(sel)
            if not el:
                continue
            label = el.get_attribute("aria-label") or ""
            if label:
                # For times, pull the time portion out of the label
                if field in ("depart", "arrive"):
                    m = re.search(r'(\d{1,2}:\d{2}\s*(?:AM|PM)?)', label, re.I)
                    if m:
                        return m.group(1)
                # For duration, pull "Xh Ym" pattern
                if field == "duration":
                    m = re.search(r'(\d+\s*hr?\s*(?:\d+\s*min?)?)', label, re.I)
                    if m:
                        return m.group(1).strip()
                # For stops, return the whole label (e.g. "Nonstop", "1 stop")
                if field == "stops":
                    return label
                # For everything else return the full label text
                if label:
                    return label
            # Fall back to inner text
            txt = el.inner_text().strip()
            if txt:
                return txt
        except Exception:
            continue
    return ""


def _calc_total_travel_time(depart_time: str, arrive_time: str) -> str:
    """
    Calculate wall-clock travel time from departure to arrival strings.
    Handles AM/PM format and overnight flights (where arrival < departure).
    Returns a string like "3 hr 15 min", or "" if times can't be parsed.
    """
    if not depart_time or not arrive_time:
        return ""
    try:
        # Normalise: strip extra whitespace, ensure uppercase AM/PM
        def parse_t(t):
            t = t.strip().upper()
            for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
                try:
                    return datetime.strptime(t, fmt)
                except ValueError:
                    continue
            return None

        d = parse_t(depart_time)
        a = parse_t(arrive_time)
        if not d or not a:
            return ""

        minutes = int((a - d).total_seconds() // 60)
        if minutes < 0:     # overnight — add 24 hours
            minutes += 24 * 60

        hrs, mins = divmod(minutes, 60)
        if hrs and mins:
            return f"{hrs} hr {mins} min"
        elif hrs:
            return f"{hrs} hr"
        else:
            return f"{mins} min"
    except Exception:
        return ""


def _parse_stops(stops_text: str) -> str:
    """Normalise stops text to a digit string."""
    if not stops_text:
        return "0"
    t = stops_text.lower()
    if "nonstop" in t or "direct" in t:
        return "0"
    m = re.search(r'(\d+)', t)
    return m.group(1) if m else "1"


def scrape_google_flights(origin: str, dest: str, depart: str,
                           return_date: str, passengers: int, cabin: str,
                           max_price: Optional[int],
                           debug: bool = False) -> list[FlightResult]:
    """Scrape live Google Flights results using Playwright."""
    results: list[FlightResult] = []

    if return_date:
        url = (f"https://www.google.com/travel/flights?"
               f"q=flights+from+{origin}+to+{dest}+{depart}+returning+{return_date}"
               f"&curr=GBP&hl=en")
    else:
        url = (f"https://www.google.com/travel/flights?"
               f"q=flights+from+{origin}+to+{dest}+{depart}"
               f"&curr=GBP&hl=en")

    print(f"  🌐  Searching {origin} → {dest} on {depart}...")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=not debug,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-GB",
        )
        page = context.new_page()

        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
            time.sleep(2)
            _dismiss_consent(page)
            time.sleep(2)

            # Wait for at least one price element to appear (data-gs is the reliable anchor)
            try:
                page.wait_for_selector("span[data-gs]", timeout=10000)
            except PlaywrightTimeout:
                # Elements may still be loading — give it extra time before giving up
                time.sleep(4)


            # ── Detect "no results" page ──────────────────────────────────────
            # When Google Flights finds no matching flights it shows a "nearby
            # airports" suggestion panel instead of flight cards.  Those panels
            # contain data-gs price spans (for the suggested alternatives) which
            # the scraper would otherwise pick up as phantom results with no
            # departure/arrival times.  We detect the no-results state here and
            # bail out immediately, treating it as a clean empty result.
            no_results_signals = [
                '[role="alert"]',           # "No results returned, alternative suggestions found."
                'h3.QEk4oc',               # "No options matching your search"
                '.gsxWqd',                 # nearby-airport suggestions container
                'ul.UlpMwb',               # the suggestion list itself
            ]
            for sig in no_results_signals:
                try:
                    el = page.query_selector(sig)
                    if el and el.is_visible():
                        print(f"    \u2139\ufe0f  No flights found for {origin}\u2192{dest} on {depart} "
                              f"(Google returned no results \u2014 skipping)")
                        return results   # empty list
                except Exception:
                    pass
            # ──────────────────────────────────────────────────────────────────
            if debug:
                # Dump page HTML for selector inspection
                html = page.content()
                debug_path = f"/tmp/gf_debug_{origin}_{dest}_{depart}.html"
                with open(debug_path, "w") as f:
                    f.write(html)
                print(f"    🐛  Debug HTML saved to {debug_path}")

            # Find flight cards
            flight_cards = []
            for sel in CARD_SELECTORS:
                cards = page.query_selector_all(sel)
                if cards:
                    flight_cards = cards
                    break

            if not flight_cards:
                print(f"    ⚠️  No flight cards found for {origin}→{dest}. "
                      f"Try --debug to inspect the page HTML.")
            else:
                for card in flight_cards[:12]:
                    price = _extract_price_from_card(card)
                    if not price:
                        continue  # skip cards where we can't confirm the price

                    airline  = _extract_airline_from_card(card) or "Unknown Airline"
                    depart_t = _extract_field(card, "depart")
                    arrive_t = _extract_field(card, "arrive")
                    duration = _extract_field(card, "duration")
                    stops    = _parse_stops(_extract_field(card, "stops"))
                    co2      = _extract_field(card, "co2")

                    results.append(FlightResult(
                        airline=airline,
                        origin=origin,
                        destination=dest,
                        depart_date=depart,
                        depart_time=depart_t,
                        arrive_time=arrive_t,
                        duration=duration,
                        total_travel_time=_calc_total_travel_time(depart_t, arrive_t),
                        stops=stops,
                        price=price,
                        currency="GBP",
                        co2=co2,
                    ))

        except PlaywrightTimeout:
            print(f"    ⚠️  Timeout loading Google Flights for {origin}→{dest}")
        except Exception as e:
            print(f"    ⚠️  Error scraping {origin}→{dest}: {e}")
        finally:
            browser.close()

    return results


# ─────────────────────────────────────────────
# Orchestration: expand dates + run all searches
# ─────────────────────────────────────────────

def generate_dates(base_date: str, flexible_days: int) -> list[str]:
    """Generate a list of dates around the base date."""
    base = datetime.strptime(base_date, "%Y-%m-%d")
    dates = []
    for delta in range(-flexible_days, flexible_days + 1):
        d = base + timedelta(days=delta)
        dates.append(d.strftime("%Y-%m-%d"))
    return dates


def run_searches(params: SearchParams, debug: bool = False) -> list[FlightResult]:
    """Run all origin/destination/date combos and collect results, with a progress bar."""
    all_results: list[FlightResult] = []
    dates = generate_dates(params.depart_date, params.flexible_days)

    combos = [
        (origin, dest, date)
        for origin in params.origins
        for dest in params.destinations
        for date in dates
    ]
    total = len(combos)
    print(f"\n🔍  Running {total} search combination(s) live on Google Flights...\n")

    def _run_all(advance_fn=None):
        for origin, dest, date in combos:
            flights = scrape_google_flights(
                origin=origin,
                dest=dest,
                depart=date,
                return_date=params.return_date,
                passengers=params.passengers,
                cabin=params.cabin,
                max_price=params.max_price,
                debug=debug,
            )
            all_results.extend(flights)
            if advance_fn:
                advance_fn()
            time.sleep(1.5)  # be polite to Google

    if RICH_AVAILABLE:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(bar_width=36),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("({task.completed}/{task.total})"),
            TimeElapsedColumn(),
        ) as progress:
            task = progress.add_task("Searching Google Flights", total=total)
            # Patch the scraper print so it shows inside the progress display
            _run_all(advance_fn=lambda: progress.advance(task))
    else:
        _run_all()

    return all_results


def score_and_sort(results: list[FlightResult], max_price: Optional[int]) -> list[FlightResult]:
    """Score results by price (primary) and stops (secondary)."""
    scored = []
    for r in results:
        try:
            price_val = float(re.sub(r'[^\d.]', '', r.price)) if r.price else 99999
            stops_val = int(r.stops) if r.stops.isdigit() else 1
            if max_price and price_val > max_price:
                continue
            # Lower score = better
            r.score = price_val + (stops_val * 30)
            scored.append(r)
        except Exception:
            pass

    scored.sort(key=lambda x: x.score)
    # Deduplicate by (airline, depart_time, origin, dest, price)
    seen = set()
    unique = []
    for r in scored:
        key = (r.airline, r.depart_time, r.origin, r.destination, r.price)
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def display_results(results: list[FlightResult], top_n: int = 10, show_bike: bool = False):
    """Pretty-print the best flight results."""
    if not results:
        print("\n❌  No flights found. Try widening your search (more flexible dates, nearby airports).\n")
        return

    print(f"\n{'═'*60}")
    print(f"  ✈️   TOP {min(top_n, len(results))} FLIGHTS FOUND")
    print(f"{'═'*60}")

    for i, flight in enumerate(results[:top_n], 1):
        flight.display(i)
        if show_bike:
            fee = getattr(flight, 'bike_fee', None)
            if fee is not None:
                print(f"      {fee.display_line()}")
                print(f"      {format_price_with_bike(float(re.sub(r'[^\d.]', '', flight.price) or 0), fee)}")
                if fee.source_url:
                    print(f"      📎 Source: {fee.source_url}")
            else:
                print("      🚲  Bike fee: not looked up for this airline")

    if len(results) > top_n:
        print(f"  … and {len(results) - top_n} more results.\n")



# ─────────────────────────────────────────────
# Post-search Claude summary
# ─────────────────────────────────────────────

def _flights_to_digest(flights: list, max_items: int = 40) -> list[dict]:
    """Convert FlightResult objects to a compact list of dicts for Claude."""
    out = []
    for f in flights[:max_items]:
        d = {
            "airline":     f.airline,
            "origin":      f.origin,
            "destination": f.destination,
            "date":        f.depart_date,
            "depart":      f.depart_time,
            "arrive":      f.arrive_time,
            "travel_time": f.total_travel_time or f.duration,
            "stops":       f.stops,
            "price_gbp":   f.price_val,
        }
        if hasattr(f, "traveller"):
            d["traveller"] = f.traveller
        out.append(d)
    return out


def _trips_to_digest(trips: list, max_items: int = 20) -> list[dict]:
    """Convert TripResult / GroupTrip objects to a compact digest for Claude."""
    out = []
    for t in trips[:max_items]:
        if hasattr(t, "outbound_legs"):
            # GroupTrip (Friends)
            out.append({
                "total_price_gbp":       t.total_cost,
                "score":                 round(t.score, 1),
                "arrival_spread_mins":   t.arrival_spread_mins,
                "departure_spread_mins": t.departure_spread_mins,
                "outbound_legs": _flights_to_digest(t.outbound_legs),
                "inbound_legs":  _flights_to_digest(t.inbound_legs),
            })
        elif hasattr(t, "outbound") and hasattr(t, "inbound"):
            # TripResult (Advanced)
            out.append({
                "total_price_gbp": t.total_price,
                "outbound": _flights_to_digest([t.outbound])[0],
                "inbound":  _flights_to_digest([t.inbound])[0],
            })
    return out


SUMMARY_SYSTEM = """You are a helpful travel advisor summarising real flight search results.
You will be given:
  1. The user's original query
  2. A JSON digest of the top flight results found by a live search

Write a concise, friendly summary (5-10 sentences) that:
- Highlights the cheapest option(s) and what makes them cheap
- Notes patterns: e.g. a specific day is consistently cheaper, a particular airport
  always offers direct flights, one airline dominates the results
- Honestly compares results against what the user asked for — if they wanted 7 days
  but 8 days is cheaper, say so clearly with specific prices
- Flags caveats: e.g. all cheap options have a stop, early morning departures, etc.
- Is specific with prices, dates, airlines and airports — never be vague
Write in plain English. No bullet points, no markdown headers. Clear flowing prose."""


def summarise_with_claude(query: str, results_digest: list, api_key: str,
                           context: str = "") -> None:
    """Call Claude to produce a human-readable summary of the search results."""
    if not results_digest:
        return

    import textwrap

    print(f"\n{chr(9552)*60}")
    print(f"  \U0001f916  AI TRAVEL SUMMARY")
    print(f"{chr(9552)*60}\n")

    client = anthropic.Anthropic(api_key=api_key)
    prompt = (
        f"User's original request: {query}\n\n"
        + (f"Additional context: {context}\n\n" if context else "")
        + f"Top results (JSON):\n{json.dumps(results_digest, indent=2)}"
    )

    try:
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            system=SUMMARY_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        summary = message.content[0].text.strip()
        for para in summary.split("\n\n"):
            print(textwrap.fill(para.strip(), width=72,
                                initial_indent="  ", subsequent_indent="  "))
            print()
    except Exception as e:
        print(f"  \u26a0\ufe0f  Could not generate summary: {e}\n")

# ─────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="✈️  AI-powered flight finder using Claude + Google Flights",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""Examples:
  python flight_finder.py "Fly from London to Genoa or nearby in late June, return after a week"
  python flight_finder.py "Cheapest flight from NYC to Tokyo any time in March, business class"
  python flight_finder.py "Edinburgh to Barcelona 15th August returning 22nd, 2 passengers"
  python flight_finder.py --interactive
""",
    )
    parser.add_argument(
        "query",
        nargs="?",
        help="Natural language flight request",
    )
    parser.add_argument(
        "--interactive", "-i",
        action="store_true",
        help="Interactive mode: ask for input",
    )
    parser.add_argument(
        "--top", "-n",
        type=int,
        default=10,
        help="Number of results to show (default: 10)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Save raw page HTML to /tmp for selector inspection, and open browser visibly",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("ANTHROPIC_API_KEY", ""),
        help="Anthropic API key (or set ANTHROPIC_API_KEY env var)",
    )
    args = parser.parse_args()

    print("""
╔══════════════════════════════════════════════════════════╗
║         ✈️   AI Flight Finder  •  Powered by Claude       ║
╚══════════════════════════════════════════════════════════╝
""")

    if not args.api_key:
        print("❌  No Anthropic API key found.")
        print("    Set it via: export ANTHROPIC_API_KEY=sk-ant-...")
        print("    Or pass --api-key sk-ant-...")
        sys.exit(1)

    if args.interactive or not args.query:
        print("  Tell me about your flight in plain English.")
        print("  Examples:")
        print("   • 'Fly from London to Genoa or nearby, mid-July, return after 10 days'")
        print("   • 'Cheapest NYC to Tokyo in March, flexible on dates, economy'\n")
        query = input("  Your request: ").strip()
    else:
        query = args.query

    if not query:
        print("❌  No query provided.")
        sys.exit(1)

    # Step 1: Claude interprets the query
    params = interpret_with_claude(query, args.api_key)

    if not params.origins or not params.destinations:
        print("❌  Claude couldn't determine origin or destination airports.")
        sys.exit(1)

    if not params.depart_date:
        print("❌  Claude couldn't determine a departure date.")
        sys.exit(1)

    # Step 2: Live scrape Google Flights
    raw_results = run_searches(params, debug=args.debug)

    # Step 3: Score, filter, deduplicate
    results = score_and_sort(raw_results, params.max_price)

    # Step 3b: Live bike fee lookup (optional)
    bike_cache = {}
    if getattr(args, 'bike', False) and BIKE_AVAILABLE:
        airlines = list({f.airline for f in results if f.airline})
        bike_cache = lookup_bike_fees(airlines, args.api_key)
        attach_bike_fees(results, bike_cache)

    # Step 4: Display
    display_results(results, top_n=args.top, show_bike=getattr(args, 'bike', False))

    if results:
        summarise_with_claude(query, _flights_to_digest(results), args.api_key)
        print(f"\n  💡 Tip: Copy a link above into your browser to search Google Flights for that flight.\n")

    if getattr(args, 'pdf', None) is not None and PDF_AVAILABLE:
        fname = args.pdf or 'flight_results.pdf'
        summary_text = ""
        try:
            import anthropic as _ant
            _client = _ant.Anthropic(api_key=args.api_key)
            _msg = _client.messages.create(
                model="claude-sonnet-4-5", max_tokens=1024,
                system=SUMMARY_SYSTEM,
                messages=[{"role": "user", "content":
                    f"User's original request: {query}\n\nTop results (JSON):\n" +
                    __import__('json').dumps(_flights_to_digest(results), indent=2)}],
            )
            summary_text = _msg.content[0].text.strip()
        except Exception:
            pass
        out = _pdf_export_simple(query, results, summary_text, fname)
        print(f"  📄  PDF saved: {out}\n")
    elif getattr(args, 'pdf', None) is not None and not PDF_AVAILABLE:
        print("  ⚠️  PDF export requires: pip install reportlab\n")


if __name__ == "__main__":
    main()
