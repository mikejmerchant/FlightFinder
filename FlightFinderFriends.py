#!/usr/bin/env python3
"""
✈️  Flight Finder Friends — Group flight optimiser
Finds the best combination of flights for a group of friends travelling from
different home airports to a shared destination (and back), minimising both
total cost AND the time gaps between each person's arrival and departure.

Example:
  python FlightFinderFriends.py "Charlie lives in Exeter and I live in Manchester.
  We want to fly to the Genoa region in late June for a 7-day cycling holiday,
  then fly home from somewhere beautiful in Italy about 500 miles away."
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
import math

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
    from pdf_export import export_friends as _pdf_export_friends
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False


# ─────────────────────────────────────────────
# Data models
# ─────────────────────────────────────────────

@dataclass
class FlightResult:
    """A single one-way flight for one traveller."""
    traveller:          str
    airline:            str
    origin:             str
    destination:        str
    depart_date:        str     # YYYY-MM-DD
    depart_time:        str
    arrive_time:        str
    duration:           str
    total_travel_time:  str
    stops:              str
    price:              str
    currency:           str   = "GBP"
    co2:                str   = ""
    price_val:          float = 0.0
    arrive_minutes:     int   = -1  # minutes since midnight, for spread calc
    depart_minutes:     int   = -1  # minutes since midnight, for spread calc

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
        if self.total_travel_time and self.duration and self.total_travel_time != self.duration:
            return f"{t}  (flight: {self.duration})"
        return t

    def display_leg(self, indent: str = "      "):
        stops_label = "✈ Direct" if self.stops == "0" else f"↩ {self.stops} stop(s)"
        co2_label   = f"  🌿 {self.co2}" if self.co2 else ""
        price_str   = f"{self.currency} {self.price}" if self.price else "Price N/A"
        print(
            f"{indent}👤 {self.traveller:<12}  ✈️  {self.airline}\n"
            f"{indent}   {self.origin} → {self.destination}   📅 {self._date_label()}\n"
            f"{indent}   🛫 {self.depart_time}  →  🛬 {self.arrive_time}"
            f"   ⏱ {self._time_label()}   {stops_label}{co2_label}\n"
            f"{indent}   💰 {price_str}\n"
            f"{indent}   🔗 {self.booking_url()}"
        )


@dataclass
class GroupTrip:
    """
    One combination of flights: one outbound per traveller + one inbound per traveller.
    Scored on: total_cost + time_sync_penalty (arrival spread + departure spread).
    """
    outbound_legs:          list[FlightResult]   # one per traveller, all arrive same destination
    inbound_legs:           list[FlightResult]   # one per traveller, all depart same origin
    total_cost:             float = 0.0
    arrival_spread_mins:    int   = 0    # max gap between travellers' arrival times (outbound)
    departure_spread_mins:  int   = 0    # max gap between travellers' departure times (inbound)
    sync_penalty:           float = 0.0  # cost-equivalent penalty for time gaps
    score:                  float = 0.0  # lower = better

    def shared_destination(self) -> str:
        dests = {f.destination for f in self.outbound_legs}
        return "/".join(sorted(dests))

    def shared_inbound_origin(self) -> str:
        origins = {f.origin for f in self.inbound_legs}
        return "/".join(sorted(origins))

    def _spread_label(self, minutes: int) -> str:
        if minutes < 0:
            return "unknown"
        if minutes == 0:
            return "same time ✅"
        h, m = divmod(minutes, 60)
        if h and m:
            return f"{h}h {m}m gap"
        elif h:
            return f"{h}h gap"
        return f"{m}m gap"

    def display(self, rank: int, currency: str = "GBP"):
        total_str    = f"{currency} {self.total_cost:.0f}"
        arr_label    = self._spread_label(self.arrival_spread_mins)
        dep_label    = self._spread_label(self.departure_spread_mins)
        n            = len(self.outbound_legs)
        per_person   = self.total_cost / n if n else 0

        print(f"\n  {'═'*62}")
        print(f"  #{rank}  💰 TOTAL {total_str}  ({currency} {per_person:.0f}/person)")
        print(f"       🕐 Arrival gap: {arr_label}   |   Departure gap: {dep_label}")
        print(f"       📊 Score: {self.score:.1f}  (lower = better balance of cost & sync)")

        # ── Outbound legs ──────────────────────────────────────────
        dest = self.shared_destination()
        print(f"\n  ── OUTBOUND  (→ {dest}) {'─'*35}")
        for leg in sorted(self.outbound_legs, key=lambda f: f.arrive_minutes):
            print()
            leg.display_leg()

        # ── Inbound legs ───────────────────────────────────────────
        orig = self.shared_inbound_origin()
        print(f"\n  ── RETURN  ({orig} →) {'─'*37}")
        for leg in sorted(self.inbound_legs, key=lambda f: f.depart_minutes):
            print()
            leg.display_leg()
        print()


# ─────────────────────────────────────────────
# Search parameter model
# ─────────────────────────────────────────────

@dataclass
class TravellerSpec:
    name:              str
    home_airports:     list[str]   # IATA — traveller's origin/return airports
    outbound_flexible: int = 0     # ±days on outbound date
    inbound_flexible:  int = 0     # ±days on inbound date


@dataclass
class FriendsSearchParams:
    travellers:             list[TravellerSpec]
    shared_destinations:    list[str]   # outbound destination airports (IATA)
    shared_inbound_origins: list[str]   # inbound origin airports (IATA) — end of trip
    outbound_date:          str         # YYYY-MM-DD
    inbound_date:           str         # YYYY-MM-DD
    cabin:                  str  = "economy"
    max_total_price:        Optional[int] = None
    # Penalty: how many £ is 1 hour of arrival/departure spread worth?
    # e.g. 10 = a 1-hour gap between friends' arrivals is penalised like £10 extra
    sync_penalty_per_hour:  float = 10.0
    reasoning:              str   = ""


# ─────────────────────────────────────────────
# Claude: interpret natural language input
# ─────────────────────────────────────────────

CLAUDE_SYSTEM = """You are an expert travel planner for groups of friends flying from different home cities.

Parse the user's natural-language request into structured JSON.

Key rules:
- Identify each named traveller and their home airport(s) — expand to nearby airports naturally
- Identify the shared OUTBOUND destination airports (where everyone flies INTO together)
- Identify the shared INBOUND origin airports (where everyone flies OUT from at the end)
  - If the trip ends at a different place than it starts (e.g. cycling holiday), this will differ
  - Use geography and context — "500 miles from Genoa in beautiful Italy" suggests Puglia, Sicily, Amalfi, Sardinia etc.
  - Provide 3–6 realistic candidate airports for any flexible location
- Parse the outbound date and inbound date (calculate "7 days later" etc.)
- Honour "no flexibility" instructions precisely
- sync_penalty_per_hour: how much (in GBP) to penalise each hour of gap between friends' arrivals/departures
  - Default 10. If user says "minimise time gaps strongly" use 25. If "cheapest above all" use 3.
- Output ONLY valid JSON — no prose, no markdown fences

Output schema:
{
  "travellers": [
    {"name": "Charlie", "home_airports": ["EXT","EXM"], "outbound_flexible": 0, "inbound_flexible": 0},
    {"name": "Alex",    "home_airports": ["MAN","LPL"], "outbound_flexible": 0, "inbound_flexible": 0}
  ],
  "shared_destinations":    ["GOA","NCE","MXP"],
  "shared_inbound_origins": ["PMO","BRI","CTA","AHO","CAG"],
  "outbound_date":  "2026-06-26",
  "inbound_date":   "2026-07-03",
  "cabin": "economy",
  "max_total_price": null,
  "sync_penalty_per_hour": 10,
  "reasoning": "Charlie near Exeter: EXT/EXM. Alex near Manchester: MAN/LPL. Outbound to Genoa region: GOA/NCE/MXP. Cycling ~500 miles in 7 days from Genoa reaches southern Italy — selected PMO (Palermo), BRI (Bari), CTA (Catania), AHO (Alghero), CAG (Cagliari) as plausible end airports."
}"""


def interpret_with_claude(user_query: str, api_key: str) -> FriendsSearchParams:
    client = anthropic.Anthropic(api_key=api_key)
    today  = datetime.today().strftime("%Y-%m-%d")
    prompt = f"Today is {today}.\n\nUser request: {user_query}"

    print("\n🤖  Asking Claude to interpret your request...")
    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2048,
        system=CLAUDE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    raw = re.sub(r"^```[a-z]*\n?", "", raw)
    raw = re.sub(r"\n?```$",       "", raw)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"⚠️  Claude returned unexpected output:\n{raw}")
        raise ValueError(f"JSON parse error: {e}")

    travellers = [
        TravellerSpec(
            name=t["name"],
            home_airports=t["home_airports"],
            outbound_flexible=int(t.get("outbound_flexible", 0)),
            inbound_flexible=int(t.get("inbound_flexible", 0)),
        )
        for t in data.get("travellers", [])
    ]

    params = FriendsSearchParams(
        travellers=travellers,
        shared_destinations=data.get("shared_destinations", []),
        shared_inbound_origins=data.get("shared_inbound_origins", []),
        outbound_date=data.get("outbound_date", ""),
        inbound_date=data.get("inbound_date", ""),
        cabin=data.get("cabin", "economy"),
        max_total_price=data.get("max_total_price"),
        sync_penalty_per_hour=float(data.get("sync_penalty_per_hour", 10.0)),
        reasoning=data.get("reasoning", ""),
    )

    print(f"\n📋  Claude's interpretation:")
    for t in params.travellers:
        print(f"    👤 {t.name:<12} home airports: {', '.join(t.home_airports)}")
    print(f"    🛬 Shared destinations  : {', '.join(params.shared_destinations)}")
    print(f"    🛫 Shared return origins: {', '.join(params.shared_inbound_origins)}")
    print(f"    📅 Outbound: {params.outbound_date}   Return: {params.inbound_date}")
    print(f"    🎚  Sync penalty: £{params.sync_penalty_per_hour:.0f}/hr of gap")
    print(f"\n    💬 {params.reasoning}\n")

    return params


# ─────────────────────────────────────────────
# Google Flights scraper (proven selectors)
# ─────────────────────────────────────────────

CARD_SELECTORS = [
    "li[data-gs]",
    "li:has(span[data-gs])",
    '[role="listitem"]:has(span[data-gs])',
    "ul[role='list'] > li",
]

FIELD_SELECTORS = {
    "airline":  ["span[aria-label*='Operated by']", "[data-testid='airline-name']"],
    "depart":   ["span[aria-label*='Departure time']", "span[aria-label*='departs']"],
    "arrive":   ["span[aria-label*='Arrival time']",   "span[aria-label*='arrives']"],
    "duration": ["span[aria-label*='Total duration']", "span[aria-label*='duration']"],
    "stops":    ["span[aria-label*='stop']", "span[aria-label*='Nonstop']", "span[aria-label*='nonstop']"],
    "co2":      ["span[aria-label*='carbon']", "span[aria-label*='CO2']"],
}


def _dismiss_consent(page) -> None:
    for sel in ['button:has-text("Accept all")', '[aria-label="Accept all"]',
                'button:has-text("I agree")', 'button:has-text("Agree")']:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click(); time.sleep(1); return
        except Exception:
            pass


def _extract_price_from_card(card) -> tuple[str, float]:
    CURRENCY_WORDS = ("pounds", "dollars", "euros", "pound", "dollar", "euro")
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
    try:
        for span in card.query_selector_all("span[data-gs]"):
            txt = re.sub(r'[£$€,\s]', '', span.inner_text().strip())
            if txt.isdigit() and 5 <= int(txt) <= 15000:
                return txt, float(txt)
    except Exception:
        pass
    return "", 0.0


def _extract_airline_from_card(card) -> str:
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
                    if m: return m.group(1)
                if field == "duration":
                    m = re.search(r'(\d+\s*hr?\s*(?:\d+\s*min?)?)', label, re.I)
                    if m: return m.group(1).strip()
                if field == "stops":
                    return label
                return label
            txt = el.inner_text().strip()
            if txt: return txt
        except Exception:
            continue
    return ""


def _parse_stops(s: str) -> str:
    if not s: return "0"
    t = s.lower()
    if "nonstop" in t or "direct" in t: return "0"
    m = re.search(r'(\d+)', t)
    return m.group(1) if m else "1"


def _time_to_minutes(t: str) -> int:
    """Parse a time string to minutes-since-midnight. Returns -1 on failure."""
    if not t: return -1
    t = t.strip().upper()
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
        try:
            dt = datetime.strptime(t, fmt)
            return dt.hour * 60 + dt.minute
        except ValueError:
            continue
    return -1


def _calc_travel_time(depart: str, arrive: str) -> str:
    d, a = _time_to_minutes(depart), _time_to_minutes(arrive)
    if d < 0 or a < 0: return ""
    mins = a - d
    if mins < 0: mins += 24 * 60
    h, m = divmod(mins, 60)
    if h and m: return f"{h} hr {m} min"
    if h:       return f"{h} hr"
    return f"{m} min"


def scrape_google_flights(traveller: str, origin: str, dest: str, depart: str,
                          debug: bool = False) -> list[FlightResult]:
    """Scrape one-way flights for one traveller on one origin/dest/date."""
    results: list[FlightResult] = []
    url = (f"https://www.google.com/travel/flights?"
           f"q=flights+from+{origin}+to+{dest}+{depart}&curr=GBP&hl=en")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=not debug,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-GB",
        )
        page = ctx.new_page()
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
                path = f"/tmp/gff_debug_{traveller}_{origin}_{dest}_{depart}.html"
                with open(path, "w") as f: f.write(html)
                print(f"    🐛 Debug HTML → {path}")

            cards = []
            for sel in CARD_SELECTORS:
                found = page.query_selector_all(sel)
                if found: cards = found; break

            for card in cards[:10]:
                price_str, price_val = _extract_price_from_card(card)
                if not price_str: continue

                depart_t = _extract_field(card, "depart")
                arrive_t = _extract_field(card, "arrive")

                results.append(FlightResult(
                    traveller=traveller,
                    airline=_extract_airline_from_card(card) or "Unknown",
                    origin=origin,
                    destination=dest,
                    depart_date=depart,
                    depart_time=depart_t,
                    arrive_time=arrive_t,
                    duration=_extract_field(card, "duration"),
                    total_travel_time=_calc_travel_time(depart_t, arrive_t),
                    stops=_parse_stops(_extract_field(card, "stops")),
                    price=price_str,
                    price_val=price_val,
                    currency="GBP",
                    co2=_extract_field(card, "co2"),
                    arrive_minutes=_time_to_minutes(arrive_t),
                    depart_minutes=_time_to_minutes(depart_t),
                ))

        except PlaywrightTimeout:
            print(f"    ⚠️  Timeout: {traveller} {origin}→{dest}")
        except Exception as e:
            print(f"    ⚠️  Error: {traveller} {origin}→{dest}: {e}")
        finally:
            browser.close()
    return results


# ─────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────

def generate_dates(base_date: str, flex: int) -> list[str]:
    base = datetime.strptime(base_date, "%Y-%m-%d")
    return [(base + timedelta(days=d)).strftime("%Y-%m-%d")
            for d in range(-flex, flex + 1)]


def run_all_searches(params: FriendsSearchParams,
                     debug: bool = False) -> dict[str, dict[str, list[FlightResult]]]:
    """
    Search every leg for every traveller.

    Returns:
      results["outbound"][traveller_name] = [FlightResult, ...]
      results["inbound"][traveller_name]  = [FlightResult, ...]
    """
    results: dict[str, dict[str, list[FlightResult]]] = {
        "outbound": {t.name: [] for t in params.travellers},
        "inbound":  {t.name: [] for t in params.travellers},
    }

    # Build the full list of scrape tasks
    tasks = []
    for t in params.travellers:
        for date in generate_dates(params.outbound_date, t.outbound_flexible):
            for home in t.home_airports:
                for dest in params.shared_destinations:
                    tasks.append(("outbound", t.name, home, dest, date))
        for date in generate_dates(params.inbound_date, t.inbound_flexible):
            for origin in params.shared_inbound_origins:
                for home in t.home_airports:
                    tasks.append(("inbound", t.name, origin, home, date))

    total = len(tasks)
    print(f"\n🔍  Running {total} searches across {len(params.travellers)} travellers...\n")

    def _run(advance_fn=None):
        for leg_type, name, orig, dest, date in tasks:
            label = "OUT" if leg_type == "outbound" else "RTN"
            print(f"  🌐  [{label}] {name}: {orig} → {dest}  on {date}")
            flights = scrape_google_flights(name, orig, dest, date, debug=debug)
            results[leg_type][name].extend(flights)
            if advance_fn: advance_fn()
            time.sleep(1.5)

    if RICH_AVAILABLE:
        with Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]{task.description}"),
            BarColumn(bar_width=30),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            TextColumn("({task.completed}/{task.total})"),
            TimeElapsedColumn(),
        ) as progress:
            task = progress.add_task("Searching Google Flights", total=total)
            _run(advance_fn=lambda: progress.advance(task))
    else:
        _run()

    return results


def _spread_minutes(flights: list[FlightResult], time_attr: str) -> int:
    """
    Given a list of flights (one per traveller), return the spread in minutes
    between the earliest and latest value of time_attr (arrive_minutes or depart_minutes).
    Returns -1 if any time is unknown.
    """
    vals = [getattr(f, time_attr) for f in flights]
    if any(v < 0 for v in vals):
        return -1
    return max(vals) - min(vals)


def score_group_trip(outbound_legs: list[FlightResult],
                     inbound_legs:  list[FlightResult],
                     penalty_per_hour: float) -> GroupTrip:
    """
    Score a group trip combination.

    Score = total_cost
          + (arrival_spread_hours  × penalty_per_hour)   # outbound: waiting at dest
          + (departure_spread_hours × penalty_per_hour)  # inbound: waiting at origin
    """
    total_cost = sum(f.price_val for f in outbound_legs + inbound_legs)

    arr_spread = _spread_minutes(outbound_legs, "arrive_minutes")
    dep_spread = _spread_minutes(inbound_legs,  "depart_minutes")

    # If time is unknown treat as a moderate penalty (30 min)
    arr_hours = (arr_spread / 60) if arr_spread >= 0 else 0.5
    dep_hours = (dep_spread / 60) if dep_spread >= 0 else 0.5

    sync_penalty = (arr_hours + dep_hours) * penalty_per_hour
    score = total_cost + sync_penalty

    return GroupTrip(
        outbound_legs=outbound_legs,
        inbound_legs=inbound_legs,
        total_cost=total_cost,
        arrival_spread_mins=arr_spread,
        departure_spread_mins=dep_spread,
        sync_penalty=sync_penalty,
        score=score,
    )


def build_and_rank_trips(search_results: dict[str, dict[str, list[FlightResult]]],
                         params: FriendsSearchParams,
                         max_combinations: int = 50_000) -> list[GroupTrip]:
    """
    Pair every outbound option for each traveller with every inbound option,
    group by shared destination/origin, score each GroupTrip, and return ranked.

    To keep combinatorics manageable we cap at max_combinations.
    """
    traveller_names = [t.name for t in params.travellers]

    # Per-traveller outbound lists (list of list of FlightResult)
    out_by_traveller = [search_results["outbound"][n] for n in traveller_names]
    inb_by_traveller = [search_results["inbound"][n]  for n in traveller_names]

    if any(len(x) == 0 for x in out_by_traveller):
        missing = [traveller_names[i] for i, x in enumerate(out_by_traveller) if not x]
        print(f"  ⚠️  No outbound flights found for: {', '.join(missing)}")
    if any(len(x) == 0 for x in inb_by_traveller):
        missing = [traveller_names[i] for i, x in enumerate(inb_by_traveller) if not x]
        print(f"  ⚠️  No inbound flights found for: {', '.join(missing)}")

    # Count total combinations before iterating
    total_out = math.prod(len(x) for x in out_by_traveller)
    total_inb = math.prod(len(x) for x in inb_by_traveller)
    total_combos = total_out * total_inb
    print(f"\n  🔢  Evaluating {total_combos:,} trip combinations...")

    if total_combos > max_combinations:
        print(f"  ⚠️  Too many combinations ({total_combos:,} > {max_combinations:,} limit).")
        print(f"      Keeping only the cheapest 5 flights per traveller per leg to reduce search space.")
        out_by_traveller = [sorted(x, key=lambda f: f.price_val)[:5] for x in out_by_traveller]
        inb_by_traveller = [sorted(x, key=lambda f: f.price_val)[:5] for x in inb_by_traveller]

    trips: list[GroupTrip] = []
    seen: set[tuple] = set()

    for out_combo in iterproduct(*out_by_traveller):
        # Only pair outbound legs that share the same destination
        dest_set = {f.destination for f in out_combo}
        if len(dest_set) > 1:
            continue  # travellers arriving at different airports — skip

        for inb_combo in iterproduct(*inb_by_traveller):
            # Only pair inbound legs that share the same origin
            orig_set = {f.origin for f in inb_combo}
            if len(orig_set) > 1:
                continue  # travellers departing from different airports — skip

            # Dedup key
            key = tuple(
                (f.traveller, f.origin, f.destination, f.depart_date, f.depart_time, f.price)
                for f in list(out_combo) + list(inb_combo)
            )
            if key in seen:
                continue
            seen.add(key)

            trip = score_group_trip(
                list(out_combo), list(inb_combo),
                params.sync_penalty_per_hour
            )

            if params.max_total_price and trip.total_cost > params.max_total_price:
                continue

            trips.append(trip)

    trips.sort(key=lambda t: t.score)
    return trips


def display_trips(trips: list[GroupTrip], top_n: int = 10, show_bike: bool = False):
    if not trips:
        print("\n❌  No group trip combinations found.")
        print("    Try: relaxing dates, broadening destination airports, or removing max price.\n")
        return

    n_travellers = len(trips[0].outbound_legs)
    print(f"\n{'═'*64}")
    print(f"  ✈️   TOP {min(top_n, len(trips))} GROUP TRIPS  "
          f"(scored on cost + arrival/departure sync)")
    print(f"  Score = total cost + time-gap penalty  "
          f"({n_travellers} traveller{'s' if n_travellers > 1 else ''})")
    print(f"{'═'*64}")

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


SUMMARY_SYSTEM = """You are a helpful travel advisor summarising real flight search results for a group trip.
You will be given:
  1. The user's original query
  2. A JSON digest of the top group trip combinations found by a live search

Write a concise, friendly summary (5-10 sentences) that:
- Highlights the best-value combination and what makes it stand out
- Describes how well the group's arrival and departure times align in the top options
- Notes patterns across results: e.g. one traveller consistently has cheaper options,
  a specific destination airport dominates the top results, direct flights only exist
  from certain home airports
- Honestly compares results against what the user asked for — if a slightly different
  date or end airport gives much better sync or price, say so with specifics
- Flags any caveats: one traveller's options are significantly more expensive,
  all cheap flights require an early start, etc.
- Is specific with names, prices, dates, airlines and airports — never be vague
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
        description="✈️  AI group flight optimiser — friends from different cities",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""Examples:
  python FlightFinderFriends.py "Charlie near Exeter and I near Manchester want to fly to the
  Genoa region in late June for a 7-day cycling trip, then fly home from southern Italy"

  python FlightFinderFriends.py "Alice (London), Bob (Edinburgh), Carol (Bristol) all want to
  meet in Barcelona for a long weekend in July, flying home Sunday evening"

  python FlightFinderFriends.py --interactive
""",
    )
    parser.add_argument("query", nargs="?", help="Natural language group trip request")
    parser.add_argument("--interactive", "-i", action="store_true")
    parser.add_argument("--top",   "-n", type=int, default=10,
                        help="Number of trip combinations to show (default: 10)")
    parser.add_argument("--debug", action="store_true",
                        help="Save debug HTML to /tmp, open browser visibly")
    parser.add_argument(
        "--bike", action="store_true",
        help="Look up live bicycle transport fees for each airline found",
    )
    parser.add_argument(
        "--pdf", metavar="FILENAME",
        nargs="?", const="flight_results_friends.pdf",
        help="Save results to a PDF (default: flight_results_friends.pdf)",
    )
    parser.add_argument("--api-key", default=os.environ.get("ANTHROPIC_API_KEY", ""),
                        help="Anthropic API key (or set ANTHROPIC_API_KEY env var)")
    args = parser.parse_args()

    print("""
╔══════════════════════════════════════════════════════════════╗
║   ✈️   Flight Finder Friends  •  Group trip optimiser         ║
║        Cheapest + most synchronised flights for your crew     ║
╚══════════════════════════════════════════════════════════════╝
""")

    if not args.api_key:
        print("❌  No Anthropic API key found.")
        print("    Set: export ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    if args.interactive or not args.query:
        print("  Describe your group trip in plain English.\n")
        print("  Tips:")
        print("   • Name each person and their home city/airport")
        print("   • Describe the destination (even vaguely — Claude will expand it)")
        print("   • Mention if the trip ends somewhere different to where it starts")
        print("   • Say if you want to prioritise cost or synchronised arrival times\n")
        query = input("  Your request: ").strip()
    else:
        query = args.query

    if not query:
        print("❌  No query provided.")
        sys.exit(1)

    # Step 1: Claude interprets the query
    params = interpret_with_claude(query, args.api_key)

    if not params.travellers:
        print("❌  Claude couldn't identify any travellers.")
        sys.exit(1)
    if not params.shared_destinations:
        print("❌  Claude couldn't determine destination airports.")
        sys.exit(1)
    if not params.shared_inbound_origins:
        print("❌  Claude couldn't determine return origin airports.")
        sys.exit(1)

    # Step 2: Search all legs on Google Flights
    search_results = run_all_searches(params, debug=args.debug)

    # Step 3: Build all combinations, score and rank
    trips = build_and_rank_trips(search_results, params)

    # Step 3b: Live bike fee lookup (optional)
    bike_cache = {}
    if getattr(args, 'bike', False) and BIKE_AVAILABLE:
        all_airlines = list({f.airline
                             for t in trips
                             for f in t.outbound_legs + t.inbound_legs
                             if f.airline})
        bike_cache = lookup_bike_fees(all_airlines, args.api_key)
        all_flights = [f for t in trips for f in t.outbound_legs + t.inbound_legs]
        attach_bike_fees(all_flights, bike_cache)

    # Step 4: Display
    display_trips(trips, top_n=args.top, show_bike=getattr(args, 'bike', False))

    if trips:
        summarise_with_claude(query, _trips_to_digest(trips), args.api_key)

    if getattr(args, 'pdf', None) is not None and PDF_AVAILABLE:
        fname = args.pdf or 'flight_results_friends.pdf'
        t_names = [t.name for t in params.travellers]
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
        out = _pdf_export_friends(query, trips, summary_text, t_names, fname)
        print(f"  📄  PDF saved: {out}\n")
    elif getattr(args, 'pdf', None) is not None and not PDF_AVAILABLE:
        print("  ⚠️  PDF export requires: pip install reportlab\n")


if __name__ == "__main__":
    main()
