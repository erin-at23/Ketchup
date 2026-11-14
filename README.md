# Ketchup

**Ketchup on what you missed.** A Vexo band and a webcam notice when you zone out in a lecture. When you come back, ask "What did I miss?" and hear a short recap of exactly that part.

Built at VexoHacks (Nov 14, 2026) with Deepgram, Gemini 4, ElevenLabs and the Vexo band.

## How it works

```
camera/   webcam ── drift start/end ──┐
                                      ├─▶  backend/  ── matches drift windows to transcript,
audio/    Vexo band mic ─ Deepgram ───┘               Gemini 4 writes a recap, ElevenLabs voices it
                                                         │
                                       timeline page (http://localhost:8001)
                                       "What did I miss?" ─▶ spoken recap
```

1. **audio/** keeps the last 60 s of lecture audio in memory on the laptop. Audio goes to Deepgram only while the student is zoned out (plus that last minute).
2. **camera/** detects drift (eyes closed, head down, unrelated screen) and reports it with a reason.
3. **backend/** waits 6 s after a drift ends, takes the transcript lines from that window, and asks Gemini 4 for a recap under 60 words.
4. The **timeline page** shows each drift as a red bar labelled with why, the live transcript with missed lines highlighted, and plays the recap.

## Setup (one laptop runs everything)

```bash
git clone https://github.com/erin-at23/Ketchup.git && cd Ketchup
git clone https://github.com/VexoAI-Org/vexo-open-kit.git      # band library (not committed)
cp .env.example .env                                           # then fill in your keys

python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt -r audio/requirements.txt
```

Keys: Gemini (aistudio.google.com), Deepgram (console.deepgram.com), ElevenLabs (optional; elevenlabs.io).

## Run the demo (3 terminals)

```bash
# 1. backend + timeline page
cd backend && uvicorn main:app --host 0.0.0.0 --port 8001
#    open http://localhost:8001 and click "Start session"

# 2. lecture audio from the band (double-tap the band first)
python3 audio/transcribe.py            # focus mode: sends audio only while zoned out
python3 audio/transcribe.py --always   # transcribe everything (testing)
python3 audio/transcribe.py --sim --always   # no band: kit's sample voice

# 3. camera drift detection (see camera/README.md)
```

No camera? Press `D` on the page to mark "zoned out" / "back". `Space` asks "What did I miss?", `V` asks out loud (Chrome).

Teammate on another laptop and the venue Wi-Fi blocks it? `cloudflared tunnel --url http://localhost:8001` and share the https URL as `REWIND_API`.

## API (backend, all times Unix epoch seconds)

| Endpoint | Used by | Body / response |
| --- | --- | --- |
| `POST /session/start` | page | → `{session_id, start_time}` (clears state) |
| `POST /drift/start` | camera, page | `{start, cause?, detail?}` → `{drift_id}` |
| `POST /drift/end` | camera, page | `{drift_id, end}` |
| `POST /transcript` | audio | `{start, end, text}` |
| `GET /transcript?since=` | camera | → `{transcript}` recent lines for the relevance check |
| `GET /focus` | audio | → `{focused}` (false while a drift is open) |
| `GET /state` | page | drifts, transcript, recaps |
| `GET /recap/latest` | page | → `{status: none\|pending\|ready, drift_id, text}` |
| `GET /recap/{id}/audio` | page | ElevenLabs MP3 |

Every POST returns `409` until a session is started. Interactive docs: `http://localhost:8001/docs`.

## Privacy

Camera frames are processed on the laptop and never stored; only drift timestamps leave the camera script. Lecture audio stays in a 60-second memory buffer and is sent to Deepgram only while the student is zoned out. Nothing is saved between sessions.
