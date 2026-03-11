"""
bike_fees.py — Live bicycle/sports-equipment fee lookup for airlines.

Strategy:
  1. Maintain a small table of known airline baggage-policy URLs.
  2. For each airline in the results, fetch that URL with Playwright (real browser,
     so JavaScript-rendered content loads correctly).
  3. Pass the page text to Claude, which extracts a structured BikeFee object.
  4. Cache results within a run so the same airline is only fetched once.

If a URL isn't known for an airline, falls back to a targeted web search via
the DuckDuckGo HTML endpoint (no API key required) to find the right page first.

Requires: playwright, anthropic  (both already in requirements.txt)
"""

import re
import time
import json
from dataclasses import dataclass, field
from typing import Optional

try:
    import anthropic
except ImportError:
    anthropic = None  # type: ignore

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sync_playwright = None  # type: ignore


# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class BikeFee:
    airline:        str
    fee_gbp:        Optional[float]   # None = couldn't determine
    fee_currency:   str  = "GBP"
    max_weight_kg:  Optional[float] = None
    max_size_cm:    str  = ""         # e.g. "277 cm linear"
    must_book:      str  = ""         # "online" / "at airport" / "online or airport"
    notes:          str  = ""         # any important caveats
    source_url:     str  = ""
    confidence:     str  = "high"     # high / medium / low
    error:          str  = ""         # set if lookup failed

    def display_line(self) -> str:
        if self.error:
            return f"🚲  Bike fee: ⚠️  {self.error}"
        if self.fee_gbp is None:
            return f"🚲  Bike fee: unknown  ({self.notes or 'check airline website'})"
        fee_str = f"£{self.fee_gbp:,.0f}" if self.fee_currency == "GBP" else f"{self.fee_currency} {self.fee_gbp:,.0f}"
        parts = [f"🚲  Bike fee: {fee_str} per flight"]
        if self.max_weight_kg:
            parts.append(f"max {self.max_weight_kg:.0f} kg")
        if self.must_book:
            parts.append(f"book {self.must_book}")
        if self.notes:
            parts.append(f"— {self.notes}")
        return "  ".join(parts)

    def total_with_fee(self, flight_price: float) -> float:
        return flight_price + (self.fee_gbp or 0.0)


# ── Airline baggage-policy URL table ──────────────────────────────────────────
# These are the deepest stable links to sports/cycle/oversized baggage pages.
# Updated 2025 — but we always fetch live so any page updates are captured.

AIRLINE_BAGGAGE_URLS: dict[str, str] = {
    "ryanair":          "https://www.ryanair.com/gb/en/plan-trip/flying-with-ryanair/sports-equipment",
    "easyjet":          "https://www.easyjet.com/en/help/baggage/sports-equipment",
    "wizz air":         "https://wizzair.com/en-gb/information-and-services/travel-information/bag-calculator",
    "wizzair":          "https://wizzair.com/en-gb/information-and-services/travel-information/bag-calculator",
    "jet2":             "https://www.jet2.com/help/luggage/sports-equipment",
    "british airways":  "https://www.britishairways.com/en-gb/information/baggage-essentials/sports-equipment",
    "ba":               "https://www.britishairways.com/en-gb/information/baggage-essentials/sports-equipment",
    "lufthansa":        "https://www.lufthansa.com/gb/en/sport-equipment",
    "klm":              "https://www.klm.com/en/information/baggage/sports-equipment",
    "air france":       "https://www.airfrance.co.uk/GB/en/common/guidevoyageur/pratique/bagage-sport-airfrance.htm",
    "vueling":          "https://www.vueling.com/en/we-are-vueling/press-room/flight-information/baggage/sports-equipment",
    "norwegian":        "https://www.norwegian.com/uk/travel-info/baggage/sports-luggage/",
    "tui":              "https://www.tui.co.uk/destinations/info/sports-equipment",
    "flybe":            "https://www.flybe.com/en/help/baggage",
    "iberia":           "https://www.iberia.com/gb/flight-information/baggage/special-baggage/sports-equipment/",
    "tap air portugal": "https://www.flytap.com/en-gb/baggage/sports-equipment",
    "tap":              "https://www.flytap.com/en-gb/baggage/sports-equipment",
    "swiss":            "https://www.swiss.com/gb/en/prepare/baggage/sports-equipment",
    "austrian":         "https://www.austrian.com/gb/en/sports-equipment",
    "finnair":          "https://www.finnair.com/gb/en/information/baggage/sports-equipment",
    "aegean":           "https://en.aegeanair.com/travel-information/baggage/special-items/sports-equipment/",
    "transavia":        "https://www.transavia.com/en-EU/service/sports-equipment/",
    "volotea":          "https://www.volotea.com/en/special-luggage/",
    "aer lingus":       "https://www.aerlingus.com/travel-information/baggage-information/sports-equipment/",
}

# DuckDuckGo HTML search — no API key, returns plain HTML we can scrape
DDG_URL = "https://html.duckduckgo.com/html/?q={query}"


# ── Internal helpers ───────────────────────────────────────────────────────────

def _normalise_airline(name: str) -> str:
    """Lowercase, strip common suffixes for lookup."""
    n = name.lower().strip()
    for suffix in (" airlines", " airways", " air", " international", " express"):
        n = n.replace(suffix, "")
    return n.strip()


def _fetch_page_text(url: str, timeout_ms: int = 20000) -> tuple[str, str]:
    """
    Fetch a URL with Playwright and return (page_text, final_url).
    Returns ("", url) on failure.
    """
    if sync_playwright is None:
        return "", url
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-GB",
            )
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                time.sleep(2)
                # Dismiss cookie banners
                for sel in ['button:has-text("Accept")', 'button:has-text("Accept all")',
                            'button:has-text("I agree")', '[aria-label="Accept cookies"]']:
                    try:
                        btn = page.query_selector(sel)
                        if btn and btn.is_visible():
                            btn.click()
                            time.sleep(0.5)
                            break
                    except Exception:
                        pass
                text = page.inner_text("body")
                final_url = page.url
            except PWTimeout:
                text, final_url = "", url
            finally:
                browser.close()
            return text, final_url
    except Exception as e:
        return "", url


def _search_for_baggage_url(airline: str) -> str:
    """
    Use DuckDuckGo HTML search to find the airline's sports/bike baggage page.
    Returns the best URL found, or "".
    """
    query = f"{airline} airline bicycle bike sports equipment fee checked baggage site:{_airline_domain_hint(airline)}"
    fallback_query = f"{airline} airline bicycle sports equipment baggage fee 2025"

    for q in [query, fallback_query]:
        try:
            text, _ = _fetch_page_text(
                DDG_URL.format(query=q.replace(' ', '+')),
                timeout_ms=15000,
            )
            if not text:
                continue
            # Extract first https result that looks like a baggage/sports page
            urls = re.findall(r'https://[^\s"<>]+', text)
            for u in urls:
                u_low = u.lower()
                if any(kw in u_low for kw in
                       ('sport', 'bike', 'cycle', 'baggage', 'luggage', 'equipment')):
                    # Skip aggregators / comparison sites
                    if not any(bad in u_low for bad in
                               ('google', 'bing', 'duckduckgo', 'tripadvisor',
                                'skyscanner', 'kayak', 'momondo')):
                        return u
        except Exception:
            continue
    return ""


def _airline_domain_hint(airline: str) -> str:
    """Return a rough domain hint for the search query."""
    hints = {
        "ryanair": "ryanair.com",
        "easyjet": "easyjet.com",
        "british airways": "britishairways.com",
        "jet2": "jet2.com",
        "wizz": "wizzair.com",
        "norwegian": "norwegian.com",
        "tui": "tui.co.uk",
    }
    n = airline.lower()
    for k, v in hints.items():
        if k in n:
            return v
    # Generic guess: take first word + .com
    word = n.split()[0] if n.split() else n
    return f"{word}.com"


CLAUDE_BIKE_SYSTEM = """You are a travel data extraction assistant. You will be given the
text content of an airline's baggage/sports-equipment webpage. Extract bicycle transport
fees and return ONLY a JSON object — no prose, no markdown fences.

Rules:
- fee_gbp: the one-way fee in GBP to transport a bicycle.
  If stated in euros, convert at 1 EUR = 0.86 GBP (round to nearest pound).
  If stated in USD, convert at 1 USD = 0.79 GBP.
  If a range is given (e.g. £30-£60), use the lower bound.
  If free, use 0.
  If truly unknown from the page text, use null.
- fee_currency: "GBP" unless you used original currency without conversion
- max_weight_kg: maximum permitted weight for the bicycle in kg, or null
- max_size_cm: maximum permitted linear size (e.g. "277 cm linear"), or ""
- must_book: "online" / "at airport" / "online or airport" / ""
- notes: one short sentence of the most important caveat (packaging, deflate tyres, etc.)
- confidence: "high" if the page clearly states a bicycle fee, "medium" if inferred,
  "low" if the page didn't mention bicycles specifically

Output schema:
{
  "fee_gbp": 35,
  "fee_currency": "GBP",
  "max_weight_kg": 20,
  "max_size_cm": "277 cm linear",
  "must_book": "online",
  "notes": "Pedals must be removed and handlebars turned sideways.",
  "confidence": "high"
}"""


def _parse_fee_with_claude(airline: str, page_text: str,
                            source_url: str, api_key: str) -> BikeFee:
    """Ask Claude to extract a structured BikeFee from raw page text."""
    if anthropic is None or not api_key:
        return BikeFee(airline=airline, fee_gbp=None, source_url=source_url,
                       confidence="low", error="Anthropic library not available")

    # Trim page text to avoid token overload — keep first 6000 chars
    trimmed = page_text[:6000]
    if not trimmed.strip():
        return BikeFee(airline=airline, fee_gbp=None, source_url=source_url,
                       confidence="low", error="Page returned no readable text")

    client = anthropic.Anthropic(api_key=api_key)
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=512,
            system=CLAUDE_BIKE_SYSTEM,
            messages=[{
                "role": "user",
                "content": (
                    f"Airline: {airline}\n"
                    f"Source URL: {source_url}\n\n"
                    f"Page text:\n{trimmed}"
                ),
            }],
        )
        raw = msg.content[0].text.strip()
        raw = re.sub(r'^```[a-z]*\n?', '', raw)
        raw = re.sub(r'\n?```$', '', raw)
        data = json.loads(raw)
        return BikeFee(
            airline=airline,
            fee_gbp=data.get("fee_gbp"),
            fee_currency=data.get("fee_currency", "GBP"),
            max_weight_kg=data.get("max_weight_kg"),
            max_size_cm=data.get("max_size_cm", ""),
            must_book=data.get("must_book", ""),
            notes=data.get("notes", ""),
            source_url=source_url,
            confidence=data.get("confidence", "medium"),
        )
    except json.JSONDecodeError as e:
        return Bikefee_error(airline, source_url, f"Claude returned unparseable JSON: {e}")
    except Exception as e:
        return BikeFee_error(airline, source_url, str(e))


def BikeFee_error(airline: str, url: str, msg: str) -> BikeFee:
    return BikeFee(airline=airline, fee_gbp=None, source_url=url,
                   confidence="low", error=msg)


# ── Public API ──────────────────────────────────────────────────────────────────

def lookup_bike_fees(airlines: list[str], api_key: str,
                     verbose: bool = True) -> dict[str, BikeFee]:
    """
    Look up live bicycle transport fees for a list of airline names.

    Returns a dict mapping normalised airline name → BikeFee.
    Results are cached within this call (each airline fetched once).
    """
    cache: dict[str, BikeFee] = {}

    unique = list(dict.fromkeys(a.strip() for a in airlines if a.strip()))
    if not unique:
        return cache

    if verbose:
        print(f"\n🚲  Looking up live bicycle fees for: {', '.join(unique)}")

    for airline in unique:
        key = _normalise_airline(airline)

        # Known URL?
        url = ""
        for k, v in AIRLINE_BAGGAGE_URLS.items():
            if k in key or key in k:
                url = v
                break

        # Fall back to search
        if not url:
            if verbose:
                print(f"    🔍  Searching for {airline} baggage policy...")
            url = _search_for_baggage_url(airline)

        if not url:
            cache[key] = BikeFee(
                airline=airline, fee_gbp=None, confidence="low",
                error="Could not find baggage policy page",
            )
            if verbose:
                print(f"    ⚠️  {airline}: no baggage page found")
            continue

        if verbose:
            print(f"    🌐  {airline}: fetching {url}")

        page_text, final_url = _fetch_page_text(url)
        fee = _parse_fee_with_claude(airline, page_text, final_url, api_key)
        fee.airline = airline  # preserve original casing
        cache[key] = fee

        if verbose:
            print(f"    {'✅' if fee.fee_gbp is not None else '⚠️ '}  {fee.display_line()}")
            if fee.source_url:
                print(f"       📎 {fee.source_url}")

    return cache


def get_fee_for_flight(airline: str, fee_cache: dict[str, BikeFee]) -> Optional[BikeFee]:
    """Retrieve a cached BikeFee for a flight's airline name."""
    key = _normalise_airline(airline)
    # Exact match
    if key in fee_cache:
        return fee_cache[key]
    # Partial match (e.g. "easyJet" matches "easyjet")
    for k, v in fee_cache.items():
        if k in key or key in k:
            return v
    return None


def attach_bike_fees(flights: list, fee_cache: dict[str, BikeFee]) -> None:
    """
    Attach bike fee info to FlightResult objects in-place.
    Adds a `bike_fee` attribute to each result.
    """
    for f in flights:
        fee = get_fee_for_flight(f.airline, fee_cache)
        f.bike_fee = fee   # may be None if airline wasn't looked up


def format_price_with_bike(flight_price_val: float, fee: Optional[BikeFee],
                            currency: str = "GBP") -> str:
    """
    Return a formatted string showing flight price + bike fee total.
    e.g. "£67 + 🚲 £35 = £102"
    """
    if fee is None or fee.fee_gbp is None:
        return f"£{flight_price_val:,.0f}  (bike fee unknown)"
    total = flight_price_val + fee.fee_gbp
    return (f"£{flight_price_val:,.0f}  +  🚲 £{fee.fee_gbp:,.0f}"
            f"  =  £{total:,.0f} with bike")
