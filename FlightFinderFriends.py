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
    arrive_minutes:     int   = -1  # absolute minutes since 2000-01-01, for spread calc
    depart_minutes:     int   = -1  # absolute minutes since 2000-01-01, for spread calc

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

    def display_leg(self, indent: str = "      ", role: str = "", show_bike: bool = False):
        """
        role: "outbound" highlights arrival time (relevant for arrival gap)
              "inbound"  highlights departure time (relevant for departure gap)
              ""         shows both equally
        """
        stops_label = "✈ Direct" if self.stops == "0" else f"↩ {self.stops} stop(s)"
        co2_label   = f"  🌿 {self.co2}" if self.co2 else ""
        price_str   = f"{self.currency} {self.price}" if self.price else "Price N/A"

        # Highlight the time that matters for the gap in this leg section
        if role == "outbound":
            time_line = (f"🛫 {self.depart_time}  →  "
                         f"🛬 arrives {self.arrive_time}  ◀ sync point")
        elif role == "inbound":
            time_line = (f"🛫 departs {self.depart_time}  ◀ sync point"
                         f"  →  🛬 {self.arrive_time}")
        else:
            time_line = f"🛫 {self.depart_time}  →  🛬 {self.arrive_time}"

        # Bike fee line (only shown when show_bike=True and fee is attached)
        bike_fee = getattr(self, 'bike_fee', None)
        bike_line = ""
        if show_bike:
            if bike_fee and getattr(bike_fee, 'fee_gbp', None) is not None:
                total_with_bike = self.price_val + bike_fee.fee_gbp
                bike_line = (f"\n{indent}   🚲 Bike fee: GBP {bike_fee.fee_gbp:.0f}  "
                             f"→  total with bike: GBP {total_with_bike:.0f}"
                             f"  ({bike_fee.notes or 'see airline site'})")
            elif bike_fee:
                bike_line = (f"\n{indent}   🚲 Bike fee: unknown  "
                             f"({getattr(bike_fee, 'notes', 'check airline site')})")
            else:
                bike_line = f"\n{indent}   🚲 Bike fee: not looked up"

        print(
            f"{indent}👤 {self.traveller:<12}  ✈️  {self.airline}\n"
            f"{indent}   {self.origin} → {self.destination}   📅 {self._date_label()}\n"
            f"{indent}   {time_line}"
            f"   ⏱ {self._time_label()}   {stops_label}{co2_label}\n"
            f"{indent}   💰 {price_str}{bike_line}\n"
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

    def display(self, rank: int, currency: str = "GBP", show_bike: bool = False):
        total_str    = f"{currency} {self.total_cost:.0f}"
        arr_label    = self._spread_label(self.arrival_spread_mins)
        n            = len(self.outbound_legs)
        per_person   = self.total_cost / n if n else 0
        is_one_way   = not self.inbound_legs

        print(f"\n  {'═'*62}")
        print(f"  #{rank}  💰 TOTAL {total_str}  ({currency} {per_person:.0f}/person)"
              + ("  ✈ one-way" if is_one_way else ""))
        print(f"       📊 Score: {self.score:.1f}  (lower = better balance of cost & sync)")

        # ── Outbound legs ──────────────────────────────────────────
        dest = self.shared_destination()
        print(f"\n  ── {'OUTBOUND' if not is_one_way else 'FLIGHTS'}  (→ {dest})"
              f"  🛬 arrival gap: {arr_label} {'─'*14}")
        for leg in sorted(self.outbound_legs, key=lambda f: f.arrive_minutes):
            print()
            leg.display_leg(role="outbound", show_bike=show_bike)

        # ── Inbound legs (return trips only) ───────────────────────
        if self.inbound_legs:
            dep_label = self._spread_label(self.departure_spread_mins)
            orig = self.shared_inbound_origin()
            print(f"\n  ── RETURN  ({orig} →)  🛫 departure gap: {dep_label} {'─'*12}")
            for leg in sorted(self.inbound_legs, key=lambda f: f.depart_minutes):
                print()
                leg.display_leg(role="inbound", show_bike=show_bike)
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
class TimePrefs:
    """
    Time-of-day preferences applied as scoring penalties.
    Each threshold is HH:MM (24h). Penalties are per traveller per leg.
    Defaults represent "sociable hours" — no airport hotel needed.
    """
    # Outbound departure
    out_depart_warn:  str   = "06:00"   # before this → £40 penalty per traveller
    out_depart_bad:   str   = "05:00"   # before this → £80 penalty per traveller
    # Outbound arrival at destination
    out_arrive_warn:  str   = "22:00"   # after this → £40 penalty
    out_arrive_bad:   str   = "23:30"   # after this → £80 penalty
    # Inbound departure from destination
    inb_depart_warn:  str   = "06:00"
    inb_depart_bad:   str   = "05:00"
    # Inbound arrival at home
    inb_arrive_warn:  str   = "22:00"
    inb_arrive_bad:   str   = "23:30"
    # Penalty amounts
    warn_penalty:     float = 40.0
    bad_penalty:      float = 80.0
    # Whether time-of-day penalties are active at all
    active:           bool  = False     # only True if user mentioned time preferences


@dataclass
class FriendsSearchParams:
    travellers:             list[TravellerSpec]
    shared_destinations:    list[str]   # outbound destination airports (IATA)
    shared_inbound_origins: list[str]   # inbound origin airports (IATA) — empty = one-way
    outbound_date:          str         # YYYY-MM-DD
    inbound_date:           str         # YYYY-MM-DD or "" for one-way
    cabin:                  str  = "economy"
    max_total_price:        Optional[int] = None
    one_way:                bool = False  # True when no return leg
    direct_only:            bool = False  # True if user requested no connecting flights
    # Penalty: how many £ is 1 hour of arrival/departure spread worth?
    sync_penalty_per_hour:  float = 10.0
    reasoning:              str   = ""
    time_prefs:             "TimePrefs" = None  # type: ignore

    def __post_init__(self):
        if self.time_prefs is None:
            object.__setattr__(self, "time_prefs", TimePrefs())


# ─────────────────────────────────────────────
# Claude: interpret natural language input
# ─────────────────────────────────────────────

CLAUDE_SYSTEM = """You are an expert travel planner for groups of friends flying from different home cities.

Parse the user's natural-language request into structured JSON.

CRITICAL — Airport selection for each traveller's home city:
Do NOT simply find the geographically nearest airport. Instead, reason about which airports
people from that location ACTUALLY use in practice for the type of trip described.

For each traveller's home location ask yourself: "Which airports do people from [CITY]
typically use for this kind of travel?" Then consider:
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

Other rules:
- Identify the shared OUTBOUND destination airports (where everyone flies INTO together)
- Identify the shared INBOUND origin airports (where everyone flies OUT from at the end)
  - If the trip ends at a different place than it started (e.g. cycling holiday), this differs
  - Use geography and context — "500 miles from Genoa in beautiful Italy" suggests Puglia,
    Sicily, Campania etc. Provide 3–6 realistic candidate airports
  - For ONE-WAY trips: set shared_inbound_origins to [] and inbound_date to null
- Parse the outbound date and inbound date (calculate "7 days later" etc.)
  - For ONE-WAY trips set inbound_date to null
- Set one_way: true for one-way trips, false for return trips
- Honour "no flexibility" or "exact airport only" instructions strictly
- Set direct_only: true if the user says "direct only", "no stops", "non-stop", "nonstop", or any equivalent phrase meaning connecting flights are not acceptable
- sync_penalty_per_hour: how much (in GBP) to penalise each hour of gap between arrivals
  Default 10. "minimise time gaps strongly" → 25. "cheapest above all" → 3.
- time_prefs: extract time-of-day preferences if the user mentions them.
  Set active: true whenever the user mentions sociable hours, hotel avoidance,
  early flights, late arrivals, or specific time windows.
  Thresholds are HH:MM 24h strings. Penalties are £ per traveller per leg.
  Examples:
    "avoid before 7am" → out_depart_warn: "07:00", out_depart_bad: "06:00",
                          inb_depart_warn: "07:00", inb_depart_bad: "06:00"
    "arrive by 10pm"   → out_arrive_warn: "21:00", out_arrive_bad: "22:00",
                          inb_arrive_warn: "21:00", inb_arrive_bad: "22:00"
    "sociable hours"   → use defaults (warn at 06:00 depart / 22:00 arrive)
    "no airport hotel" → set out_depart_warn: "06:30", inb_depart_warn: "06:30"
  If user says "strongly avoid" or "never", increase warn_penalty to 60 and bad_penalty to 120.
  If not mentioned, set active: false and use default thresholds.
- Output ONLY valid JSON — no prose, no markdown fences

Output schema:
{
  "travellers": [
    {
      "name": "Charlie",
      "home_airports": ["EXT","BRS","LHR","LGW"],
      "outbound_flexible": 0,
      "inbound_flexible": 0
    },
    {
      "name": "Mike",
      "home_airports": ["MAN","LPL"],
      "outbound_flexible": 0,
      "inbound_flexible": 0
    }
  ],
  "shared_destinations":    ["GOA","NCE","MXP"],
  "shared_inbound_origins": ["PMO","BRI","CTA","NAP","FCO"],
  "outbound_date":  "2026-06-26",
  "inbound_date":   "2026-07-03",
  "cabin": "economy",
  "max_total_price": null,
  "one_way": false,
  "direct_only": false,
  "sync_penalty_per_hour": 10,
  "time_prefs": {
    "active": false,
    "out_depart_warn": "06:00", "out_depart_bad": "05:00",
    "out_arrive_warn": "22:00", "out_arrive_bad": "23:30",
    "inb_depart_warn": "06:00", "inb_depart_bad": "05:00",
    "inb_arrive_warn": "22:00", "inb_arrive_bad": "23:30",
    "warn_penalty": 40, "bad_penalty": 80
  },
  "reasoning": "Charlie lives in Exeter: EXT has almost no international routes. BRS (Bristol, 1hr drive) is far better connected. Exeter residents also regularly travel to LHR/LGW (London, ~2.5hrs) for international flights — included both. Mike in Manchester: MAN is the main hub, LPL (Liverpool, 45min) is a common cheaper alternative. Outbound to Genoa region: GOA itself is small — NCE and MXP have the real international connections. Cycling ~500 miles from Genoa in 7 days reaches southern Italy: NAP (Naples), FCO (Rome), PMO (Palermo), BRI (Bari), CTA (Catania) all plausible end airports."
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

    def _empty(val) -> bool:
        """True if val is absent, null, empty list, or the string 'None'/'null'."""
        if val is None:             return True
        if val == "None":           return True
        if val == "null":           return True
        if isinstance(val, list):   return len(val) == 0
        if isinstance(val, str):    return val.strip() == ""
        return False

    raw_inbound_date    = data.get("inbound_date")
    raw_inbound_origins = data.get("shared_inbound_origins")
    explicit_one_way    = bool(data.get("one_way", False))

    is_one_way = (
        explicit_one_way
        or _empty(raw_inbound_date)
        or _empty(raw_inbound_origins)
    )

    params = FriendsSearchParams(
        travellers=travellers,
        shared_destinations=data.get("shared_destinations", []),
        shared_inbound_origins=raw_inbound_origins if not _empty(raw_inbound_origins) else [],
        outbound_date=data.get("outbound_date", ""),
        inbound_date="" if _empty(raw_inbound_date) else str(raw_inbound_date),
        cabin=data.get("cabin", "economy"),
        max_total_price=data.get("max_total_price"),
        one_way=is_one_way,
        direct_only=bool(data.get("direct_only", False)),
        sync_penalty_per_hour=float(data.get("sync_penalty_per_hour", 10.0)),
        reasoning=data.get("reasoning", ""),
        time_prefs=_parse_time_prefs(data.get("time_prefs", {})),
    )

    print(f"\n📋  Claude's interpretation:")
    for t in params.travellers:
        print(f"    👤 {t.name:<12} home airports: {', '.join(t.home_airports)}")
    print(f"    🛬 Shared destinations  : {', '.join(params.shared_destinations)}")
    if params.one_way:
        print(f"    🛫 One-way trip (no return leg)")
        print(f"    📅 Outbound: {params.outbound_date}")
    else:
        print(f"    🛫 Shared return origins: {', '.join(params.shared_inbound_origins)}")
        print(f"    📅 Outbound: {params.outbound_date}   Return: {params.inbound_date}")
    print(f"    🎚  Sync penalty: £{params.sync_penalty_per_hour:.0f}/hr of gap")
    if params.time_prefs and params.time_prefs.active:
        tp = params.time_prefs
        print(f"    🕐  Time prefs: depart ≥{tp.out_depart_warn} · arrive ≤{tp.out_arrive_warn} · penalty £{tp.warn_penalty:.0f}–£{tp.bad_penalty:.0f}/leg")
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


_ABS_EPOCH = datetime(2000, 1, 1)  # arbitrary fixed reference for absolute minute calc

def _time_to_minutes(t: str) -> int:
    """Parse a time string to minutes-since-midnight. Returns -1 on failure.
    Still used for display sorting where only relative order within a day matters."""
    if not t: return -1
    t = t.strip().upper()
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
        try:
            dt = datetime.strptime(t, fmt)
            return dt.hour * 60 + dt.minute
        except ValueError:
            continue
    return -1


def _to_abs_minutes(date_str: str, time_str: str) -> int:
    """Convert a date + time string to absolute minutes since _ABS_EPOCH.
    Returns -1 on any parse failure."""
    if not date_str or not time_str:
        return -1
    time_str = time_str.strip().upper()
    for t_fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
        try:
            t = datetime.strptime(time_str, t_fmt)
            d = datetime.strptime(date_str, "%Y-%m-%d")
            combined = d.replace(hour=t.hour, minute=t.minute)
            return int((combined - _ABS_EPOCH).total_seconds() // 60)
        except ValueError:
            continue
    return -1


def _infer_arrive_date(depart_date: str, depart_time: str, arrive_time: str,
                       duration_str: str) -> str:
    """
    Infer the arrival date.  Flights almost never take more than 20 hours, so:
    - If arrive_time (clock) >= depart_time (clock): same day
    - If arrive_time (clock) <  depart_time (clock): next day
    Falls back to same day if anything is unparseable.
    """
    dep_mins = _time_to_minutes(depart_time)
    arr_mins = _time_to_minutes(arrive_time)
    if dep_mins < 0 or arr_mins < 0:
        return depart_date
    try:
        base = datetime.strptime(depart_date, "%Y-%m-%d")
        offset = 1 if arr_mins < dep_mins else 0
        return (base + timedelta(days=offset)).strftime("%Y-%m-%d")
    except ValueError:
        return depart_date


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
                arrive_date = _infer_arrive_date(depart, depart_t, arrive_t,
                                                 _extract_field(card, "duration"))

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
                    arrive_minutes=_to_abs_minutes(arrive_date, arrive_t),
                    depart_minutes=_to_abs_minutes(depart, depart_t),
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


# ── Feature 1: Feasibility check ─────────────────────────────────────────────

FEASIBILITY_SYSTEM = """You are a travel expert explaining why a flight search found no results.
You will be given the search parameters and a summary of what was found (or not found).

Write a short, friendly 3-5 sentence explanation that:
- States clearly which part of the search failed (outbound, return, or both)
- Gives the most likely real-world reason (e.g. no airline serves this route, small regional
  airport with limited connections, destination airports too obscure)
- Suggests 2-3 concrete alternative searches the user could try instead, with specific
  examples of airports, dates, or approaches that are more likely to work

Be specific and practical. No bullet points — just clear flowing prose."""


def feasibility_check(params: FriendsSearchParams, api_key: str,
                      debug: bool = False) -> tuple[bool, FriendsSearchParams]:
    """
    Run one sample outbound + one sample inbound search per traveller before
    committing to the full search.

    Checks:
    - Any traveller with zero results → bail with Claude explanation
    - If direct_only=True and no direct flights found → prompt user to continue
      without the restriction (returns updated params) or abort

    Returns (feasible: bool, params: FriendsSearchParams).
    params may be modified (direct_only cleared) if the user opts to continue.
    """
    print("\n🔎  Running feasibility check before full search...")

    # ── Airport connectivity ranking ─────────────────────────────────────────
    # When probing feasibility we want the airport most likely to have flights,
    # not the first in the list (which Claude orders local-first). Any airport
    # in this set is preferred over one that isn't; within the set, lower index
    # = higher priority (rough global connectivity order).
    MAJOR_HUBS = [
        # UK & Ireland
        "LHR","LGW","MAN","EDI","BHX","BRS","GLA","LTN","STN","LCY",
        "BFS","ABZ","NCL","LPL","EMA","SOU","CWL","PIK",
        # Western Europe
        "CDG","AMS","FRA","MAD","BCN","FCO","MXP","LIN","ZRH","VIE",
        "BRU","LIS","ATH","OSL","CPH","ARN","HEL","DUB",
        # North America
        "JFK","EWR","ORD","LAX","SFO","ATL","DFW","MIA","BOS","YYZ",
        # Middle East / Asia hubs
        "DXB","DOH","SIN","HKG","NRT","ICN","PEK","PVG",
        # Rest of world
        "SYD","MEL","JNB","GRU","MEX",
    ]

    def _best_airport(airports: list[str]) -> str:
        """Return the highest-connectivity airport from a list."""
        for hub in MAJOR_HUBS:
            if hub in airports:
                return hub
        return airports[0]  # fallback: nothing recognised, use first

    # Pick the single best-bet route per traveller per leg:
    # best home airport × best shared destination/origin × base date
    best_dest   = _best_airport(params.shared_destinations)
    best_origin = _best_airport(params.shared_inbound_origins) if params.shared_inbound_origins else None

    sample_tasks = []
    for t in params.travellers:
        best_home = _best_airport(t.home_airports)
        if t.home_airports and params.shared_destinations:
            sample_tasks.append((
                "outbound", t.name,
                best_home, best_dest,
                params.outbound_date,
            ))
        if not params.one_way and params.shared_inbound_origins and t.home_airports:
            sample_tasks.append((
                "inbound", t.name,
                best_origin, best_home,
                params.inbound_date,
            ))

    sample_results: dict[str, dict[str, list]] = {
        "outbound": {t.name: [] for t in params.travellers},
        "inbound":  {t.name: [] for t in params.travellers},
    }

    for leg_type, name, orig, dest, date in sample_tasks:
        label = "OUT" if leg_type == "outbound" else "RTN"
        print(f"  🔎  [{label}] sample: {name}: {orig} → {dest} on {date}")
        flights = scrape_google_flights(name, orig, dest, date, debug=debug)
        sample_results[leg_type][name].extend(flights)
        time.sleep(1.0)

    # Assess: any traveller with zero results on their best-bet routes?
    failures = []
    for t in params.travellers:
        best_home = _best_airport(t.home_airports)
        out_ok = len(sample_results["outbound"][t.name]) > 0
        if not out_ok:
            failures.append(f"{t.name}: no outbound flights found "
                            f"({best_home}→{best_dest} "
                            f"on {params.outbound_date})")
        if not params.one_way:
            inb_ok = len(sample_results["inbound"][t.name]) > 0
            if not inb_ok:
                failures.append(f"{t.name}: no return flights found "
                                f"({best_origin}→{best_home} "
                                f"on {params.inbound_date})")

    if not failures:
        total_sample = sum(
            len(sample_results["outbound"][t.name]) +
            len(sample_results["inbound"][t.name])
            for t in params.travellers
        )
        # ── Direct-only check ────────────────────────────────────────────────
        if params.direct_only:
            all_sample_flights = [
                f for t in params.travellers
                for f in (sample_results["outbound"][t.name] +
                          sample_results["inbound"][t.name])
            ]
            direct_flights = [f for f in all_sample_flights if f.stops == "0"]
            indirect_only  = all_sample_flights and not direct_flights

            if indirect_only:
                print(f"\n  ⚠️  Direct flights only was requested, but the sample "
                      f"search found no direct flights on the best-bet routes.")
                print(f"      ({len(all_sample_flights)} indirect flight(s) found on "
                      f"sample routes.)")
                print()
                if sys.stdin.isatty():
                    print("  ❓  Continue anyway with indirect flights included? [y/N] ",
                          end="", flush=True)
                    answer = input().strip().lower()
                else:
                    answer = "n"

                if answer in ("y", "yes"):
                    import copy
                    params = copy.replace(params, direct_only=False)
                    print("  ↩️   Continuing without direct-only restriction.\n")
                else:
                    print("  ❌  Search aborted. Try different airports or dates.\n")
                    return False, params
            elif direct_flights:
                n_direct = len(direct_flights)
                n_total  = len(all_sample_flights)
                print(f"  ✅  Direct flights confirmed — "
                      f"{n_direct}/{n_total} sample flights are direct. "
                      f"Starting full search...\n")
                return True, params
            # all_sample_flights empty → caught by failures above

        print(f"  ✅  Feasibility check passed — "
              f"found {total_sample} sample flights. Starting full search...\n")
        return True, params

    # Build context for Claude's explanation
    print(f"\n  ⚠️  Feasibility check failed:\n")
    for f in failures:
        print(f"     • {f}")

    context = {
        "search_params": {
            "travellers": [
                {"name": t.name, "home_airports": t.home_airports}
                for t in params.travellers
            ],
            "shared_destinations":    params.shared_destinations,
            "shared_inbound_origins": params.shared_inbound_origins,
            "outbound_date":  params.outbound_date,
            "inbound_date":   params.inbound_date,
            "cabin":          params.cabin,
        },
        "failures": failures,
        "sample_results_summary": {
            t.name: {
                "outbound_found": len(sample_results["outbound"][t.name]),
                "inbound_found":  len(sample_results["inbound"][t.name]),
            }
            for t in params.travellers
        },
    }

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=512,
            system=FEASIBILITY_SYSTEM,
            messages=[{"role": "user", "content": json.dumps(context, indent=2)}],
        )
        explanation = msg.content[0].text.strip()
        print(f"\n  🤖  {explanation}\n")
    except Exception as e:
        print(f"\n  ℹ️  Could not generate explanation: {e}\n")

    return False, params


# ── Feature 3: Search checkpoint (resume) ────────────────────────────────────

import hashlib
import pickle
import tempfile

try:
    from time_preferences import (
        TimePrefs as _TimePrefsShared, flight_time_penalty as _flight_time_penalty_shared,
        parse_time_prefs_dict as _parse_time_prefs_dict_shared,
    )
    _TIME_PREFS_MODULE = True
except ImportError:
    _TIME_PREFS_MODULE = False

try:
    from search_store import save as store_save, find_matching as store_find, describe_all as store_describe, find_route_flights as store_find_route
    STORE_AVAILABLE = True
except ImportError:
    STORE_AVAILABLE = False

try:
    from reanalyse import reanalyse as do_reanalyse, list_searches
    REANALYSE_AVAILABLE = True
except ImportError:
    REANALYSE_AVAILABLE = False

def _cache_path(params: FriendsSearchParams) -> str:
    """Derive a stable cache file path from the search parameters."""
    key_data = _params_key_data(params)
    h = hashlib.md5(key_data.encode()).hexdigest()[:12]
    return os.path.join(tempfile.gettempdir(), f"fff_cache_{h}.pkl")


def _params_key_data(params: FriendsSearchParams) -> str:
    """Canonical JSON string of params used for both checkpoint and store key."""
    return json.dumps({
        "travellers": [(t.name, sorted(t.home_airports),
                        t.outbound_flexible, t.inbound_flexible)
                       for t in sorted(params.travellers, key=lambda x: x.name)],
        "destinations":    sorted(params.shared_destinations),
        "inbound_origins": sorted(params.shared_inbound_origins),
        "outbound_date":   params.outbound_date,
        "inbound_date":    params.inbound_date,
        "cabin":           params.cabin,
    }, sort_keys=True)


def _params_key_hash(params: FriendsSearchParams) -> str:
    return hashlib.md5(_params_key_data(params).encode()).hexdigest()[:12]


def _params_to_dict(params: FriendsSearchParams) -> dict:
    """Serialise FriendsSearchParams to a plain dict for JSON storage."""
    return {
        "travellers": [
            {"name": t.name, "home_airports": t.home_airports,
             "outbound_flexible": t.outbound_flexible,
             "inbound_flexible": t.inbound_flexible}
            for t in params.travellers
        ],
        "shared_destinations":    params.shared_destinations,
        "shared_inbound_origins": params.shared_inbound_origins,
        "outbound_date":          params.outbound_date,
        "inbound_date":           params.inbound_date,
        "cabin":                  params.cabin,
        "max_total_price":        params.max_total_price,
        "sync_penalty_per_hour":  params.sync_penalty_per_hour,
        "reasoning":              params.reasoning,
    }


def _flight_to_dict(f: FlightResult) -> dict:
    """Serialise a FlightResult to a plain dict."""
    return {
        "traveller":  f.traveller, "airline":   f.airline,
        "origin":     f.origin,    "destination": f.destination,
        "depart_date": f.depart_date, "depart_time": f.depart_time,
        "arrive_time": f.arrive_time, "duration": f.duration,
        "total_travel_time": f.total_travel_time, "stops": f.stops,
        "price": f.price, "price_val": f.price_val, "currency": f.currency,
        "co2": f.co2, "arrive_minutes": f.arrive_minutes,
        "depart_minutes": f.depart_minutes,
    }


def _raw_results_to_dict(results: dict) -> dict:
    """Serialise the nested results dict to JSON-safe form."""
    out = {}
    for leg_type, by_name in results.items():
        out[leg_type] = {
            name: [_flight_to_dict(f) for f in flights]
            for name, flights in by_name.items()
        }
    return out


def _dict_to_flight(d: dict) -> FlightResult:
    """Reconstruct a FlightResult from a stored dict."""
    return FlightResult(
        traveller=d.get("traveller",""),
        airline=d.get("airline",""),
        origin=d.get("origin",""),
        destination=d.get("destination",""),
        depart_date=d.get("depart_date",""),
        depart_time=d.get("depart_time",""),
        arrive_time=d.get("arrive_time",""),
        duration=d.get("duration",""),
        total_travel_time=d.get("total_travel_time",""),
        stops=d.get("stops",""),
        price=d.get("price",""),
        price_val=float(d.get("price_val", 0)),
        currency=d.get("currency","GBP"),
        co2=d.get("co2",""),
        arrive_minutes=int(d.get("arrive_minutes", -1)),
        depart_minutes=int(d.get("depart_minutes", -1)),
    )


def _save_checkpoint(cache_path: str,
                     results: dict,
                     completed_tasks: list) -> None:
    """Persist completed results to disk."""
    try:
        with open(cache_path, "wb") as f:
            pickle.dump({"results": results, "completed": completed_tasks}, f)
    except Exception as e:
        print(f"  ⚠️  Could not save checkpoint: {e}")


def _load_checkpoint(cache_path: str) -> tuple[dict | None, list]:
    """Load a previous checkpoint if it exists. Returns (results, completed_tasks)."""
    if not os.path.exists(cache_path):
        return None, []
    try:
        with open(cache_path, "rb") as f:
            data = pickle.load(f)
        completed = data.get("completed", [])
        results   = data.get("results", None)
        age_mins  = (time.time() - os.path.getmtime(cache_path)) / 60
        print(f"  💾  Found checkpoint from {age_mins:.0f} min ago "
              f"with {len(completed)} completed task(s).")
        return results, completed
    except Exception as e:
        print(f"  ⚠️  Could not load checkpoint ({e}) — starting fresh.")
        return None, []


# ── Feature 2: Live running summary ──────────────────────────────────────────

class LiveSummary:
    """
    Prints a single overwriting status line after each search completes.
    Shows: searches done/total, valid combinations found so far, cheapest price.
    Falls back to plain newline output if stdout is not a TTY (e.g. piped to a file).
    """

    def __init__(self, total: int, traveller_names: list[str],
                 penalty_per_hour: float, one_way: bool = False):
        self._total      = total
        self._done       = 0
        self._names      = traveller_names
        self._penalty    = penalty_per_hour
        self._one_way    = one_way
        self._is_tty     = hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()
        # Lightweight running state — we track per-traveller per-leg lists
        self._out: dict[str, list] = {n: [] for n in traveller_names}
        self._inb: dict[str, list] = {n: [] for n in traveller_names}

    def update(self, leg_type: str, name: str,
               new_flights: list[FlightResult]) -> None:
        """Call after each scrape with the newly found flights."""
        self._done += 1
        if leg_type == "outbound":
            self._out[name].extend(new_flights)
        else:
            self._inb[name].extend(new_flights)

        valid, cheapest = self._quick_score()
        pct = self._done / self._total * 100

        line = (
            f"  📊  [{self._done}/{self._total}  {pct:.0f}%]  "
            f"valid combos so far: {valid:,}"
        )
        if cheapest < 999_999:
            line += f"  ·  cheapest: £{cheapest:,.0f}"
        else:
            line += f"  ·  no full combinations yet"

        if self._is_tty:
            # Overwrite the current line
            print(f"\r{line:<78}", end="", flush=True)
        else:
            print(line)

    def finalise(self) -> None:
        """Move to a new line after the running summary is complete."""
        if self._is_tty:
            print()  # newline after the final \r line

    def _quick_score(self) -> tuple[int, float]:
        """
        Cheaply estimate valid combinations and cheapest price seen so far.
        Uses the same pre-grouping-by-destination logic as build_and_rank_trips
        to avoid counting cross-destination combinations that can never be valid.
        For one-way trips, only outbound counts matter.
        """
        out_lists = [self._out[n] for n in self._names]

        # Pre-group outbound by shared destination
        dest_sets = [{f.destination for f in fl} for fl in out_lists]
        if not dest_sets:
            return 0, 999_999
        shared_dests = set.intersection(*dest_sets)
        if not shared_dests:
            return 0, 999_999

        if self._one_way:
            valid_count = 0
            cheapest    = 999_999.0
            for dest in shared_dests:
                out_per = [[f for f in fl if f.destination == dest] for fl in out_lists]
                if not all(out_per):
                    continue
                valid_count += math.prod(len(x) for x in out_per)
                total = sum(min(f.price_val for f in x) for x in out_per)
                cheapest = min(cheapest, total)
            return valid_count, cheapest

        # Return trip: also need shared inbound origins
        inb_lists = [self._inb[n] for n in self._names]
        orig_sets = [{f.origin for f in fl} for fl in inb_lists]
        if not orig_sets:
            return 0, 999_999
        shared_origs = set.intersection(*orig_sets)
        if not shared_origs:
            return 0, 999_999

        valid_count = 0
        cheapest    = 999_999.0
        for dest in shared_dests:
            out_per = [[f for f in fl if f.destination == dest] for fl in out_lists]
            if not all(out_per):
                continue
            for orig in shared_origs:
                inb_per = [[f for f in fl if f.origin == orig] for fl in inb_lists]
                if not all(inb_per):
                    continue
                valid_count += (math.prod(len(x) for x in out_per) *
                                math.prod(len(x) for x in inb_per))
                total = (sum(min(f.price_val for f in x) for x in out_per) +
                         sum(min(f.price_val for f in x) for x in inb_per))
                cheapest = min(cheapest, total)

        return valid_count, cheapest


# ── Main search orchestrator ──────────────────────────────────────────────────

def run_all_searches(params: FriendsSearchParams,
                     debug: bool = False,
                     resume: bool = True,
                     max_age: float = 7.0) -> dict[str, dict[str, list[FlightResult]]]:
    """
    Search every leg for every traveller.

    Features:
    - Checkpoint/resume: saves progress after each task; resumes from last
      checkpoint on restart (pass resume=False to start fresh).
    - Live summary: prints a single overwriting line after each search showing
      running combination count and cheapest price found so far.

    Returns:
      results["outbound"][traveller_name] = [FlightResult, ...]
      results["inbound"][traveller_name]  = [FlightResult, ...]
    """
    # Build the full list of scrape tasks
    tasks = []
    for t in params.travellers:
        for date in generate_dates(params.outbound_date, t.outbound_flexible):
            for home in t.home_airports:
                for dest in params.shared_destinations:
                    tasks.append(("outbound", t.name, home, dest, date))
        if not params.one_way:
            for date in generate_dates(params.inbound_date, t.inbound_flexible):
                for origin in params.shared_inbound_origins:
                    for home in t.home_airports:
                        tasks.append(("inbound", t.name, origin, home, date))

    total = len(tasks)

    # ── Resume from checkpoint if available ──────────────────────────────────
    cache_path       = _cache_path(params)
    results          = {"outbound": {t.name: [] for t in params.travellers},
                        "inbound":  {t.name: [] for t in params.travellers}}
    completed_keys: set[tuple] = set()

    if resume:
        cached_results, completed_list = _load_checkpoint(cache_path)
        if cached_results:
            results        = cached_results
            completed_keys = set(tuple(c) for c in completed_list)
            remaining      = [t for t in tasks if tuple(t) not in completed_keys]
            skipped        = total - len(remaining)
            if skipped:
                print(f"  ⏭️   Skipping {skipped} already-completed search(es) — "
                      f"{len(remaining)} remaining.\n"
                      f"      (Delete {cache_path} or use --no-resume to start fresh.)\n")
            tasks = remaining

    if not tasks:
        print("  ✅  All searches already completed from checkpoint.\n")
        return results

    # ── Route-level cache: reuse flights from other stored searches ───────────
    # Even if this is a new search (different dates/airports), individual routes
    # that overlap with previously stored searches can be reused without scraping.
    if STORE_AVAILABLE:
        route_hits = 0
        still_needed = []
        for task in tasks:
            leg_type, name, orig, dest, date = task
            cached_flights = store_find_route(
                leg_type, name, orig, dest, date, max_age_days=max_age
            )
            if cached_flights is not None:
                from_objs = [_dict_to_flight(f) for f in cached_flights]
                results[leg_type][name].extend(from_objs)
                completed_keys.add(tuple(task))
                route_hits += 1
            else:
                still_needed.append(task)
        if route_hits:
            print(f"  💾  Reused {route_hits} route(s) from previous searches "
                  f"— {len(still_needed)} new route(s) to scrape.\n")
        tasks = still_needed

    if not tasks:
        print("  ✅  All routes satisfied from stored searches.\n")
        return results

    print(f"\n🔍  Running {len(tasks)} search(es) "
          f"({total - len(tasks)} from cache/store) "
          f"across {len(params.travellers)} traveller(s)...\n")

    # ── Live summary tracker ──────────────────────────────────────────────────
    traveller_names = [t.name for t in params.travellers]
    live = LiveSummary(len(tasks), traveller_names, params.sync_penalty_per_hour, one_way=params.one_way)

    # Pre-populate live summary with any already-loaded checkpoint/store data
    for name in traveller_names:
        for f in results["outbound"].get(name, []):
            live._out[name].append(f)
        for f in results["inbound"].get(name, []):
            live._inb[name].append(f)

    # ── Run each remaining task ───────────────────────────────────────────────
    completed_list = list(completed_keys)

    for leg_type, name, orig, dest, date in tasks:
        label = "OUT" if leg_type == "outbound" else "RTN"
        # Print the current task on its own line above the running summary
        if live._is_tty:
            print(f"\r  🌐  [{label}] {name}: {orig} → {dest}  on {date:<12}", flush=True)
        else:
            print(f"  🌐  [{label}] {name}: {orig} → {dest}  on {date}")

        flights = scrape_google_flights(name, orig, dest, date, debug=debug)
        results[leg_type][name].extend(flights)

        # Update live summary line
        live.update(leg_type, name, flights)

        # Save checkpoint
        completed_list.append((leg_type, name, orig, dest, date))
        _save_checkpoint(cache_path, results, completed_list)

        time.sleep(1.5)

    live.finalise()

    # Clean up checkpoint on successful completion
    try:
        os.remove(cache_path)
    except OSError:
        pass

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


def _parse_time_prefs(tp: dict) -> "TimePrefs":
    """Parse time_prefs dict from Claude JSON into a TimePrefs dataclass."""
    if _TIME_PREFS_MODULE:
        shared = _parse_time_prefs_dict_shared(tp)
        # Map shared TimePrefs fields onto local TimePrefs dataclass
        return TimePrefs(
            active=shared.active,
            out_depart_warn=shared.out_depart_warn, out_depart_bad=shared.out_depart_bad,
            out_arrive_warn=shared.out_arrive_warn, out_arrive_bad=shared.out_arrive_bad,
            inb_depart_warn=shared.inb_depart_warn, inb_depart_bad=shared.inb_depart_bad,
            inb_arrive_warn=shared.inb_arrive_warn, inb_arrive_bad=shared.inb_arrive_bad,
            warn_penalty=shared.warn_penalty, bad_penalty=shared.bad_penalty,
        ) if tp else TimePrefs()
    # Fallback: parse inline
    if not tp:
        return TimePrefs()
    return TimePrefs(
        active         = bool(tp.get("active", False)),
        out_depart_warn= tp.get("out_depart_warn", "06:00"),
        out_depart_bad = tp.get("out_depart_bad",  "05:00"),
        out_arrive_warn= tp.get("out_arrive_warn", "22:00"),
        out_arrive_bad = tp.get("out_arrive_bad",  "23:30"),
        inb_depart_warn= tp.get("inb_depart_warn", "06:00"),
        inb_depart_bad = tp.get("inb_depart_bad",  "05:00"),
        inb_arrive_warn= tp.get("inb_arrive_warn", "22:00"),
        inb_arrive_bad = tp.get("inb_arrive_bad",  "23:30"),
        warn_penalty   = float(tp.get("warn_penalty", 40.0)),
        bad_penalty    = float(tp.get("bad_penalty",  80.0)),
    )


def _hhmm_to_mins(t: str) -> int:
    """Convert HH:MM or H:MM (24h) to minutes since midnight. -1 on failure."""
    try:
        h, m = t.strip().split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return -1


def _flight_time_penalty(flight: "FlightResult", tp: "TimePrefs",
                         is_outbound: bool) -> float:
    """
    Return the time-of-day penalty (£) for a single flight leg.
    Checks departure time on outbound/inbound and arrival time on outbound/inbound.
    is_outbound=True  → check depart_time against out_depart thresholds
                         and arrive_time against out_arrive thresholds.
    is_outbound=False → check depart_time against inb_depart thresholds
                         and arrive_time against inb_arrive thresholds.
    """
    if not tp or not tp.active:
        return 0.0

    penalty = 0.0

    def _time_mins(t: str) -> int:
        """Parse both 24h and 12h AM/PM formats."""
        t = t.strip()
        if t.upper().endswith(("AM", "PM")):
            from datetime import datetime as _dt
            for fmt in ("%I:%M %p", "%I:%M%p"):
                try:
                    p = _dt.strptime(t.upper(), fmt)
                    return p.hour * 60 + p.minute
                except ValueError:
                    continue
            return -1
        return _hhmm_to_mins(t)

    dep_mins = _time_mins(flight.depart_time)
    arr_mins = _time_mins(flight.arrive_time)

    if is_outbound:
        dep_warn = _hhmm_to_mins(tp.out_depart_warn)
        dep_bad  = _hhmm_to_mins(tp.out_depart_bad)
        arr_warn = _hhmm_to_mins(tp.out_arrive_warn)
        arr_bad  = _hhmm_to_mins(tp.out_arrive_bad)
    else:
        dep_warn = _hhmm_to_mins(tp.inb_depart_warn)
        dep_bad  = _hhmm_to_mins(tp.inb_depart_bad)
        arr_warn = _hhmm_to_mins(tp.inb_arrive_warn)
        arr_bad  = _hhmm_to_mins(tp.inb_arrive_bad)

    # Departure too early
    if dep_mins >= 0 and dep_warn >= 0:
        if dep_bad >= 0 and dep_mins < dep_bad:
            penalty += tp.bad_penalty
        elif dep_mins < dep_warn:
            penalty += tp.warn_penalty

    # Arrival too late
    if arr_mins >= 0 and arr_warn >= 0:
        if arr_bad >= 0 and arr_mins >= arr_bad:
            penalty += tp.bad_penalty
        elif arr_mins >= arr_warn:
            penalty += tp.warn_penalty

    return penalty


STOP_PENALTY_PER_LEG = 50.0   # £ added to score per stop per leg


def score_group_trip(outbound_legs: list[FlightResult],
                     inbound_legs:  list[FlightResult],
                     penalty_per_hour: float,
                     time_prefs: "TimePrefs | None" = None) -> GroupTrip:
    """
    Score a group trip combination.

    Score = total_cost
          + (arrival_spread_hours  × penalty_per_hour)
          + (departure_spread_hours × penalty_per_hour)
          + (stops × STOP_PENALTY_PER_LEG)  -- prefer direct
          + time-of-day penalties per leg per traveller
    """
    total_cost = sum(f.price_val for f in outbound_legs + inbound_legs)
    total_cost += sum(
        int(f.stops) * STOP_PENALTY_PER_LEG
        for f in outbound_legs + inbound_legs
        if f.stops.isdigit()
    )
    if time_prefs and time_prefs.active:
        total_cost += sum(_flight_time_penalty(f, time_prefs, True)  for f in outbound_legs)
        total_cost += sum(_flight_time_penalty(f, time_prefs, False) for f in inbound_legs)

    arr_spread = _spread_minutes(outbound_legs, "arrive_minutes")
    dep_spread = _spread_minutes(inbound_legs,  "depart_minutes") if inbound_legs else 0

    arr_hours  = (arr_spread / 60) if arr_spread >= 0 else 0.5
    dep_hours  = (dep_spread / 60) if (dep_spread >= 0 and inbound_legs) else 0.0

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


def _flights_by_dest(flights_per_traveller):
    """
    Pre-group per-traveller flight lists by shared destination.
    Returns {dest: [flights_for_t0, flights_for_t1, ...]}
    Only includes destinations where EVERY traveller has at least one flight.
    """
    dest_sets = [{f.destination for f in flights} for flights in flights_per_traveller]
    if not dest_sets:
        return {}
    shared = set.intersection(*dest_sets)
    result = {}
    for dest in shared:
        per_t = [[f for f in flights if f.destination == dest]
                 for flights in flights_per_traveller]
        if all(per_t):
            result[dest] = per_t
    return result


def _flights_by_orig(flights_per_traveller):
    """Same but grouped by shared origin (for inbound legs)."""
    orig_sets = [{f.origin for f in flights} for flights in flights_per_traveller]
    if not orig_sets:
        return {}
    shared = set.intersection(*orig_sets)
    result = {}
    for orig in shared:
        per_t = [[f for f in flights if f.origin == orig]
                 for flights in flights_per_traveller]
        if all(per_t):
            result[orig] = per_t
    return result


def build_and_rank_trips(search_results, params, max_combinations=50_000):
    """
    Pair every outbound option for each traveller with every inbound option,
    group by shared destination/origin, score each GroupTrip, and return ranked.

    Pre-groups by destination/origin BEFORE iterproduct to avoid the N^T explosion
    that comes from multiplying all flights across all home airports and then filtering.
    The correct count is: sum over each shared-dest of product(flights_to_dest per traveller).
    For one-way trips, inbound_legs is empty and only arrival sync is scored.
    """
    traveller_names  = [t.name for t in params.travellers]
    out_by_traveller = [search_results["outbound"][n] for n in traveller_names]

    if any(len(x) == 0 for x in out_by_traveller):
        missing = [traveller_names[i] for i, x in enumerate(out_by_traveller) if not x]
        print(f"  \u26a0\ufe0f  No outbound flights found for: {', '.join(missing)}")

    # Pre-group by destination — this is the key fix.
    # Without this, math.prod([all_flights_traveller_0, all_flights_traveller_1, ...])
    # counts combinations where people fly to DIFFERENT destinations, which is nonsense
    # and explodes: 43^5 = 147M for a 5-"traveller" (5-home-airport) search.
    out_by_dest = _flights_by_dest(out_by_traveller)
    if not out_by_dest:
        print("  \u274c  No shared destination found where every traveller has flights.")
        return []

    total_out = sum(math.prod(len(x) for x in per_t) for per_t in out_by_dest.values())

    # ── One-way ───────────────────────────────────────────────────────────────
    if params.one_way:
        print(f"\n  \U0001f522  Evaluating {total_out:,} outbound combinations "
              f"across {len(out_by_dest)} shared destination(s) (one-way)...")

        if total_out > max_combinations:
            print(f"  \u26a0\ufe0f  Capping at cheapest 5 per traveller per destination.")
        def _prune(flights, direct_only):
            direct   = [f for f in flights if f.stops == "0"]
            indirect = [f for f in flights if f.stops != "0"]
            if direct_only:
                # direct_only: only keep direct; fall back to indirect if none found
                return sorted(direct, key=lambda f: f.price_val)[:5] or \
                       sorted(indirect, key=lambda f: f.price_val)[:5]
            # Mixed: always keep up to 3 cheapest direct, fill rest with cheapest indirect
            kept = sorted(direct, key=lambda f: f.price_val)[:3]
            kept += sorted(indirect, key=lambda f: f.price_val)[:5 - len(kept)]
            return kept
            out_by_dest = {
                dest: [_prune(x, params.direct_only) for x in per_t]
                for dest, per_t in out_by_dest.items()
            }

        trips = []
        seen = set()
        for dest, per_traveller in out_by_dest.items():
            for out_combo in iterproduct(*per_traveller):
                if params.direct_only and any(f.stops != "0" for f in out_combo):
                    continue
                key = tuple(
                    (f.traveller, f.origin, f.destination, f.depart_date, f.depart_time, f.price)
                    for f in out_combo
                )
                if key in seen:
                    continue
                seen.add(key)
                trip = score_group_trip(list(out_combo), [], params.sync_penalty_per_hour, params.time_prefs)
                if params.max_total_price and trip.total_cost > params.max_total_price:
                    continue
                trips.append(trip)
        trips.sort(key=lambda t: t.score)
        return trips

    # ── Return trip ───────────────────────────────────────────────────────────
    inb_by_traveller = [search_results["inbound"][n] for n in traveller_names]

    if any(len(x) == 0 for x in inb_by_traveller):
        missing = [traveller_names[i] for i, x in enumerate(inb_by_traveller) if not x]
        print(f"  \u26a0\ufe0f  No inbound flights found for: {', '.join(missing)}")

    inb_by_orig = _flights_by_orig(inb_by_traveller)
    if not inb_by_orig:
        print("  \u274c  No shared return origin found where every traveller has flights.")
        return []

    total_inb    = sum(math.prod(len(x) for x in per_t) for per_t in inb_by_orig.values())
    total_combos = total_out * total_inb
    print(f"\n  \U0001f522  Evaluating up to {total_combos:,} combinations "
          f"({len(out_by_dest)} shared destination(s), "
          f"{len(inb_by_orig)} shared return origin(s))...")

    if total_combos > max_combinations:
        print(f"  \u26a0\ufe0f  Large search space — capping at cheapest 5 per traveller per leg/airport.")
        def _prune(flights, direct_only):
            direct   = [f for f in flights if f.stops == "0"]
            indirect = [f for f in flights if f.stops != "0"]
            if direct_only:
                # direct_only: keep direct only; fall back to indirect if none
                return sorted(direct, key=lambda f: f.price_val)[:5] or \
                       sorted(indirect, key=lambda f: f.price_val)[:5]
            # Mixed: always keep up to 3 cheapest direct, fill rest with cheapest indirect
            kept = sorted(direct, key=lambda f: f.price_val)[:3]
            kept += sorted(indirect, key=lambda f: f.price_val)[:5 - len(kept)]
            return kept
        out_by_dest = {
            dest: [_prune(x, params.direct_only) for x in per_t]
            for dest, per_t in out_by_dest.items()
        }
        inb_by_orig = {
            orig: [_prune(x, params.direct_only) for x in per_t]
            for orig, per_t in inb_by_orig.items()
        }

    trips = []
    seen  = set()
    for dest, out_per_t in out_by_dest.items():
        for out_combo in iterproduct(*out_per_t):
            if params.direct_only and any(f.stops != "0" for f in out_combo):
                continue
            for orig, inb_per_t in inb_by_orig.items():
                for inb_combo in iterproduct(*inb_per_t):
                    if params.direct_only and any(f.stops != "0" for f in inb_combo):
                        continue
                    key = tuple(
                        (f.traveller, f.origin, f.destination,
                         f.depart_date, f.depart_time, f.price)
                        for f in list(out_combo) + list(inb_combo)
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    trip = score_group_trip(
                        list(out_combo), list(inb_combo),
                        params.sync_penalty_per_hour,
                        params.time_prefs,
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
        trip.display(i, show_bike=show_bike)

    if len(trips) > top_n:
        print(f"  … and {len(trips) - top_n} more combinations.\n")

    print(f"  💡 Copy any link above into your browser to open that flight on Google Flights.\n")



# ─────────────────────────────────────────────
# Post-search Claude summary
# ─────────────────────────────────────────────

def _flights_to_digest(flights: list, max_items: int = 40,
                        lean: bool = False) -> list[dict]:
    """
    Serialise flights for storage or Claude consumption.
    lean=True: strips fields not needed for filtering/reanalysis, reducing token count.
    lean=False: full set of fields (for storage and AI summaries).
    """
    out = []
    for f in flights[:max_items]:
        if lean:
            d = {
                "origin":      f.origin,
                "destination": f.destination,
                "date":        f.depart_date,
                "depart":      f.depart_time,
                "arrive":      f.arrive_time,
                "stops":       f.stops,
                "price_gbp":   round(f.price_val),
            }
        else:
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


def _trips_to_digest(trips: list, max_items: int = 20,
                     lean: bool = False) -> list[dict]:
    out = []
    for t in trips[:max_items]:
        if hasattr(t, "outbound_legs"):
            out.append({
                "total_price_gbp":       round(t.total_cost) if lean else t.total_cost,
                "score":                 round(t.score, 1),
                "arrival_spread_mins":   t.arrival_spread_mins,
                "departure_spread_mins": t.departure_spread_mins,
                "outbound_legs": _flights_to_digest(t.outbound_legs, lean=lean),
                "inbound_legs":  _flights_to_digest(t.inbound_legs,  lean=lean),
            })
        elif hasattr(t, "outbound") and hasattr(t, "inbound"):
            out.append({
                "total_price_gbp": t.total_price,
                "outbound": _flights_to_digest([t.outbound], lean=lean)[0],
                "inbound":  _flights_to_digest([t.inbound],  lean=lean)[0],
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
    parser.add_argument("--no-resume", action="store_true",
                        help="Ignore any saved checkpoint and start the search from scratch")
    parser.add_argument("--no-feasibility", action="store_true",
                        help="Skip the quick feasibility check and go straight to full search")
    parser.add_argument("--max-age", type=float, default=7.0, metavar="DAYS",
                        help="Re-run search if cached result is older than DAYS (default: 7). "
                             "Use 0 to always run a fresh search.")
    parser.add_argument("--reanalyse", nargs="+", metavar=("SEARCH_REF", "INSTRUCTION"),
                        help="Re-analyse a saved search without re-running it.\n"
                             "SEARCH_REF: search ID (e.g. 003), 'previous', or part of the name.\n"
                             "INSTRUCTION: optional filter/sort instruction in plain English.\n"
                             "Examples:\n"
                             "  --reanalyse previous\n"
                             "  --reanalyse 003 'show cheapest options'\n"
                             "  --reanalyse previous 'only where Charlie returns via LGW'")
    parser.add_argument("--list-searches", action="store_true",
                        help="List all saved searches and exit")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Auto-confirm prompts (e.g. run new search after reanalysis)")
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

    # ── --list-searches: show saved searches and exit ─────────────────────────
    if getattr(args, 'list_searches', False):
        if REANALYSE_AVAILABLE:
            list_searches()
        elif STORE_AVAILABLE:
            print(f"\n  {'═'*62}")
            print(f"  📂  SAVED SEARCHES  (~/.flightfinder/searches/)")
            print(f"  {'═'*62}")
            print(store_describe())
        else:
            print("  ❌  search_store.py not found.")
        sys.exit(0)

    # ── --reanalyse: re-analyse a saved search and exit ───────────────────────
    if getattr(args, 'reanalyse', None):
        if not REANALYSE_AVAILABLE:
            print("  ❌  reanalyse.py not found.")
            sys.exit(1)
        reanalyse_args = args.reanalyse
        search_ref     = reanalyse_args[0]
        instruction    = " ".join(reanalyse_args[1:]) if len(reanalyse_args) > 1 \
                         else "show all results ranked by score"
        ra_result = do_reanalyse(search_ref, instruction, args.api_key, top_n=args.top)

        # If Claude determined a new search is needed, run it now
        if ra_result and ra_result.get("new_search_needed"):
            new_query = ra_result.get("_new_query", "").strip()
            if not new_query:
                print("  ❌  No synthesised query returned — cannot run new search.")
                sys.exit(1)

            # Confirm with the user (skip if --yes / non-interactive)
            if sys.stdin.isatty() and not getattr(args, 'yes', False):
                print(f"  ❓  Run a new search with the above query? [Y/n] ", end="", flush=True)
                answer = input().strip().lower()
                if answer not in ("", "y", "yes"):
                    print("  ↩️   New search cancelled.")
                    sys.exit(0)

            print(f"\n  🚀  Launching new search...\n{'═'*64}\n")
            # Fall through to the main search pipeline using the synthesised query
            query = new_query
            # Skip re-prompting for query below
            args.query       = query
            args.interactive = False
        else:
            sys.exit(0)

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
    if not params.one_way and not params.shared_inbound_origins:
        print("❌  Claude couldn't determine return origin airports.")
        print("    If this is a one-way trip, say 'one-way' explicitly in your query.")
        sys.exit(1)

    # Step 2: Feasibility check (sample search before committing to full run)
    if not getattr(args, 'no_feasibility', False):
        feasible, params = feasibility_check(params, args.api_key, debug=args.debug)
        if not feasible:
            print("  💡  Tip: try broadening your destination airports, adjusting dates,")
            print("      or rephrasing your query and running again.\n")
            sys.exit(0)

    # Step 3: Check persistent store for a cached result
    key_hash       = _params_key_hash(params)
    max_age        = getattr(args, 'max_age', 7.0)
    cached_record  = None
    search_results = None
    trips          = None

    if STORE_AVAILABLE and max_age > 0:
        cached_record = store_find(key_hash, max_age_days=max_age)

    if cached_record:
        # Reconstruct search_results from stored dicts
        raw = cached_record.get("raw_results", {})
        search_results = {
            leg_type: {
                name: [_dict_to_flight(f) for f in flights]
                for name, flights in by_name.items()
            }
            for leg_type, by_name in raw.items()
        }

    # Step 4: Run live search if no valid cache
    if search_results is None:
        resume = not getattr(args, 'no_resume', False)
        search_results = run_all_searches(params, debug=args.debug, resume=resume, max_age=max_age)

    # Step 5: Build all combinations, score and rank
    trips = build_and_rank_trips(search_results, params)

    # Step 5b: Live bike fee lookup (--bike flag OR bike keywords in query)
    BIKE_KEYWORDS = ("bike", "bicycle", "cycling", "bikepacking", "cycle")
    query_wants_bike = any(kw in query.lower() for kw in BIKE_KEYWORDS)
    show_bike = getattr(args, 'bike', False) or query_wants_bike
    if query_wants_bike and not getattr(args, 'bike', False):
        print("  🚲  Bike/cycling detected in query — looking up airline bike fees..\n")

    bike_cache = {}
    if show_bike and BIKE_AVAILABLE:
        all_airlines = list({f.airline
                             for t in trips
                             for f in t.outbound_legs + t.inbound_legs
                             if f.airline})
        bike_cache = lookup_bike_fees(all_airlines, args.api_key)
        all_flights = [f for t in trips for f in t.outbound_legs + t.inbound_legs]
        attach_bike_fees(all_flights, bike_cache)

    # Step 6: Display
    display_trips(trips, top_n=args.top, show_bike=show_bike)

    if trips:
        summarise_with_claude(query, _trips_to_digest(trips), args.api_key)

    # Step 7: Save to persistent store (skip if we loaded from cache)
    if STORE_AVAILABLE and not cached_record and trips:
        try:
            record = {
                "tool":         "friends",
                "query":        query,
                "key_hash":     key_hash,
                "params":       _params_to_dict(params),
                "raw_results":  _raw_results_to_dict(search_results),
                "trips_digest": _trips_to_digest(trips, max_items=200, lean=True),
            }
            filename = store_save(record)
            print(f"  💾  Search saved as '{record['id']}' → {filename}")
            print(f"      Reanalyse later with: "
                  f"--reanalyse {record['id']} 'your instruction'\n")
        except Exception as e:
            print(f"  ⚠️  Could not save search: {e}\n")

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
