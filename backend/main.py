import asyncio
import json
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response as XMLResponse
from twilio.twiml.voice_response import Gather, VoiceResponse

from pydantic import BaseModel
from schemas import ChatRequest, ChatMessage, BayStatus, AgentStatus, ShopConfig, MechanicIntent
from parts_agent import lookup_parts
from billing import calculate_billing
from browser_agent import run_browser_checkout
from chat_agent import process_chat_message
import twilio_agent as tw

load_dotenv()

bays: dict[str, BayStatus] = {}
shop_config = ShopConfig()

# Maps bay_id → Desktop file path of the currently open Excel workbook.
# Populated when the user clicks "Export to Excel" for the first time.
# All subsequent billing changes trigger a live in-place update.
excel_files: dict[str, str] = {}


def _assemble_bay_data(bay: BayStatus) -> dict:
    """Build the bay_data dict consumed by excel_export functions."""
    veh = bay.vehicle
    part_num_map = {
        r.get("description", "").lower(): r.get("part_number", "N/A")
        for r in bay.all_results
    }
    parts = [
        {
            "description": item.description,
            "vendor": item.source or "—",
            "part_number": part_num_map.get(item.description.lower(), "N/A"),
            "quantity": item.quantity,
            "unit_cost": item.unit_cost,
            "markup_pct": item.markup_pct * 100,
            "extended_price": item.extended_price,
        }
        for item in (bay.billing.parts_items if bay.billing else [])
    ]
    labor = [
        {
            "description": item.description,
            "hours": item.quantity,
            "rate": item.unit_cost,
            "extended_price": item.extended_price,
        }
        for item in (bay.billing.labor_items if bay.billing else [])
    ]
    return {
        "bay_number": bay.bay_number,
        "technician_name": bay.technician_name or "—",
        "vehicle": {"year": veh.year, "make": veh.make, "model": veh.model} if veh else {},
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "parts": parts,
        "labor": labor,
        "parts_subtotal": bay.billing.parts_subtotal if bay.billing else 0.0,
        "labor_subtotal": bay.billing.labor_subtotal if bay.billing else 0.0,
        "tax_rate": bay.billing.tax_rate if bay.billing else 0.0825,
        "tax_amount": bay.billing.tax_amount if bay.billing else 0.0,
        "grand_total": bay.billing.total if bay.billing else 0.0,
    }


def _trigger_excel_update(bay_id: str):
    """If an Excel file is tracked for this bay, push a live update in the background."""
    if bay_id not in excel_files:
        return
    bay = bays.get(bay_id)
    if not bay or not bay.billing or bay.billing.total == 0:
        return
    file_path = excel_files[bay_id]
    bay_data = _assemble_bay_data(bay)

    def _run():
        # Excel COM runs in STA; initialise the apartment for this background thread
        try:
            import pythoncom
            pythoncom.CoInitialize()
        except Exception:
            pass
        try:
            from excel_export import update_excel_live
            update_excel_live(file_path, bay_data)
        finally:
            try:
                import pythoncom
                pythoncom.CoUninitialize()
            except Exception:
                pass

    threading.Thread(target=_run, daemon=True).start()


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        data = json.dumps(message)
        for conn in list(self.active_connections):
            try:
                await conn.send_text(data)
            except Exception:
                self.active_connections.remove(conn)

manager = ConnectionManager()


async def recalculate_billing(bay_id: str):
    bay = bays[bay_id]
    bay.billing = calculate_billing(bay.all_items, bay.all_results, shop_config)


async def execute_intent(intent: MechanicIntent, bay_id: str) -> dict:
    """Shared pipeline: update bay state, search parts, recalc billing. Returns search results."""
    from fitment_agent import verify_parts_fitment

    bay = bays[bay_id]

    if intent.vehicle.year != "N/A" and intent.vehicle.make != "N/A":
        bay.vehicle = intent.vehicle
    if intent.technician_name != "Unknown":
        bay.technician_name = intent.technician_name

    bay.items = intent.items
    # Extend all_items, skipping exact duplicates (same type + description)
    existing = {(it.item_type, it.description.lower()) for it in bay.all_items}
    for item in intent.items:
        if (item.item_type, item.description.lower()) not in existing:
            bay.all_items.append(item)
            existing.add((item.item_type, item.description.lower()))

    has_parts = any(i.item_type == "PART" for i in intent.items)
    search_results = {"results": [], "summary": ""}

    if has_parts:
        part_count = sum(1 for i in intent.items if i.item_type == "PART")
        bay.logs.append(f"Searching {part_count} part(s) across vendors...")
        await manager.broadcast({"type": "search_started", "bay": bay_id, "count": part_count})
        search_results = await lookup_parts(intent)
        bay.results = search_results
        if search_results.get("results"):
            bay.all_results.extend(search_results["results"])
        await manager.broadcast({"type": "search_complete", "bay": bay_id, "results": search_results})

        # Fitment verification gate — runs after search, before browser launch
        if search_results.get("results") and not bay.fitment_override:
            bay.logs.append("Verifying fitment...")
            fitment = await verify_parts_fitment(intent.vehicle, intent.items, search_results)

            if fitment["status"] == "halted":
                bay.pending_browser_intent = {
                    "intent": intent.model_dump(),
                    "search_results": search_results,
                }
                await manager.broadcast({
                    "type": "fitment_warning",
                    "bay": bay_id,
                    "halted": True,
                    "issues": fitment["issues"],
                    "clarification_needed": fitment["clarification_needed"],
                })
                await recalculate_billing(bay_id)
                _trigger_excel_update(bay_id)
                return search_results  # exit WITHOUT launching browser

            elif fitment["status"] == "warning":
                await manager.broadcast({
                    "type": "fitment_warning",
                    "bay": bay_id,
                    "halted": False,
                    "issues": fitment["issues"],
                    "clarification_needed": [],
                })
                # fall through — browser still launches

    await recalculate_billing(bay_id)
    _trigger_excel_update(bay_id)
    return search_results


def start_browser_thread(intent, search_results, bay_id):
    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_browser_work(intent, search_results, bay_id))
        finally:
            loop.close()
    threading.Thread(target=_run, daemon=True).start()


async def _browser_work(intent, search_results, bay_id):
    import traceback

    async def log_cb(msg: str):
        if bay_id in bays:
            bays[bay_id].logs.append(msg)
        try:
            print(f"[Browser Bay {bay_id}] {msg}", flush=True)
        except (ValueError, OSError):
            pass

    try:
        bays[bay_id].status = AgentStatus.BROWSING
        outcomes = await run_browser_checkout(intent, search_results, log_cb, bay_id)

        # Sync real prices scraped from vendor pages back to results
        vendor_prices = outcomes.get("vendor_prices", {})
        if vendor_prices:
            for r in bays[bay_id].all_results:
                desc = r.get("description", "").lower().strip()
                matched = next((vp_list for k, vp_list in vendor_prices.items() if k.lower().strip() == desc), None)
                if matched:
                    # Update each vendor_option with the real scraped price
                    for opt in r.get("vendor_options", []):
                        for vp in matched:
                            if vp["vendor"] == opt["vendor"] and vp["price"] > 0:
                                opt["price"] = f"${vp['price']:.2f}"
                    # Determine cheapest from real prices
                    priced = sorted([vp for vp in matched if vp["price"] > 0], key=lambda x: x["price"])
                    if priced:
                        best = priced[0]
                        r["price"] = f"${best['price']:.2f}"
                        r["vendor"] = best["vendor"]
                        # Update the matching vendor_option as new selected
                        bays[bay_id].logs.append(
                            f"Real prices — " + ", ".join(f"{vp['vendor']}: ${vp['price']:.2f}" for vp in priced)
                        )
                        bays[bay_id].logs.append(f"Cheapest: {best['vendor']} at ${best['price']:.2f}")

            # Also sync to bay.results display
            if bays[bay_id].results.get("results"):
                for r in bays[bay_id].results["results"]:
                    desc = r.get("description", "").lower().strip()
                    matched = next((vp_list for k, vp_list in vendor_prices.items() if k.lower().strip() == desc), None)
                    if matched:
                        for opt in r.get("vendor_options", []):
                            for vp in matched:
                                if vp["vendor"] == opt["vendor"] and vp["price"] > 0:
                                    opt["price"] = f"${vp['price']:.2f}"
                        priced = sorted([vp for vp in matched if vp["price"] > 0], key=lambda x: x["price"])
                        if priced:
                            r["price"] = f"${priced[0]['price']:.2f}"
                            r["vendor"] = priced[0]["vendor"]

        # Also sync cart item prices (for AutoZone-specific cart scraping)
        cart_items = outcomes.get("cart_items", [])
        if cart_items:
            part_results = [r for r in bays[bay_id].all_results if r.get("description")]
            for i, ci in enumerate(cart_items):
                if i < len(part_results) and ci.get("price", 0) > 0:
                    part_results[i]["price"] = f"${ci['price']:.2f}"
                    if ci.get("part_number"):
                        part_results[i]["part_number"] = ci["part_number"]

        # Recalculate billing with real prices
        bays[bay_id].billing = calculate_billing(bays[bay_id].all_items, bays[bay_id].all_results, shop_config)
        bays[bay_id].logs.append(f"Billing updated with real prices: ${bays[bay_id].billing.total:.2f}")
        _trigger_excel_update(bay_id)

        # Push real prices and updated billing to the dashboard
        await manager.broadcast({
            "type": "billing_update",
            "bay": bay_id,
            "billing": bays[bay_id].billing.model_dump(),
        })
        await manager.broadcast({
            "type": "search_complete",
            "bay": bay_id,
            "results": bays[bay_id].results,
        })

        bays[bay_id].status = AgentStatus.COMPLETE
        bays[bay_id].results["browser"] = outcomes

        # Broadcast cart verification result so the frontend can show a confirmation card
        cart_items = outcomes.get("cart_items", [])
        cart_total = outcomes.get("cart_total", 0.0)
        expected = bays[bay_id].billing.parts_subtotal
        mismatch = cart_total > 0 and abs(cart_total - expected) > 1.00
        await manager.broadcast({
            "type": "cart_verified",
            "bay": bay_id,
            "verified": cart_total > 0,
            "cart_total": cart_total,
            "expected_total": expected,
            "mismatch": mismatch,
            "cart_items": cart_items,
        })
    except Exception as e:
        try:
            print(f"[Browser Bay {bay_id}] CRASH: {traceback.format_exc()}", flush=True)
        except (ValueError, OSError):
            pass
        bays[bay_id].status = AgentStatus.ERROR
        bays[bay_id].logs.append(f"Browser failed: {str(e)}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    for i in range(1, 7):
        bays[str(i)] = BayStatus(bay_number=str(i))
    yield

app = FastAPI(title="BayOps AI", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/api/transcribe")
async def transcribe_audio(audio: UploadFile = File(...)):
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        return {"error": "ELEVENLABS_API_KEY not set in .env"}

    audio_bytes = await audio.read()
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.elevenlabs.io/v1/speech-to-text",
            headers={"xi-api-key": api_key},
            files={"file": (audio.filename or "recording.webm", audio_bytes, audio.content_type or "audio/webm")},
            data={"model_id": "scribe_v1"},
        )

    if resp.status_code != 200:
        return {"error": f"ElevenLabs error {resp.status_code}: {resp.text}"}
    return {"transcript": resp.json().get("text", "")}


@app.post("/api/tts")
async def text_to_speech(req: dict):
    """Convert text to speech using ElevenLabs TTS."""
    from fastapi.responses import Response

    api_key = os.getenv("ELEVENLABS_API_KEY")
    text = req.get("text", "")
    if not api_key or not text:
        return Response(content=b"", media_type="audio/mpeg")

    voice_id = "JBFqnCBsd6RMkjVDRZzb"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json={
                "text": text,
                "model_id": "eleven_turbo_v2_5",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            },
        )

    if resp.status_code != 200:
        return Response(content=b"", media_type="audio/mpeg")

    return Response(content=resp.content, media_type="audio/mpeg")


async def handle_chat_turn(bay_id: str, message: str) -> dict:
    """Core conversational pipeline: text in, agent reasoning + action execution, reply out.

    Shared by the web chat endpoint (/api/chat) and the Twilio phone-call
    endpoints (/twilio/gather). A browser chat message and a transcribed
    phone utterance both reduce to "this bay_id said this" — so both entry
    points run through this exact same logic rather than duplicating it.
    """
    if bay_id not in bays:
        bays[bay_id] = BayStatus(bay_number=bay_id)

    bay = bays[bay_id]
    now = datetime.now().isoformat()

    # Add user message to history
    bay.chat_history.append(ChatMessage(role="user", content=message, timestamp=now))

    # Call the conversational agent
    try:
        result = await process_chat_message(bay, message)
    except Exception as e:
        print(f"[handle_chat_turn] agent error for bay {bay_id}: {e}")
        reply = "Sorry, I hit a snag on my end — could you say that one more time?"
        bay.chat_history.append(ChatMessage(role="assistant", content=reply, timestamp=now))
        return {"reply": reply, "response_type": "message", "billing": None, "parts_results": None}

    reply = result.get("reply", "")
    response_type = result.get("response_type", "message")
    action_data = result.get("action_data")

    billing_out = None
    parts_out = None

    # Execute action if the agent decided to act
    if response_type == "action" and action_data:
        try:
            action_type = action_data.get("action", "")
            items_data = action_data.get("items", [])

            if action_type == "REMOVE_ITEM":
                remove_desc = items_data[0].get("description", "").lower() if items_data else ""
                if remove_desc:
                    before = len(bay.all_items)
                    # Exact match first; fall back to substring so "brake pads" ≠ "brake fluid"
                    exact = [it for it in bay.all_items if it.description.lower() == remove_desc]
                    if exact:
                        bay.all_items  = [it for it in bay.all_items  if it.description.lower() != remove_desc]
                        bay.all_results = [r for r in bay.all_results if r.get("description", "").lower() != remove_desc]
                        bay.items       = [it for it in bay.items       if it.description.lower() != remove_desc]
                    else:
                        bay.all_items  = [it for it in bay.all_items  if remove_desc not in it.description.lower()]
                        bay.all_results = [r for r in bay.all_results if remove_desc not in r.get("description", "").lower()]
                        bay.items       = [it for it in bay.items       if remove_desc not in it.description.lower()]
                    removed = before - len(bay.all_items)
                    await recalculate_billing(bay_id)
                    _trigger_excel_update(bay_id)
                    if removed > 0:
                        reply = f"Removed. Total: ${bay.billing.total:.2f}."
                    else:
                        reply = "Couldn't find that item on the estimate."

            elif action_type == "EDIT_ITEM":
                edit_desc = items_data[0].get("description", "").lower() if items_data else ""
                new_qty = float(items_data[0].get("quantity", 1)) if items_data else 1.0
                if edit_desc:
                    updated = False
                    for it in bay.all_items:
                        stored = it.description.lower()
                        if stored == edit_desc or edit_desc in stored or stored in edit_desc:
                            it.quantity = new_qty
                            if it.item_type == "LABOR" and it.hours is not None:
                                it.hours = new_qty
                            updated = True
                    await recalculate_billing(bay_id)
                    _trigger_excel_update(bay_id)
                    if updated:
                        reply = f"Updated to {new_qty:.0f}. Total: ${bay.billing.total:.2f}."
                    else:
                        reply = "Couldn't find that item on the estimate."

            elif action_type == "SET_PRICE":
                from utils import parse_price_float
                desc = items_data[0].get("description", "").lower() if items_data else ""
                new_price = parse_price_float(str(items_data[0].get("unit_cost", 0))) if items_data else 0.0
                if desc and new_price > 0:
                    updated = False
                    for r in bay.all_results:
                        if r.get("description", "").lower() == desc or desc in r.get("description", "").lower():
                            r["price"] = f"${new_price:.2f}"
                            updated = True
                            break
                    if bay.results.get("results"):
                        for r in bay.results["results"]:
                            if r.get("description", "").lower() == desc or desc in r.get("description", "").lower():
                                r["price"] = f"${new_price:.2f}"
                                break
                    await recalculate_billing(bay_id)
                    _trigger_excel_update(bay_id)
                    reply = f"Price updated to ${new_price:.2f}. Total: ${bay.billing.total:.2f}." if updated else "Couldn't find that part on the estimate."
                else:
                    reply = "Please specify the part name and new price."

            elif action_type == "CHECKOUT":
                # Fill in missing fields from bay state for MechanicIntent
                if not action_data.get("vehicle") and bay.vehicle:
                    action_data["vehicle"] = {"year": bay.vehicle.year, "make": bay.vehicle.make, "model": bay.vehicle.model, "vin": None}
                action_data.setdefault("bay_number", bay_id)
                action_data.setdefault("technician_name", bay.technician_name or "Unknown")
                action_data.setdefault("vehicle", {"year": "N/A", "make": "N/A", "model": "N/A", "vin": None})
                action_data.setdefault("items", [])

                intent = MechanicIntent(**action_data)
                search_results = bay.results if bay.results.get("results") else {"results": [], "summary": ""}
                bay.fitment_override = True  # explicit checkout bypasses gate
                start_browser_thread(intent, search_results, bay_id)
                bay.logs.append("Browser checkout launched.")
                reply += f" Opening AutoZone checkout..."

            else:
                # SOURCE_PARTS / ADD_LABOR — fill missing fields from bay state
                action_data.setdefault("bay_number", bay_id)
                action_data.setdefault("technician_name", bay.technician_name or "Unknown")
                if not action_data.get("vehicle") and bay.vehicle:
                    action_data["vehicle"] = {"year": bay.vehicle.year, "make": bay.vehicle.make, "model": bay.vehicle.model, "vin": None}
                action_data.setdefault("vehicle", {"year": "N/A", "make": "N/A", "model": "N/A", "vin": None})

                intent = MechanicIntent(**action_data)
                search_results = await execute_intent(intent, bay_id)
                parts_out = search_results

                if search_results.get("results"):
                    parts_info = ", ".join(
                        f"{r.get('product_name', r.get('description', ''))} at {r.get('price', 'N/A')}"
                        for r in search_results["results"]
                    )
                    reply += f" Found: {parts_info}."

                if bay.billing.total > 0:
                    reply += f" Current total: ${bay.billing.total:.2f}."

                # Auto-launch browser to add parts to cart
                # Skip if fitment engine halted the order (pending_browser_intent is set)
                has_parts = any(i.item_type == "PART" for i in intent.items)
                if has_parts and search_results.get("results"):
                    if bay.pending_browser_intent:
                        reply += " Fitment check flagged an issue — see the warning above."
                    else:
                        start_browser_thread(intent, search_results, bay_id)
                        reply += " Opening AutoZone to add to cart..."

            billing_out = bay.billing.model_dump()
            bay.status = AgentStatus.COMPLETE

        except Exception as e:
            print(f"[handle_chat_turn] action error for bay {bay_id} ({action_type}): {e}")
            reply += " Hmm, something didn't go through on that — want me to try again?"
            bay.status = AgentStatus.ERROR

    # Add assistant reply to history
    bay.chat_history.append(ChatMessage(
        role="assistant",
        content=reply,
        timestamp=datetime.now().isoformat(),
        has_action=(response_type == "action"),
    ))

    # Trim history to last 20 messages
    if len(bay.chat_history) > 20:
        bay.chat_history = bay.chat_history[-20:]

    if billing_out:
        await manager.broadcast({"type": "billing_update", "bay": bay_id, "billing": billing_out})

    return {
        "reply": reply,
        "response_type": response_type,
        "billing": billing_out,
        "parts_results": parts_out,
    }


@app.post("/api/chat")
async def chat(req: ChatRequest):
    return await handle_chat_turn(req.bay_number, req.message)


# ─────────────────────────────────────────────────────────────────────────
# Twilio phone integration
#
# A phone call is just another way for a mechanic to talk to BayOps — so
# each call gets its own bay in the same `bays` dict (keyed by Twilio's
# CallSid), and every turn is routed through the same handle_chat_turn()
# pipeline the web chat uses. Twilio does the speech-to-text for us via
# <Gather input="speech">; we generate the spoken reply with the same
# ElevenLabs voice /api/tts already uses, and hand Twilio a URL to fetch it.
# ─────────────────────────────────────────────────────────────────────────

def _call_bay_id(form: dict) -> str:
    return f"call-{form.get('CallSid', 'unknown')}"


def _speech_gather() -> Gather:
    return Gather(
        input="speech",
        action=tw.absolute_url("/twilio/gather"),
        method="POST",
        speech_timeout="auto",
        speech_model="phone_call",
        hints=tw.SPEECH_HINTS,
    )


async def _respond_to_job(job_id: str) -> VoiceResponse:
    """Waits briefly for a chat turn to finish. Quick turns (asking for a
    missing field, confirming something) resolve well within this window and
    the caller never notices. Slow turns (parts search, checkout) don't — so
    instead of leaving the caller in silence or risking a Twilio webhook
    timeout, we hand back a short natural "hold on" line plus a redirect
    to /twilio/poll, while the real work keeps running in the background."""
    result = await tw.wait_for_job(job_id, timeout=3.0)
    vr = VoiceResponse()

    if result is None:
        if not tw.job_is_known(job_id):
            # Job genuinely vanished (server restart mid-call, or a stale/
            # invalid id) — recover with a normal question instead of
            # looping a "still working" message on a job that doesn't exist.
            gather = _speech_gather()
            await tw.say(gather, "Sorry, I lost track of that — could you say that again?")
            vr.append(gather)
            vr.redirect(tw.absolute_url("/twilio/voice"))
            return vr

        await tw.say(vr, tw.next_hold_message(job_id))
        vr.pause(length=1)
        vr.redirect(tw.absolute_url(f"/twilio/poll?job={job_id}"), method="POST")
        return vr

    reply_text = result.get("reply") or "Sorry, could you say that again?"
    gather = _speech_gather()
    await tw.say(gather, reply_text)
    vr.append(gather)
    # If they hang up mid-Gather this never fires; if they just go quiet,
    # loop back to the greeting rather than dead-ending the call.
    vr.redirect(tw.absolute_url("/twilio/voice"))
    return vr


@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    """Twilio hits this the moment someone calls the shop's BayOps number."""
    form = dict(await request.form())
    if not tw.validate_twilio_request(request, form):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    bay_id = _call_bay_id(form)
    bays.setdefault(bay_id, BayStatus(bay_number=bay_id))

    vr = VoiceResponse()
    gather = _speech_gather()
    await tw.say(gather, tw.GREETING)
    vr.append(gather)
    # Caller said nothing before the Gather timed out — try once more.
    vr.redirect(tw.absolute_url("/twilio/voice"))
    return XMLResponse(content=str(vr), media_type="application/xml")


@app.post("/twilio/gather")
async def twilio_gather(request: Request):
    """Twilio hits this with the transcribed SpeechResult after each utterance."""
    form = dict(await request.form())
    if not tw.validate_twilio_request(request, form):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    bay_id = _call_bay_id(form)
    speech_text = form.get("SpeechResult", "").strip()

    if not speech_text:
        vr = VoiceResponse()
        gather = _speech_gather()
        await tw.say(gather, tw.NO_INPUT)
        vr.append(gather)
        vr.redirect(tw.absolute_url("/twilio/voice"))
        return XMLResponse(content=str(vr), media_type="application/xml")

    job_id = tw.start_job(handle_chat_turn(bay_id, speech_text))
    vr = await _respond_to_job(job_id)
    return XMLResponse(content=str(vr), media_type="application/xml")


@app.post("/twilio/poll")
async def twilio_poll(request: Request, job: str):
    """Twilio redirects here in a short loop while a slow chat turn (parts
    search, checkout) is still finishing in the background."""
    form = dict(await request.form())
    if not tw.validate_twilio_request(request, form):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    vr = await _respond_to_job(job)
    return XMLResponse(content=str(vr), media_type="application/xml")


@app.post("/twilio/status")
async def twilio_status(request: Request):
    """Optional call-state callback (ringing, completed, etc.) — set this as
    the phone number's 'Call Status Changes' webhook in the Twilio console
    to log call end in the bay's activity log."""
    form = dict(await request.form())
    bay_id = _call_bay_id(form)
    call_status = form.get("CallStatus", "")
    if bay_id in bays:
        bays[bay_id].logs.append(f"Call {call_status}")
        await manager.broadcast({"type": "agent_log", "bay": bay_id, "message": f"Call {call_status}"})
    return XMLResponse(status_code=204)


@app.get("/twilio/audio/{token}.mp3")
async def twilio_audio(token: str):
    """Twilio's <Play> verb fetches this with a plain GET — it can't receive
    audio bytes inline in the TwiML response, so generated clips are cached
    here briefly by tw.say() and served back out by token."""
    data = tw.get_cached_audio(token)
    if not data:
        raise HTTPException(status_code=404, detail="Audio expired or not found")
    return XMLResponse(content=data, media_type="audio/mpeg")


@app.get("/api/bays")
async def get_bays():
    return {bid: b.model_dump() for bid, b in bays.items()}


@app.get("/api/bays/{bay_id}/billing")
async def get_bay_billing(bay_id: str):
    if bay_id not in bays:
        return {"error": "Bay not found"}
    return bays[bay_id].billing.model_dump()


class SwitchVendorRequest(BaseModel):
    description: str
    vendor: str


@app.post("/api/bays/{bay_id}/switch-vendor")
async def switch_vendor(bay_id: str, req: SwitchVendorRequest):
    """Switch the selected vendor for a part and recalculate billing."""
    if bay_id not in bays:
        return {"error": "Bay not found"}

    bay = bays[bay_id]
    switched = False

    for r in bay.all_results:
        if r.get("description", "").lower() != req.description.lower():
            continue
        options = r.get("vendor_options", [])
        chosen = next((o for o in options if o.get("vendor") == req.vendor), None)
        if not chosen:
            return {"error": f"Vendor '{req.vendor}' not found for '{req.description}'"}

        # Update top-level fields to the chosen vendor
        r["product_name"] = chosen.get("product_name", r.get("product_name", ""))
        r["price"] = chosen.get("price", r.get("price", ""))
        r["vendor"] = chosen.get("vendor", "")
        r["source_url"] = chosen.get("source_url", "")
        r["in_stock"] = chosen.get("in_stock", True)
        r["part_number"] = chosen.get("part_number", "N/A")
        r["snippet"] = chosen.get("snippet", "")
        switched = True
        break

    # Also update bay.results (latest search display)
    if bay.results and bay.results.get("results"):
        for r in bay.results["results"]:
            if r.get("description", "").lower() != req.description.lower():
                continue
            options = r.get("vendor_options", [])
            chosen = next((o for o in options if o.get("vendor") == req.vendor), None)
            if chosen:
                r["product_name"] = chosen.get("product_name", "")
                r["price"] = chosen.get("price", "")
                r["vendor"] = chosen.get("vendor", "")
                r["source_url"] = chosen.get("source_url", "")
                r["in_stock"] = chosen.get("in_stock", True)
                r["part_number"] = chosen.get("part_number", "N/A")
                r["snippet"] = chosen.get("snippet", "")
            break

    if not switched:
        return {"error": f"Part '{req.description}' not found in results"}

    # Recalculate billing with new price
    bay.billing = calculate_billing(bay.all_items, bay.all_results, shop_config)
    _trigger_excel_update(bay_id)

    await manager.broadcast({
        "type": "billing_update", "bay": bay_id,
        "billing": bay.billing.model_dump(),
    })
    await manager.broadcast({
        "type": "search_complete", "bay": bay_id,
        "results": bay.results,
    })

    return {"status": "ok", "billing": bay.billing.model_dump()}


@app.post("/api/bays/{bay_id}/remove-item")
async def remove_bay_item(bay_id: str, body: dict):
    if bay_id not in bays:
        raise HTTPException(status_code=404, detail="Bay not found")
    bay = bays[bay_id]
    desc = body.get("description", "").lower().strip()
    if not desc:
        raise HTTPException(status_code=400, detail="description required")
    before = len(bay.all_items)
    exact = [it for it in bay.all_items if it.description.lower() == desc]
    if exact:
        bay.all_items   = [it for it in bay.all_items   if it.description.lower() != desc]
        bay.all_results = [r  for r  in bay.all_results if r.get("description", "").lower() != desc]
        bay.items       = [it for it in bay.items        if it.description.lower() != desc]
    else:
        bay.all_items   = [it for it in bay.all_items   if desc not in it.description.lower()]
        bay.all_results = [r  for r  in bay.all_results if desc not in r.get("description", "").lower()]
        bay.items       = [it for it in bay.items        if desc not in it.description.lower()]
    removed = before - len(bay.all_items)
    await recalculate_billing(bay_id)
    _trigger_excel_update(bay_id)
    await manager.broadcast({"type": "billing_update", "bay": bay_id, "billing": bay.billing.model_dump()})
    return {"status": "ok", "removed": removed, "billing": bay.billing.model_dump()}


@app.post("/api/bays/{bay_id}/edit-item")
async def edit_bay_item(bay_id: str, body: dict):
    if bay_id not in bays:
        raise HTTPException(status_code=404, detail="Bay not found")
    bay = bays[bay_id]
    desc = body.get("description", "").lower().strip()
    new_qty = float(body.get("quantity", 1))
    if not desc:
        raise HTTPException(status_code=400, detail="description required")
    updated = False
    for it in bay.all_items:
        stored = it.description.lower()
        if stored == desc or desc in stored or stored in desc:
            it.quantity = new_qty
            if it.item_type == "LABOR" and it.hours is not None:
                it.hours = new_qty
            updated = True
    await recalculate_billing(bay_id)
    _trigger_excel_update(bay_id)
    await manager.broadcast({"type": "billing_update", "bay": bay_id, "billing": bay.billing.model_dump()})
    return {"status": "ok", "updated": updated, "billing": bay.billing.model_dump()}


@app.post("/api/bays/{bay_id}/override-fitment")
async def override_fitment(bay_id: str):
    """User chose to proceed despite fitment warning — launch the pending browser job."""
    if bay_id not in bays:
        raise HTTPException(status_code=404, detail="Bay not found")
    bay = bays[bay_id]
    bay.fitment_override = True
    pending = bay.pending_browser_intent
    if pending:
        intent = MechanicIntent(**pending["intent"])
        start_browser_thread(intent, pending["search_results"], bay_id)
        bay.pending_browser_intent = None
    return {"status": "ok"}


@app.post("/api/bays/{bay_id}/export-excel")
async def export_bay_to_excel(bay_id: str):
    from excel_export import export_order_to_excel, open_in_excel, update_excel_live

    if bay_id not in bays:
        raise HTTPException(status_code=404, detail="Bay not found")

    bay = bays[bay_id]
    if not bay.billing or bay.billing.total == 0:
        raise HTTPException(status_code=400, detail="No billing data to export")

    bay_data = _assemble_bay_data(bay)
    existing_path = excel_files.get(bay_id)

    if existing_path and os.path.exists(existing_path):
        # File already created for this bay — update it in place, don't create a new one
        await asyncio.to_thread(update_excel_live, existing_path, bay_data)
        filename = os.path.basename(existing_path)
        await manager.broadcast({
            "type": "agent_log",
            "bay": bay_id,
            "message": f"Order updated in existing file: {filename}",
        })
        return {"status": "ok", "file_path": existing_path, "filename": filename}

    # First export for this bay — create the file and open it
    file_path = await asyncio.to_thread(export_order_to_excel, bay_data)
    filename = os.path.basename(file_path)

    # Register for live updates BEFORE opening so the first update doesn't race
    excel_files[bay_id] = file_path

    # Open in Excel via xlwings (keeps COM handle alive for live updates)
    await asyncio.to_thread(open_in_excel, file_path)

    await manager.broadcast({
        "type": "agent_log",
        "bay": bay_id,
        "message": f"Order exported to Desktop: {filename} — live updates enabled",
    })

    return {"status": "ok", "file_path": file_path, "filename": filename}


@app.post("/api/bays/{bay_id}/clear")
async def clear_bay(bay_id: str):
    from browser_agent import cleanup_bay_browser
    if bay_id in bays:
        bays[bay_id] = BayStatus(bay_number=bay_id)
    excel_files.pop(bay_id, None)   # stop live updates for this bay
    cleanup_bay_browser(bay_id)     # close any open Playwright window for this bay
    await manager.broadcast({"type": "bay_cleared", "bay": bay_id})
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000)