"""
Automotive Parts Verification Engine.

Safety gate between lookup_parts() and browser checkout.
Stage 1: Normalize vehicle via NHTSA vPIC API (free, no key required).
Stage 2: Use Groq to reason about whether found parts fit that vehicle.
Returns: {status, issues, clarification_needed, vehicle_details}
  status = "cleared"  → proceed to cart
  status = "warning"  → proceed to cart + broadcast advisory
  status = "halted"   → block cart, prompt mechanic to clarify
"""

import json
import os

import httpx
from groq import AsyncGroq

from schemas import Vehicle, LineItem

NHTSA_BASE = "https://vpic.nhtsa.dot.gov/api/vehicles"

_groq_client: AsyncGroq | None = None

def _get_groq_client() -> AsyncGroq:
    global _groq_client
    if _groq_client is None:
        _groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))
    return _groq_client

_NHTSA_CACHE: dict[str, dict] = {}


async def _normalize_vehicle(vehicle: Vehicle) -> dict:
    """Call NHTSA vPIC to get standardized vehicle attributes. Results cached per vehicle."""
    cache_key = f"{vehicle.vin or ''}|{vehicle.year}|{vehicle.make}|{vehicle.model}".lower()
    if cache_key in _NHTSA_CACHE:
        return _NHTSA_CACHE[cache_key]
    details = {}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            if vehicle.vin:
                resp = await client.get(
                    f"{NHTSA_BASE}/DecodeVin/{vehicle.vin}?format=json"
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for item in data.get("Results", []):
                        val = (item.get("Value") or "").strip()
                        var = (item.get("Variable") or "").strip()
                        if val and val not in ("", "Not Applicable", "0"):
                            details[var] = val
            else:
                make_enc = vehicle.make.replace(" ", "%20")
                resp = await client.get(
                    f"{NHTSA_BASE}/GetModelsForMakeYear/make/{make_enc}"
                    f"/modelyear/{vehicle.year}?format=json"
                )
                if resp.status_code == 200:
                    data = resp.json()
                    models = [
                        r.get("Model_Name", "")
                        for r in data.get("Results", [])
                    ]
                    details["Make"] = vehicle.make
                    details["Model Year"] = vehicle.year
                    details["Available Models"] = ", ".join(models[:10])
                    # Mark whether our model is confirmed
                    matched = any(
                        vehicle.model.lower() in m.lower() or m.lower() in vehicle.model.lower()
                        for m in models
                    )
                    details["Model Confirmed"] = str(matched)
    except Exception:
        pass  # NHTSA failure must never block orders
    _NHTSA_CACHE[cache_key] = details
    return details


_FITMENT_SYSTEM = """You are an automotive parts fitment expert.
Given a vehicle and a list of parts with their product details, determine if each part fits the vehicle.

Rules:
- Flag any axle-specific exclusion (front-only, rear-only) where the other axle may also be needed
- Flag any dimension that is explicitly stated in the product description and falls outside ±1.5mm tolerance for the vehicle's known specs
- Flag missing fitment details that are critical (e.g., caliper bolt pattern for brake pads, thread pitch for filters)
- If the product's "fitment" field already says "Exact Fit" and no dimension conflicts are visible, clear it
- Be lenient: only halt when there is a REAL risk of ordering the wrong part
- If uncertain, prefer "warning" over "halted"

Respond ONLY with valid JSON — no explanation outside the JSON:
{
  "status": "cleared | warning | halted",
  "issues": [
    {"part": "part description", "issue": "what is wrong or ambiguous", "severity": "warning | error"}
  ],
  "clarification_needed": ["question for the mechanic if halted or warning"]
}

If everything fits cleanly, return {"status": "cleared", "issues": [], "clarification_needed": []}"""


async def _groq_fitment_check(vehicle: Vehicle, vehicle_details: dict, parts: list[dict]) -> dict:
    """Ask Groq to reason about part fitment for this vehicle."""
    default_cleared = {"status": "cleared", "issues": [], "clarification_needed": []}
    try:
        client = _get_groq_client()

        veh_str = f"{vehicle.year} {vehicle.make} {vehicle.model}"
        veh_context = veh_str
        if vehicle_details:
            interesting = {k: v for k, v in vehicle_details.items()
                          if k in ("Body Class", "Engine Displacement (L)",
                                   "Drive Type", "Engine Configuration",
                                   "Model Confirmed", "Available Models")}
            if interesting:
                veh_context += "\nNHTSA details: " + json.dumps(interesting)

        parts_summary = []
        for p in parts:
            parts_summary.append({
                "description": p.get("description", ""),
                "product_name": p.get("product_name", ""),
                "part_number": p.get("part_number", ""),
                "fitment": p.get("fitment", ""),
                "snippet": (p.get("snippet") or "")[:200],
            })

        user_msg = (
            f"Vehicle: {veh_context}\n\n"
            f"Parts to verify:\n{json.dumps(parts_summary, indent=2)}"
        )

        response = await client.chat.completions.create(
            model="openai/gpt-oss-120b",
            messages=[
                {"role": "system", "content": _FITMENT_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.1,
            response_format={"type": "json_object"},
            max_tokens=512,
        )

        result = json.loads(response.choices[0].message.content)
        if "status" not in result:
            return default_cleared
        if result["status"] not in ("cleared", "warning", "halted"):
            result["status"] = "cleared"
        result.setdefault("issues", [])
        result.setdefault("clarification_needed", [])
        return result

    except Exception:
        # Any Groq failure must never block orders
        return default_cleared


async def verify_parts_fitment(
    vehicle: Vehicle,
    items: list[LineItem],
    search_results: dict,
) -> dict:
    """
    Main entry point.
    Returns {status, issues, clarification_needed, vehicle_details}.
    Always returns "cleared" on any internal failure.
    """
    default_cleared = {
        "status": "cleared",
        "issues": [],
        "clarification_needed": [],
        "vehicle_details": {},
    }

    try:
        parts = search_results.get("results", [])
        if not parts:
            return default_cleared

        # Stage 1 — vehicle normalization (best-effort)
        vehicle_details = await _normalize_vehicle(vehicle)

        # Stage 2 — Groq fitment reasoning
        groq_result = await _groq_fitment_check(vehicle, vehicle_details, parts)

        return {
            "status": groq_result["status"],
            "issues": groq_result["issues"],
            "clarification_needed": groq_result["clarification_needed"],
            "vehicle_details": vehicle_details,
        }

    except Exception:
        return default_cleared
