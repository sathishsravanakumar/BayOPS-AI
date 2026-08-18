import asyncio
import json
import os
import re
import time
import httpx
from ddgs import DDGS
from groq import AsyncGroq
from schemas import MechanicIntent
from utils import parse_price_float

_groq_client: AsyncGroq | None = None

def _get_groq_client() -> AsyncGroq:
    global _groq_client
    if _groq_client is None:
        _groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))
    return _groq_client

VENDORS = [
    {"name": "AutoZone",          "domain": "autozone.com"},
    {"name": "NAPA Auto Parts",   "domain": "napaonline.com"},
    {"name": "Advance Auto Parts", "domain": "advanceautoparts.com"},
]

# In-process search cache keyed by (year|make|model|description). TTL = 10 min.
_SEARCH_CACHE: dict[str, tuple[list, float]] = {}
_CACHE_TTL = 600

# Single prompt handles all vendors — one Groq call per part instead of one per vendor.
EXTRACT_PROMPT = """You are an automotive parts price extractor. I give you search results for one part across multiple vendors. Extract the best match for each vendor.

RULES:
- Find any dollar amount in titles or snippets: "$29.99", "starting at $20", "from $15"
- Use the starting price for price ranges (e.g. "from $15" -> "$15.00")
- product_name must include the brand
- source_url must be an exact URL from the provided results — never invent one
- If no price exists for a vendor, set price to "See website"
- Use the exact vendor name shown in each section header

Return ONLY valid JSON:
{
  "vendors": [
    {
      "vendor": "ExactVendorName",
      "product_name": "Brand + product name",
      "part_number": "Part number or N/A",
      "price": "$XX.XX",
      "in_stock": true,
      "fitment": "Exact Fit",
      "source_url": "exact URL from results or empty string",
      "snippet": "One line about the product"
    }
  ]
}"""


async def _httpx_price_fallback(year: str, make: str, model: str, description: str, domain: str) -> list[dict]:
    """Direct HTTP fallback when DDG returns nothing. Parses embedded JSON price data from vendor pages."""
    search_urls = {
        "autozone.com": f"https://www.autozone.com/searchresult?searchText={year}+{make}+{model}+{description}".replace(" ", "+"),
        "napaonline.com": f"https://www.napaonline.com/en/search?q={year}+{make}+{model}+{description}".replace(" ", "+"),
        "advanceautoparts.com": f"https://shop.advanceautoparts.com/find/{year}-{make}-{model}-{description}".replace(" ", "-"),
    }
    url = search_urls.get(domain)
    if not url:
        return []
    try:
        async with httpx.AsyncClient(timeout=8, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                text = resp.text
                prices = re.findall(r'"price":\s*"?(\d+\.\d{2})"?', text)
                names = re.findall(r'"(?:productName|name)":\s*"([^"]{10,80})"', text)
                if prices:
                    return [{"title": names[0] if names else description, "href": url, "body": f"Price: ${prices[0]}"}]
    except Exception:
        pass
    return []


def search_vendor_results(year: str, make: str, model: str, description: str, domain: str) -> list[dict]:
    """Search DuckDuckGo for a part on a specific vendor site. Retries once on failure."""
    query = f"site:{domain} {year} {make} {model} {description}"
    for attempt in range(2):
        try:
            results = DDGS().text(query, max_results=5)
            return [r for r in results if domain in r.get("href", "")]
        except Exception:
            if attempt == 0:
                time.sleep(0.5)
    return []


async def extract_all_vendors(
    vendor_data: list[dict],
    year: str, make: str, model: str, description: str,
) -> list[dict]:
    """Single Groq call extracts price info for all vendors at once (was 3 separate calls)."""
    def _default(vd: dict) -> dict:
        return {
            "vendor": vd["vendor"], "domain": vd["domain"],
            "product_name": description, "part_number": "N/A",
            "price": "See website", "in_stock": True,
            "fitment": "Exact Fit", "source_url": "",
            "snippet": f"Visit {vd['vendor']} directly",
        }

    if not any(vd["results"] for vd in vendor_data):
        return [_default(vd) for vd in vendor_data]

    sections = []
    for vd in vendor_data:
        if vd["results"]:
            body = "\n\n".join(
                f"Title: {r.get('title', '')[:120]}\nURL: {r.get('href', '')}\nSnippet: {r.get('body', '')[:150]}"
                for r in vd["results"][:3]
            )
        else:
            body = "No results found."
        sections.append(f"--- {vd['vendor']} ---\n{body}")

    user_msg = (
        f"Vehicle: {year} {make} {model}\nPart: {description}\n\n"
        + "\n\n".join(sections)
    )

    try:
        client = _get_groq_client()
        response = await client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": EXTRACT_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0,
            response_format={"type": "json_object"},
            max_tokens=600,
        )
        parsed = json.loads(response.choices[0].message.content)
        extracted = parsed.get("vendors", [])
    except Exception:
        return [_default(vd) for vd in vendor_data]

    domain_map = {vd["vendor"]: vd["domain"] for vd in vendor_data}
    seen: set[str] = set()
    results: list[dict] = []
    for entry in extracted:
        vname = entry.get("vendor", "")
        entry["domain"] = domain_map.get(vname, "")
        seen.add(vname)
        price_val = parse_price_float(entry.get("price", ""))
        if price_val > 0 and (price_val < 0.50 or price_val > 5000.0):
            entry["price"] = "See website"
        results.append(entry)

    # Fill defaults for any vendor Groq missed
    for vd in vendor_data:
        if vd["vendor"] not in seen:
            results.append(_default(vd))

    return results


def _cache_key(year: str, make: str, model: str, description: str) -> str:
    return f"{year}|{make}|{model}|{description}".lower().strip()


async def search_all_vendors(
    year: str, make: str, model: str, description: str
) -> list[dict]:
    """Fetch raw results for all vendors concurrently, then extract with a single Groq call."""
    key = _cache_key(year, make, model, description)
    entry = _SEARCH_CACHE.get(key)
    if entry and time.time() - entry[1] < _CACHE_TTL:
        return entry[0]

    async def fetch_raw(vendor: dict) -> dict:
        raw = await asyncio.get_event_loop().run_in_executor(
            None, search_vendor_results, year, make, model, description, vendor["domain"],
        )
        if not raw:
            raw = await _httpx_price_fallback(year, make, model, description, vendor["domain"])
        return {"vendor": vendor["name"], "domain": vendor["domain"], "results": raw}

    vendor_data = list(await asyncio.gather(*[fetch_raw(v) for v in VENDORS]))
    vendor_options = await extract_all_vendors(vendor_data, year, make, model, description)

    vendor_order = {v["name"]: i for i, v in enumerate(VENDORS)}
    vendor_options.sort(key=lambda x: (
        parse_price_float(x.get("price", "0")) == 0,
        parse_price_float(x.get("price", "0")),
        vendor_order.get(x.get("vendor", ""), 99),
    ))

    _SEARCH_CACHE[key] = (vendor_options, time.time())
    return vendor_options


async def lookup_parts(intent: MechanicIntent) -> dict:
    """Search all vendors for each part concurrently and return results with vendor comparison."""
    part_items = [item for item in intent.items if item.item_type == "PART"]

    if not part_items:
        return {"results": [], "summary": "No parts to search — only labor items."}

    vehicle = intent.vehicle

    async def search_one_part(item) -> dict | None:
        vendor_options = await search_all_vendors(
            vehicle.year, vehicle.make, vehicle.model, item.description
        )
        if not vendor_options:
            return None
        best = vendor_options[0]
        return {
            "description": item.description,
            "product_name": best.get("product_name", item.description),
            "part_number": best.get("part_number", "N/A"),
            "price": best.get("price", "See website"),
            "vendor": best.get("vendor", "AutoZone"),
            "in_stock": best.get("in_stock", True),
            "fitment": best.get("fitment", "Exact Fit"),
            "source_url": best.get("source_url", ""),
            "snippet": best.get("snippet", ""),
            "vendor_options": vendor_options,
        }

    raw = await asyncio.gather(*[search_one_part(item) for item in part_items], return_exceptions=True)
    results = [r for r in raw if isinstance(r, dict)]

    vendors_searched = ", ".join(v["name"] for v in VENDORS)
    return {
        "results": results,
        "summary": f"Found {len(results)} part(s) for {vehicle.year} {vehicle.make} {vehicle.model} — compared {vendors_searched}.",
    }
