"""
Twilio phone-call integration for BayOps AI.

Flow
----
1. A mechanic calls the shop's BayOps number. Twilio POSTs to /twilio/voice.
   We reply with TwiML that greets the caller and opens a
   <Gather input="speech"> to listen for what they say.

2. Twilio does the speech-to-text itself and POSTs the transcript
   (SpeechResult) to /twilio/gather. That text is run through the exact
   same handle_chat_turn() pipeline the web chat uses in main.py, a spoken
   reply is generated with the same ElevenLabs voice /api/tts already uses,
   and the response opens another <Gather> — repeating for as long as the
   call continues.

3. /twilio/status is an optional callback Twilio hits on call-state changes
   (ringing, completed, etc.) so the bay's activity log picks up "Call ended".

Each phone call gets its own "bay" in main.py's `bays` dict, keyed by
Twilio's CallSid (see `_call_bay_id` in main.py) — so a phone-based order
shows up on the live dashboard exactly like a bay terminal would, and the
mechanic can still say "I'm Mike, bay 3" over the phone the same way they'd
type it, since the agent already asks for bay number as one of its six
required fields.

Why <Gather input="speech"> instead of Twilio Media Streams?
--------------------------------------------------------------
Media Streams (a raw, bidirectional audio WebSocket) is the lower-latency,
more natural-feeling option, but it means transcoding 8kHz mulaw audio,
running your own streaming STT, and handling turn-detection/barge-in
yourself. <Gather input="speech"> lets Twilio's own speech recognizer do
that work and simply POSTs you the finished transcript, which is enough to
get a real, working phone line stood up quickly on top of the existing
chat pipeline. If call latency or interruption-handling becomes the
bottleneck later, that's the natural place to upgrade to Media Streams —
the handle_chat_turn() pipeline underneath doesn't need to change either way.
"""

import os
import secrets
import time
from typing import Optional, Union

import httpx
from fastapi import Request
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Gather, VoiceResponse

GREETING = "Hey, this is BayOps. Go ahead — give me your name, bay number, and what you need."
NO_INPUT = "I didn't catch that. Are you still there?"

# In-memory cache of generated TTS clips: token -> (mp3 bytes, expires_at).
# Twilio's <Play> verb fetches its URL with a plain GET some moments after
# we build the TwiML, so the audio bytes can't be handed back inline in the
# webhook response — they're stashed here and served by /twilio/audio/{token}.mp3.
# This is fine for a single-process dev/small-shop deployment; for multiple
# backend workers, swap this dict for shared storage (Redis, S3, etc.).
_audio_cache: dict[str, tuple[bytes, float]] = {}
_AUDIO_TTL_SECONDS = 300

_ELEVENLABS_VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"  # same voice /api/tts uses


def _public_base_url() -> str:
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if not base:
        raise RuntimeError(
            "PUBLIC_BASE_URL is not set in backend/.env — Twilio needs a public "
            "HTTPS URL (e.g. an ngrok tunnel in dev, your real domain in prod) "
            "to fetch generated audio clips from /twilio/audio/*.mp3."
        )
    return base


async def synthesize_speech(text: str) -> bytes:
    """Same ElevenLabs call the web app's /api/tts endpoint makes."""
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key or not text:
        return b""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{_ELEVENLABS_VOICE_ID}",
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json={
                "text": text,
                "model_id": "eleven_turbo_v2_5",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            },
        )
    return resp.content if resp.status_code == 200 else b""


def _cache_audio(mp3_bytes: bytes) -> str:
    token = secrets.token_urlsafe(16)
    _audio_cache[token] = (mp3_bytes, time.time() + _AUDIO_TTL_SECONDS)
    _evict_expired()
    return token


def _evict_expired() -> None:
    now = time.time()
    for k in [k for k, (_, expires_at) in _audio_cache.items() if expires_at < now]:
        _audio_cache.pop(k, None)


def get_cached_audio(token: str) -> Optional[bytes]:
    entry = _audio_cache.get(token)
    if not entry:
        return None
    data, expires_at = entry
    if expires_at < time.time():
        _audio_cache.pop(token, None)
        return None
    return data


async def say(verb: Union[VoiceResponse, Gather], text: str) -> None:
    """Speak `text` inside a TwiML verb (VoiceResponse or Gather), using the
    shop's ElevenLabs voice when available and falling back to Twilio's
    built-in TTS (<Say>) if ElevenLabs isn't configured or the call fails —
    so a missing/expired API key degrades the call instead of dropping it."""
    audio = await synthesize_speech(text)
    if audio:
        token = _cache_audio(audio)
        verb.play(f"{_public_base_url()}/twilio/audio/{token}.mp3")
    else:
        verb.say(text, voice="Polly.Matthew")


def validate_twilio_request(request: Request, form: dict) -> bool:
    """Verifies the X-Twilio-Signature header so these webhooks can't be
    spoofed by a random POST — important here specifically because a fake
    'gather' request could otherwise trigger a real cart checkout.

    Skipped (returns True) only when TWILIO_AUTH_TOKEN isn't set, so local
    development without a Twilio account configured doesn't hard-fail.
    Do not deploy without TWILIO_AUTH_TOKEN set.
    """
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not auth_token:
        return True
    signature = request.headers.get("X-Twilio-Signature", "")
    validator = RequestValidator(auth_token)
    return validator.validate(str(request.url), form, signature)
