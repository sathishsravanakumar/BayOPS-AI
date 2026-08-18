# Voice Agent Setup — connecting BayOps AI to a real phone number

This is the full path from "I have the code and a Twilio phone number" to
"someone can call and talk to BayOps." A phone call is handled by the same
`handle_chat_turn()` pipeline the web chat already uses — Twilio just
becomes another way to reach it, alongside the browser.

Do these in order. Each phase says clearly whether it's something you run,
or something you click through in a browser.

---

## Phase 1 — Get the code in place

**You run:**

If you haven't already, drop these into `backend/`:
- `main.py` (replaces the existing one — adds `handle_chat_turn()` and the `/twilio/*` routes)
- `twilio_agent.py` (new file)
- `requirements.txt` (replaces the existing one — adds `twilio`, `python-multipart`)
- `.env.example` (replaces the existing one — documents the new Twilio variables)

```bash
cd backend
pip install -r requirements.txt
playwright install chromium
```

Confirm it's wired up correctly:
```bash
python -c "import main; print([r.path for r in main.app.routes if hasattr(r,'path')])"
```
You should see `/twilio/voice`, `/twilio/gather`, `/twilio/status`, and
`/twilio/audio/{token}.mp3` in the list, alongside the existing `/api/*` routes.

---

## Phase 2 — Environment variables

**You run:**

Create `backend/.env` (copy from `.env.example` as a starting point):

```
GROQ_API_KEY=your_groq_key
ELEVENLABS_API_KEY=your_elevenlabs_key
ANTHROPIC_API_KEY=your_anthropic_key      # optional
BROWSER_HEADLESS=false

TWILIO_AUTH_TOKEN=your_twilio_auth_token
PUBLIC_BASE_URL=                           # filled in during Phase 4
```

**You click, in the Twilio console:**

`console.twilio.com` → **Account → API keys & tokens** → copy the **Auth
Token** → paste it into `TWILIO_AUTH_TOKEN` above.

---

## Phase 3 — Get a phone number (skip if you already have one)

**You click, in the Twilio console:**

1. **Numbers & senders → Overview** — check if a trial number was already
   assigned to your account (very common). If so, skip to Phase 4.
2. If not: **Numbers & senders → Buy a number**
   - Destination country: yours
   - Capabilities: check **Voice** only
   - Number type: **Local** (not "Mobile" — that category doesn't mean
     much in the US/Canada and often returns no results)
   - Search, then **Buy** a number you like.
3. If search returns "No results found" — try again with a specific area
   code typed into "Search by digits or phrases," or click Reset and retry.
4. If you're prompted to upgrade before you can buy: trial accounts can
   provision one free number, but need a verified account (ID + payment
   method on file) for anything beyond that. Your trial credit still covers
   the number's cost after upgrading.

---

## Phase 4 — Expose your local backend to the internet (ngrok)

**You run:**

```bash
# install (Mac)
brew install ngrok
# or download from ngrok.com/download for your OS

ngrok config add-authtoken <your-authtoken>   # from your ngrok dashboard
```

Every free ngrok account gets **one fixed domain** tied to the account
(e.g. `your-name.ngrok-free.app`) — check **Domains** in your ngrok
dashboard. Using it means you configure the Twilio webhook once and it
keeps working across restarts, instead of changing every time.

Start the backend in one terminal:
```bash
cd backend
uvicorn main:app --reload --port 8000
```

Start the tunnel in a second terminal:
```bash
ngrok http --url=https://your-name.ngrok-free.app 8000
# (or just `ngrok http 8000` if you haven't reserved a domain — the URL
# it gives you will just change on restart, so you'd repeat Phase 5
# each time)
```

Copy the `https://...` URL ngrok shows you, put it in `backend/.env`:
```
PUBLIC_BASE_URL=https://your-name.ngrok-free.app
```
Restart `uvicorn` (Ctrl+C, run it again) so it picks up the new value —
`--reload` watches code changes, not `.env` changes.

---

## Phase 5 — Point the phone number at your backend

**You click, in the Twilio console:**

1. **Phone Numbers → Manage → Active Numbers** → click your number.
2. Under **Voice Configuration**:
   - **"A call comes in"** → Webhook → `https://your-name.ngrok-free.app/twilio/voice` → **HTTP POST**
   - **"Call status changes"** → `https://your-name.ngrok-free.app/twilio/status` → **HTTP POST**
3. **Save.**

---

## Phase 6 — Verify your test phone (trial accounts only)

**You click, in the Twilio console:**

**Numbers & senders → Verified Caller IDs → Add a new Caller ID** → enter
your own cell number → Twilio calls or texts you a code → enter it to
confirm.

Trial accounts generally can't receive calls from *any* number they
haven't verified this way — skip this and your test call may get silently
blocked before it ever reaches your webhook.

---

## Phase 7 — Test it

**Dry run first, no phone call needed** (confirms the pipeline works before you spend a minute of trial time):
```bash
curl -X POST http://localhost:8000/twilio/voice \
  -d "CallSid=CAtest1" -d "From=+15551234567"

curl -X POST http://localhost:8000/twilio/gather \
  -d "CallSid=CAtest1" -d "SpeechResult=Hey this is Mike in bay 3, I need front brake pads"
```
Both should return TwiML XML with a spoken reply inside a `<Gather>` block.
If the second one's reply is a generic error, check `GROQ_API_KEY` is set
correctly — that's the same failure path the web chat has if that key's missing.

**Real call:** dial your Twilio number from your verified phone. Watch:
- the `uvicorn` terminal, for request logs and any tracebacks
- `http://127.0.0.1:4040` (ngrok's local inspector) for the raw webhook traffic
- the BayOps dashboard — the call should appear as its own live bay,
  `call-<CallSid>`

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Call doesn't connect / goes to voicemail-like silence | ngrok not running, or webhook URL in Twilio doesn't match the current ngrok URL exactly |
| Call connects but immediately hangs up | Check `uvicorn` logs for a traceback — often a missing `.env` key |
| "This call cannot be completed" before it even rings | Your phone isn't in Verified Caller IDs (Phase 6) |
| Reply is Twilio's default computerized voice instead of the natural one | `ELEVENLABS_API_KEY` missing/invalid — `twilio_agent.py` falls back to Twilio's built-in voice on purpose so the call still works, but check the key if you want the real voice |
| Works via curl but not on a real call | Almost always the Twilio console webhook URL — re-check Phase 5 against your *current* ngrok URL |
| ngrok URL changed and everything broke | You're on a random (non-reserved) ngrok URL — go back to Phase 4 and reserve a static domain to stop this happening |

## Later: production

Everything above uses ngrok, which is a dev-only bridge. For a real
deployment, replace ngrok with an actual hosted backend (a real domain,
HTTPS) and point `PUBLIC_BASE_URL` and the Twilio webhooks at that instead
— nothing else about the integration changes.
