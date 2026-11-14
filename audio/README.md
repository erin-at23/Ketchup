# audio/ — lecture transcript

`transcribe.py` sends the lecture audio from the Vexo band (Vexo-HACK-08) to Deepgram and POSTs
each final result to `$REWIND_API/transcript` as `{"start", "end", "text"}` (epoch seconds).
Every line is also appended to `audio/transcript.jsonl`.

Run everything from the repo root.

## Setup

The script looks for the Vexo Open kit at `vexo-open-kit/` in the repo root. That folder is not
committed (it's in `.gitignore`), so clone it there yourself:

    git clone https://github.com/VexoAI-Org/vexo-open-kit.git
    python3 -m pip install -r audio/requirements.txt

Keys live in the repo-root `.env` (copy `.env.example`): `DEEPGRAM_API_KEY` and `REWIND_API`.

## Focus mode (the default)

The band's mic is always on. The last 60 s of sound stay in memory on this laptop and are sent
nowhere. The script polls `GET $REWIND_API/focus` every 0.5 s and expects `{"focused": true}` or
`{"focused": false}`. The backend provides it.

- When `focused` turns false, the last minute goes to Deepgram first, then live sound. Lines carry
  their real (past) timestamps.
- When `focused` turns true again, transcription stops and sound goes back into the 60 s buffer.
- With `--nudge`, after 60 s out of focus the band's LED blinks orange for 3 s, once per lapse. The
  band has **no vibration motor** (kit docs: "Not on this band: ... vibration"). Its LED is locked
  while the mic is on, so the mic pauses for those 3 s, leaving a 3 s gap in the transcript.
- If `/focus` 404s or is unreachable, the last known state is kept. At startup that state is focused.

## Commands

| What | Command |
|---|---|
| Live, focus-driven, band over Bluetooth (double-tap the band first) | `python3 audio/transcribe.py` |
| ...with the LED nudge | `python3 audio/transcribe.py --nudge` |
| Transcribe everything, ignore focus | `python3 audio/transcribe.py --always` |
| Tune it | `--rewind 60`, `--nudge-after 60` (seconds) |
| Live, band on USB cable | `python3 audio/transcribe.py --usb` |
| Record the 3-minute sample while transcribing | `python3 audio/transcribe.py --record audio/sample-lecture.ogg` |
| Record only, nothing sent anywhere | `python3 audio/transcribe.py --record audio/sample-lecture.ogg --no-transcribe` |
| Replay a recording through the pipeline (testing / stage fallback) | `python3 audio/transcribe.py --replay audio/sample-lecture.ogg --always` |
| Quick test, no band (kit's 16 s sample voice) | `python3 audio/transcribe.py --sim` |
| Backup: laptop mic (`pip install sounddevice`) | `python3 audio/transcribe.py --laptop-mic` |

Replayed lines get *current* timestamps, so they look live to the backend.

## Behaviour

- Only `is_final` results with text are sent, and interim results are skipped. Deepgram runs with
  `nova-3`, `smart_format`, `interim_results`, `endpointing=100` (change it with `--endpointing`), and
  `mip_opt_out=true`.
- The band's Opus packets go to Deepgram as they are, with no decoding.
- Timestamps: each sent chunk's capture time is recorded, so Deepgram's word times map to epoch
  seconds correctly even after lost packets or a reconnect.
- 409, network errors and 5xx keep the line queued and retry it every second, in order.
- If Deepgram drops, it reconnects automatically. Audio from the gap (up to 30 s) is sent once it's back.
- If the band link drops, the kit reconnects for 2 minutes, then this script keeps retrying
  (double-tap the band).
- The band's light is solid blue while the mic is on, and LED commands are refused meanwhile.
  Tell people nearby that audio goes to Deepgram.
