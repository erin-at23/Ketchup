"""Ketchup backend.

Run:  uvicorn main:app --host 0.0.0.0 --port 8000
Fake: FAKE_MODE=1 uvicorn main:app --host 0.0.0.0 --port 8000

Contract (all times are Unix epoch seconds, floats):
  POST /session/start   {}                                   -> {session_id, start_time}
  GET  /session                                              -> {session_id, start_time} | {session_id: null}
  POST /drift/start     {start, cause?, detail?}             -> {drift_id}
  POST /drift/end       {drift_id, end}                      -> {ok: true}
  POST /transcript      {start, end, text}                   -> {ok: true}
  GET  /state                                                -> {start_time, now, drifts, transcript, recaps}
  GET  /recap/latest                                         -> {status: none|pending|ready, drift_id, text}
  GET  /focus                                                -> {focused: bool}  (false while a drift is open; P2 polls it)
Every POST returns 409 if no session has started.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-4-hydrogen")  # fast Gemini 4, free tier (builder guide)
GEMINI_THINKING = os.getenv("GEMINI_THINKING", "low")  # hydrogen: low | medium | high | xhigh (never "minimal")
# Wait for late transcript lines. P2 stops streaming when focus returns and its last words
# can take up to ~5 s to come back from Deepgram, so 6 s.
RECAP_DELAY = float(os.getenv("RECAP_DELAY", "6"))
PAD_BEFORE = float(os.getenv("PAD_BEFORE", "5"))
PAD_AFTER = float(os.getenv("PAD_AFTER", "2"))
GEMINI_TIMEOUT = float(os.getenv("GEMINI_TIMEOUT", "10"))
MAX_INPUT_CHARS = int(os.getenv("MAX_INPUT_CHARS", "6000"))
FAKE_MODE = os.getenv("FAKE_MODE", "0") == "1"
FAKE_FILE = Path(__file__).parent / "fake_state.json"
UI_FILE = Path(__file__).parent / "static" / "index.html"

# ElevenLabs (recap voice). If the key is missing or the call fails, the page falls back to the
# browser's built-in voice, so the demo still speaks.
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "") or "21m00Tcm4TlvDq8ikWAM"
ELEVENLABS_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_flash_v2_5")
AUDIO = {}  # drift_id -> mp3 bytes (cache, cleared per session)

EMPTY_RECAP = "Nothing much was said while you were away."
PROMPT = (
    "A student zoned out during a lecture. Here is what the speaker said while they were away:\n\n"
    "{transcript}\n\n"
    "In under 60 words, tell the student what they missed. Speak directly to them "
    '("You missed..."). Keep key terms and examples. No intro.'
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("rewind")

app = FastAPI(title="Ketchup backend")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

# ---------------------------------------------------------------- state
# Everything runs on one asyncio event loop, so plain dicts are safe.
S = {
    "session_id": None,
    "start_time": None,
    "drifts": {},       # drift_id -> {drift_id, start, end, status}
    "transcript": [],   # [{start, end, text}]
    "recaps": {},       # drift_id -> {drift_id, text}
    "next_id": 1,
}

_gemini = None


def gemini():
    global _gemini
    if _gemini is None and GEMINI_API_KEY:
        from google import genai
        _gemini = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini


def require_session():
    if FAKE_MODE:
        return
    if S["session_id"] is None:
        raise HTTPException(status_code=409, detail="No session started. Call POST /session/start.")


def load_fake():
    return json.loads(FAKE_FILE.read_text())


# ---------------------------------------------------------------- models
class DriftStart(BaseModel):
    start: float
    cause: Optional[str] = None   # from P1's camera, e.g. "unrelated_screen"
    detail: Optional[str] = None  # human-readable, e.g. "Online shopping page with beauty products"


class DriftEnd(BaseModel):
    drift_id: int
    end: float


class Line(BaseModel):
    start: float
    end: float
    text: str


# ---------------------------------------------------------------- logging
@app.middleware("http")
async def log_requests(request: Request, call_next):
    t0 = time.time()
    response = await call_next(request)
    if request.url.path not in ("/state", "/focus"):  # polled constantly; would flood the log
        log.info("%s %s -> %s (%.0f ms)", request.method, request.url.path,
                 response.status_code, (time.time() - t0) * 1000)
    return response


# ---------------------------------------------------------------- session
@app.post("/session/start")
async def session_start():
    if FAKE_MODE:
        fake = load_fake()
        return {"session_id": "fake", "start_time": fake["start_time"]}
    AUDIO.clear()
    S.update(session_id=uuid.uuid4().hex[:8], start_time=time.time(),
             drifts={}, transcript=[], recaps={}, next_id=1)
    log.info("session %s started", S["session_id"])
    return {"session_id": S["session_id"], "start_time": S["start_time"]}


@app.get("/session")
async def session_get():
    if FAKE_MODE:
        return {"session_id": "fake", "start_time": load_fake()["start_time"]}
    if S["session_id"] is None:
        return {"session_id": None}
    return {"session_id": S["session_id"], "start_time": S["start_time"]}


# ---------------------------------------------------------------- drifts
@app.post("/drift/start")
async def drift_start(body: DriftStart):
    require_session()
    if FAKE_MODE:
        return {"drift_id": 99}
    drift_id = S["next_id"]
    S["next_id"] += 1
    S["drifts"][drift_id] = {"drift_id": drift_id, "start": body.start, "end": None, "status": "open",
                             "cause": body.cause, "detail": body.detail}
    return {"drift_id": drift_id}


@app.post("/drift/end")
async def drift_end(body: DriftEnd):
    require_session()
    if FAKE_MODE:
        return {"ok": True}
    d = S["drifts"].get(body.drift_id)
    if d is None:
        raise HTTPException(status_code=404, detail=f"Unknown drift_id {body.drift_id}")
    if d["end"] is not None:
        return {"ok": True}  # duplicate end: ignore
    d["end"] = max(body.end, d["start"])
    d["status"] = "pending"
    asyncio.create_task(make_recap(S["session_id"], body.drift_id))
    return {"ok": True}


# ---------------------------------------------------------------- transcript
@app.post("/transcript")
async def transcript(body: Line):
    require_session()
    if FAKE_MODE:
        return {"ok": True}
    text = body.text.strip()
    if text:
        S["transcript"].append({"start": body.start, "end": body.end, "text": text})
        S["transcript"].sort(key=lambda l: l["start"])  # lines can arrive out of order
    return {"ok": True}


# ---------------------------------------------------------------- reads
@app.get("/state")
async def state():
    if FAKE_MODE:
        fake = load_fake()
        fake["now"] = fake["start_time"] + 180
        return fake
    return {
        "start_time": S["start_time"],
        "now": time.time(),
        "drifts": sorted(S["drifts"].values(), key=lambda d: d["drift_id"]),
        "transcript": S["transcript"],
        "recaps": sorted(S["recaps"].values(), key=lambda r: r["drift_id"]),
    }


@app.get("/focus")
async def focus():
    """Polled by P2 (~2x/s). P2 only sends audio to Deepgram while focused is false,
    plus the 60 s before it, so lecture audio leaves the laptop only when the student drifts."""
    if FAKE_MODE or S["session_id"] is None:
        return {"focused": True}
    drifting = any(d["status"] == "open" for d in S["drifts"].values())
    return {"focused": not drifting}


@app.get("/recap/latest")
async def recap_latest():
    if FAKE_MODE:
        r = load_fake()["recaps"][-1]
        return {"status": "ready", "drift_id": r["drift_id"], "text": r["text"]}
    ended = [d for d in S["drifts"].values() if d["end"] is not None]
    if not ended:
        return {"status": "none", "drift_id": None, "text": None}
    d = max(ended, key=lambda d: d["end"])
    if d["status"] != "ready":
        return {"status": "pending", "drift_id": d["drift_id"], "text": None}
    return {"status": "ready", "drift_id": d["drift_id"], "text": S["recaps"][d["drift_id"]]["text"]}


# ---------------------------------------------------------------- recap logic
def missed_lines(drift):
    lo, hi = drift["start"] - PAD_BEFORE, drift["end"] + PAD_AFTER
    return [l for l in S["transcript"] if l["end"] >= lo and l["start"] <= hi]


def fallback_recap(text):
    """Used when Gemini is missing, slow or failing: first ~2 sentences of what was said."""
    words = text.split()
    short = " ".join(words[:45])
    return "You missed this: " + short + ("..." if len(words) > 45 else "")


def call_gemini(text):
    """Gemini 4 via the Interactions API (google-genai >= 3.2.0), per the hackathon builder guide.
    Gemini 4 models reject temperature/top_p/top_k, so only thinking_level is set."""
    client = gemini()
    if client is None:
        raise RuntimeError("GEMINI_API_KEY not set")
    resp = client.interactions.create(
        model=GEMINI_MODEL,
        input=PROMPT.format(transcript=text),
        generation_config={"thinking_level": GEMINI_THINKING},
    )
    out = (resp.output_text or "").strip()
    if not out:
        raise RuntimeError("empty Gemini response")
    return out


async def make_recap(session_id, drift_id):
    await asyncio.sleep(RECAP_DELAY)
    if S["session_id"] != session_id:  # session was reset meanwhile
        return
    d = S["drifts"][drift_id]
    lines = missed_lines(d)
    text = " ".join(l["text"] for l in lines)[-MAX_INPUT_CHARS:]
    t0 = time.time()
    if not text:
        recap = EMPTY_RECAP
    else:
        try:
            recap = await asyncio.wait_for(asyncio.to_thread(call_gemini, text), GEMINI_TIMEOUT)
        except Exception as e:  # noqa: BLE001 - never let a recap failure break the demo
            log.warning("Gemini failed (%s); using fallback", e)
            recap = fallback_recap(text)
    if S["session_id"] != session_id:
        return
    S["recaps"][drift_id] = {"drift_id": drift_id, "text": recap}
    d["status"] = "ready"
    if ELEVENLABS_API_KEY:  # make the voice now so "What did I miss?" plays instantly
        try:
            AUDIO[drift_id] = await asyncio.to_thread(tts, recap)
        except Exception as e:  # noqa: BLE001
            log.warning("ElevenLabs pre-generation failed (%s)", e)
    log.info("recap %d ready in %.1fs (%d lines): %s", drift_id, time.time() - t0, len(lines), recap[:80])


# ---------------------------------------------------------------- page + voice
@app.get("/")
async def page():
    """The timeline page (P5). Open http://<ip>:8001/ on the demo laptop."""
    return FileResponse(UI_FILE)


def tts(text):
    import requests
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}?output_format=mp3_44100_128",
        headers={"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"},
        json={"text": text, "model_id": ELEVENLABS_MODEL},
        timeout=15,
    )
    if r.status_code != 200:
        raise RuntimeError(f"ElevenLabs {r.status_code}: {r.text[:200]}")
    return r.content


@app.get("/recap/{drift_id}/audio")
async def recap_audio(drift_id: int):
    """MP3 of a recap in the ElevenLabs voice. 503 = use the browser voice instead."""
    rec = S["recaps"].get(drift_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="No recap for that drift yet")
    if drift_id not in AUDIO:
        if not ELEVENLABS_API_KEY:
            raise HTTPException(status_code=503, detail="ELEVENLABS_API_KEY not set")
        try:
            AUDIO[drift_id] = await asyncio.to_thread(tts, rec["text"])
        except Exception as e:  # noqa: BLE001
            log.warning("ElevenLabs failed (%s)", e)
            raise HTTPException(status_code=503, detail=str(e))
    return Response(content=AUDIO[drift_id], media_type="audio/mpeg")
