"""
reanalyse.py — Re-analyse a previously saved flight search without re-running it.

Filtering and sorting is done directly in Python against the stored structured data.
Claude is only called for one purpose: detecting whether the instruction requires a
new search (different dates / airports not in the store) and synthesising the new query.
It is NOT used to filter or rank results — that is all Python.

Usage (from FlightFinderFriends.py):
    python FlightFinderFriends.py --reanalyse "003"
    python FlightFinderFriends.py --reanalyse "previous" "show cheapest options"
    python FlightFinderFriends.py --reanalyse "previous" "only where Charlie returns via LGW"
"""

import json
import re
import sys
import time
from typing import Optional

try:
    import anthropic
except ImportError:
    print("❌  Please install: pip install anthropic")
    sys.exit(1)


# ── New-search detection (the only Claude call remaining) ─────────────────────

NEW_SEARCH_SYSTEM = """You are a travel search assistant. The user has a stored flight search
and wants to either filter/explore the stored results OR run a new search with different
parameters.

You will be given:
1. The user's instruction
2. Metadata about the stored search: what dates and airports are present

Decide: does this instruction require DATA NOT IN THE STORE?
A new search is needed if the instruction asks for:
- Different specific dates than those stored
- Airports not present in any stored leg
- Removing date flexibility (asking for an exact date when stored data used ±N days)

If a new search IS needed, synthesise a complete natural-language query for it by:
- Starting from the original query (same travellers, airports, preferences)
- Replacing only what the instruction changes (dates, flexibility, specific airports)
- Preserving traveller names, home airports, cabin class, and any "direct only" preference

Return ONLY a JSON object, no prose, no markdown fences:
{
  "new_search_needed": true | false,
  "reason": "one sentence explanation",
  "new_query": "full natural language query string, or empty string if not needed"
}"""


def _check_new_search_needed(
    instruction: str,
    original_query: str,
    store_meta: dict,
    api_key: str,
) -> dict:
    """
    Ask Claude only whether a new search is needed and — if so — what query to use.
    Sends ~200 tokens (instruction + metadata), not the full digest.
    """
    payload = {
        "instruction":    instruction,
        "original_query": original_query,
        "stored_data_meta": store_meta,
    }
    last_exc = None
    for attempt in range(3):
        try:
            client = anthropic.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=512,
                system=NEW_SEARCH_SYSTEM,
                messages=[{"role": "user", "content": json.dumps(payload)}],
            )
            raw = msg.content[0].text.strip()
            raw = re.sub(r"^```[a-z]*\n?", "", raw)
            raw = re.sub(r"\n?```$",       "", raw)
            return json.loads(raw)
        except Exception as e:
            last_exc = e
            if "rate_limit" in str(e).lower() or "429" in str(e):
                wait = 20 * (attempt + 1)
                print(f"  ⏳  Rate limit — waiting {wait}s (attempt {attempt+1}/3)...")
                time.sleep(wait)
            else:
                raise
    raise last_exc


def _store_meta(trips_digest: list, original_query: str) -> dict:
    """Extract compact metadata about what's in the store — sent to Claude instead of digest."""
    out_dates, inb_dates = set(), set()
    out_airports, inb_airports = set(), set()
    travellers = set()
    for t in trips_digest:
        for leg in t.get("outbound_legs", []):
            out_dates.add(leg.get("date", ""))
            out_airports.add(leg.get("origin", ""))
            out_airports.add(leg.get("destination", ""))
            travellers.add(leg.get("traveller", ""))
        for leg in t.get("inbound_legs", []):
            inb_dates.add(leg.get("date", ""))
            inb_airports.add(leg.get("origin", ""))
            inb_airports.add(leg.get("destination", ""))
            travellers.add(leg.get("traveller", ""))
    return {
        "outbound_dates":   sorted(d for d in out_dates if d),
        "inbound_dates":    sorted(d for d in inb_dates if d),
        "outbound_airports": sorted(a for a in out_airports if a),
        "inbound_airports":  sorted(a for a in inb_airports if a),
        "travellers":        sorted(t for t in travellers if t),
        "trip_count":        len(trips_digest),
    }


# ── Pure-Python filter / sort engine ─────────────────────────────────────────

class FilterSpec:
    """Parsed representation of a reanalysis instruction."""
    def __init__(self):
        self.sort_key       = "score"       # score | total_price_gbp | arrival_spread_mins | departure_spread_mins
        self.sort_ascending = True
        self.only_direct    = False
        self.max_price      = None          # float
        self.min_price      = None
        self.traveller_out_origin  = {}     # {name: airport}
        self.traveller_out_dest    = {}
        self.traveller_inb_origin  = {}
        self.traveller_inb_dest    = {}
        self.max_arrival_spread    = None   # minutes
        self.max_departure_spread  = None
        self.description           = ""


def parse_instruction(instruction: str) -> FilterSpec:
    """
    Convert a plain-English instruction into a FilterSpec using keyword matching.
    Handles the vast majority of sensible filter/sort requests without Claude.
    """
    spec = FilterSpec()
    ins  = instruction.lower()

    # ── Sort key ─────────────────────────────────────────────────────────────
    if any(w in ins for w in ("cheapest", "least expensive", "lowest price", "by price", "by cost")):
        spec.sort_key = "total_price_gbp"
        spec.description = "sorted by cheapest first"
    elif any(w in ins for w in ("most expensive", "highest price")):
        spec.sort_key = "total_price_gbp"
        spec.sort_ascending = False
        spec.description = "sorted by most expensive first"
    elif any(w in ins for w in ("tightest arrival", "arrival gap", "best arrival sync",
                                 "most synchronised arrival", "arrival spread")):
        spec.sort_key = "arrival_spread_mins"
        spec.description = "sorted by tightest arrival gap"
    elif any(w in ins for w in ("tightest departure", "departure gap", "best departure sync",
                                 "departure spread")):
        spec.sort_key = "departure_spread_mins"
        spec.description = "sorted by tightest departure gap"
    elif any(w in ins for w in ("quickest", "fastest", "shortest", "time efficient",
                                 "efficient", "best score")):
        spec.sort_key = "score"
        spec.description = "sorted by best score (cost + sync)"
    else:
        spec.description = "sorted by score (default)"

    # ── Direct flights filter ─────────────────────────────────────────────────
    if any(w in ins for w in ("direct only", "only direct", "no stops", "non-stop", "nonstop")):
        spec.only_direct = True
        spec.description += ", direct flights only"

    # ── Price filter ──────────────────────────────────────────────────────────
    under = re.search(r"under\s*[£$€]?\s*(\d+)", ins)
    over  = re.search(r"over\s*[£$€]?\s*(\d+)", ins)
    if under:
        spec.max_price = float(under.group(1))
        spec.description += f", under £{spec.max_price:.0f}"
    if over:
        spec.min_price = float(over.group(1))
        spec.description += f", over £{spec.min_price:.0f}"

    # ── Arrival / departure spread filter ────────────────────────────────────
    same_day = re.search(r"(arriving|arrive|arrival).*same\s*(day|time)", ins)
    if same_day:
        spec.max_arrival_spread = 120
        spec.description += ", arrival gap ≤ 2h"
    arr_limit = re.search(r"arrival\s*(?:gap\s*)?(?:under|less than|within)\s*(\d+)\s*(h|hr|hour|min)", ins)
    if arr_limit:
        n, unit = int(arr_limit.group(1)), arr_limit.group(2)
        spec.max_arrival_spread = n * 60 if unit.startswith("h") else n
        spec.description += f", arrival gap ≤ {arr_limit.group(1)}{arr_limit.group(2)}"

    # ── Per-traveller airport filters ─────────────────────────────────────────
    # Patterns: "Charlie returns via LGW", "Mike flies from MAN", "Mike outbound from MAN"
    # "NAME returns/flies back/inbound via/from IATA"
    # "NAME flies out/outbound from IATA"  or "NAME to IATA"
    iata = r"([A-Z]{3})"

    for m in re.finditer(
        r"(\w+)\s+(?:returns?|flies?\s+back|inbound|return\s+leg).*?(?:via|from|through|at)\s+" + iata,
        instruction, re.IGNORECASE
    ):
        name, airport = m.group(1).capitalize(), m.group(2).upper()
        spec.traveller_inb_origin[name] = airport
        spec.description += f", {name} departs {airport} on return"

    for m in re.finditer(
        r"(\w+)\s+(?:returns?|flies?\s+back|inbound|return\s+leg).*?(?:to|into|arriving)\s+" + iata,
        instruction, re.IGNORECASE
    ):
        name, airport = m.group(1).capitalize(), m.group(2).upper()
        spec.traveller_inb_dest[name] = airport
        spec.description += f", {name} returns to {airport}"

    for m in re.finditer(
        r"(\w+)\s+(?:flies?\s+out(?:bound)?|outbound|going out).*?(?:from|via|out\s+of)\s+" + iata,
        instruction, re.IGNORECASE
    ):
        name, airport = m.group(1).capitalize(), m.group(2).upper()
        spec.traveller_out_origin[name] = airport
        spec.description += f", {name} departs {airport} outbound"

    for m in re.finditer(
        r"(\w+)\s+(?:flies?\s+out(?:bound)?|outbound|going\s+to).*?(?:to|into)\s+" + iata,
        instruction, re.IGNORECASE
    ):
        name, airport = m.group(1).capitalize(), m.group(2).upper()
        spec.traveller_out_dest[name] = airport
        spec.description += f", {name} flies to {airport}"

    spec.description = spec.description.lstrip(", ")
    return spec


def apply_filter(trips_digest: list, spec: FilterSpec) -> list[int]:
    """
    Apply a FilterSpec to the stored trips digest.
    Returns a list of 0-based indices into trips_digest that pass all filters,
    sorted according to spec.sort_key.
    """

    def leg_matches_airport(legs: list[dict], traveller: str,
                            field: str, airport: str) -> bool:
        for leg in legs:
            if leg.get("traveller", "").lower() == traveller.lower():
                return leg.get(field, "").upper() == airport.upper()
        return True   # traveller not in this trip — don't exclude

    passing = []
    for idx, trip in enumerate(trips_digest):
        out_legs = trip.get("outbound_legs", [])
        inb_legs = trip.get("inbound_legs", [])
        all_legs = out_legs + inb_legs

        # Direct-only filter
        if spec.only_direct:
            if any(str(leg.get("stops", "0")) != "0" for leg in all_legs):
                continue

        # Price filters
        price = trip.get("total_price_gbp", 0)
        if spec.max_price is not None and price > spec.max_price:
            continue
        if spec.min_price is not None and price < spec.min_price:
            continue

        # Arrival / departure spread filters
        arr = trip.get("arrival_spread_mins", -1)
        dep = trip.get("departure_spread_mins", -1)
        if spec.max_arrival_spread is not None and arr >= 0 and arr > spec.max_arrival_spread:
            continue
        if spec.max_departure_spread is not None and dep >= 0 and dep > spec.max_departure_spread:
            continue

        # Per-traveller outbound origin
        fail = False
        for name, airport in spec.traveller_out_origin.items():
            if not leg_matches_airport(out_legs, name, "origin", airport):
                fail = True; break
        if fail: continue

        # Per-traveller outbound destination
        for name, airport in spec.traveller_out_dest.items():
            if not leg_matches_airport(out_legs, name, "destination", airport):
                fail = True; break
        if fail: continue

        # Per-traveller inbound origin
        for name, airport in spec.traveller_inb_origin.items():
            if not leg_matches_airport(inb_legs, name, "origin", airport):
                fail = True; break
        if fail: continue

        # Per-traveller inbound destination
        for name, airport in spec.traveller_inb_dest.items():
            if not leg_matches_airport(inb_legs, name, "destination", airport):
                fail = True; break
        if fail: continue

        passing.append(idx)

    # Sort
    key_map = {
        "total_price_gbp":      lambda i: trips_digest[i].get("total_price_gbp", 0),
        "score":                lambda i: trips_digest[i].get("score", 0),
        "arrival_spread_mins":  lambda i: trips_digest[i].get("arrival_spread_mins", 9999),
        "departure_spread_mins":lambda i: trips_digest[i].get("departure_spread_mins", 9999),
    }
    key_fn = key_map.get(spec.sort_key, key_map["score"])
    passing.sort(key=key_fn, reverse=not spec.sort_ascending)
    return passing


# ── Summary builder (pure Python, no Claude) ─────────────────────────────────

def _build_summary(trips_digest: list, indices: list, spec: FilterSpec) -> str:
    if not indices:
        # Describe what IS available to help the user
        prices = [trips_digest[i].get("total_price_gbp", 0) for i in range(len(trips_digest))]
        out_dates = sorted({leg.get("date","") for t in trips_digest
                            for leg in t.get("outbound_legs",[]) if leg.get("date")})
        inb_dates = sorted({leg.get("date","") for t in trips_digest
                            for leg in t.get("inbound_legs",[]) if leg.get("date")})
        parts = [f"No trips match '{spec.description}'."]
        if prices:
            parts.append(f"Stored prices range from £{min(prices):.0f}–£{max(prices):.0f}.")
        if out_dates:
            parts.append(f"Outbound dates in store: {', '.join(out_dates[:6])}{'…' if len(out_dates)>6 else ''}.")
        if inb_dates:
            parts.append(f"Return dates in store: {', '.join(inb_dates[:6])}{'…' if len(inb_dates)>6 else ''}.")
        return " ".join(parts)

    selected = [trips_digest[i] for i in indices]
    prices   = [t.get("total_price_gbp", 0) for t in selected]
    best     = selected[0]  # already sorted

    out_legs = best.get("outbound_legs", [])
    inb_legs = best.get("inbound_legs",  [])
    best_dest  = out_legs[0].get("destination","?") if out_legs else "?"
    best_orig  = inb_legs[0].get("origin","?")      if inb_legs else "?"

    travellers = sorted({leg.get("traveller","") for leg in out_legs + inb_legs if leg.get("traveller")})
    trav_str   = " and ".join(travellers) if travellers else "travellers"

    parts = [f"Found {len(indices)} trip{'s' if len(indices)!=1 else ''} matching: {spec.description}."]
    parts.append(
        f"Best option: {trav_str} fly to {best_dest} "
        f"for £{best.get('total_price_gbp',0):.0f} total"
        + (f", returning from {best_orig}" if inb_legs else "")
        + "."
    )
    if len(indices) > 1:
        parts.append(f"Prices range from £{min(prices):.0f} to £{max(prices):.0f}.")
    return " ".join(parts)


# ── Main entry point ──────────────────────────────────────────────────────────

def reanalyse(
    search_id_or_ref: str,
    instruction: str,
    api_key: str,
    top_n: int = 10,
) -> Optional[dict]:
    from search_store import load, describe_all

    record = load(search_id_or_ref)
    if record is None:
        print(f"\n  ❌  No saved search found matching '{search_id_or_ref}'.")
        print(f"\n  Available searches:\n{describe_all()}")
        return None

    trips_digest = record.get("trips_digest")
    if not trips_digest:
        print(f"\n  ❌  Search '{record['id']}' has no stored trips digest.")
        return None

    original_query = record.get("query", "(unknown)")
    slug           = record.get("slug", "")
    searched_at    = record.get("searched_at", "")

    print(f"\n  🔍  Reanalysing search {record['id']} — {slug}")
    print(f"      Original query: {original_query[:80]}{'…' if len(original_query)>80 else ''}")
    print(f"      Searched at:    {searched_at}")
    print(f"      Trips stored:   {len(trips_digest)}")
    print(f"      Instruction:    {instruction}\n")

    # Step 1: check whether a new search is needed (small Claude call — no digest sent)
    meta = _store_meta(trips_digest, original_query)
    try:
        new_search_result = _check_new_search_needed(
            instruction, original_query, meta, api_key
        )
    except Exception as e:
        print(f"  ⚠️  Could not check for new search ({e}) — proceeding with filter.\n")
        new_search_result = {"new_search_needed": False}

    if new_search_result.get("new_search_needed"):
        new_query = new_search_result.get("new_query", "").strip()
        reason    = new_search_result.get("reason", "")
        print(f"  🔄  New search needed: {reason}")
        if new_query:
            print(f"\n  📝  Synthesised query for new search:")
            print(f"      {new_query}\n")
        result = {
            "new_search_needed": True,
            "_new_query":        new_query,
            "_record":           record,
        }
        return result

    # Step 2: pure-Python filter + sort — no Claude needed
    spec    = parse_instruction(instruction)
    indices = apply_filter(trips_digest, spec)
    summary = _build_summary(trips_digest, indices, spec)

    print(f"  📋  Filter: {spec.description}")
    print(f"  📊  Sorted by: {spec.sort_key}")
    print(f"  ✅  {len(indices)} matching trip(s) "
          f"(showing top {min(top_n, len(indices))})\n")

    if not indices:
        _print_wrapped(summary)
        return {"new_search_needed": False, "indices": [], "_record": record}

    _display_reanalysis_trips(trips_digest, indices[:top_n])

    print(f"\n  {'═'*62}")
    print(f"  📊  SUMMARY\n  {'═'*62}")
    _print_wrapped(summary)

    return {"new_search_needed": False, "indices": indices, "_record": record}


# ── Display helpers ───────────────────────────────────────────────────────────

def _print_wrapped(text: str, width: int = 72, indent: str = "  ") -> None:
    if not text: return
    words = text.split()
    line  = indent
    for word in words:
        if len(line) + len(word) + 1 > width:
            print(line); line = indent + word
        else:
            line = line + (" " if line != indent else "") + word
    if line.strip(): print(line)
    print()


def _display_reanalysis_trips(trips_digest: list, indices: list):
    for rank, idx in enumerate(indices, 1):
        if idx >= len(trips_digest): continue
        t = trips_digest[idx]

        total    = t.get("total_price_gbp", 0)
        score    = t.get("score", 0)
        arr_mins = t.get("arrival_spread_mins", -1)
        dep_mins = t.get("departure_spread_mins", -1)
        out_legs = t.get("outbound_legs", [])
        inb_legs = t.get("inbound_legs",  [])
        n_legs   = len(out_legs) or 1
        per_p    = total / n_legs
        is_ow    = not inb_legs

        arr_label = _spread_label(arr_mins)
        dep_label = _spread_label(dep_mins)

        print(f"  {'═'*62}")
        print(f"  #{rank} (stored #{idx+1})  💰 GBP {total:.0f}  (GBP {per_p:.0f}/person)"
              + ("  ✈ one-way" if is_ow else ""))
        print(f"       📊 Score {score:.1f}  |  🛬 arrival gap: {arr_label}"
              + (f"  |  🛫 dep gap: {dep_label}" if not is_ow else ""))

        if out_legs:
            dest = out_legs[0].get("destination", "?")
            print(f"\n  ── {'OUTBOUND' if not is_ow else 'FLIGHTS'}  (→ {dest})  "
                  f"🛬 arrival gap: {arr_label} {'─'*10}")
            for leg in sorted(out_legs, key=lambda f: f.get("arrive", "") or ""):
                _print_leg(leg, role="outbound")

        if inb_legs:
            orig = inb_legs[0].get("origin", "?")
            print(f"\n  ── RETURN  ({orig} →)  🛫 dep gap: {dep_label} {'─'*12}")
            for leg in sorted(inb_legs, key=lambda f: f.get("depart", "") or ""):
                _print_leg(leg, role="inbound")
        print()


def _print_leg(leg: dict, role: str = ""):
    name    = leg.get("traveller", "?")
    airline = leg.get("airline",   "")
    orig    = leg.get("origin",    "?")
    dest    = leg.get("destination","?")
    date    = leg.get("date",       "?")
    depart  = leg.get("depart",     "?")
    arrive  = leg.get("arrive",     "?")
    price   = leg.get("price_gbp",  0)
    stops   = leg.get("stops",      "?")
    travel  = leg.get("travel_time","")

    airline_str = f"  ✈️  {airline}" if airline else ""
    stops_label = "✈ Direct" if str(stops) == "0" else f"↩ {stops} stop(s)"

    if role == "outbound":
        time_line = f"🛫 {depart}  →  🛬 arrives {arrive}  ◀ sync point"
    elif role == "inbound":
        time_line = f"🛫 departs {depart}  ◀ sync point  →  🛬 {arrive}"
    else:
        time_line = f"🛫 {depart}  →  🛬 {arrive}"

    indent = "      "
    print(
        f"\n{indent}👤 {name:<12}{airline_str}\n"
        f"{indent}   {orig} → {dest}   📅 {date}\n"
        f"{indent}   {time_line}   ⏱ {travel}   {stops_label}\n"
        f"{indent}   💰 GBP {price:.0f}"
    )


def _spread_label(minutes: int) -> str:
    if minutes < 0:   return "unknown"
    if minutes == 0:  return "same time ✅"
    h, m = divmod(minutes, 60)
    if h and m: return f"{h}h {m}m gap"
    if h:       return f"{h}h gap"
    return f"{m}m gap"


def list_searches():
    from search_store import describe_all
    print(f"\n  {'═'*62}")
    print(f"  📂  SAVED SEARCHES  (~/.flightfinder/searches/)")
    print(f"  {'═'*62}")
    print(describe_all())
