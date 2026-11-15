# Ketchup

**Ketchup on what you missed.** Ketchup notices when you zone out in a lecture, keeps listening for you, and catches you up when you're back: a short spoken recap of exactly the part you missed, the exact transcript lines, and answers to your questions about it. When class ends, you get a report on how much you missed, why, and what to try next time.

Built at VexoHacks, Stanford, November 14, 2026, with the Vexo band, Deepgram, Gemini 4, OpenAI and ElevenLabs.

## What it does

- **Notices when you drift.** An outward-facing camera knows what "paying attention" looks like (the slides, the speaker). When you look away for 5 s, pick up your phone, or switch to an unrelated screen, it marks a drift and says why ("Online shopping page"). You can also mark drifts by hand.
- **Keeps listening.** The Vexo band's microphone holds the last 60 seconds of the lecture in memory on the laptop. Audio is sent to Deepgram for transcription only while you're zoned out (plus that last minute), so most of the lecture never leaves the laptop.
- **Catches you up.** Ask "What did I miss?" (button, `Space`, or out loud) and hear a recap of the gap, with the exact transcript lines underneath. Ask follow-up questions in plain words; answers come from the transcript.
- **Reports on the session.** End the session for a report: % of class missed, number and length of drifts, why you drifted, a summary of the whole lecture, what you missed, your patterns compared with earlier sessions, and three concrete tips. A ketchup bottle pops up to praise or (gently) roast you. Download everything as notes.
- **Remembers past sessions.** Every report is saved and can be reopened from Past sessions.

## How it works

```
camera/   webcam ── drift start/end + reason ──┐
                                               ├──▶ backend/ ── matches each drift window to the transcript
audio/    Vexo band mic ── Deepgram ── lines ──┘              ├─ recap + answers (OpenAI)
                                                              ├─ session report (Gemini 4)
                                                              └─ voice (ElevenLabs)
                                                                    │
                                              page at http://localhost:8001
                                     timeline · "What did I miss?" · Q&A · report · past sessions
```

1. **camera/** compares each frame with saved "lecture views" (ORB feature matching). Off target, MediaPipe looks for a phone, laptop or notes, and an AI vision check decides whether a screen is related to the lecture. It reports drifts with a cause and a short detail.
2. **audio/** streams the band's Opus audio to Deepgram (`nova-3`) and posts each final line with real timestamps.
3. **backend/** waits 6 s after a drift ends (so the last words arrive), takes the transcript lines in that window, and asks the AI for a recap under 60 words. It serves the page, the recap voice, Q&A, the end-of-session report and saved history.

## Sponsor tech

| Tech | What it does in Ketchup |
| --- | --- |
| **Vexo band** | Lecture microphone (Opus over Bluetooth or USB), with a 60 s private buffer; optional LED nudge after a long drift |
| **Deepgram** | Live transcription of the lecture with word timestamps |
| **Gemini 4** | Writes the end-of-session report: lecture summary, missed topics, drift patterns, personalized tips |
| **OpenAI** | Real-time recaps, answers to questions about what you missed, and the camera's screen-relevance check |
| **ElevenLabs** | Voices every recap and answer |

Each AI falls back to the other if a call fails, so the demo never stalls (set with `LLM_PROVIDER` and `REPORT_PROVIDER`).

## Setup

```bash
git clone https://github.com/erin-at23/Ketchup.git && cd Ketchup
git clone https://github.com/VexoAI-Org/vexo-open-kit.git      # Vexo band library (not committed)
cp .env.example .env                                           # then fill in your keys

python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt -r audio/requirements.txt

cd camera && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && cd ..
cp camera/.env.example camera/.env
```

Keys go in `.env` (never committed): `OPENAI_API_KEY`, `GEMINI_API_KEY`, `DEEPGRAM_API_KEY`, and `ELEVENLABS_API_KEY` (optional; the page falls back to the browser's voice). A camera started from the page uses these keys too.

## Run the demo

```bash
cd backend && uvicorn main:app --host 0.0.0.0 --port 8001
```

Open **http://localhost:8001**, then:

1. **Start session.**
2. **Start camera.** Point it at the slides and click **Save lecture view** (save 2–3 views: slides, speaker, board).
3. **Connect band.** Follow the steps in the dialog: double-tap the band until it blinks blue, then **Connect now**.
4. Zone out (look away, pick up your phone), come back, and press **What did I miss?**
5. **End session** for the report, then **Download notes**.

Keyboard: `D` mark zoned out / back · `Space` what did I miss · `V` ask out loud (Chrome).

The camera and band can also run on their own: `cd camera && python3 drift.py`, and `python3 audio/transcribe.py` (`--always` to transcribe everything, `--sim` without a band, `--usb` for the cable).

## API

All times are Unix epoch seconds. Interactive docs at `http://localhost:8001/docs`.

| Endpoint | Used by | Does |
| --- | --- | --- |
| `POST /session/start`, `POST /session/end` | page | Start a session; end it and build the report |
| `POST /drift/start` `{start, cause?, detail?}`, `POST /drift/end` `{drift_id, end}` | camera, page | Report a drift |
| `POST /transcript` `{start, end, text}`, `GET /transcript?since=` | audio, camera | Add / read transcript lines |
| `GET /focus` | audio | `{focused}`: false while a drift is open (audio is sent only then) |
| `GET /state` | page | Drifts, transcript, recaps, camera and band status |
| `GET /recap/latest`, `GET /recap/{id}/audio` | page | Latest recap and its voice |
| `POST /ask` `{question, drift_id?}`, `POST /speak` | page | Answer a question about the missed part; voice it |
| `GET /sessions`, `GET /sessions/{id}` | page | Past sessions and their reports |
| `POST /camera/start`, `/camera/stop`, `/camera/key`, `/camera/ping` | page, camera | Run the camera from the page, send it keys, heartbeat |
| `POST /band/start` `{mode, usb}`, `/band/stop` | page | Connect / disconnect the Vexo band |

Every POST that changes a session returns `409` until a session is started.

## Repo layout

```
backend/   FastAPI server, AI calls, report, history (sessions.json, not committed)
  static/index.html   the whole web page
audio/     band → Deepgram → transcript lines
camera/    drift detection (OpenCV, MediaPipe, AI vision check)
```

## Privacy

Camera frames are processed on the laptop and never stored; only drift times and a short reason leave the camera script. Lecture audio sits in a 60-second memory buffer and goes to Deepgram only while you're zoned out. Session reports are stored locally in `backend/sessions.json`. In a real deployment, record only where the instructor allows it; the band's light is solid blue whenever its microphone is on.
