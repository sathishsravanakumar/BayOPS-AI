# BayOps AI

Voice-driven parts ordering assistant for auto repair shops. A mechanic speaks from the bay — or calls the shop's BayOps phone number — and BayOps AI handles the conversation, verifies fitment, searches parts prices across vendors, builds the estimate, and adds items to cart automatically.

---

## Quick Start

### 1. API Keys — create `backend/.env`

```
GROQ_API_KEY=your_groq_key
ELEVENLABS_API_KEY=your_elevenlabs_key
ANTHROPIC_API_KEY=your_anthropic_key      # optional — Claude Computer Use browser agent
BROWSER_HEADLESS=false                    # set true to run browser in background

TWILIO_ACCOUNT_SID=your_account_sid       # required for phone call integration
TWILIO_AUTH_TOKEN=your_auth_token         # required for phone call integration
TWILIO_PHONE_NUMBER=+1xxxxxxxxxx          # your Twilio phone number
PUBLIC_BASE_URL=https://your-ngrok-url    # public HTTPS URL Twilio can reach (ngrok in dev)
```

### 2. Backend

```bash
cd backend
pip install -r requirements.txt
playwright install chromium
uvicorn main:app --reload --port 8000 --proxy-headers --forwarded-allow-ips="*"
```

> `--proxy-headers --forwarded-allow-ips="*"` is required when running behind ngrok or a reverse proxy so Twilio's request signature validation works correctly.

### 3. Frontend

```bash
cd frontend
npm install
npm run dev
```

Frontend: http://localhost:5173 · Backend: http://localhost:8000

---

## What It Does

| Feature | Status |
|---------|--------|
| Voice input (mic + file upload) | Working |
| Text chat with AI advisor | Working |
| **Phone call voice ordering (Twilio)** | **Working** |
| Multi-vendor price comparison (AutoZone, NAPA, Advance Auto) | Working |
| Parallel parts search — all parts searched simultaneously | Working |
| Search result caching (10-min TTL, avoids repeat DDG hits) | Working |
| Batched vendor extraction — 1 Groq call per part across all 3 vendors | Working |
| Price sanity check (rejects hallucinated $0.01 / $9999 prices) | Working |
| Fitment verification engine (NHTSA + Groq) | Working |
| Manual price override via chat ("set brake pads to $45") | Working |
| Playwright browser — adds cheapest part to cart | Working |
| Cart verification after checkout | Working |
| Live billing estimate with markup + tax | Working |
| Remove / edit items from estimate | Working |
| Export to Excel — one file per order, updates live | Working |
| TTS voice replies (ElevenLabs) | Working |
| Shop Management System integration | Planned |

---

## Example

**Web / mic:**
> "Hey this is Mike in bay 3, I got a 2019 Honda Civic that needs front brake pads and an oil filter"

**Phone call:**
> Mechanic dials the shop's Twilio number → BayOps greets them → they speak the same request → same pipeline runs → order appears on the live dashboard

BayOps asks for anything missing, searches all vendors in parallel, verifies fitment, shows prices, adds to cart, and builds the live estimate — all in the chat.

---

## Tech Stack

- **Frontend** — React 19 + Vite + Tailwind CSS v4
- **Backend** — Python + FastAPI + WebSockets
- **AI Chat** — Groq `llama-3.3-70b-versatile`
- **Fitment Verification** — NHTSA vPIC API + Groq reasoning
- **Speech-to-Text / TTS** — ElevenLabs (web) · Twilio `<Gather input="speech">` (phone)
- **Phone Integration** — Twilio Voice webhooks + ElevenLabs TTS audio served per-call
- **Parts Search** — DuckDuckGo (free, no key) with session cache
- **Browser Automation** — Playwright / Claude Computer Use
- **Excel** — openpyxl (create) + xlwings (live updates, one file per order)

---

## Project Structure

```
backend/
  main.py           API server, WebSocket, all endpoints, bay state
  chat_agent.py     Conversational AI (Groq) — collects fields, dispatches actions
  twilio_agent.py   Phone call integration — TwiML, audio cache, job queue, signature validation
  parts_agent.py    Parallel multi-vendor price search with caching
  billing.py        Markup, labor, tax calculations
  browser_agent.py  Playwright cart automation (headless optional)
  fitment_agent.py  Parts fitment verification (NHTSA + Groq)
  excel_export.py   Excel file creation + live updates
  schemas.py        Pydantic data models
  utils.py          Shared utilities (parse_price)
  .env              API keys (not committed)

frontend/src/
  App.jsx           Main dashboard (dark sidebar, amber accent)
  HomePage.jsx      Landing page with nav bar
  components/       ChatThread, BillingPanel, BayCard, AgentLog
  bay_logo.png      BayOps AI brand logo (Sonic Wrench)
```