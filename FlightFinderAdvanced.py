#!/usr/bin/env python3
"""
✈️  Flight Finder Advanced — AI-powered open-jaw flight search
Handles outbound and return legs independently, so you can fly into one city
and back from a completely different one.  Results are ranked by total trip cost.

Example:
  python FlightFinderAdvanced.py "Fly from Manchester to Nice on 26th June,
  return 7 days later from Rome. No flexibility on dates or airports."
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
from itertools import product as iterproduct

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
    from pdf_export import export_advanced as _pdf_export_advanced
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False


# ─────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────

@dataclass
class FlightResult:
    """A single one-way flight."""
    airline: str
    origin: str
    destination: str
    depart_date: str            # YYYY-MM-DD
    depart_time: str
    arrive_time: str
    duration: str
    total_travel_time: str      # wall-clock door-to-door incl. layovers
    stops: str
    price: str
    currency: str = "GBP"
    co2: str = ""
    price_val: float = 0.0      # numeric price for arithmetic

    def booking_url(self) -> str:
        q = f"flights+from+{self.origin}+to+{self.destination}+on+{self.depart_date}"
        return f"https://www.google.com/travel/flights?q={q}&curr=GBP&hl=en"

    def _date_label(self) -> str:
        try:
            return datetime.strptime(self.depart_date, "%Y-%m-%d").strftime("%a %d %b %Y")
        except ValueError:
            return self.depart_date

    def _time_label(self) -> str:
        t = self.total_travel_time if self.total_travel_time else self.duration
        extra = ""
        if self.total_travel_time and self.duration and self.total_travel_time != self.duration:
            extra = f"  (flight: {self.duration})"
        return f"{t}{extra}"

    def display_leg(self, label: str):
        stops_label = "✈ Direct" if self.stops == "0" else f"↩ {self.stops} stop(s)"
        co2_label   = f"  🌿 {self.co2}" if self.co2 else ""
        price_str   = f"{self.currency} {self.price}" if self.price else "Price N/A"
        print(
            f"  {'─'*58}\n"
            f"  {label}  💰 {price_str}   {stops_label}{co2_label}\n"
            f"      ✈️  {self.airline}\n"
            f"      {self.origin} → {self.destination}   📅 {self._date_label()}\n"
            f"      🛫 {self.depart_time}  →  🛬 {self.arrive_time}   ⏱ {self._time_label()}\n"
            f"      🔗 {self.booking_url()}"
        )


@dataclass
class TripResult:
    """A paired outbound + return flight (open-jaw aware)."""
    outbound: FlightResult
    inbound: FlightResult
    sort_key: str = "total_price"   # "total_price" | "outbound_price" | "inbound_price"

    @property
    def total_price(self) -> float:
        return self.outbound.price_val + self.inbound.price_val

    @property
    def total_price_str(self) -> str:
        currency = self.outbound.currency
        return f"{currency} {self.total_price:.0f}"

    def display(self, rank: int):
        print(f"\n  {'═'*58}")
        print(f"  #{rank}  TOTAL: 💰 {self.total_price_str}")
        print(f"  {'═'*58}")
        self.outbound.display_leg("OUTBOUND")
        print()
        self.inbound.display_leg("RETURN  ")
        print()


@dataclass
class LegParams:
    """Search parameters for one flight leg."""
    origins: list[str] = field(default_factory=list)
    destinations: list[str] = field(default_factory=list)
    date: str = ""
    flexible_days: int = 0


@dataclass
class AdvancedSearchParams:
    """Full trip parameters including separate outbound and inbound legs."""
    outbound: LegParams = field(default_factory=LegParams)
    inbound: LegParams = field(default_factory=LegParams)
    passengers: int = 1
    cabin: str = "economy"
    max_total_price: Optional[int] = None
    sort_by: str = "total_price"    # "total_price" | "outbound_price" | "inbound_price"
    reasoning: str = ""


# ─────────────────────────────────────────────
# Claude: interpret natural language input
# ─────────────────────────────────────────────

CLAUDE_SYSTEM = """You are an advanced flight search assistant. Parse the user's natural-language
request into structured JSON for an open-jaw trip — where the return flight may depart from a
DIFFERENT airport than the outbound destination.

Key rules:
- Honour explicit "no flexibility" or "no nearby airports" instructions — do NOT expand those legs
- When the user says "or nearby" or is flexible, expand to nearby airports (IATA codes)
- Understand "return N days later" by calculating from the outbound date
- Understand sort preferences: "cheapest total", "cheapest outbound", "cheapest return"
- Map all city/airport names to IATA codes
- If no year given, assume the next upcoming date
- Output ONLY valid JSON — no prose, no markdown fences

Output schema (all fields required):
{
  "outbound": {
    "origins": ["MAN"],
    "destinations": ["NCE"],
    "date": "2026-06-26",
    "flexible_days": 0
  },
  "inbound": {
    "origins": ["FCO"],
    "destinations": ["MAN"],
    "date": "2026-07-03",
    "flexible_days": 0
  },
  "passengers": 1,
  "cabin": "economy",
  "max_total_price": null,
  "sort_by": "total_price",
  "reasoning": "Outbound MAN→NCE fixed 26 Jun. Return FCO→MAN fixed 3 Jul (7 days later). No airport expansion on either leg per user instruction."
}"""


def interpret_with_claude(user_query: str, api_key: str) -> AdvancedSearchParams:
    """Use Claude to parse the user's query into AdvancedSearchParams."""
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
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"⚠️  Claude returned unexpected output:\n{raw}")
        raise ValueError(f"JSON parse error: {e}")

    out = data.get("outbound", {})
    inb = data.get("inbound", {})

    params = AdvancedSearchParams(
        outbound=LegParams(
            origins=out.get("origins", []),
            destinations=out.get("destinations", []),
            date=out.get("date", ""),
            flexible_days=int(out.get("flexible_days", 0)),
        ),
        inbound=LegParams(
            origins=inb.get("origins", []),
            destinations=inb.get("destinations", []),
            date=inb.get("date", ""),
            flexible_days=int(inb.get("flexible_days", 0)),
        ),
        passengers=int(data.get("passengers", 1)),
        cabin=data.get("cabin", "economy"),
        max_total_price=data.get("max_total_price"),
        sort_by=data.get("sort_by", "total_price"),
        reasoning=data.get("reasoning", ""),
    )

    def _fmt_leg(leg: LegParams) -> str:
        flex = f" ±{leg.flexible_days} days" if leg.flexible_days else ""
        return f"{', '.join(leg.origins)} → {', '.join(leg.destinations)}  on {leg.date}{flex}"

    print(f"\n📋  Claude's interpretation:")
    print(f"    Outbound : {_fmt_leg(params.outbound)}")
    print(f"    Inbound  : {_fmt_leg(params.inbound)}")
    print(f"    Passengers: {params.passengers}  |  Cabin: {params.cabin}")
    print(f"    Sort by  : {params.sort_by}")
    if params.max_total_price:
        print(f"    Max total: £{params.max_total_price}")
    print(f"\n    💬 {params.reasoning}")

    return params


# ─────────────────────────────────────────────
# Google Flights scraper  (shared with original)
# ─────────────────────────────────────────────

CABIN_MAP = {
    "economy": "1",
    "premium_economy": "2",
    "business": "3",
    "first": "4",
}

CARD_SELECTORS = [
    "li[data-gs]",
    "li:has(span[data-gs])",
    '[role="listitem"]:has(span[data-gs])',
    "ul[role='list'] > li",
]

FIELD_SELECTORS = {
    "airline": [
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


def _extract_price_from_card(card) -> tuple[str, float]:
    """Return (price_str, price_float) or ("", 0.0) if not found."""
    CURRENCY_WORDS = ("pounds", "dollars", "euros", "pound", "dollar", "euro")

    # Strategy 1: span[data-gs] with currency aria-label
    try:
        for span in card.query_selector_all("span[data-gs]"):
            label = (span.get_attribute("aria-label") or "").lower()
            if any(w in label for w in CURRENCY_WORDS):
                m = re.match(r'([\d,]+)', label.strip())
                if m:
                    s = m.group(1).replace(',', '')
                    return s, float(s)
    except Exception:
        pass

    # Strategy 2: role="text" with currency aria-label
    try:
        for el in card.query_selector_all('[role="text"]'):
            label = (el.get_attribute("aria-label") or "").lower()
            if any(w in label for w in CURRENCY_WORDS):
                m = re.match(r'([\d,]+)', label.strip())
                if m:
                    val = int(m.group(1).replace(',', ''))
                    if 5 <= val <= 15000:
                        return str(val), float(val)
    except Exception:
        pass

    # Strategy 3: inner text of data-gs span
    try:
        for span in card.query_selector_all("span[data-gs]"):
            txt = re.sub(r'[£$€,\s]', '', span.inner_text().strip())
            if txt.isdigit() and 5 <= int(txt) <= 15000:
                return txt, float(txt)
    except Exception:
        pass

    return "", 0.0


def _extract_airline_from_card(card) -> str:
    # Confirmed live selector: div.sSHqwe span contains plain-text airline name
    try:
        el = card.query_selector("div.sSHqwe span")
        if el:
            txt = el.inner_text().strip()
            if 2 <= len(txt) <= 60:
                return txt
    except Exception:
        pass
    try:
        img = card.query_selector("img[alt]")
        if img:
            alt = (img.get_attribute("alt") or "").strip()
            if 2 <= len(alt) <= 60 and "logo" not in alt.lower():
                return alt
    except Exception:
        pass
    return _extract_field(card, "airline")


def _extract_field(card, field: str) -> str:
    for sel in FIELD_SELECTORS.get(field, []):
        try:
            el = card.query_selector(sel)
            if not el:
                continue
            label = el.get_attribute("aria-label") or ""
            if label:
                if field in ("depart", "arrive"):
                    m = re.search(r'(\d{1,2}:\d{2}\s*(?:AM|PM)?)', label, re.I)
                    if m:
                        return m.group(1)
                if field == "duration":
                    m = re.search(r'(\d+\s*hr?\s*(?:\d+\s*min?)?)', label, re.I)
                    if m:
                        return m.group(1).strip()
                if field == "stops":
                    return label
                return label
            txt = el.inner_text().strip()
            if txt:
                return txt
        except Exception:
            continue
    return ""


def _parse_stops(stops_text: str) -> str:
    if not stops_text:
        return "0"
    t = stops_text.lower()
    if "nonstop" in t or "direct" in t:
        return "0"
    m = re.search(r'(\d+)', t)
    return m.group(1) if m else "1"


def _calc_total_travel_time(depart_time: str, arrive_time: str) -> str:
    if not depart_time or not arrive_time:
        return ""
    try:
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
        if minutes < 0:
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


def scrape_google_flights(origin: str, dest: str, depart: str,
                           passengers: int, cabin: str,
                           debug: bool = False) -> list[FlightResult]:
    """Scrape one-way flights for a single origin/dest/date combination."""
    results: list[FlightResult] = []

    url = (f"https://www.google.com/travel/flights?"
           f"q=flights+from+{origin}+to+{dest}+{depart}"
           f"&curr=GBP&hl=en")

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

            try:
                page.wait_for_selector("span[data-gs]", timeout=10000)
            except PlaywrightTimeout:
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
                html = page.content()
                debug_path = f"/tmp/gf_adv_debug_{origin}_{dest}_{depart}.html"
                with open(debug_path, "w") as f:
                    f.write(html)
                print(f"    🐛  Debug HTML → {debug_path}")

            flight_cards = []
            for sel in CARD_SELECTORS:
                cards = page.query_selector_all(sel)
                if cards:
                    flight_cards = cards
                    break

            if not flight_cards:
                print(f"    ⚠️  No cards found for {origin}→{dest} on {depart}")
            else:
                for card in flight_cards[:12]:
                    price_str, price_val = _extract_price_from_card(card)
                    if not price_str:
                        continue

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
                        price=price_str,
                        price_val=price_val,
                        currency="GBP",
                        co2=co2,
                    ))

        except PlaywrightTimeout:
            print(f"    ⚠️  Timeout: {origin}→{dest} on {depart}")
        except Exception as e:
            print(f"    ⚠️  Error scraping {origin}→{dest}: {e}")
        finally:
            browser.close()

    return results


# ─────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────

def generate_dates(base_date: str, flexible_days: int) -> list[str]:
    base = datetime.strptime(base_date, "%Y-%m-%d")
    return [
        (base + timedelta(days=d)).strftime("%Y-%m-%d")
        for d in range(-flexible_days, flexible_days + 1)
    ]


def build_combos(leg: LegParams) -> list[tuple[str, str, str]]:
    """Return all (origin, dest, date) combos for a leg."""
    dates = generate_dates(leg.date, leg.flexible_days)
    return [
        (origin, dest, date)
        for origin in leg.origins
        for dest in leg.destinations
        for date in dates
    ]


def run_searches(params: AdvancedSearchParams, debug: bool = False) \
        -> tuple[list[FlightResult], list[FlightResult]]:
    """Search outbound and inbound legs, returning two separate result lists."""

    out_combos = build_combos(params.outbound)
    inb_combos = build_combos(params.inbound)
    total = len(out_combos) + len(inb_combos)

    print(f"\n🔍  Searching {len(out_combos)} outbound + "
          f"{len(inb_combos)} inbound combination(s) on Google Flights...\n")

    outbound_results: list[FlightResult] = []
    inbound_results:  list[FlightResult] = []

    def _run(combos, accumulator, leg_label, advance_fn=None):
        for origin, dest, date in combos:
            print(f"  🌐  [{leg_label}] {origin} → {dest}  on {date}")
            flights = scrape_google_flights(
                origin=origin, dest=dest, depart=date,
                passengers=params.passengers, cabin=params.cabin, debug=debug,
            )
            accumulator.extend(flights)
            if advance_fn:
                advance_fn()
            time.sleep(1.5)

    if RICH_AVAILABLE:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(bar_width=32),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("({task.completed}/{task.total})"),
            TimeElapsedColumn(),
        ) as progress:
            task = progress.add_task("Searching Google Flights", total=total)
            adv = lambda: progress.advance(task)
            _run(out_combos, outbound_results, "OUTBOUND", adv)
            _run(inb_combos, inbound_results,  "INBOUND",  adv)
    else:
        _run(out_combos, outbound_results, "OUTBOUND")
        _run(inb_combos, inbound_results,  "INBOUND")

    return outbound_results, inbound_results


def pair_and_sort(outbound_results: list[FlightResult],
                  inbound_results:  list[FlightResult],
                  params: AdvancedSearchParams) -> list[TripResult]:
    """
    Pair every outbound flight with every inbound flight, apply price filter,
    deduplicate, and sort by the requested criterion.
    """
    trips: list[TripResult] = []
    seen: set[tuple] = set()

    for out, inb in iterproduct(outbound_results, inbound_results):
        total = out.price_val + inb.price_val
        if params.max_total_price and total > params.max_total_price:
            continue

        # Deduplicate on the combination of both legs' key fields
        key = (
            out.airline, out.depart_time, out.origin, out.destination, out.price,
            inb.airline, inb.depart_time, inb.origin, inb.destination, inb.price,
        )
        if key in seen:
            continue
        seen.add(key)

        trips.append(TripResult(outbound=out, inbound=inb, sort_key=params.sort_by))

    # Sort by requested criterion
    sort_fns = {
        "total_price":    lambda t: t.total_price,
        "outbound_price": lambda t: t.outbound.price_val,
        "inbound_price":  lambda t: t.inbound.price_val,
    }
    trips.sort(key=sort_fns.get(params.sort_by, sort_fns["total_price"]))
    return trips


def display_trips(trips: list[TripResult], top_n: int = 10, show_bike: bool = False):
    if not trips:
        print("\n❌  No trip combinations found. Try relaxing dates or airports.\n")
        return

    sort_labels = {
        "total_price":    "cheapest total price",
        "outbound_price": "cheapest outbound flight",
        "inbound_price":  "cheapest return flight",
    }
    sort_label = sort_labels.get(trips[0].sort_key, "total price")

    print(f"\n{'═'*60}")
    print(f"  ✈️   TOP {min(top_n, len(trips))} TRIPS  (sorted by {sort_label})")
    print(f"{'═'*60}")

    for i, trip in enumerate(trips[:top_n], 1):
        trip.display(i)

    if len(trips) > top_n:
        print(f"  … and {len(trips) - top_n} more combinations.\n")

    print(f"  💡 Copy any link above into your browser to open that flight on Google Flights.\n")



# ─────────────────────────────────────────────
# Post-search Claude summary
# ─────────────────────────────────────────────

def _flights_to_digest(flights: list, max_items: int = 40) -> list[dict]:
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
    out = []
    for t in trips[:max_items]:
        if hasattr(t, "outbound_legs"):
            out.append({
                "total_price_gbp":       t.total_cost,
                "score":                 round(t.score, 1),
                "arrival_spread_mins":   t.arrival_spread_mins,
                "departure_spread_mins": t.departure_spread_mins,
                "outbound_legs": _flights_to_digest(t.outbound_legs),
                "inbound_legs":  _flights_to_digest(t.inbound_legs),
            })
        elif hasattr(t, "outbound") and hasattr(t, "inbound"):
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
    import textwrap
    if not results_digest:
        return

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
        description="✈️  AI-powered open-jaw flight finder using Claude + Google Flights",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""Examples:
  python FlightFinderAdvanced.py "Fly Manchester to Nice 26th June, return from Rome 7 days later"
  python FlightFinderAdvanced.py "LHR to JFK 1st July, return from LAX 14th July, business class"
  python FlightFinderAdvanced.py "Cheapest Edinburgh to somewhere in Spain mid-August, return flexible airport"
  python FlightFinderAdvanced.py --interactive
""",
    )
    parser.add_argument("query", nargs="?", help="Natural language trip request")
    parser.add_argument("--interactive", "-i", action="store_true")
    parser.add_argument("--top", "-n", type=int, default=10,
                        help="Number of trip combinations to show (default: 10)")
    parser.add_argument("--debug", action="store_true",
                        help="Save debug HTML to /tmp and open browser visibly")
    parser.add_argument(
        "--bike", action="store_true",
        help="Look up live bicycle transport fees for each airline found",
    )
    parser.add_argument(
        "--pdf", metavar="FILENAME",
        nargs="?", const="flight_results_advanced.pdf",
        help="Save results to a PDF (default: flight_results_advanced.pdf)",
    )
    parser.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY", ""),
                        help="Anthropic API key (or set ANTHROPIC_API_KEY env var)")
    args = parser.parse_args()

    print("""
╔══════════════════════════════════════════════════════════╗
║    ✈️   AI Flight Finder Advanced  •  Powered by Claude   ║
║         Open-jaw trips  •  Ranked by total cost           ║
╚══════════════════════════════════════════════════════════╝
""")

    if not args.api_key:
        print("❌  No Anthropic API key found.")
        print("    Set it via: export ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    if args.interactive or not args.query:
        print("  Describe your trip — outbound and return legs can be different airports.\n")
        print("  Examples:")
        print("   • 'Manchester to Nice 26 Jun, return from Rome 3 Jul, no flexibility'")
        print("   • 'LHR to NYC 1 Aug returning from Boston 15 Aug, cheapest total'\n")
        query = input("  Your request: ").strip()
    else:
        query = args.query

    if not query:
        print("❌  No query provided.")
        sys.exit(1)

    # Step 1: Claude interprets the query
    params = interpret_with_claude(query, args.api_key)

    for leg, name in [(params.outbound, "outbound"), (params.inbound, "inbound")]:
        if not leg.origins or not leg.destinations:
            print(f"❌  Claude couldn't determine {name} airports.")
            sys.exit(1)
        if not leg.date:
            print(f"❌  Claude couldn't determine the {name} date.")
            sys.exit(1)

    # Step 2: Search both legs live on Google Flights
    outbound_results, inbound_results = run_searches(params, debug=args.debug)

    if not outbound_results:
        print("❌  No outbound flights found.")
        sys.exit(1)
    if not inbound_results:
        print("❌  No inbound flights found.")
        sys.exit(1)

    print(f"\n  Found {len(outbound_results)} outbound and "
          f"{len(inbound_results)} inbound flights — building combinations...")

    # Step 3: Pair, filter, sort
    trips = pair_and_sort(outbound_results, inbound_results, params)

    # Step 3b: Live bike fee lookup (optional)
    bike_cache = {}
    if getattr(args, 'bike', False) and BIKE_AVAILABLE:
        all_airlines = list({f.airline
                             for t in trips
                             for f in [t.outbound, t.inbound]
                             if f.airline})
        bike_cache = lookup_bike_fees(all_airlines, args.api_key)
        all_flights = [f for t in trips for f in [t.outbound, t.inbound]]
        attach_bike_fees(all_flights, bike_cache)

    # Step 4: Display
    display_trips(trips, top_n=args.top, show_bike=getattr(args, 'bike', False))

    if trips:
        summarise_with_claude(query, _trips_to_digest(trips), args.api_key)

    if getattr(args, 'pdf', None) is not None and PDF_AVAILABLE:
        fname = args.pdf or 'flight_results_advanced.pdf'
        summary_text = ""
        try:
            import anthropic as _ant
            _client = _ant.Anthropic(api_key=args.api_key)
            _msg = _client.messages.create(
                model="claude-sonnet-4-5", max_tokens=1024,
                system=SUMMARY_SYSTEM,
                messages=[{"role": "user", "content":
                    f"User's original request: {query}\n\nTop results (JSON):\n" +
                    __import__('json').dumps(_trips_to_digest(trips), indent=2)}],
            )
            summary_text = _msg.content[0].text.strip()
        except Exception:
            pass
        out = _pdf_export_advanced(query, trips, summary_text, fname)
        print(f"  📄  PDF saved: {out}\n")
    elif getattr(args, 'pdf', None) is not None and not PDF_AVAILABLE:
        print("  ⚠️  PDF export requires: pip install reportlab\n")


if __name__ == "__main__":
    main()
