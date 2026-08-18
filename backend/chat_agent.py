import json
import os
import re
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

PARSING TIPS — mechanics often give more than one field in a single answer:
- If someone gives make and model together (e.g. "Ford Mustang", "Toyota Camry", "a Honda Civic"), split it: the brand is the make, the rest is the model. Do NOT ask for the model again if it was already given this way — check REQUIRED FIELDS STATUS below before asking for anything, it always reflects what's truly confirmed so far.
- If the REQUIRED FIELDS STATUS below shows something as CONFIRMED, trust it and do not ask for it again, even if it isn't repeated in the last message you received — it was already established earlier in the call.

HOW TO WRITE THE "reply" FIELD'S TEXT (this does not change your output format — see OUTPUT FORMAT below, which always applies):
- Talk like a real, busy-but-friendly service advisor, not a chatbot. Use contractions: "I'll", "that's", "you're", "let's".
- Vary your acknowledgments — don't say "Got it" every single turn. Mix in things like "Sounds good", "Perfect", "Alright", "No problem", "Sure thing".
- Never use bullet points, numbered lists, dashes, asterisks, or any text formatting inside the reply text — it gets read aloud, and a list read aloud sounds broken.
- Never say internal terms like "action_data", field names, error codes, or anything that sounds like you're reading from a database. If something failed, say so plainly and naturally ("I couldn't find that — mind trying again?"), never repeat a raw error message.
- If you only caught part of what they said, ask a short, natural clarifying question instead of guessing.

OUTPUT FORMAT — this is a strict, non-negotiable requirement that applies no matter what: your entire output must be ONE valid JSON object and NOTHING else. Not markdown, not a code fence, not a sentence before or after it, not plain conversational text. The natural, human-sounding writing described above happens ONLY inside the "reply" string value below — it never changes the fact that your raw output is always this exact JSON shape:
{
  "response_type": "question" | "action" | "message",
  "reply": "Your short, natural-sounding response to the mechanic",
  "missing_fields": ["name", "bay", "year", "make", "model", "quantity"],
  "collected": {
    "technician_name": "string or null",
    "vehicle": {"year": "string or null", "make": "string or null", "model": "string or null"}
  },
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

"collected" — CRITICAL, fill this in on EVERY turn, not just when taking an action: it's how the system remembers what's already been confirmed across the whole call, so state survives even through a noisy phone line. Put every value you currently know here — both what was just said AND everything already shown as CONFIRMED in REQUIRED FIELDS STATUS below — not just what's new in this message. Use null only for something truly never established yet.

action meanings:
- "SOURCE_PARTS": Search for parts across vendors and compare prices
- "ADD_LABOR": Add labor hours to the estimate
- "CHECKOUT": User confirmed, open browser to add to cart
- "REMOVE_ITEM": Remove an item. Put description in items[0].description.
- "EDIT_ITEM": Change the quantity of an existing item. Put description in items[0].description, new quantity in items[0].quantity. Use when user says "change X to 2", "update quantity of X", "I need 3 of X instead".
- "SET_PRICE": Override the price for a found part. Use when user says "set brake pads to $45", "change the price to $30", "that part costs $X". Put description in items[0].description and the dollar amount in items[0].unit_cost.

Reminder: no matter how conversational the reply text sounds, your raw output is always, only, the JSON object above."""


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


def _safe_fallback(reply_text: str) -> dict:
    """Used only when the model's output can't be salvaged at all. Note this
    NEVER fabricates action_data — a checkout/parts-search action is only
    ever taken from a cleanly-parsed model response, never guessed at from
    a regex match, since action_data drives real browser checkout."""
    return {
        "response_type": "message",
        "reply": reply_text,
        "missing_fields": [],
        "action_data": None,
        "collected": {},
    }


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

    async def _call(use_json_mode: bool):
        kwargs = dict(
            model="openai/gpt-oss-120b",  # llama-3.3-70b-versatile was deprecated by Groq June 2026
            messages=messages,
            temperature=0.3,
        )
        if use_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        return await client.chat.completions.create(**kwargs)

    # Primary attempt: Groq's JSON mode, which is the real reliability
    # mechanism here (guarantees a parseable JSON envelope) — but it can
    # hard-fail with a 400 if the model tries to answer in plain text
    # instead of the required shape. If that happens, retry once WITHOUT
    # json mode so we at least get text back, then parse it ourselves.
    try:
        response = await _call(use_json_mode=True)
        raw = response.choices[0].message.content or ""
    except Exception as e:
        print(f"[process_chat_message] Groq JSON-mode call failed, retrying without it: {e}")
        try:
            response = await _call(use_json_mode=False)
            raw = response.choices[0].message.content or ""
        except Exception as e2:
            print(f"[process_chat_message] Groq retry also failed: {e2}")
            return _safe_fallback("Sorry, I'm having trouble right now — could you say that again in a moment?")

    # Model may still wrap JSON in markdown fences or add stray prose.
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group())
            except json.JSONDecodeError:
                parsed = None
        else:
            parsed = None

        if parsed is None:
            # Truly couldn't recover structured data. Treat the raw text as
            # a plain conversational reply — but NEVER invent action_data
            # from a partial/failed parse, since that field can trigger a
            # real cart checkout.
            fallback_text = raw.strip() or "Sorry, could you say that one more time?"
            return _safe_fallback(fallback_text)

    if "response_type" not in parsed:
        parsed["response_type"] = "message"
    if "reply" not in parsed:
        parsed["reply"] = "I didn't quite get that. Could you repeat?"
    if "action_data" not in parsed:
        parsed["action_data"] = None
    if "collected" not in parsed or not isinstance(parsed.get("collected"), dict):
        parsed["collected"] = {}

    return parsed