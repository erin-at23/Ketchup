"""Fetches the latest recap from P3's backend, following the P4 plan:

    GET {REWIND_API}/recap/latest -> {"status": "none" | "pending" | "ready", "drift_id": 3, "text": "You missed..."}

- ready:   return the text
- pending: the recap is still being written: poll every 0.5 s; after the first wait (6 s) say
           "Give me a second" and keep polling for a second, longer wait (10 s)
- none:    "Looks like you haven't missed anything."

Two timing details from the real backend and camera:
- While a drift is still open (the student just looked back, and the camera sends /drift/end
  about 5 s later), /recap/latest ignores it and would say "none" or return an OLDER recap.
  So GET /focus is checked too: {"focused": false} means a drift is open, which counts as pending.
- The backend waits RECAP_DELAY (6 s) after the drift ends, then asks Gemini, so a recap is ready
  about 13-14 s after the student looks back. The two waits (6 s + 10 s) cover that.
"""
import os
import time

import requests

NOTHING = "Looks like you haven't missed anything."
WAIT = "Give me a second."
STILL = "I'm still writing your recap. Ask me again in a moment."
SORRY = "Sorry, I can't reach the recap right now."


def _get(api, path):
    r = requests.get(f"{api.rstrip('/')}{path}", timeout=3)
    r.raise_for_status()
    return r.json()


def _drift_open(api):
    """True while a drift is still open. Unknown (endpoint missing or failing) counts as not open."""
    try:
        return _get(api, "/focus").get("focused") is False
    except Exception:
        return False


def fetch_recap(api=None, poll=0.5, wait=6.0, say=None, log=print, second_wait=10.0):
    """Returns the sentence to speak. `say(text)` is called once with "Give me a second" while waiting."""
    api = api or os.environ.get("REWIND_API", "")
    if not api:
        return SORRY
    t0 = time.time()
    said_wait = False
    try:
        for attempt, limit in enumerate((wait, second_wait)):
            deadline = time.time() + limit
            while True:
                if _drift_open(api):
                    status, d = "open", {}
                else:
                    d = _get(api, "/recap/latest")
                    status = d.get("status")
                if status == "ready" and d.get("text"):
                    log(f"[recap] ready (drift {d.get('drift_id')}) after {time.time() - t0:.1f}s")
                    return d["text"]
                if status not in ("pending", "open"):
                    log(f"[recap] status={status} after {time.time() - t0:.1f}s")
                    return NOTHING
                if time.time() >= deadline:
                    break
                time.sleep(poll)
            if attempt == 0 and not said_wait:
                log(f"[recap] still {status} after {time.time() - t0:.1f}s")
                said_wait = True
                if say:
                    say(WAIT)
        log(f"[recap] still not ready after {time.time() - t0:.1f}s")
        return STILL
    except Exception as e:
        log(f"[recap] error: {e.__class__.__name__}: {str(e)[:80]}")
        return SORRY


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    print(fetch_recap(say=lambda t: print("SAY:", t)))
