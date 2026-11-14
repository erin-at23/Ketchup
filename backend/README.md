# Ketchup backend

## Run it (2 minutes)

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example ../.env        # then paste GEMINI_API_KEY into ../.env (main.py finds it automatically)
```

Until 2:30 (stub mode, serves fake_state.json so others can build):

```bash
FAKE_MODE=1 uvicorn main:app --host 0.0.0.0 --port 8001
```

From 2:30 (real mode):

```bash
uvicorn main:app --host 0.0.0.0 --port 8001
```

Find your IP and post it in the team chat as `REWIND_API=http://<ip>:8001`:

```bash
ipconfig getifaddr en0      # macOS
hostname -I                 # Linux
```

## Check your Gemini key first

```bash
python gemini_check.py     # prints a test recap + how long it took
```

## Test it

```bash
python smoke_test.py                          # full loop against localhost (starts a new session!)
curl $REWIND_API/session                      # teammates run this to check they can reach you
```

Interactive docs: open `http://<ip>:8001/docs` in a browser; every endpoint can be tried there.

## Tuning knobs (.env)

| Var | Default | Why |
| --- | --- | --- |
| `GEMINI_MODEL` | `gemini-4-hydrogen` | Fast Gemini 4 (free tier). `gemini-4-argon` = smarter but slower |
| `GEMINI_THINKING` | `low` | Raise to `medium` if recaps feel shallow; never `minimal` (Hydrogen rejects it) |
| `RECAP_DELAY` | `3` | Seconds to wait after a drift ends for late transcript lines |
| `PAD_BEFORE` / `PAD_AFTER` | `5` / `2` | Extra seconds of transcript included around each drift |
| `GEMINI_TIMEOUT` | `10` | After this, the fallback recap (raw transcript) is used |

## If something breaks

- Teammates get connection refused: you're not on `--host 0.0.0.0`, the firewall is blocking port 8000, or the Wi-Fi isolates clients (move everyone to a phone hotspot).
- Recap says "You missed this: ..." (raw text): Gemini failed. The server log shows why (bad key, wrong model id, timeout).
- Recap says "Nothing much was said": no transcript lines overlapped the drift. Check that P2 sends epoch times, and that everyone's clocks are synced.
