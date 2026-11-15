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
  GET  /transcript?since=<epoch>                             -> {transcript: [...]}  (camera's relevance check)
  POST /session/end                                          -> report (stats + Gemini analysis)
  POST /ask             {question, drift_id?}                -> {answer}
  GET  /sessions, GET /sessions/{id}                          -> past sessions + their reports
  POST /camera/start, /camera/stop                           -> launch/stop camera/drift.py from the page
  POST /camera/ping                                          -> camera heartbeat (shown on the page)
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
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
# which AI writes recaps/answers/reports: "openai" or "gemini". The other one is the backup.
LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "openai").strip().lower()
AI_BACKUP = os.getenv("AI_BACKUP", "0") == "1"  # 1 = if the chosen AI fails, try the other one
# the end-of-session report can use a different AI (one call per session), e.g. REPORT_PROVIDER=gemini
REPORT_PROVIDER = (os.getenv("REPORT_PROVIDER") or LLM_PROVIDER).strip().lower()  # optional override for the report
LAST_PROVIDER = {"name": None}
PROVIDER_TIMEOUT = float(os.getenv("PROVIDER_TIMEOUT", "20"))  # seconds per AI before trying the other
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
HISTORY_FILE = Path(__file__).parent / "sessions.json"  # past session stats, for "what do I usually miss"

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
    "ended_at": None,
    "summary": None,
}

_gemini = None
CAMERA = {"t": 0.0}  # last heartbeat from the camera script (camera/drift.py)
CAMERA_DIR = Path(os.getenv("CAMERA_DIR") or Path(__file__).resolve().parent.parent / "camera")
CAMERA_PROC = {"p": None, "log": None}  # camera started from the page
CAMERA_FRAME = {"jpg": None, "t": 0.0}  # latest annotated camera frame (shown live on the page)
CAMERA_CMDS = []                          # key presses sent from the page to the camera (c, x, d, [, ])
REPO_DIR = Path(__file__).resolve().parent.parent
BAND_PROC = {"p": None, "log": None, "mode": None}  # audio/transcribe.py started from the page


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


def require_live():
    require_session()
    if S["ended_at"] is not None:
        raise HTTPException(status_code=409, detail="Session ended. Start a new session.")


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
    if request.url.path not in ("/state", "/focus", "/camera/ping", "/camera/frame", "/camera/stream"):  # polled constantly; would flood the log
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
             drifts={}, transcript=[], recaps={}, next_id=1, ended_at=None, summary=None)
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
    require_live()
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


# ---------------------------------------------------------------- camera status
class CameraPing(BaseModel):
    state: Optional[str] = None
    why: Optional[str] = None
    match: Optional[int] = None
    thr: Optional[int] = None
    targets: Optional[int] = None


@app.post("/camera/ping")
async def camera_ping(body: CameraPing):
    """Heartbeat from camera/drift.py every 2 s (works with or without a session)."""
    CAMERA.update(body.model_dump(), t=time.time())
    cmds = CAMERA_CMDS[:]
    CAMERA_CMDS.clear()
    return {"ok": True, "commands": cmds}


@app.post("/camera/frame")
async def camera_frame_in(request: Request):
    CAMERA_FRAME.update(jpg=await request.body(), t=time.time())
    return {"ok": True}


@app.get("/camera/stream")
async def camera_stream():
    """MJPEG stream of the camera view: <img src="/camera/stream">."""
    from fastapi.responses import StreamingResponse

    async def gen():
        last = None
        while True:
            jpg = CAMERA_FRAME["jpg"]
            if jpg is not None and jpg is not last and time.time() - CAMERA_FRAME["t"] < 5:
                last = jpg
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n"
            await asyncio.sleep(0.1)
    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


class CameraKey(BaseModel):
    key: str


@app.post("/camera/key")
async def camera_key(body: CameraKey):
    """Press a camera key from the page: c (set lecture view), x (clear), d (manual drift), [ or ] (threshold)."""
    if body.key not in ("c", "x", "d", "[", "]"):
        raise HTTPException(status_code=400, detail="Allowed keys: c x d [ ]")
    CAMERA_CMDS.append(body.key)
    return {"ok": True}


# ---------------------------------------------------------------- transcript
@app.post("/transcript")
async def transcript(body: Line):
    require_live()
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
        "now": S["ended_at"] or time.time(),
        "ended_at": S["ended_at"],
        "camera": {**CAMERA, "connected": time.time() - CAMERA["t"] < 6, **_camera_proc_state(),
                   "streaming": time.time() - CAMERA_FRAME["t"] < 3},
        "band": _band_state(),
        "drifts": sorted(S["drifts"].values(), key=lambda d: d["drift_id"]),
        "transcript": S["transcript"],
        "recaps": sorted(S["recaps"].values(), key=lambda r: r["drift_id"]),
    }


@app.get("/transcript")
async def transcript_recent(since: Optional[float] = None):
    """Recent lines for the camera's relevance check (P1): GET /transcript?since=<epoch>."""
    lines = load_fake()["transcript"] if FAKE_MODE else S["transcript"]
    if since is not None:
        lines = [l for l in lines if l["end"] >= since]
    return {"transcript": lines}


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
    lo, hi = drift["start"] - PAD_BEFORE, (drift["end"] or time.time()) + PAD_AFTER
    return [l for l in S["transcript"] if l["end"] >= lo and l["start"] <= hi]


def fallback_recap(text):
    """Used when Gemini is missing, slow or failing: first ~2 sentences of what was said."""
    words = text.split()
    short = " ".join(words[:45])
    return "You missed this: " + short + ("..." if len(words) > 45 else "")


def ask_gemini(prompt):
    """Gemini 4 via the Interactions API (google-genai >= 3.2.0), per the hackathon builder guide.
    Gemini 4 models reject temperature/top_p/top_k, so only thinking_level is set."""
    client = gemini()
    if client is None:
        raise RuntimeError("GEMINI_API_KEY not set")
    resp = client.interactions.create(
        model=GEMINI_MODEL,
        input=prompt,
        generation_config={"thinking_level": GEMINI_THINKING},
    )
    out = (resp.output_text or "").strip()
    if not out:
        raise RuntimeError("empty Gemini response")
    return out


def ask_openai(prompt):
    import requests
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not set")
    r = requests.post(f"{OPENAI_BASE_URL}/chat/completions",
                      headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                      json={"model": OPENAI_MODEL, "messages": [{"role": "user", "content": prompt}]},
                      timeout=25)
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI {r.status_code}: {r.text[:200]}")
    out = (r.json()["choices"][0]["message"]["content"] or "").strip()
    if not out:
        raise RuntimeError("empty OpenAI response")
    return out


def ask_llm(prompt, prefer=None):
    """Ask the chosen AI (LLM_PROVIDER, or `prefer`); if it fails, try the other one."""
    order = [("openai", ask_openai), ("gemini", ask_gemini)]
    if (prefer or LLM_PROVIDER) != "openai":
        order.reverse()
    if not AI_BACKUP:  # default: only the chosen AI, no fallback to the other
        order = order[:1]
    # Each AI gets its own time limit. A rate-limited key (429) makes the Google SDK retry for a
    # long time; without a per-provider limit that would use up the whole wait and the backup
    # would never be tried.
    import concurrent.futures as cf
    errors = []
    for name, fn in order:
        pool = cf.ThreadPoolExecutor(max_workers=1)
        try:
            out = pool.submit(fn, prompt).result(timeout=PROVIDER_TIMEOUT)
            LAST_PROVIDER["name"] = name
            return out
        except cf.TimeoutError:
            errors.append(f"{name}: no answer within {PROVIDER_TIMEOUT:.0f}s")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    raise RuntimeError(" | ".join(errors))


def call_gemini(text):  # name kept for older code paths; uses whichever AI is configured
    return ask_llm(PROMPT.format(transcript=text))


async def make_recap(session_id, drift_id, delay=None):
    await asyncio.sleep(RECAP_DELAY if delay is None else delay)
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
            recap = await asyncio.wait_for(asyncio.to_thread(call_gemini, text), PROVIDER_TIMEOUT * 2 + 5)
        except Exception as e:  # noqa: BLE001 - never let a recap failure break the demo
            log.warning("AI recap failed (%s); using fallback", e)
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


class SpeakBody(BaseModel):
    text: str


@app.post("/speak")
async def speak(body: SpeakBody):
    """MP3 of any short text (answers to questions) in the ElevenLabs voice. 503 = use the browser voice."""
    if not ELEVENLABS_API_KEY:
        raise HTTPException(status_code=503, detail="ELEVENLABS_API_KEY not set")
    try:
        audio = await asyncio.to_thread(tts, body.text[:1200])
    except Exception as e:  # noqa: BLE001
        log.warning("ElevenLabs failed (%s)", e)
        raise HTTPException(status_code=503, detail=str(e))
    return Response(content=audio, media_type="audio/mpeg")


# ---------------------------------------------------------------- ask about what you missed
class AskBody(BaseModel):
    question: str
    drift_id: Optional[int] = None


ASK_PROMPT = (
    "You help a student catch up on a lecture they zoned out of. Below is the lecture transcript "
    "(speech-to-text, may contain errors). The part they MISSED is marked between <missed> tags; "
    "the rest is surrounding context.\n\n{context}\n\n"
    "Student's question: {question}\n\n"
    "Answer in under 70 words, speaking directly to the student. Use only the transcript. "
    "If it doesn't cover the question, say so briefly. No intro."
)


def _fmt_rel(t):
    s = max(0, int(t - (S["start_time"] or t)))
    return f"{s // 60}:{s % 60:02d}"


def _context_for(drift):
    lo, hi = drift["start"] - PAD_BEFORE, (drift["end"] or time.time()) + PAD_AFTER
    out = []
    for l in S["transcript"]:
        if l["end"] < lo - 90 or l["start"] > hi + 60:
            continue
        missed = l["end"] >= lo and l["start"] <= hi
        line = f"[{_fmt_rel(l['start'])}] {l['text']}"
        out.append(f"<missed>{line}</missed>" if missed else line)
    return "\n".join(out)[-MAX_INPUT_CHARS:]


@app.post("/ask")
async def ask(body: AskBody):
    require_session()
    drifts = sorted(S["drifts"].values(), key=lambda d: d["start"])
    if body.drift_id is not None:
        drift = S["drifts"].get(body.drift_id)
    else:
        ended = [d for d in drifts if d["end"] is not None]
        drift = (ended or drifts)[-1] if drifts else None
    if drift is None:
        context = "\n".join(f"[{_fmt_rel(l['start'])}] {l['text']}" for l in S["transcript"])[-MAX_INPUT_CHARS:]
    else:
        context = _context_for(drift)
    if not context.strip():
        return {"answer": "There's no transcript for that part yet, so I can't answer.",
                "drift_id": drift and drift["drift_id"]}
    try:
        answer = await asyncio.wait_for(
            asyncio.to_thread(ask_llm, ASK_PROMPT.format(context=context, question=body.question.strip()[:500])),
            PROVIDER_TIMEOUT * 2 + 5)
    except Exception as e:  # noqa: BLE001
        log.warning("AI /ask failed (%s)", e)
        answer = "I couldn't reach the AI just now. Here's what was said: " + \
                 " ".join(l["text"] for l in missed_lines(drift))[:400] if drift else "I couldn't reach the AI just now."
    return {"answer": answer, "drift_id": drift and drift["drift_id"]}


# ---------------------------------------------------------------- end session + report
CAUSE_LABEL = {
    "away": "Looked away", "phone": "Phone", "unrelated_screen": "Unrelated screen",
    "manual": "Marked by hand", "eyes_closed": "Eyes closed", None: "Marked by hand",
}

TIPS = {
    "phone": "Put your phone face down in your bag before class starts; a phone in view pulls attention even when you don't touch it.",
    "unrelated_screen": "Close every tab except the slides or notes, or use a site blocker during lecture hours.",
    "away": "Sit closer to the front and take notes by hand; writing keeps your eyes on the board.",
    "eyes_closed": "You may be tired: get up at the break, drink water, and aim for more sleep before this class.",
    "manual": "Right after you zone out, write down the last thing you heard; it makes catching up faster.",
}


def _load_history():
    try:
        return json.loads(HISTORY_FILE.read_text())
    except Exception:  # noqa: BLE001
        return []


def _stats():
    t0, t1 = S["start_time"], S["ended_at"] or time.time()
    dur = max(1.0, t1 - t0)
    rows, causes, missed = [], {}, 0.0
    for d in sorted(S["drifts"].values(), key=lambda d: d["start"]):
        a, b = max(d["start"], t0), min(d["end"] or t1, t1)
        length = max(0.0, b - a)
        missed += length
        key = d.get("cause") or "manual"
        c = causes.setdefault(key, {"cause": key, "label": CAUSE_LABEL.get(key, key.replace("_", " ").capitalize()),
                                    "count": 0, "seconds": 0.0})
        c["count"] += 1
        c["seconds"] += length
        rec = S["recaps"].get(d["drift_id"], {}).get("text")
        rows.append({"drift_id": d["drift_id"], "at": a - t0, "length": length, "cause": key,
                     "label": c["label"], "detail": d.get("detail"), "recap": rec})
    half = t0 + dur / 2
    first_half = sum(1 for d in S["drifts"].values() if d["start"] < half)
    return {
        "duration": dur, "missed": missed, "percent": round(100 * missed / dur, 1),
        "count": len(rows), "longest": max((r["length"] for r in rows), default=0.0),
        "average": (missed / len(rows)) if rows else 0.0,
        "first_half": first_half, "second_half": len(rows) - first_half,
        "causes": sorted(causes.values(), key=lambda c: -c["seconds"]),
        "drifts": rows,
    }


SUMMARY_PROMPT = (
    "You are a friendly study coach. A student just finished a lecture while a wearable tracked when they "
    "zoned out. Write their end-of-class report.\n\n"
    "THIS SESSION (seconds):\n{stats}\n\n"
    "WHAT THEY MISSED, per drift (time into class, length, why, transcript recap):\n{drifts}\n\n"
    "EARLIER SESSIONS (most recent last):\n{history}\n\n"
    "LECTURE TRANSCRIPT (may be partial):\n{transcript}\n\n"
    "Return ONLY a JSON object with these keys:\n"
    '  "headline": one sentence summing up their focus today (specific, kind, no fluff),\n'
    '  "missed_topics": 2-5 short phrases naming the concepts they missed,\n'
    '  "patterns": 2-4 short observations about when and why they drift (use earlier sessions if any),\n'
    '  "tips": 3 concrete, specific things to try next class, tied to their actual causes,\n'
    '  "lecture_summary": 3-6 sentences summarizing what the whole lecture covered, in order.\n'
    "Speak to the student as \"you\"."
)


def _fallback_analysis(st):
    causes = st["causes"]
    top = causes[0]["label"].lower() if causes else None
    return {
        "headline": (f"You stayed with the lecture {100 - st['percent']:.0f}% of the time"
                     + (f"; most of what you missed came from: {top}." if top else ".")),
        "missed_topics": [r["recap"].split(". ")[0][:90] for r in st["drifts"] if r["recap"]][:5],
        "patterns": [f"{c['label']}: {c['count']} time(s), {int(c['seconds'])} s" for c in causes][:4]
                    + ([f"More drifts in the {'second' if st['second_half'] > st['first_half'] else 'first'} half of class."]
                       if st["count"] > 1 and st["first_half"] != st["second_half"] else []),
        "tips": [TIPS.get(c["cause"], TIPS["manual"]) for c in causes][:3] or [TIPS["manual"]],
        "lecture_summary": "",
    }


async def _build_summary():
    st = _stats()
    history = _load_history()[-5:]
    hist_txt = "\n".join(
        f"- {h.get('date', '?')}: missed {h['percent']}% over {int(h['duration'] // 60)} min; causes: "
        + ", ".join(f"{c['label']} x{c['count']}" for c in h.get("causes", []))
        for h in history) or "(none)"
    drift_txt = "\n".join(
        f"- at {int(r['at'] // 60)}:{int(r['at'] % 60):02d}, {int(r['length'])} s, {r['label']}"
        + (f" ({r['detail']})" if r["detail"] else "") + f": {r['recap'] or '(no recap)'}"
        for r in st["drifts"]) or "(no drifts)"
    stats_txt = json.dumps({k: (round(v, 1) if isinstance(v, float) else v) for k, v in st.items()
                            if k not in ("drifts", "causes")})
    transcript = " ".join(l["text"] for l in S["transcript"])[-4000:] or "(none)"
    analysis, by = None, "fallback"
    try:
        raw = await asyncio.wait_for(asyncio.to_thread(ask_llm, SUMMARY_PROMPT.format(
            stats=stats_txt, drifts=drift_txt, history=hist_txt, transcript=transcript), REPORT_PROVIDER), PROVIDER_TIMEOUT * 2 + 10)
        analysis = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
        for k in ("missed_topics", "patterns", "tips"):
            analysis[k] = [str(x) for x in analysis.get(k, [])][:5]
        analysis["headline"] = str(analysis.get("headline", ""))
        analysis["lecture_summary"] = str(analysis.get("lecture_summary", ""))
        by = LAST_PROVIDER["name"] or LLM_PROVIDER
    except Exception as e:  # noqa: BLE001
        log.warning("AI summary failed (%s); using fallback", e)
    analysis = analysis or _fallback_analysis(st)
    summary = {"session_id": S["session_id"], "date": time.strftime("%a %b %d, %I:%M %p"),
               "start_time": S["start_time"], "transcript": S["transcript"],
               "stats": st, "analysis": analysis, "generated_by": by,
               "history": [{"date": h.get("date"), "percent": h["percent"]} for h in history]}
    try:
        all_hist = _load_history()
        all_hist.append({"id": S["session_id"], "date": time.strftime("%b %d %H:%M"), "percent": st["percent"],
                         "duration": st["duration"], "count": st["count"],
                         "causes": [{k: c[k] for k in ("cause", "label", "count", "seconds")} for c in st["causes"]],
                         "summary": summary})
        HISTORY_FILE.write_text(json.dumps(all_hist[-30:]))
    except Exception as e:  # noqa: BLE001
        log.warning("could not save session history (%s)", e)
    return summary


@app.post("/session/end")
async def session_end():
    require_session()
    if S["summary"] is not None:
        return S["summary"]
    sid = S["session_id"]
    if S["ended_at"] is None:
        S["ended_at"] = time.time()
        for d in S["drifts"].values():  # close any drift still open; recap it right away
            if d["end"] is None:
                d["end"], d["status"] = S["ended_at"], "pending"
                asyncio.create_task(make_recap(sid, d["drift_id"], delay=1))
    # Answer right away with the numbers; the AI-written parts are filled in by a background task
    # and the page polls GET /session/summary until generated_by is no longer "pending".
    S["summary"] = {"session_id": sid, "date": time.strftime("%a %b %d, %I:%M %p"),
                    "start_time": S["start_time"], "transcript": S["transcript"],
                    "stats": _stats(), "analysis": None, "generated_by": "pending",
                    "history": [{"date": h.get("date"), "percent": h["percent"]} for h in _load_history()[-5:]]}
    asyncio.create_task(_finish_report(sid))
    return S["summary"]


async def _finish_report(sid):
    deadline = time.time() + PROVIDER_TIMEOUT + 5
    while any(d["status"] != "ready" for d in S["drifts"].values()) and time.time() < deadline:
        await asyncio.sleep(0.3)
    if S["session_id"] != sid:
        return
    summary = await _build_summary()
    if S["session_id"] == sid:
        S["summary"] = summary
    log.info("session %s ended: missed %.0f%% (%d drifts), report by %s",
             sid, summary["stats"]["percent"], summary["stats"]["count"], summary["generated_by"])


@app.get("/session/summary")
async def session_summary():
    if S["summary"] is None:
        raise HTTPException(status_code=404, detail="Session not ended yet")
    return S["summary"]


# ---------------------------------------------------------------- past sessions
@app.get("/sessions")
async def sessions_list():
    """Past sessions, newest first (from sessions.json)."""
    out = []
    for h in reversed(_load_history()):
        out.append({"id": h.get("id"), "date": (h.get("summary") or {}).get("date") or h.get("date"),
                    "percent": h.get("percent"), "duration": h.get("duration"), "count": h.get("count"),
                    "top": (h.get("causes") or [{}])[0].get("label") if h.get("causes") else None,
                    "has_report": "summary" in h})
    return {"sessions": out}


@app.get("/sessions/{sid}")
async def sessions_get(sid: str):
    for h in _load_history():
        if h.get("id") == sid and "summary" in h:
            return h["summary"]
    raise HTTPException(status_code=404, detail="No saved report for that session")


# ---------------------------------------------------------------- start/stop the camera from the page
def _camera_proc_state():
    p = CAMERA_PROC["p"]
    if p is None:
        return {"running": False, "error": None}
    code = p.poll()
    if code is None:
        return {"running": True, "error": None}
    err = None
    if code not in (0, -2, 130):
        try:
            tail = (CAMERA_DIR / "camera.log").read_text(errors="replace").strip().splitlines()[-3:]
            err = " / ".join(tail)[-300:] or f"exited with code {code}"
        except Exception:  # noqa: BLE001
            err = f"exited with code {code}"
    return {"running": False, "error": err}


@app.post("/camera/start")
async def camera_start():
    """Launch camera/drift.py on this laptop (its window opens here). Uses camera/.venv if present."""
    import subprocess
    import sys
    if CAMERA_PROC["p"] is not None and CAMERA_PROC["p"].poll() is None:
        return {"running": True}
    script = CAMERA_DIR / "drift.py"
    if not script.exists():
        raise HTTPException(status_code=404, detail=f"Can't find {script}. Run the server from the Ketchup repo.")
    venv_py = CAMERA_DIR / ".venv" / "bin" / "python"
    py = str(venv_py) if venv_py.exists() else sys.executable
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    env.pop("REWIND_API", None)  # camera reads its own camera/.env (localhost:8001)
    logf = open(CAMERA_DIR / "camera.log", "w")
    CAMERA_PROC.update(p=subprocess.Popen([py, str(script)], cwd=str(CAMERA_DIR), env=env,
                                          stdout=logf, stderr=subprocess.STDOUT), log=logf)
    log.info("camera started (pid %s) with %s", CAMERA_PROC["p"].pid, py)
    return {"running": True}


@app.post("/camera/stop")
async def camera_stop():
    import signal
    p = CAMERA_PROC["p"]
    if p is None or p.poll() is not None:
        return {"running": False}
    p.send_signal(signal.SIGINT)  # drift.py ends any open drift on the way out
    for _ in range(30):
        if p.poll() is not None:
            break
        await asyncio.sleep(0.2)
    if p.poll() is None:
        p.kill()
    CAMERA["t"] = 0.0
    return {"running": False}


# ---------------------------------------------------------------- connect the Vexo band from the page
def _band_state():
    p = BAND_PROC["p"]
    if p is None:
        return {"running": False, "status": "off", "detail": None, "mode": None}
    try:
        lines = [l.strip() for l in (REPO_DIR / "audio" / "band.log").read_text(errors="replace").splitlines() if l.strip()]
    except Exception:  # noqa: BLE001
        lines = []
    text = "\n".join(lines)
    code = p.poll()
    if code is not None:
        tail = " / ".join(lines[-2:])[-300:] if lines else f"exited with code {code}"
        return {"running": False, "status": "stopped" if code in (0, -2, 130) else "error",
                "detail": None if code in (0, -2, 130) else tail, "mode": BAND_PROC["mode"]}
    if "Connected to Vexo" in text or "Microphone on" in text:
        status = "listening"
        if "band link lost" in lines[-1] if lines else False:
            status = "reconnecting"
    elif "Could not see" in text or "not see" in text:
        status = "searching"
    else:
        status = "connecting"
    last = next((l for l in reversed(lines) if not l.startswith("[")), lines[-1] if lines else "")
    return {"running": True, "status": status, "detail": last[-200:], "mode": BAND_PROC["mode"]}


class BandStart(BaseModel):
    mode: str = "focus"   # "focus": audio leaves the laptop only while zoned out; "always": transcribe everything
    usb: bool = False


@app.post("/band/start")
async def band_start(body: BandStart):
    import subprocess
    import sys
    if BAND_PROC["p"] is not None and BAND_PROC["p"].poll() is None:
        return _band_state()
    script = REPO_DIR / "audio" / "transcribe.py"
    if not script.exists() or not (REPO_DIR / "vexo-open-kit").exists():
        raise HTTPException(status_code=404, detail="Can't find audio/transcribe.py and vexo-open-kit/ next to backend/. "
                                                    "Run the server from the Ketchup folder and clone vexo-open-kit there.")
    py = os.getenv("BAND_PYTHON") or sys.executable
    check = subprocess.run([py, "-c", "import bleak, certifi"], capture_output=True)
    if check.returncode != 0:
        raise HTTPException(status_code=400, detail="The Bluetooth package is missing. In the server's terminal run: "
                                                    "pip install bleak certifi   then click Connect again.")
    args = [py, str(script)]
    if body.mode == "always":
        args.append("--always")
    if body.usb:
        args.append("--usb")
    logf = open(REPO_DIR / "audio" / "band.log", "w")
    env = dict(os.environ, PYTHONUNBUFFERED="1", REWIND_API=f"http://localhost:{os.getenv('PORT', '8001')}")
    BAND_PROC.update(p=subprocess.Popen(args, cwd=str(REPO_DIR), env=env, stdout=logf, stderr=subprocess.STDOUT),
                     log=logf, mode=body.mode)
    log.info("band transcriber started (pid %s, %s mode)", BAND_PROC["p"].pid, body.mode)
    return _band_state()


@app.post("/band/stop")
async def band_stop():
    import signal
    p = BAND_PROC["p"]
    if p is None or p.poll() is not None:
        return _band_state()
    p.send_signal(signal.SIGINT)  # turns the band's mic off and flushes the last lines
    for _ in range(50):
        if p.poll() is not None:
            break
        await asyncio.sleep(0.2)
    if p.poll() is None:
        p.kill()
    return _band_state()
