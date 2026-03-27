#!/usr/bin/env python3
"""
FlightFinderConnections.py — Hub airport analyser for FlightFinder searches.

Reads the ~/.flightfinder/searches/ JSON store, extracts every direct flight
found across all searches, then identifies destination airports where travellers
from different origins can both arrive on direct flights — and how close their
arrival times are (sync quality).

Output: an interactive HTML map (Leaflet.js) with airport pins coloured by
best arrival gap (green = tight sync, red = poor sync, grey = only one origin
covered).

Usage:
    python FlightFinderConnections.py
    python FlightFinderConnections.py --out map.html
    python FlightFinderConnections.py --min-searches 2   # hub must appear in N+ searches
    python FlightFinderConnections.py --direction outbound   # outbound | inbound | both
    python FlightFinderConnections.py --open              # open map in browser when done
"""

import argparse
import json
import math
import os
import sys
import webbrowser
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Optional


# ── Airport coordinates (IATA → lat/lon) ─────────────────────────────────────
# Covers airports commonly appearing in UK/European searches.
# Unknown airports are geocoded approximately from IATA prefix heuristics.

AIRPORT_COORDS: dict[str, tuple[float, float]] = {
    # UK & Ireland
    "LHR": (51.477,  -0.461), "LGW": (51.157,  -0.182), "MAN": (53.354,  -2.275),
    "STN": (51.885,   0.235), "LTN": (51.875,  -0.368), "LCY": (51.505,   0.055),
    "BHX": (52.454,  -1.748), "BRS": (51.382,  -2.719), "EXT": (50.734,  -3.414),
    "EDI": (55.950,  -3.373), "GLA": (55.872,  -4.433), "PIK": (55.509,  -4.587),
    "ABZ": (57.202,  -2.198), "INV": (57.543,  -4.047), "NCL": (54.996,  -1.691),
    "LBA": (53.866,  -1.660), "DSA": (53.475,  -1.011), "HUY": (53.574,  -0.350),
    "LPL": (53.334,  -2.850), "EMA": (52.831,  -1.328), "NWI": (52.676,   1.282),
    "SOU": (50.950,  -1.357), "BOH": (50.780,  -1.831), "CWL": (51.397,  -3.344),
    "BFS": (54.658,  -6.216), "DUB": (53.421,  -6.270), "ORK": (51.841,  -8.491),
    "SNN": (52.702,  -8.925),
    # Western Europe — France
    "CDG": (49.009,   2.548), "ORY": (48.723,   2.380), "NCE": (43.658,   7.215),
    "LYS": (45.726,   5.090), "MRS": (43.437,   5.215), "BOD": (44.828,  -0.715),
    "TLS": (43.629,   1.368), "NTE": (47.156,  -1.608), "BIQ": (43.469,  -1.523),
    "MPL": (43.576,   3.963),
    # Western Europe — Spain & Portugal
    "MAD": (40.472,  -3.561), "BCN": (41.297,   2.078), "AGP": (36.675,  -4.499),
    "PMI": (39.551,   2.739), "IBZ": (38.873,   1.373), "ALC": (38.282,  -0.558),
    "VLC": (39.489,  -0.481), "SVQ": (37.418,  -5.893), "BIO": (43.301,  -2.911),
    "LIS": (38.774,  -9.134), "OPO": (41.248,  -8.681), "FAO": (37.014,  -7.966),
    "FNC": (32.698, -16.778),
    # Western Europe — Italy
    "FCO": (41.800,  12.239), "MXP": (45.631,   8.723), "LIN": (45.445,   9.277),
    "VCE": (45.505,  12.352), "NAP": (40.886,  14.291), "BGY": (45.674,   9.704),
    "PSA": (43.683,  10.393), "BLQ": (44.535,  11.289), "PMO": (38.180,  13.091),
    "CTA": (37.467,  15.066), "BRI": (41.139,  16.761), "CAG": (39.251,   9.054),
    "REG": (38.071,  15.651), "TRN": (45.201,   7.649), "GOA": (44.413,   8.838),
    "VRN": (45.396,  10.888), "FLR": (43.810,  11.202), "PSR": (42.432,  14.181),
    "NAP": (40.886,  14.291),
    # Western Europe — Germany, Austria, Switzerland
    "FRA": (50.026,   8.543), "MUC": (48.354,  11.786), "DUS": (51.289,   6.767),
    "TXL": (52.560,  13.288), "BER": (52.366,  13.503), "HAM": (53.630,   9.991),
    "STR": (48.690,   9.222), "CGN": (50.866,   7.143), "HAJ": (52.461,   9.685),
    "VIE": (48.110,  16.570), "GRZ": (46.991,  15.440), "INN": (47.260,  11.344),
    "ZRH": (47.458,   8.548), "GVA": (46.238,   6.109), "BSL": (47.590,   7.530),
    # Western Europe — Benelux, Nordics
    "AMS": (52.310,   4.768), "BRU": (50.901,   4.484), "LGG": (50.637,   5.443),
    "OSL": (60.194,  11.100), "BGO": (60.294,   5.218), "CPH": (55.618,  12.656),
    "ARN": (59.652,  17.919), "GOT": (57.668,  12.292), "HEL": (60.317,  24.963),
    "TLL": (59.413,  24.833), "RIX": (56.924,  23.971), "VNO": (54.634,  25.285),
    # Eastern Europe & Balkans
    "WAW": (52.166,  20.967), "KRK": (50.078,  19.785), "PRG": (50.101,  14.260),
    "BUD": (47.437,  19.261), "OTP": (44.572,  26.102), "SOF": (42.697,  23.411),
    "BEG": (44.818,  20.309), "ZAG": (45.743,  16.069), "LJU": (46.224,  14.457),
    "SKG": (40.520,  22.970), "ATH": (37.936,  23.947), "HER": (35.340,  25.180),
    "RHO": (36.405,  28.086), "CFU": (39.602,  19.911), "JMK": (37.435,  25.348),
    "JTR": (36.399,  25.479), "CHQ": (35.532,  24.150),
    # Turkey & Middle East
    "IST": (41.262,  28.727), "SAW": (40.898,  29.309), "AYT": (36.899,  30.800),
    "DXB": (25.253,  55.365), "DOH": (25.273,  51.608), "AUH": (24.433,  54.651),
    "AMM": (31.723,  35.994), "BEY": (33.821,  35.491),
    # North Africa & Canaries
    "CMN": (33.368,  -7.590), "RAK": (31.607,  -8.036), "TNG": (35.727,  -5.916),
    "TUN": (36.851,  10.227), "CAI": (30.122,  31.406),
    "TFS": (28.045, -16.573), "LPA": (27.932, -15.387), "ACE": (28.945, -13.605),
    "FUE": (28.300, -13.864), "VDE": (27.815, -17.887),
}


def airport_coords(iata: str) -> Optional[tuple[float, float]]:
    return AIRPORT_COORDS.get(iata.upper())


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class DirectFlight:
    """A single direct flight extracted from the store."""
    traveller:   str
    origin:      str   # IATA
    destination: str   # IATA
    date:        str   # YYYY-MM-DD
    depart_time: str   # HH:MM
    arrive_time: str   # HH:MM
    price_val:   float
    search_id:   str
    searched_at: str


@dataclass
class HubAnalysis:
    """Analysis of a single hub airport for a pair of traveller origins."""
    hub:            str        # IATA destination
    traveller_a:    str        # name
    origin_a:       str        # their best origin for this hub
    dest_a:         str        # their destination (= hub for outbound, home for inbound)
    traveller_b:    str
    origin_b:       str
    dest_b:         str
    direction:      str        # "outbound" or "inbound"

    # Best pairing found (minimum arrival gap)
    best_gap_mins:  int        # -1 = no pairing possible
    best_arrive_a:  str        # HH:MM of best pairing
    best_arrive_b:  str
    best_date:      str        # date of best pairing
    best_price_a:   float
    best_price_b:   float

    # All pairings for popup detail — one entry per (date, best gap on that date)
    all_pairings:   list[dict] = field(default_factory=list)  # {date, gap, arrive_a, arrive_b, price_a, price_b}
    n_flights_a:    int        = 0
    n_flights_b:    int        = 0


# ── Store reader ──────────────────────────────────────────────────────────────

def load_store(store_path: Path) -> list[dict]:
    """Load all JSON search records from the store directory."""
    records = []
    if not store_path.exists():
        return records
    for f in sorted(store_path.glob("search_*.json")):
        try:
            with open(f, encoding="utf-8") as fh:
                records.append(json.load(fh))
        except (json.JSONDecodeError, OSError) as e:
            print(f"  ⚠️  Skipping {f.name}: {e}", file=sys.stderr)
    return records


def extract_direct_flights(records: list[dict],
                           traveller_filter: set[str] | None = None) -> list[DirectFlight]:
    """
    Extract every direct flight from raw_results across all stored searches.
    Only includes flights where stops == "0".
    Deduplicates on (traveller, origin, destination, date, depart_time) —
    the same flight scraped across multiple searches is counted only once,
    keeping the most recently searched copy.
    traveller_filter: if provided, only include these traveller names (lowercase).
    """
    # Dict keyed by dedup signature — later records overwrite earlier ones
    # so we keep the freshest searched_at for each unique flight.
    seen: dict[tuple, DirectFlight] = {}

    for rec in records:
        sid      = rec.get("id", "?")
        searched = rec.get("searched_at", "")
        raw      = rec.get("raw_results", {})
        for leg_type in ("outbound", "inbound"):
            by_traveller = raw.get(leg_type, {})
            for traveller, leg_flights in by_traveller.items():
                if traveller_filter and traveller.lower() not in traveller_filter:
                    continue
                for f in leg_flights:
                    if str(f.get("stops", "?")) != "0":
                        continue   # indirect — skip
                    key = (
                        traveller,
                        f.get("origin", ""),
                        f.get("destination", ""),
                        f.get("depart_date", ""),
                        f.get("depart_time", ""),
                    )
                    seen[key] = DirectFlight(
                        traveller   = traveller,
                        origin      = f.get("origin", ""),
                        destination = f.get("destination", ""),
                        date        = f.get("depart_date", ""),
                        depart_time = f.get("depart_time", ""),
                        arrive_time = f.get("arrive_time", ""),
                        price_val   = float(f.get("price_val", 0) or 0),
                        search_id   = sid,
                        searched_at = searched,
                    )
    return list(seen.values())


@dataclass
class SoloAirport:
    """An airport that has direct flights for only one traveller (or one direction)."""
    iata:       str
    direction:  str          # "outbound" or "inbound"
    travellers: list[str]    # which travellers have direct flights here
    n_flights:  int          # total direct flights across all travellers


# ── Gap calculation ────────────────────────────────────────────────────────────

def _time_to_mins(t: str) -> int:
    """
    Convert a time string to minutes since midnight. Returns -1 on failure.
    Handles both 24-hour ('14:35') and 12-hour ('2:35 PM', '7:55 PM') formats.
    """
    if not t:
        return -1
    t = t.strip()
    try:
        # 12-hour with AM/PM: "7:55 PM", "11:25 AM", "12:00 PM"
        if t.upper().endswith("AM") or t.upper().endswith("PM"):
            from datetime import datetime as _dt
            for fmt in ("%I:%M %p", "%I:%M%p"):
                try:
                    parsed = _dt.strptime(t.upper(), fmt)
                    return parsed.hour * 60 + parsed.minute
                except ValueError:
                    continue
            return -1
        # 24-hour: "14:35" or "7:55"
        parts = t.split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        return -1


def arrival_gap_mins(arrive_a: str, arrive_b: str) -> int:
    """Absolute gap in minutes between two HH:MM arrival times."""
    ma, mb = _time_to_mins(arrive_a), _time_to_mins(arrive_b)
    if ma < 0 or mb < 0:
        return -1
    return abs(ma - mb)


# ── Hub analysis ──────────────────────────────────────────────────────────────

def analyse_hubs(
    flights: list[DirectFlight],
    direction: str = "both",          # "outbound" | "inbound" | "both"
    min_searches: int = 1,
) -> list[HubAnalysis]:
    """
    For every destination airport and every pair of distinct travellers,
    find the best (minimum) arrival gap across all stored direct flights.

    direction="outbound": hub = destination (both fly TO same airport)
    direction="inbound":  hub = origin      (both fly FROM same airport)
    direction="both":     analyse both directions separately
    """
    directions = (
        ["outbound", "inbound"] if direction == "both"
        else [direction]
    )

    # Index: direction → hub → traveller → list[DirectFlight]
    index: dict[str, dict[str, dict[str, list[DirectFlight]]]] = {
        d: defaultdict(lambda: defaultdict(list)) for d in directions
    }

    for fl in flights:
        if "outbound" in directions:
            hub = fl.destination
            index["outbound"][hub][fl.traveller].append(fl)
        if "inbound" in directions:
            hub = fl.origin
            index["inbound"][hub][fl.traveller].append(fl)

    results: list[HubAnalysis] = []

    for dir_key in directions:
        for hub, by_traveller in index[dir_key].items():
            travellers = list(by_traveller.keys())
            if len(travellers) < 2:
                continue   # only one traveller has flights here — not a hub

            # Check hub has coords (skip unknown airports)
            if not airport_coords(hub):
                continue

            # Analyse every pair of travellers
            for i in range(len(travellers)):
                for j in range(i + 1, len(travellers)):
                    ta = travellers[i]
                    tb = travellers[j]
                    flights_a = by_traveller[ta]
                    flights_b = by_traveller[tb]

                    # Count unique search IDs — filter by min_searches
                    search_ids = {f.search_id for f in flights_a} | \
                                 {f.search_id for f in flights_b}
                    if len(search_ids) < min_searches:
                        continue

                    # Build per-date best pairings.
                    # Group flights by date for each traveller, then find the
                    # best (minimum gap) pairing on each date that both share.
                    from collections import defaultdict as _dd
                    by_date_a: dict = _dd(list)
                    by_date_b: dict = _dd(list)
                    for fa in flights_a:
                        by_date_a[fa.date].append(fa)
                    for fb in flights_b:
                        by_date_b[fb.date].append(fb)

                    shared_dates = sorted(set(by_date_a) & set(by_date_b))

                    # Also allow cross-date pairings (time-of-day sync across
                    # different search dates) — collect overall best too
                    best_gap  = 9999
                    best_fa   = None
                    best_fb   = None
                    all_pairings = []   # one entry per shared date

                    # Per shared-date bests
                    for d in shared_dates:
                        date_best_gap = 9999
                        date_best_fa  = None
                        date_best_fb  = None
                        for fa in by_date_a[d]:
                            for fb in by_date_b[d]:
                                gap = arrival_gap_mins(fa.arrive_time, fb.arrive_time)
                                if gap < 0:
                                    continue
                                if gap < date_best_gap:
                                    date_best_gap = gap
                                    date_best_fa  = fa
                                    date_best_fb  = fb
                        if date_best_fa is not None:
                            all_pairings.append({
                                "date":        d,
                                "gap":         date_best_gap,
                                "arrive_a":    date_best_fa.arrive_time,
                                "arrive_b":    date_best_fb.arrive_time,
                                "price_a":     date_best_fa.price_val,
                                "price_b":     date_best_fb.price_val,
                                "searched_at": date_best_fa.searched_at,
                            })
                            if date_best_gap < best_gap:
                                best_gap = date_best_gap
                                best_fa  = date_best_fa
                                best_fb  = date_best_fb

                    # Fallback: if no shared dates, use cross-date time-of-day best
                    if best_fa is None:
                        for fa in flights_a:
                            for fb in flights_b:
                                gap = arrival_gap_mins(fa.arrive_time, fb.arrive_time)
                                if gap < 0:
                                    continue
                                if gap < best_gap:
                                    best_gap = gap
                                    best_fa  = fa
                                    best_fb  = fb
                        if best_fa is not None:
                            all_pairings.append({
                                "date":        best_fa.date,
                                "gap":         best_gap,
                                "arrive_a":    best_fa.arrive_time,
                                "arrive_b":    best_fb.arrive_time,
                                "price_a":     best_fa.price_val,
                                "price_b":     best_fb.price_val,
                                "searched_at": best_fa.searched_at,
                            })

                    if best_fa is None:
                        continue

                    # Pick best origin per traveller for this hub
                    origin_a = best_fa.origin if dir_key == "outbound" else hub
                    dest_a   = hub            if dir_key == "outbound" else best_fa.destination
                    origin_b = best_fb.origin if dir_key == "outbound" else hub
                    dest_b   = hub            if dir_key == "outbound" else best_fb.destination

                    results.append(HubAnalysis(
                        hub           = hub,
                        traveller_a   = ta,
                        origin_a      = origin_a,
                        dest_a        = dest_a,
                        traveller_b   = tb,
                        origin_b      = origin_b,
                        dest_b        = dest_b,
                        direction     = dir_key,
                        best_gap_mins = best_gap,
                        best_arrive_a = best_fa.arrive_time,
                        best_arrive_b = best_fb.arrive_time,
                        best_date     = best_fa.date,
                        best_price_a  = best_fa.price_val,
                        best_price_b  = best_fb.price_val,
                        all_pairings  = sorted(all_pairings, key=lambda x: x["date"]),
                        n_flights_a   = len(flights_a),
                        n_flights_b   = len(flights_b),
                    ))

    # Sort: best (smallest) gap first
    results.sort(key=lambda h: h.best_gap_mins)
    return results


def collect_solo_airports(
    flights: list[DirectFlight],
    hub_iatas: set[str],
    direction: str = "both",
) -> list[SoloAirport]:
    """
    Return airports that appear in direct flights but are NOT hubs
    (i.e. only one traveller has direct flights there, or it only appears
    in one direction). These are plotted as grey markers.
    hub_iatas: set of airport codes already shown as coloured hub pins.
    """
    from collections import defaultdict
    directions = ["outbound", "inbound"] if direction == "both" else [direction]
    results = []
    for dir_key in directions:
        by_airport: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for f in flights:
            apt = f.destination if dir_key == "outbound" else f.origin
            by_airport[apt][f.traveller] += 1
        for apt, by_trav in by_airport.items():
            if apt in hub_iatas:
                continue   # already shown as a coloured hub
            if not airport_coords(apt):
                continue   # no coords — can't plot
            results.append(SoloAirport(
                iata       = apt,
                direction  = dir_key,
                travellers = sorted(by_trav.keys()),
                n_flights  = sum(by_trav.values()),
            ))
    # Deduplicate: if same airport appears in both directions, merge into one
    seen: dict[str, SoloAirport] = {}
    for s in results:
        if s.iata in seen:
            existing = seen[s.iata]
            merged_travs = sorted(set(existing.travellers) | set(s.travellers))
            seen[s.iata] = SoloAirport(
                iata       = s.iata,
                direction  = "both",
                travellers = merged_travs,
                n_flights  = existing.n_flights + s.n_flights,
            )
        else:
            seen[s.iata] = s
    return list(seen.values())


# ── Colour scale ──────────────────────────────────────────────────────────────

def gap_to_colour(gap_mins: int) -> str:
    """
    Map arrival gap (minutes) to a hex colour.
    0 min  → deep green  #00c853
    60 min → amber       #ffab00
    120min → red         #d50000
    >120   → dark red    #7f0000
    """
    if gap_mins <= 0:
        return "#00c853"
    if gap_mins >= 120:
        return "#7f0000"

    # 0-60: green → amber
    if gap_mins <= 60:
        t = gap_mins / 60.0
        r = int(0x00 + t * (0xff - 0x00))
        g = int(0xc8 + t * (0xab - 0xc8))
        b = int(0x53 + t * (0x00 - 0x53))
    else:
        # 60-120: amber → red
        t = (gap_mins - 60) / 60.0
        r = int(0xff + t * (0xd5 - 0xff))
        g = int(0xab + t * (0x00 - 0xab))
        b = 0
    return f"#{r:02x}{g:02x}{b:02x}"


def gap_label(gap_mins: int) -> str:
    if gap_mins < 0:    return "unknown"
    if gap_mins == 0:   return "same time"
    h, m = divmod(gap_mins, 60)
    if h and m: return f"{h}h {m}m"
    if h:       return f"{h}h"
    return f"{m}m"


# ── HTML / Leaflet map generator ──────────────────────────────────────────────

STALE_DAYS = 7   # pairings older than this are flagged as potentially stale


def _data_age_days(searched_at: str) -> float:
    """Return how many days ago searched_at (ISO datetime string) was."""
    try:
        from datetime import datetime as _dt, timezone
        scraped = _dt.fromisoformat(searched_at.replace("Z", "+00:00"))
        if scraped.tzinfo is None:
            scraped = scraped.replace(tzinfo=timezone.utc)
        now = _dt.now(tz=timezone.utc)
        return (now - scraped).total_seconds() / 86400
    except Exception:
        return 0.0


def _age_label(searched_at: str) -> tuple[str, bool]:
    """Return (human label, is_stale) for a searched_at timestamp."""
    days = _data_age_days(searched_at)
    stale = days > STALE_DAYS
    if days < 1:
        return "today", stale
    if days < 2:
        return "yesterday", stale
    return f"{int(days)}d ago", stale


def _fmt_date(d: str) -> str:
    """Format YYYY-MM-DD as e.g. Mon 26 Jun."""
    try:
        from datetime import datetime as _dt
        return _dt.strptime(d, "%Y-%m-%d").strftime("%a %-d %b")
    except Exception:
        return d


def _build_popup(hub: str, pairs: list, time_col_header: str, gap_row_label: str) -> str:
    """Build popup HTML for a hub pin. Shows per-date pairings for each traveller pair."""
    # One section per traveller pair
    sections = ""
    for p in sorted(pairs, key=lambda x: x.best_gap_mins):
        # Header row for this pair
        sections += (
            f"<tr class='pair-header'>"
            f"<td colspan='5'>"
            f"<b>{p.traveller_a}</b> {p.origin_a}\u2192{p.dest_a} &nbsp;+&nbsp; "
            f"<b>{p.traveller_b}</b> {p.origin_b}\u2192{p.dest_b}"
            f"</td></tr>"
        )
        # One row per date
        for pr in p.all_pairings:
            colour        = gap_to_colour(pr["gap"])
            total         = pr["price_a"] + pr["price_b"]
            dot           = f"<span style='display:inline-block;width:9px;height:9px;border-radius:50%;background:{colour};margin-right:4px;vertical-align:middle'></span>"
            age_lbl, stale = _age_label(pr.get("searched_at", ""))
            stale_html    = (
                f" <span style='color:#c0392b;font-size:0.68rem' title='Data scraped {age_lbl} — rerun to verify'>&#9888; {age_lbl}</span>"
                if stale else
                f" <span style='color:#27ae60;font-size:0.68rem'>{age_lbl}</span>"
            )
            row_style     = " style='opacity:0.6'" if stale else ""
            sections  += (
                f"<tr{row_style}>"
                f"<td>{_fmt_date(pr['date'])}</td>"
                f"<td>{pr['arrive_a']}</td>"
                f"<td>{pr['arrive_b']}</td>"
                f"<td>{dot}{gap_label(pr['gap'])}</td>"
                f"<td>\u00a3{total:.0f}{stale_html}</td>"
                f"</tr>"
            )

    best    = pairs[0]
    n_dates = sum(len(p.all_pairings) for p in pairs)
    all_pr  = [pr for p in pairs for pr in p.all_pairings]
    n_stale = sum(1 for pr in all_pr if _age_label(pr.get("searched_at",""))[1])
    stale_banner = (
        f"<div style='background:#fff3cd;border:1px solid #ffc107;border-radius:4px;"
        f"padding:5px 8px;margin-bottom:8px;font-size:0.75rem;color:#856404'>"
        f"\u26a0\ufe0f {n_stale} of {n_dates} date(s) based on data older than {STALE_DAYS} days"
        f" \u2014 rerun FlightFinderFriends to verify.</div>"
    ) if n_stale > 0 else ""
    return (
        "<div class='popup'>"
        f"<div class='popup-header'>{hub}</div>"
        f"<div class='popup-sub'>{best.direction.upper()} \u00b7 {n_dates} date(s)</div>"
        f"{stale_banner}"
        "<table class='popup-table'>"
        f"<thead><tr>"
        f"<th>Date</th>"
        f"<th>{best.traveller_a if len(pairs)==1 else time_col_header} A</th>"
        f"<th>{best.traveller_b if len(pairs)==1 else time_col_header} B</th>"
        f"<th>{gap_row_label}</th>"
        f"<th>Total</th>"
        f"</tr></thead>"
        f"<tbody>{sections}</tbody>"
        "</table>"
        "</div>"
    )


def build_map_html(
    hubs: list[HubAnalysis],
    title: str = "FlightFinder — Hub Airport Connections",
    outbound_hubs: list[HubAnalysis] | None = None,
    inbound_hubs:  list[HubAnalysis] | None = None,
    solo_airports: list[SoloAirport] | None = None,
) -> str:
    """
    Generate a self-contained HTML file using Leaflet.js.
    Outbound hubs (arrival sync)   → coloured circle markers.
    Inbound hubs  (departure sync) → coloured square markers.
    Solo airports (one traveller)  → small grey circle/square markers.
    Clicking any pin shows a popup with details.
    Multiple traveller pairs at the same hub are merged into one pin.

    Pass outbound_hubs and inbound_hubs explicitly, or pass hubs for
    backwards-compatible single-direction behaviour.
    """
    # Support both old single-list API and new split API
    if outbound_hubs is None and inbound_hubs is None:
        # Legacy: split by direction field
        outbound_hubs = [h for h in hubs if h.direction == "outbound"]
        inbound_hubs  = [h for h in hubs if h.direction == "inbound"]

    def _best_map(hub_list):
        best: dict[str, HubAnalysis] = {}
        all_:  dict[str, list[HubAnalysis]] = defaultdict(list)
        for h in hub_list:
            all_[h.hub].append(h)
            if h.hub not in best or h.best_gap_mins < best[h.hub].best_gap_mins:
                best[h.hub] = h
        return best, all_

    best_out, all_out = _best_map(outbound_hubs)
    best_inb, all_inb = _best_map(inbound_hubs)

    # Keep legacy variable for centre/zoom calculation
    best_per_hub = {**best_out, **best_inb}
    all_per_hub  = defaultdict(list)
    for k, v in all_out.items(): all_per_hub[k].extend(v)
    for k, v in all_inb.items(): all_per_hub[k].extend(v)

    if not best_per_hub:
        # No hubs found — still generate a map with a message
        centre_lat, centre_lon = 48.0, 10.0
        zoom = 4
    else:
        lats = [airport_coords(hub)[0] for hub in best_per_hub if airport_coords(hub)]
        lons = [airport_coords(hub)[1] for hub in best_per_hub if airport_coords(hub)]
        centre_lat = sum(lats) / len(lats)
        centre_lon = sum(lons) / len(lons)
        zoom = 4

    # _build_popup moved to module level

    markers_js_lines = []

    # ── Circle markers — outbound / arrival sync ──────────────────────────────
    for hub, best in sorted(best_out.items(), key=lambda x: x[1].best_gap_mins):
        coords = airport_coords(hub)
        if not coords:
            continue
        lat, lon = coords
        colour  = gap_to_colour(best.best_gap_mins)
        gap_lbl = gap_label(best.best_gap_mins)
        pairs   = sorted(all_out[hub], key=lambda x: x.best_gap_mins)
        all_pr  = [pr for p in pairs for pr in p.all_pairings]
        any_stale = any(_age_label(pr.get("searched_at",""))[1] for pr in all_pr)
        stale_suffix = " ⚠ stale data" if any_stale else ""
        popup   = _build_popup(hub, pairs, "Arrives", "Arrival gap")
        escaped = popup.replace("\\", "\\\\").replace("`", "\\`")
        radius  = max(10, 22 - best.best_gap_mins // 8)
        markers_js_lines.append(f"""
  L.circleMarker([{lat}, {lon}], {{
    radius:      {radius},
    fillColor:   "{colour}",
    color:       "#fff",
    weight:      2,
    opacity:     1,
    fillOpacity: 0.92
  }})
  .bindPopup(`{escaped}`, {{maxWidth: 420}})
  .bindTooltip("\u2708 {hub} \u00b7 arrival {gap_lbl}{stale_suffix}", {{permanent: false, direction: "top"}})
  .addTo(map);""")

    # ── Square markers — inbound / departure sync ─────────────────────────────
    # Leaflet has no native square, so we use a DivIcon with a styled div
    for hub, best in sorted(best_inb.items(), key=lambda x: x[1].best_gap_mins):
        coords = airport_coords(hub)
        if not coords:
            continue
        lat, lon = coords
        colour  = gap_to_colour(best.best_gap_mins)
        gap_lbl = gap_label(best.best_gap_mins)
        pairs     = sorted(all_inb[hub], key=lambda x: x.best_gap_mins)
        all_pr    = [pr for p in pairs for pr in p.all_pairings]
        any_stale = any(_age_label(pr.get("searched_at",""))[1] for pr in all_pr)
        stale_suffix = " \u26a0 stale data" if any_stale else ""
        popup   = _build_popup(hub, pairs, "Departs", "Departure gap")
        escaped = popup.replace("\\", "\\\\").replace("`", "\\`")
        size    = max(18, 36 - best.best_gap_mins // 5)
        offset  = size // 2
        markers_js_lines.append(f"""
  L.marker([{lat}, {lon}], {{
    icon: L.divIcon({{
      className: "",
      html: `<div style="
        width:{size}px; height:{size}px;
        background:{colour};
        border:2.5px solid #fff;
        border-radius:3px;
        opacity:0.92;
        box-shadow:0 1px 4px rgba(0,0,0,0.5);
      "></div>`,
      iconSize:   [{size}, {size}],
      iconAnchor: [{offset}, {offset}]
    }})
  }})
  .bindPopup(`{escaped}`, {{maxWidth: 420}})
  .bindTooltip("\U0001f6eb {hub} \u00b7 departure {gap_lbl}{stale_suffix}", {{permanent: false, direction: "top"}})
  .addTo(map);""")

    # ── Grey markers — solo airports (one traveller only) ────────────────────
    if solo_airports:
        for s in solo_airports:
            coords = airport_coords(s.iata)
            if not coords:
                continue
            slat, slon = coords
            trav_str   = ", ".join(s.travellers)
            dir_icon   = "✈" if s.direction in ("outbound", "both") else "🛫"
            dir_label  = s.direction.capitalize()
            popup_html = (
                "<div class='popup'>"
                f"<div class='popup-header'>{s.iata}</div>"
                f"<div class='popup-sub'>{dir_label} · no shared connection</div>"
                f"<div style='padding:6px 0;font-size:0.82rem;color:#555;'>"
                f"Only <b>{trav_str}</b> has direct flights here.<br>"
                f"{s.n_flights} direct flight(s) in store.</div>"
                "</div>"
            )
            escaped = popup_html.replace("\\", "\\\\").replace("`", "\\`")
            tooltip  = f"{dir_icon} {s.iata} · {trav_str} only"

            if s.direction == "inbound":
                # Square — grey
                markers_js_lines.append(f"""
  L.marker([{slat}, {slon}], {{
    icon: L.divIcon({{
      className: "",
      html: `<div style="
        width:14px; height:14px;
        background:#555;
        border:2px solid #888;
        border-radius:2px;
        opacity:0.7;
      "></div>`,
      iconSize:   [14, 14],
      iconAnchor: [7, 7]
    }})
  }})
  .bindPopup(`{escaped}`, {{maxWidth: 320}})
  .bindTooltip("{tooltip}", {{permanent: false, direction: "top"}})
  .addTo(map);""")
            else:
                # Circle — grey
                markers_js_lines.append(f"""
  L.circleMarker([{slat}, {slon}], {{
    radius:      7,
    fillColor:   "#555",
    color:       "#888",
    weight:      2,
    opacity:     0.7,
    fillOpacity: 0.7
  }})
  .bindPopup(`{escaped}`, {{maxWidth: 320}})
  .bindTooltip("{tooltip}", {{permanent: false, direction: "top"}})
  .addTo(map);""")

    markers_js = "\n".join(markers_js_lines)

    # Legend entries
    legend_entries = [
        ("#00c853", "0 – 15 min"),
        ("#66bb00", "15 – 30 min"),
        ("#ffab00", "30 – 60 min"),
        ("#ff5500", "60 – 90 min"),
        ("#d50000", "90 – 120 min"),
        ("#7f0000", "> 120 min"),
    ]
    legend_html = "\n".join(
        f'<div class="legend-row"><span class="legend-dot" style="background:{c}"></span>{lbl}</div>'
        for c, lbl in legend_entries
    )

    no_hubs_msg = "" if best_per_hub else """
<div id="no-hubs-msg">
  No hub airports found with direct flights for 2+ travellers.<br>
  Run more searches with FlightFinderFriends to populate the store.
</div>"""

    summary_count = len(set(best_out) | set(best_inb))
    solo_count    = len(solo_airports) if solo_airports else 0
    out_count = len(best_out)
    inb_count = len(best_inb)
    best_hubs_list = sorted(best_per_hub.values(), key=lambda h: h.best_gap_mins)[:5]
    top_hubs_html = "".join(
        f'<div class="top-hub" style="border-left:4px solid {gap_to_colour(h.best_gap_mins)}">'
        f'<b>{h.hub}</b> <span class="top-gap">{gap_label(h.best_gap_mins)}</span>'
        f'<div class="top-pair">{h.traveller_a} + {h.traveller_b}</div></div>'
        for h in best_hubs_list
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: #0d1117;
    color: #e6edf3;
    height: 100vh;
    display: flex;
    flex-direction: column;
  }}
  #header {{
    padding: 12px 20px;
    background: #161b22;
    border-bottom: 1px solid #30363d;
    display: flex;
    align-items: center;
    gap: 16px;
    flex-shrink: 0;
  }}
  #header h1 {{
    font-size: 1.05rem;
    font-weight: 600;
    color: #f0f6fc;
    letter-spacing: -0.01em;
  }}
  #header .subtitle {{
    font-size: 0.78rem;
    color: #8b949e;
  }}
  #hub-count {{
    margin-left: auto;
    font-size: 0.78rem;
    color: #8b949e;
    white-space: nowrap;
  }}
  #main {{
    display: flex;
    flex: 1;
    overflow: hidden;
  }}
  #sidebar {{
    width: 220px;
    flex-shrink: 0;
    background: #161b22;
    border-right: 1px solid #30363d;
    overflow-y: auto;
    padding: 16px 12px;
    display: flex;
    flex-direction: column;
    gap: 20px;
  }}
  .sidebar-section h3 {{
    font-size: 0.7rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: #8b949e;
    margin-bottom: 10px;
  }}
  .legend-row {{
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 0.78rem;
    color: #c9d1d9;
    margin-bottom: 5px;
  }}
  .legend-dot {{
    width: 12px; height: 12px;
    border-radius: 50%;
    flex-shrink: 0;
    border: 1.5px solid rgba(255,255,255,0.3);
  }}
  .top-hub {{
    padding: 8px 10px;
    background: #0d1117;
    border-radius: 6px;
    margin-bottom: 6px;
    font-size: 0.8rem;
  }}
  .top-hub b {{ font-size: 0.9rem; color: #f0f6fc; }}
  .top-gap {{ color: #8b949e; font-size: 0.75rem; margin-left: 6px; }}
  .top-pair {{ color: #8b949e; font-size: 0.72rem; margin-top: 2px; }}
  #map {{
    flex: 1;
  }}
  /* Popup styles */
  .popup {{ font-family: 'Segoe UI', system-ui, sans-serif; }}
  .popup-header {{
    font-size: 1.1rem; font-weight: 700;
    color: #0d1117; margin-bottom: 2px;
  }}
  .popup-sub {{
    font-size: 0.72rem; color: #666;
    margin-bottom: 10px; text-transform: uppercase;
    letter-spacing: 0.05em;
  }}
  .popup-table {{
    width: 100%; border-collapse: collapse;
    font-size: 0.8rem;
  }}
  .popup-table th {{
    text-align: left; font-weight: 600;
    padding: 3px 6px; background: #f0f6fc;
    font-size: 0.7rem; color: #444;
    text-transform: uppercase; letter-spacing: 0.04em;
  }}
  .popup-table td {{
    padding: 4px 6px; border-bottom: 1px solid #e8e8e8;
    color: #1a1a2e;
  }}
  .pair-header td {{
    background: #e8f0fe; font-size: 0.72rem; color: #333;
    padding: 5px 6px 3px; border-bottom: 1px solid #ccc;
  }}
  .gap-row td {{
    background: #f8f9fa; font-size: 0.78rem; color: #333;
    border-bottom: 2px solid #dee2e6;
  }}
  .popup-footer {{
    margin-top: 8px; font-size: 0.72rem; color: #888;
  }}
  #no-hubs-msg {{
    position: absolute; top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    background: rgba(22,27,34,0.95);
    border: 1px solid #30363d;
    border-radius: 10px;
    padding: 24px 32px;
    text-align: center;
    color: #8b949e;
    font-size: 0.9rem;
    line-height: 1.6;
    z-index: 1000;
    pointer-events: none;
  }}
</style>
</head>
<body>
<div id="header">
  <span style="font-size:1.4rem">✈️</span>
  <div>
    <h1>FlightFinder — Hub Airport Connections</h1>
    <div class="subtitle">Direct flight hubs with best arrival sync · click a pin for details</div>
  </div>
  <div id="hub-count">{out_count} arrival hub{'s' if out_count != 1 else ''} · {inb_count} departure hub{'s' if inb_count != 1 else ''} · {solo_count} ruled out</div>
</div>
<div id="main">
  <div id="sidebar">
    <div class="sidebar-section">
      <h3>Sync gap</h3>
      {legend_html}
    </div>
    <div class="sidebar-section">
      <h3>Shape key</h3>
      <div class="legend-row">
        <svg width="14" height="14" style="flex-shrink:0"><circle cx="7" cy="7" r="6" fill="#8b949e" stroke="#fff" stroke-width="1.5"/></svg>
        Arrival (outbound)
      </div>
      <div class="legend-row">
        <svg width="14" height="14" style="flex-shrink:0"><rect x="1" y="1" width="12" height="12" rx="2" fill="#8b949e" stroke="#fff" stroke-width="1.5"/></svg>
        Departure (return)
      </div>
      <div class="legend-row" style="margin-top:8px;opacity:0.7">
        <svg width="14" height="14" style="flex-shrink:0"><circle cx="7" cy="7" r="5" fill="#555" stroke="#888" stroke-width="1.5"/></svg>
        Ruled out (one traveller)
      </div>
    </div>
    <div class="sidebar-section">
      <h3>Top hubs</h3>
      {top_hubs_html if top_hubs_html else '<div style="font-size:0.78rem;color:#8b949e">None found yet</div>'}
    </div>
  </div>
  <div id="map"></div>
</div>
{no_hubs_msg}
<script>
  var map = L.map('map', {{
    center: [{centre_lat:.3f}, {centre_lon:.3f}],
    zoom: {zoom},
    zoomControl: true,
  }});

  L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/">CARTO</a>',
    subdomains: 'abcd',
    maxZoom: 19
  }}).addTo(map);

{markers_js}
</script>
</body>
</html>"""
    return html


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Analyse FlightFinder search store for hub airports with direct flights "
                    "and good arrival sync between travellers."
    )
    parser.add_argument("--store", default=str(Path.home() / ".flightfinder" / "searches"),
                        help="Path to search store directory (default: ~/.flightfinder/searches)")
    parser.add_argument("--out", default="hub_connections.html",
                        help="Output HTML file (default: hub_connections.html)")
    parser.add_argument("--direction", choices=["outbound", "inbound", "both"], default="both",
                        help="Which leg direction to analyse (default: both)")
    parser.add_argument("--min-searches", type=int, default=1, metavar="N",
                        help="Only show hubs appearing in at least N searches (default: 1)")
    parser.add_argument("--travellers", metavar="NAME", nargs="+",
                        help="Only analyse these traveller names (case-insensitive). "
                             "E.g. --travellers Mike Charlie")
    parser.add_argument("--debug-airport", metavar="IATA",
                        help="Print every flight involving this airport from the store "
                             "and exit — useful for diagnosing unexpected hub results. "
                             "E.g. --debug-airport FCO")
    parser.add_argument("--open", action="store_true",
                        help="Open the map in a browser when done")
    args = parser.parse_args()

    store_path = Path(args.store)
    print(f"\n✈️   FlightFinder Connections Analyser")
    print(f"{'─'*44}")
    print(f"  Store:     {store_path}")
    print(f"  Direction: {args.direction}")
    print(f"  Min searches per hub: {args.min_searches}\n")

    # Load
    records = load_store(store_path)
    if not records:
        print(f"  ❌  No search records found in {store_path}")
        print(f"      Run FlightFinderFriends.py first to build the store.\n")
        sys.exit(1)
    print(f"  📂  Loaded {len(records)} search record(s)")

    # Extract direct flights
    traveller_filter = (
        {n.strip().lower() for n in args.travellers}
        if getattr(args, "travellers", None) else None
    )
    if traveller_filter:
        print(f"  🔍  Filtering to travellers: {', '.join(sorted(t.title() for t in traveller_filter))}\n")
    flights = extract_direct_flights(records, traveller_filter=traveller_filter)
    # ── Debug mode: dump all flights for a specific airport and exit ─────────
    if getattr(args, "debug_airport", None):
        target = args.debug_airport.upper()
        print(f"\n  🔍  All stored flights involving {target}:\n")
        raw_records = load_store(store_path)
        found = False
        for rec in raw_records:
            raw = rec.get("raw_results", {})
            for leg_type, by_trav in raw.items():
                for trav, leg_flights in by_trav.items():
                    for f in leg_flights:
                        if f.get("origin") == target or f.get("destination") == target:
                            found = True
                            stops_raw = f.get("stops", "?")
                            is_direct = str(stops_raw) == "0"
                            flag = "✅ DIRECT" if is_direct else f"❌ {stops_raw} stop(s)"
                            print(f"  [{rec.get('id','?')}] [{leg_type}] {trav}: "
                                  f"{f.get('origin')}→{f.get('destination')} "
                                  f"{f.get('depart_date','')} "
                                  f"dep:{f.get('depart_time','')} arr:{f.get('arrive_time','')} "
                                  f"{flag}")
        if not found:
            print(f"  No flights found involving {target} in the store.")
        print()
        sys.exit(0)

    direct_count = len(flights)
    if not direct_count:
        print(f"  ⚠️  No direct flights found in store.")
        print(f"      All stored searches may have used indirect flights only.\n")
    else:
        travellers = sorted({f.traveller for f in flights})
        airports   = sorted({f.destination for f in flights} | {f.origin for f in flights})
        print(f"  ✈️   {direct_count} direct flights across "
              f"{len(travellers)} traveller(s), "
              f"{len(airports)} airport(s)")
        print(f"  👤  Travellers: {', '.join(travellers)}")

    # Analyse hubs
    hubs = analyse_hubs(flights, direction=args.direction, min_searches=args.min_searches)

    if not hubs:
        print(f"\n  ⚠️  No hub airports found where 2+ travellers have direct flights.")
        print(f"      This usually means searches so far only cover single-traveller routes,")
        print(f"      or all flights were indirect. Still generating map...\n")
    else:
        print(f"\n  📍  {len(hubs)} hub pairing(s) found\n")
        print(f"  {'Hub':<6}  {'Travellers':<24}  {'Best gap':<10}  Arrives")
        print(f"  {'─'*60}")
        for h in hubs[:15]:
            print(f"  {h.hub:<6}  {h.traveller_a + ' + ' + h.traveller_b:<24}  "
                  f"{gap_label(h.best_gap_mins):<10}  "
                  f"{h.best_arrive_a} / {h.best_arrive_b}")
        if len(hubs) > 15:
            print(f"  … and {len(hubs) - 15} more (see map for full detail)")

    # Generate map
    out_hubs   = [h for h in hubs if h.direction == "outbound"]
    inb_hubs   = [h for h in hubs if h.direction == "inbound"]
    hub_iatas  = {h.hub for h in hubs}
    solo_apts  = collect_solo_airports(flights, hub_iatas, direction=args.direction)
    if solo_apts:
        print(f"  ⬜  {len(solo_apts)} solo airport(s) (grey — one traveller only): "
              f"{', '.join(s.iata for s in sorted(solo_apts, key=lambda x: x.iata))}")
    html = build_map_html(hubs, outbound_hubs=out_hubs, inbound_hubs=inb_hubs,
                          solo_airports=solo_apts)
    out_path = Path(args.out)
    out_path.write_text(html, encoding="utf-8")
    print(f"\n  🗺️   Map saved → {out_path.resolve()}\n")

    if args.open:
        webbrowser.open(out_path.resolve().as_uri())


if __name__ == "__main__":
    main()
