# Rewind P1: camera drift detection (instructions for Claude Code)

Read this whole file before doing anything. Then read `README.md` and `drift.py` in this folder.

## Context

- **Event:** VexoHacks, an AI wearables hackathon at Y Combinator, San Francisco, today (Saturday, November 14, 2026).
- **Product:** Rewind notices when a student’s attention drifts in a lecture and gives a spoken recap of exactly what they missed. A camera detects drift, the Vexo band records the lecture, and a voice agent answers “What did I miss?”
- **Team:** 5 people. I am **P1** and own drift detection. My output goes to **P3** (backend), whose data shows up on **P5**’s timeline.
- **Deadlines:** the whole team loop must work by **4:00 PM**, feature freeze at **4:30 PM**, submission by **5:45 PM**. Treat these as hard limits; a working, stable script beats a clever unfinished one.

## What’s different from the team’s original plan

The original plan used the laptop webcam pointed at the student’s face (eyes closed, head down). That was dropped. A laptop camera can’t tell whether the student is looking at the slides or at their phone, and looking up at the speaker reads as “looking away.”

The camera is now **head-mounted and faces outward**: it sees what the wearer sees. Hardware options, in order of reliability:

1. A USB webcam clipped to glasses, with a USB extension cable to the laptop.
2. An iPhone on a cap or headband via Continuity Camera (Mac only), which appears to OpenCV as a normal camera.
3. An Android phone used as a webcam (USB webcam mode or an app like DroidCam).

Ask me which one I’m using if it matters for a fix.

## How drift.py works

**Step 1, every frame, local: is the wearer looking at the slides or speaker?**
ORB feature matching against 1–3 reference frames set with the `c` key, scored by RANSAC homography inliers. At or above `inlier_threshold` means on target. A steady, strong match refreshes an automatic reference every 10 s so slide changes don’t break it.

**Step 2, only when off target: what are they looking at?**
- **2a, local:** MediaPipe Object Detector (EfficientDet-Lite0) finds `cell phone`, `laptop`/`keyboard` and `book`. A `tv` is ignored because it’s usually the projector. Notes count as focused.
- **2b, Gemini, rarely:** if a phone or laptop is in view, one frame goes to Gemini with the lecture topic and the recent transcript. Gemini answers related, unrelated or unclear. The verdict is cached for 6 s and refreshed early if the screen’s colors change a lot (a tab switch). Requests run on a background thread.

**State per frame:**

| Situation | Status | Drift starts after |
| --- | --- | --- |
| On target | focused | — |
| Notes in view, or a screen Gemini rates related | focused | — |
| Screen rated unrelated, confidence ≥ 0.7 | off (`phone` or `unrelated_screen`) | 2 s from when the screen appeared |
| No Gemini configured: phone in view | off (`phone`) | 3 s |
| No Gemini configured: laptop in view | focused | — |
| Off target, no screen | off (`away`) | 5 s |
| Screen with verdict pending or unclear | hold (neither drifts nor returns) | — |

Focused for 2 s ends a drift. Drifts less than 3 s apart are merged, so the end is only sent after the 3 s merge window. Start times are backdated to when the drift actually began. The `d` key forces a manual drift on and off. All numbers are in `config.json`, reloadable with `r`.

## The contract with P3 (do not change it)

```
POST {REWIND_API}/drift/start  {"start": <epoch seconds>, "cause": "...", "detail": "..."}
  -> {"drift_id": <id>}
POST {REWIND_API}/drift/end    {"drift_id": <id>, "end": <epoch seconds>}
```

- Times are Unix epoch seconds (`time.time()`). Never convert to session time; the backend does that.
- The backend assigns `drift_id`. Never invent one.
- A **409** means no session has started yet: retry every 2 s and keep the camera running.
- Network calls stay on the background thread so the video never freezes.
- If the script stops mid-drift, it must send `/drift/end` on exit (the `try/finally` in `main`).
- `cause` (`away`, `phone`, `unrelated_screen`, `manual`) and `detail` are extra optional fields. P3 has been asked to accept or ignore them. If P3’s backend rejects them, drop them rather than change anything else.
- Optional: recent transcript for the Gemini prompt comes from `GET {REWIND_API}{REWIND_TRANSCRIPT_PATH}?since=<epoch>`, accepting either a list of `{"t", "text"}` or `{"transcript": [...]}`. If P3 doesn’t provide it, the prompt just uses `REWIND_TOPIC`.

## Configuration

Everything comes from `.env` in this folder (template: `.env.example`): `REWIND_API`, `REWIND_TRANSCRIPT_PATH`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `REWIND_TOPIC`, `CAMERA_INDEX`.

- **Never print, log or commit `GEMINI_API_KEY`.** Make sure `.env` is in `.gitignore`.
- The Gemini call uses the REST endpoint `v1beta/models/{GEMINI_MODEL}:generateContent` with the key in the `x-goog-api-key` header and JSON output. If it fails, the overlay’s verdict line shows the error; a 404 usually means a wrong model ID. Ask me for the right ID rather than guessing one.
- The first run downloads `efficientdet_lite0.tflite` next to the script. If the download fails, the detector disables itself and the script keeps running.

## What has been verified, and what hasn’t

**Verified on synthetic video, before the event:**
- an 8 s look-away becomes one drift with the right backdated start and end
- a 1.5 s glance is ignored
- two drifts 2 s apart merge into one
- the 409 retry and the start and end calls work against `fake_backend.py`

**Not verified yet, so check these first:**
- the real camera opening (and on a Mac, the terminal getting camera permission)
- the MediaPipe model download and phone or laptop detection
- the Gemini call with our key and model
- real-world inlier numbers, so the threshold is a guess

## Tasks, in order

Work through these in order and tell me when each checkpoint passes. Prefer small, targeted changes over rewrites.

1. **Get it running.** Install `requirements.txt`, run `python3 drift.py --list-cameras`, set `CAMERA_INDEX`, and get the live window up with the overlay. Fix whatever breaks.
2. **Test alone.** Start `python3 fake_backend.py` (without `--started` to check the 409 retry, then visit `/session/start`). Run against it with `REWIND_API=http://localhost:8787`. Checkpoint: a look-away produces exactly one start and one end in the fake backend’s output.
3. **Tune step 1.** Help me find the threshold: I look at the slides, then away, and we note the match numbers. Set `inlier_threshold` in `config.json` between the two. Make sure slide changes don’t knock me off target.
4. **Detection and Gemini.** Confirm the phone and laptop show boxes in the overlay. With Gemini configured, confirm code related to the topic reads as related and a shopping site as unrelated. Check that a verdict arrives within about 2–3 s.
5. **Make it easy to replay.** Make sure `--video recording.mp4 --calibrate-at 1` works for footage recorded from the real mount, so we can tune without re-acting.
6. **Integrate (from about 2:30).** Point `REWIND_API` at P3’s real backend. Checkpoint: drifts appear on P5’s timeline within about 2 s, with their causes.
7. **Measure.** Write a small script that reads `drift_log.jsonl` and prints how many drifts there were and how long each lasted. I’ll stage 10 drifts plus some normal behavior; we record catches and false alarms. That number goes in the pitch.
8. **Freeze at 4:30.** After that, only fix bugs that would break the demo.

**Stretch, only if everything above is stable after 4:00:** add an optional second camera (the laptop’s own webcam, facing the student) that only detects eyes closed for more than 2 s, using MediaPipe Face Landmarker and eye aspect ratio. It should feed the same state machine as cause `eyes_closed`. Skip this if time is tight.

## Rules for this work

- **Privacy:** never save camera frames to disk or send them anywhere except the single Gemini request in step 2b. Only drift times and short labels are kept. This is part of our pitch, so don’t break it, even for debugging. (Showing frames in the local window is fine.)
- Keep the manual `d` key working at all times; it’s the demo backup.
- Keep the video loop smooth: no blocking network calls or long computations on the main thread.
- Keep all tunable numbers in `config.json`, not hard-coded.
- Don’t add heavy dependencies or switch frameworks. Ask first if something new seems necessary.
- Before telling me something works, actually run it (the fake backend and `--video` replay are there for this) and show me the output.

## Demo context (so fixes serve the demo)

The demo is about 2 minutes, live on stage:
1. A teammate gives a short C++ lecture.
2. I wear the camera. I look at my laptop showing related C++ code and stay focused.
3. I switch to a shopping site, which should turn into a drift labeled “shopping site” on the timeline.
4. I ask “What did I miss?” and the recap plays.

The two moments that must work reliably are “code stays focused” and “shopping site becomes a drift within about 3 s.” If they’re shaky on stage, I press `d`.

## Troubleshooting notes

- **Camera won’t open on a Mac:** grant the terminal app camera access in System Settings → Privacy & Security → Camera, then restart the terminal.
- **Wrong camera opens:** use `--list-cameras` and set `CAMERA_INDEX`. Continuity Camera can take a different index after reconnecting.
- **Low frame rate:** step 1 already runs on every 2nd frame; it’s fine to drop to every 3rd. Detection runs 4 times a second. Don’t reduce the frame size below 640 px wide, because matching gets worse.
- **Gemini slow:** keep the image at about 768 px and the 4 s timeout; a timeout counts as unclear, which holds the current state.
