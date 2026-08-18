import json
import os
import re  # noqa: F401 — used in _extract_json below
from groq import AsyncGroq
from schemas import ChatMessage, BayStatus

_groq_client: AsyncGroq | None = None

def _get_groq_client() -> AsyncGroq:
    global _groq_client
    if _groq_client is None:
        _groq_client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))
    return _groq_client

SYSTEM_PROMPT = """You are BayOps AI, a service advisor at an automotive repair shop.

REQUIRED FIELDS — you MUST collect ALL of these before taking any action:
1. Technician name (first name is fine)
2. Bay number (which bay they are working in)
3. Vehicle YEAR (e.g. 2019)
4. Vehicle MAKE (brand, e.g. Honda)
5. Vehicle MODEL (e.g. Civic)
6. Part or labor description AND quantity (how many of each item)

DO NOT search for parts or take any action until ALL 6 fields above are confirmed.
Ask for missing fields one or two at a time — keep it conversational and short.

Conversation flow:
- If name is missing → ask for it first
- If bay number is missing → ask for it
- If vehicle info is incomplete → ask for year, make, or model
- If quantity is not specified → ask "How many do you need?"
- Once ALL fields are collected → proceed with the action

Other rules:
- Keep responses SHORT — 1-2 sentences max. This is voice-driven.
- If user says "yes", "go ahead", "do it" — that's confirmation to proceed
- For labor: extract hours. Ask if not specified.
- Always be helpful and conversational

VOICE AND TONE — this gets read aloud on a phone call, so it has to sound like a person, not a script:
- Talk like a real, busy-but-friendly service advisor, not a chatbot. Use contractions: "I'll", "that's", "you're", "let's".
- Vary your acknowledgments — don't say "Got it" every single turn. Mix in things like "Sounds good", "Perfect", "Alright", "No problem", "Sure thing".
- Never use bullet points, numbered lists, dashes, asterisks, or any text formatting. Say everything as plain spoken sentences — a list read aloud sounds broken.
- Never say internal terms like "action_data", field names, error codes, or anything that sounds like you're reading from a database. If something failed, say so plainly and naturally ("I couldn't find that — mind trying again?"), never repeat a raw error message.
- If you only caught part of what they said, ask a short, natural clarifying question instead of guessing.

You MUST respond with ONLY valid JSON:
{
  "response_type": "question" | "action" | "message",
  "reply": "Your short response to the mechanic",
  "missing_fields": ["name", "bay", "year", "make", "model", "quantity"],
  "action_data": null | {
    "bay_number": "string",
    "technician_name": "string",
    "vehicle": {"year": "string", "make": "string", "model": "string", "vin": null},
    "items": [{"item_type": "PART|LABOR", "description": "string", "quantity": 1.0, "vendor": null, "hours": null}],
    "action": "SOURCE_PARTS | ADD_LABOR | CHECKOUT | REMOVE_ITEM | EDIT_ITEM"
  }
}

response_type meanings:
- "question": One or more required fields are still missing. action_data MUST be null.
- "action": ALL required fields are confirmed. action_data must contain the full intent.
- "message": Informational reply only. action_data must be null.

missing_fields: list which of the 6 required fields are still unknown. Empty list [] when all are known.

action meanings:
- "SOURCE_PARTS": Search for parts across vendors and compare prices
- "ADD_LABOR": Add labor hours to the estimate
- "CHECKOUT": User confirmed, open browser to add to cart
- "REMOVE_ITEM": Remove an item. Put description in items[0].description.
- "EDIT_ITEM": Change the quantity of an existing item. Put description in items[0].description, new quantity in items[0].quantity. Use when user says "change X to 2", "update quantity of X", "I need 3 of X instead".
- "SET_PRICE": Override the price for a found part. Use when user says "set brake pads to $45", "change the price to $30", "that part costs $X". Put description in items[0].description and the dollar amount in items[0].unit_cost."""


def build_bay_context(bay: BayStatus) -> str:
    lines = []

    # --- Required fields status ---
    has_name = bay.technician_name and bay.technician_name != "Unknown"
    veh = bay.vehicle
    has_year  = veh and veh.year  not in (None, "", "N/A")
    has_make  = veh and veh.make  not in (None, "", "N/A")
    has_model = veh and veh.model not in (None, "", "N/A")

    lines.append("REQUIRED FIELDS STATUS:")
    name_status = f'"{bay.technician_name}" (CONFIRMED)' if has_name else "MISSING - must ask"
    year_status  = f'"{veh.year}"  (CONFIRMED)' if has_year  else "MISSING - must ask"
    make_status  = f'"{veh.make}"  (CONFIRMED)' if has_make  else "MISSING - must ask"
    model_status = f'"{veh.model}" (CONFIRMED)' if has_model else "MISSING - must ask"
    lines.append(f"- Technician name: {name_status}")
    lines.append(f"- Bay number: {bay.bay_number} (CONFIRMED)")
    lines.append(f"- Vehicle year:  {year_status}")
    lines.append(f"- Vehicle make:  {make_status}")
    lines.append(f"- Vehicle model: {model_status}")

    if bay.all_items:
        lines.append("\nItems on estimate (quantities confirmed):")
        for it in bay.all_items:
            qty = f"{it.quantity:.0f}x" if it.quantity != 1 else ""
            lines.append(f"  - {qty} {it.description} ({it.item_type})")
    else:
        lines.append("\nItems: none yet — must ask what part/service is needed and quantity")

    if bay.billing and bay.billing.total > 0:
        lines.append(f"\nCurrent estimate total: ${bay.billing.total:.2f}")

    if bay.results and bay.results.get("results"):
        lines.append("\nParts already found:")
        for r in bay.results["results"]:
            lines.append(f"  - {r.get('description', '')} → {r.get('product_name', '')} at {r.get('price', 'N/A')} ({r.get('vendor', '')})")

    return "\n".join(lines)


async def process_chat_message(
    bay: BayStatus,
    user_message: str,
) -> dict:
    client = _get_groq_client()

    bay_context = build_bay_context(bay)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": bay_context},
    ]

    for msg in bay.chat_history[-10:]:
        messages.append({"role": msg.role, "content": msg.content})

    messages.append({"role": "user", "content": user_message})

    response = await client.chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=messages,
        temperature=0.3,
    )

    raw = response.choices[0].message.content or ""

    # Model may wrap JSON in markdown fences or add prose around it.
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group())
            except json.JSONDecodeError:
                parsed = {}
        else:
            # Model returned plain text — treat it as a conversational reply.
            parsed = {"response_type": "message", "reply": raw.strip()}

    if "response_type" not in parsed:
        parsed["response_type"] = "message"
    if "reply" not in parsed:
        parsed["reply"] = "I didn't quite get that. Could you repeat?"
    if "action_data" not in parsed:
        parsed["action_data"] = None

    return parsed