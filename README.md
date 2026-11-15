# Ketchup

**Zone out in class. Ketchup catches you up.**

Everyone drifts in a long lecture, and you usually notice only once you're lost. Ketchup is a wearable study companion: it notices the moment you zone out, keeps listening for you, and when you're back it tells you exactly what you missed, in about 20 seconds.

Built in six hours at VexoHacks, Stanford, November 2026.

---

## What it does

**It notices.** A small outward-facing camera learns what paying attention looks like for you: the slides, the speaker. Look away, pick up your phone, or open a shopping tab, and Ketchup marks the moment and why.

**It listens.** The Vexo band's microphone keeps the last minute of the lecture in memory on your laptop. Audio is transcribed only while you're zoned out, so most of the lecture never leaves your laptop.

**It catches you up.** Ask *"What did I miss?"* and hear a short recap of just that gap, with the exact words the lecturer said underneath. Ask follow-up questions in plain language.

**It coaches you.** When class ends you get a report: how much you missed, why you drifted, what the lecture covered, and three things to try next time. A ketchup bottle pops up to cheer you on, or roast you a little. Download it all as notes, and look back at past sessions.

## How it works

```
 camera ─── "drifted at 3:12: phone in view" ───┐
                                                ├──▶  Ketchup server  ──▶  web app
 Vexo band mic ─── live transcript ─────────────┘     recaps · answers · report · voice
```

When a drift ends, the server lines it up with the transcript, asks OpenAI to recap that window, and ElevenLabs reads it aloud. Everything shows up live in the web app: a timeline of when you drifted, the transcript with missed lines highlighted, and the catch-up panel.

## Built with

**Vexo band** (microphone) · **Deepgram** (live transcription) · **OpenAI** (recaps, answers, reports, screen check) · **ElevenLabs** (voice) · **Pipecat** (push-to-talk voice agent) · MediaPipe and OpenCV (camera) · FastAPI

## Run it

```bash
git clone https://github.com/erin-at23/Ketchup.git && cd Ketchup
git clone https://github.com/VexoAI-Org/vexo-open-kit.git
cp .env.example .env            # add your OpenAI, Deepgram and ElevenLabs keys
cp camera/.env.example camera/.env

python3 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt -r audio/requirements.txt
(cd camera && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)

cd backend && uvicorn main:app --port 8001
```

Open **http://localhost:8001**, then **Start session** → **Start camera** (point it at the slides, click **Save lecture view**) → **Connect band** (double-tap the band, follow the steps). Zone out, come back, press **What did I miss?**

Shortcuts: `D` mark zoned out · `Space` what did I miss · `V` ask out loud.

## What's in here

| Folder | |
| --- | --- |
| `backend/` | the server and the web app (`static/index.html`) |
| `camera/` | drift detection from the camera |
| `audio/` | Vexo band → Deepgram transcript |
| `voice/` | push-to-talk voice agent (Pipecat) |

API docs are served at `http://localhost:8001/docs`.

## Privacy

Camera frames stay on your laptop; only "when" and "why" leave the camera. Lecture audio is sent for transcription only while you're zoned out. Reports are saved locally. The band's light is solid blue whenever its microphone is on.
