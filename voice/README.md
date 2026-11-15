# Rewind P4: "What did I miss?" voice agent

Hold **Space**, ask "What did I miss?", and the recap plays out loud in about 1.5 s.

    local mic (push-to-talk) -> Deepgram STT -> OpenAI gpt-4.1-mini (or Gemini) + get_missed_recap -> ElevenLabs TTS -> speakers

## Setup (macOS)

    brew install portaudio
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    cp .env.example .env            # fill in the keys; never commit .env
    .venv/bin/python check_keys.py  # tests the keys, lists ElevenLabs voices

## Run

    .venv/bin/python agent.py          # the Pipecat agent
    .venv/bin/python backup_button.py  # backup: Space/click -> recap -> ElevenLabs, no Pipecat

Click the small **Rewind voice** window, then hold Space while you ask. The mic only reaches Deepgram while
Space is held, so the lecturer and the lecture mic never trigger it. The first run asks for microphone access
(System Settings -> Privacy & Security -> Microphone -> Terminal).

## How the recap is fetched (recap.py)

`GET $REWIND_API/recap/latest` -> `{"status": "none" | "pending" | "ready", "drift_id", "text"}`

- ready: the text is spoken as written (this skips a second LLM call, about 1 s faster)
- pending: poll every 0.5 s for up to 6 s; still pending -> "Give me a second", then poll up to 10 s more;
  still pending -> "I'm still writing your recap. Ask me again in a moment."
- none: "Looks like you haven't missed anything."
- a drift that is still open (`GET /focus` -> `{"focused": false}`) counts as pending. Right after you look
  back, the camera only sends `/drift/end` about 5 s later, and until then `/recap/latest` would say
  "none" or return an older recap.

Timing on the real backend: the recap is ready about 8-9 s after `/drift/end` (RECAP_DELAY 6 s + Gemini),
so about 13-14 s after you look back. Asking earlier is fine: the agent says "Give me a second" and waits.

## Measured (15:19, real mic, P3's backend over the tunnel)

| step | time after Space release |
| --- | --- |
| LLM decides to call the tool | ~1.0 s |
| recap from the backend | +0.2 s |
| first ElevenLabs audio | 1.4-1.7 s |

## Settings (.env)

`LLM_PROVIDER` openai (default) or gemini; `ELEVENLABS_VOICE_ID` is Bella (`hpp4J3VqNfWAUOO0d1Us`);
`ELEVENLABS_MODEL` eleven_flash_v2_5. If Deepgram/ElevenLabs fail with CERTIFICATE_VERIFY_FAILED, agent.py
already points Python at certifi's certificates; python.org's macOS Python has none by default.
