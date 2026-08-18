# BayOps AI — Design Document

**Version:** 1.4 | **Date:** July 2026

---

## What It Is

Voice-driven parts ordering assistant for auto repair shops. A mechanic speaks from the bay — or calls the shop's dedicated Twilio phone number — and BayOps AI handles the conversation, verifies fitment, searches parts prices across vendors in parallel, builds the estimate, and adds items to cart automatically. Every action appears on the live dashboard in real time via WebSocket, regardless of whether the order originated from the web UI or a phone call.

---

## How It Works

```
Mechanic speaks / types / phones in
        ↓
Chat Agent (Groq) — collects name, bay, vehicle, parts via conversation
        ↓
Fitment Agent — NHTSA vPIC lookup + Groq verification (confirms part fits vehicle)
        ↓
Parts Agent — parallel async search across AutoZone, NAPA Auto Parts, Advance Auto (DuckDuckGo)
             ↳ In-process cache (10-min TTL) skips repeat searches
             ↳ Price sanity check rejects hallucinated prices (<$0.50 or >$5,000)
        ↓
Billing — applies 25% markup + 8.25% tax, shows live estimate
        ↓
Browser Agent (background) — opens vendor site, adds to cart
        ↓
Excel Export (on demand) — one file per order; first export creates + opens it,
                           subsequent exports update the same open workbook live
```

**Phone call path (Twilio):**
```
Mechanic dials shop number
        ↓
Twilio → POST /twilio/voice → TwiML greeting + <Gather input="speech">
        ↓
Mechanic speaks → Twilio STT → POST /twilio/gather (SpeechResult)
        ↓
handle_chat_turn() — same pipeline as web chat (Groq → fitment → parts → billing)
        ↓
ElevenLabs TTS → MP3 cached at /twilio/audio/{token}.mp3 → Twilio <Play>
        ↓
Loop: next <Gather> keeps the call open for follow-up turns
```

All updates broadcast to the frontend dashboard in real time via WebSocket — phone orders appear alongside bay terminal orders.

---

## Tech Stack

| Layer | Tech |
|-------|------|
| Frontend | React 19 + Vite + Tailwind CSS v4 |
| Backend | Python + FastAPI |
| AI Conversation | Groq (`llama-3.3-70b-versatile`) |
| Fitment Verification | NHTSA vPIC API + Groq reasoning |
| Parts Search | DuckDuckGo (free, no key) + in-process session cache |
| Speech-to-Text (web) | ElevenLabs Scribe v1 |
| Speech-to-Text (phone) | Twilio `<Gather input="speech">` (built-in) |
| Text-to-Speech | ElevenLabs TTS (both web and phone paths) |
| Phone Integration | Twilio Voice webhooks + per-call audio cache |
| Browser Automation | Playwright (fallback) or Claude Computer Use |
| Excel Creation | openpyxl |
| Excel Live Updates | xlwings (Windows COM) |
| Real-time | FastAPI WebSocket |

---

## Files

```
backend/
  main.py            — FastAPI server, all endpoints, WebSocket, bay state
  chat_agent.py      — Conversational AI; enforces required fields before acting
  twilio_agent.py    — Phone call TwiML, ElevenLabs audio cache, async job queue,
                       Twilio request signature validation
  parts_agent.py     — Parallel vendor search + Groq price extraction + session cache
  fitment_agent.py   — NHTSA vPIC lookup + Groq fitment verification
  billing.py         — Markup, labor, tax calculation
  browser_agent.py   — Playwright / Claude Computer Use cart automation
  excel_export.py    — openpyxl file creation + xlwings live COM updates
  schemas.py         — Pydantic models (BayStatus, BillingLineItem, etc.)
  utils.py           — Shared utilities (parse_price, parse_price_float)
  requirements.txt

frontend/src/
  App.jsx            — Dashboard (dark charcoal sidebar, amber accent, chat, parts, billing)
  HomePage.jsx       — Landing page with sticky nav bar (logo + CTA)
  components/        — ChatThread, BillingPanel, BayCard, AgentLog
  bay_logo.png       — BayOps AI brand logo (Sonic Wrench — silver wrench + amber ECG waveform)
```

---

## API Endpoints

### Web / Dashboard

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/chat` | POST | Send a chat message, get AI response + trigger actions |
| `/api/transcribe` | POST | Audio file → text (ElevenLabs) |
| `/api/tts` | POST | Text → speech (ElevenLabs) |
| `/api/bays` | GET | All 6 bay states |
| `/api/bays/{id}/billing` | GET | Bay billing breakdown |
| `/api/bays/{id}/switch-vendor` | POST | Override vendor for a part |
| `/api/bays/{id}/remove-item` | POST | Remove a part or labor item from estimate |
| `/api/bays/{id}/edit-item` | POST | Change quantity/hours of an item |
| `/api/bays/{id}/override-fitment` | POST | Mark a fitment warning as accepted |
| `/api/bays/{id}/export-excel` | POST | Export order to Desktop .xlsx + open/update in Excel |
| `/api/bays/{id}/clear` | POST | Reset bay |
| `/ws` | WebSocket | Real-time push for all state changes |

### Twilio Voice

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/twilio/voice` | POST | Twilio calls this when a mechanic phones in — returns TwiML greeting + `<Gather>` |
| `/twilio/gather` | POST | Receives `SpeechResult` from Twilio, runs `handle_chat_turn()`, returns TwiML reply |
| `/twilio/status` | POST | Call-state change callback (ringing → completed); logs to bay activity |
| `/twilio/audio/{token}.mp3` | GET | Serves ElevenLabs MP3 audio clips for Twilio `<Play>` — 5-min TTL cache |

All Twilio endpoints validate `X-Twilio-Signature` via `RequestValidator`. Requests without a valid signature return HTTP 403.

### WebSocket Event Types

| Event | Direction | Payload |
|-------|-----------|---------|
| `search_started` | Server → Client | `{bay, count}` — triggers search spinner |
| `search_complete` | Server → Client | `{bay, results}` — dismisses spinner, shows parts table |
| `billing_update` | Server → Client | `{bay, billing}` — refreshes live estimate panel |
| `fitment_warning` | Server → Client | `{bay, halted, issues, clarification_needed}` |
| `cart_verified` | Server → Client | `{bay, verified, cart_total, expected_total, mismatch, cart_items}` |
| `agent_log` | Server → Client | `{bay, message}` — appends a line to the agent log panel |
| `bay_cleared` | Server → Client | `{bay}` — resets all UI state for that bay |

### Chat Action Types (`action` field in `/api/chat`)

| Action | Meaning |
|--------|---------|
| `SOURCE_PARTS` | Search vendors and compare prices |
| `ADD_LABOR` | Add labor hours to estimate |
| `CHECKOUT` | Open browser, add parts to cart |
| `REMOVE_ITEM` | Remove a part or labor line |
| `EDIT_ITEM` | Change quantity or hours |
| `SET_PRICE` | Manually override a part price |

---

## Twilio Phone Integration

Each inbound phone call is assigned a virtual bay keyed by Twilio's `CallSid` (`_call_bay_id` in `main.py`). This means:
- The phone order appears on the live dashboard alongside bay terminal orders
- The mechanic says "I'm Mike, bay 3" over the phone just like they'd type it — the agent already requires bay number as one of its six required fields
- All WebSocket events fire normally, so the dashboard updates in real time during the call

**Slow operation handling:** Parts search and checkout can take 3–15 seconds — too long to hold a synchronous Twilio webhook open. `twilio_agent.py` runs these as background `asyncio.Task` jobs. The `/twilio/gather` response returns immediately with a rotating hold message ("Give me just a second...") and opens a new `<Gather>` that polls back. Once the job completes, the next `/twilio/gather` hit delivers the real result.

**Audio:** ElevenLabs TTS generates an MP3 for each reply. The bytes are cached in-memory at a random token and served via `GET /twilio/audio/{token}.mp3`, which Twilio fetches when playing the `<Play>` verb. Falls back to Twilio's built-in `<Say voice="Polly.Matthew">` if ElevenLabs is unavailable.

**Speech hints:** `<Gather>` includes a `hints` parameter with common automotive vocabulary (part names, makes, "bay one/two/three") to bias Twilio's phone speech recognizer toward shop terminology, which measurably reduces mishears on low-quality phone audio.

---

## Bay State

Each of the 6 bays (plus any active phone calls) holds:
- `vehicle` — year, make, model
- `technician_name`
- `all_items` — accumulated parts + labor across conversation turns
- `all_results` — accumulated search results (for vendor switching + price override)
- `billing` — live estimate (parts, labor, tax, total)
- `chat_history` — full conversation per bay
- `logs` — browser agent terminal output

State is in-memory only — resets on server restart.

---

## Search Architecture

Parts search runs with `asyncio.gather` so all parts in a single request are searched concurrently. Each vendor is also searched concurrently within a part.

**Per-part flow (one Groq call total, not one per vendor):**
1. Three vendor DDG fetches run in parallel via `run_in_executor` (one `site:{domain}` query each, max 5 results)
2. If DDG returns nothing for a vendor, an `httpx` direct-GET fallback hits the vendor's search page and parses embedded `"price":"XX.XX"` JSON patterns from the HTML
3. All three vendors' raw results are combined into a single Groq call (`extract_all_vendors`) — the model receives one labelled section per vendor and returns a `vendors` array. This reduces Groq calls from 3 per part to 1 per part (9 → 3 for a typical 3-part request)
4. Snippet and title text is capped at 150/120 characters per result, and at most 3 results per vendor are sent, keeping the extraction prompt compact

A dict-based cache (`_SEARCH_CACHE`) keyed on `{year}|{make}|{model}|{description}` stores results for 10 minutes, preventing redundant DDG hits when the same part is re-queried within a session.

After extraction, a sanity check rejects prices below $0.50 or above $5,000 (replaced with "See website") to prevent hallucinated values entering billing. DDG searches retry once with a 0.5 s delay on transient failure.

**Chat agent token controls:** history sent to Groq is capped at the last 8 messages (4 turns); the completion is bounded by `max_tokens=512`; bay context omits verbose product names and sends only description + price + vendor for already-found parts.

---

## Fitment Verification

When parts are found, `fitment_agent.py` calls NHTSA vPIC to normalize the vehicle (decode make/model IDs, trim levels, engine variants) then asks Groq whether each part is likely compatible. NHTSA responses are cached in-process per `{vin}|{year}|{make}|{model}` to avoid repeat API calls within a session. Results are broadcast as `fitment_warning` events. Mechanics can override a warning via the frontend toggle or the `/api/bays/{id}/override-fitment` endpoint.

---

## Shop Configuration

```python
labor_rate     = $150.00 / hr
parts_markup   = 25%
tax_rate       = 8.25%
```

Configurable in `ShopConfig` in `schemas.py`.

---

## Excel Export

- Click **Export to Excel** → first call creates `BayOps_Order_YYYYMMDD_HHMMSS.xlsx` on the Desktop and opens it in Excel via xlwings COM
- The file path is stored per bay in `excel_files[bay_id]`; subsequent export calls update the same open workbook in place — no new file is created
- Every billing change also auto-updates the open workbook live
- Labor sub-headers and total rows are rewritten on every update to survive `clear_contents()` calls
- On bay reset, the live link is severed and the next export creates a new file

---

## UI & Design

**Color system:** Two-tone palette derived from the brand logo — charcoal `#111110` for the sidebar and HomePage nav bar, amber-600 for all interactive accents (buttons, selected states, chat bubbles, mic button). Semantic colors are kept separate: emerald for money/status indicators, red for destructive actions and recording state.

**Logo:** `bay_logo.png` (Sonic Wrench — silver wrench + amber ECG waveform + amber wrench outline) used in the sidebar header, HomePage nav, and footer. Imported as a Vite-bundled PNG asset.

---

## UX Features

| Feature | How it works |
|---------|-------------|
| Search spinner | `search_started` WebSocket event shows a spinner card during the 3–10 s vendor search |
| Product name | Parts panel shows mechanic's description + actual product name from vendor |
| Vendor comparison | All 3 vendors shown per part; click any row to switch — billing recalculates immediately |
| Confirm before clear | Trash button requires inline Yes/No confirmation before wiping a bay |
| Copy estimate | One-click plain-text copy of the full estimate for pasting into a shop management system |
| Expandable agent log | Toggle between compact (180 px) and full-height (400 px) terminal view |
| Mobile sidebar | Hamburger menu slides sidebar in/out on small screens; overlay tap dismisses |
| Cart chime | Web Audio API plays a short 880 Hz tone when `cart_verified` fires |
| Phone ordering | Mechanic dials in, speaks their request — same pipeline, order appears on dashboard |

---

## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `GROQ_API_KEY` | Yes | AI conversation + price extraction |
| `ELEVENLABS_API_KEY` | Yes | Speech-to-text + TTS (web and phone) |
| `ANTHROPIC_API_KEY` | No | Claude Computer Use browser agent (Playwright used if absent) |
| `BROWSER_HEADLESS` | No | Set `true` to run Playwright browser in background (default: `false`) |
| `TWILIO_ACCOUNT_SID` | For phone | Twilio account identifier |
| `TWILIO_AUTH_TOKEN` | For phone | Twilio webhook signature validation |
| `TWILIO_PHONE_NUMBER` | For phone | The shop's Twilio number (e.g. `+15551234567`) |
| `PUBLIC_BASE_URL` | For phone | Public HTTPS URL Twilio can reach — ngrok tunnel in dev, real domain in prod |

---

## Backend Startup (Production / Twilio)

```bash
uvicorn main:app --reload --port 8000 --proxy-headers --forwarded-allow-ips="*"
```

`--proxy-headers` and `--forwarded-allow-ips="*"` are required when running behind ngrok or any reverse proxy so that Twilio's `X-Twilio-Signature` validation uses the original public URL rather than the localhost URL.

---

## Roadmap

- Agent 2: Auto-create repair orders in Tekmetric / Shop-Ware
- SQLite persistence — bay state survives server restarts
- Order history — completed jobs stored and searchable
- Twilio Media Streams — lower-latency bidirectional audio vs. current `<Gather>` polling
- More vendors (WorldPac, RockAuto)
- PDF export for print-ready estimates
