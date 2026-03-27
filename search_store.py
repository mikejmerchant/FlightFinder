"""
search_store.py — Persistent search result storage for the Flight Finder suite.

Searches are stored as JSON files in ~/.flightfinder/searches/.
Each file is named with a sequential ID and a short human-readable slug:

    search_001_man-nce_2026-06-26.json
    search_002_lgw-pmo_2026-09-06.json

The store provides:
    - save(search_record)          → filename
    - find_matching(key_hash)      → SearchRecord | None   (cache hit)
    - list_all()                   → [SearchRecord, ...]
    - load(search_id_or_filename)  → SearchRecord
    - describe_all()               → formatted string for display

A SearchRecord is a plain dict with the shape:
    {
        "id":           "003",
        "filename":     "search_003_man-nce_2026-06-26.json",
        "slug":         "man-nce 2026-06-26",
        "query":        "original user query string",
        "tool":         "friends" | "standard" | "advanced",
        "key_hash":     "a3f9c12b4d1e",   # MD5 of normalised params
        "searched_at":  "2026-03-11T19:10:00",
        "params":       { ... },           # serialised FriendsSearchParams etc.
        "raw_results":  { ... },           # outbound/inbound flight dicts
        "trips_digest": [ ... ],           # _trips_to_digest() output
    }
"""

import os
import json
import re
from datetime import datetime
from pathlib import Path


# ── Store location ────────────────────────────────────────────────────────────

def store_dir() -> Path:
    """Return (and create if needed) the search store directory."""
    d = Path.home() / ".flightfinder" / "searches"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Helpers ───────────────────────────────────────────────────────────────────

def _next_id() -> str:
    """Return the next zero-padded 3-digit search ID."""
    existing = _all_files()
    if not existing:
        return "001"
    ids = []
    for f in existing:
        m = re.match(r"search_(\d+)_", f.name)
        if m:
            ids.append(int(m.group(1)))
    return f"{(max(ids) + 1):03d}" if ids else "001"


def _all_files() -> list[Path]:
    return sorted(store_dir().glob("search_*.json"))


def _make_slug(record: dict) -> str:
    """Derive a short human-readable slug from search params."""
    p = record.get("params", {})
    tool = record.get("tool", "")

    if tool == "friends":
        names = "+".join(t["name"] for t in p.get("travellers", []))
        dests = "-".join(p.get("shared_destinations", [])[:2])
        date  = p.get("outbound_date", "")[:10]
        return f"{names}→{dests} {date}"

    if tool == "advanced":
        out_orig = "-".join(p.get("outbound", {}).get("origins", [])[:1])
        out_dest = "-".join(p.get("outbound", {}).get("destinations", [])[:1])
        date     = p.get("outbound", {}).get("date", "")[:10]
        return f"{out_orig}→{out_dest} {date}"

    # standard
    orig = "-".join(p.get("origins", [])[:1])
    dest = "-".join(p.get("destinations", [])[:2])
    date = p.get("depart_date", "")[:10]
    return f"{orig}→{dest} {date}"


def _safe_filename_part(s: str) -> str:
    """Strip characters that are unsafe in filenames."""
    return re.sub(r"[^\w\-]", "_", s)[:40]


# ── Public API ────────────────────────────────────────────────────────────────

def save(record: dict) -> str:
    """
    Save a search record to disk.  Assigns id, filename, slug if not already set.
    Returns the filename (basename only).
    """
    if "id" not in record:
        record["id"] = _next_id()
    if "searched_at" not in record:
        record["searched_at"] = datetime.now().isoformat(timespec="seconds")
    if "slug" not in record:
        record["slug"] = _make_slug(record)

    safe_slug = _safe_filename_part(record["slug"])
    filename  = f"search_{record['id']}_{safe_slug}.json"
    record["filename"] = filename

    path = store_dir() / filename
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, default=str)

    return filename


def find_matching(key_hash: str, max_age_days: float = 7.0) -> dict | None:
    """
    Look for a previously saved search with the same key_hash.
    Returns the record if found AND fresh enough, otherwise None.
    Reports clearly if a stale match is found.
    """
    for path in _all_files():
        try:
            with open(path, encoding="utf-8") as f:
                record = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        if record.get("key_hash") != key_hash:
            continue

        # Found a match — check age
        searched_at = record.get("searched_at", "")
        try:
            age_days = (datetime.now() -
                        datetime.fromisoformat(searched_at)).total_seconds() / 86400
        except ValueError:
            age_days = 999

        if age_days <= max_age_days:
            age_str = _age_label(age_days)
            print(f"\n  💾  Found cached search '{record['id']}' "
                  f"({record.get('slug','')}) from {age_str} ago.")
            print(f"      Using stored results. "
                  f"(Pass --max-age 0 to force a fresh search.)\n")
            return record
        else:
            age_str = _age_label(age_days)
            print(f"\n  🔄  Found cached search '{record['id']}' "
                  f"({record.get('slug','')}) but it is {age_str} old "
                  f"(threshold: {max_age_days:.0f} days).")
            print(f"      Running a fresh search and updating the cache.\n")
            return None

    return None


def load(search_ref: str) -> dict | None:
    """
    Load a search by ID (e.g. "003"), filename, or "previous" / "last".
    Returns None if not found.
    """
    files = _all_files()
    if not files:
        return None

    # "previous" / "last" → most recent
    if search_ref.lower() in ("previous", "last", "latest"):
        path = files[-1]
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    # Numeric ID
    if re.match(r"^\d+$", search_ref):
        padded = search_ref.zfill(3)
        for path in files:
            if path.name.startswith(f"search_{padded}_"):
                with open(path, encoding="utf-8") as f:
                    return json.load(f)

    # Exact filename
    path = store_dir() / search_ref
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    # Partial slug match
    search_ref_lower = search_ref.lower()
    for path in files:
        if search_ref_lower in path.name.lower():
            with open(path, encoding="utf-8") as f:
                return json.load(f)

    return None


def find_route_flights(
    leg_type: str,          # "outbound" or "inbound"
    traveller: str,         # traveller name
    origin: str,            # IATA origin
    destination: str,       # IATA destination
    date: str,              # YYYY-MM-DD
    max_age_days: float = 7.0,
) -> list[dict] | None:
    """
    Search all stored searches for flights matching a specific route + date.

    Returns the list of flight dicts if found in a fresh-enough record,
    or None if not found (caller must scrape).

    This allows partial cache reuse: e.g. outbound NCE flights from a previous
    search can be reused even when a new inbound date requires fresh scraping.
    """
    best: list[dict] | None = None
    best_age = float("inf")

    for path in _all_files():
        try:
            with open(path, encoding="utf-8") as f:
                record = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        searched_at = record.get("searched_at", "")
        try:
            age_days = (datetime.now() -
                        datetime.fromisoformat(searched_at)).total_seconds() / 86400
        except ValueError:
            age_days = 999

        if age_days > max_age_days:
            continue

        raw = record.get("raw_results", {})
        flights_for_traveller = raw.get(leg_type, {}).get(traveller, [])

        matches = [
            f for f in flights_for_traveller
            if f.get("origin")      == origin
            and f.get("destination") == destination
            and f.get("depart_date") == date
        ]

        if matches and age_days < best_age:
            best     = matches
            best_age = age_days

    return best


def list_all() -> list[dict]:
    """Return all saved records as a list, newest last."""
    out = []
    for path in _all_files():
        try:
            with open(path, encoding="utf-8") as f:
                rec = json.load(f)
            # Keep only the header fields for the listing
            out.append({k: rec.get(k) for k in
                        ("id", "filename", "slug", "query",
                         "tool", "searched_at", "key_hash")})
        except (json.JSONDecodeError, OSError):
            continue
    return out


def describe_all() -> str:
    """Return a formatted table of saved searches for terminal display."""
    records = list_all()
    if not records:
        return "  (no saved searches yet)\n"
    lines = [f"  {'ID':<5}  {'Searched':<20}  {'Slug':<40}  Query"]
    lines.append("  " + "─" * 90)
    for r in records:
        age   = _age_label_from_iso(r.get("searched_at", ""))
        slug  = (r.get("slug") or "")[:38]
        query = (r.get("query") or "")[:50]
        lines.append(f"  {r['id']:<5}  {age:<20}  {slug:<40}  {query}")
    return "\n".join(lines) + "\n"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _age_label(days: float) -> str:
    if days < 1/24:
        return f"{int(days*24*60)} min"
    if days < 1:
        return f"{days*24:.1f} hrs"
    if days < 2:
        return "1 day"
    return f"{days:.0f} days"


def _age_label_from_iso(iso: str) -> str:
    try:
        days = (datetime.now() - datetime.fromisoformat(iso)).total_seconds() / 86400
        return _age_label(days)
    except ValueError:
        return iso[:16] if iso else "unknown"
