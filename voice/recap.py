"""Fetches the latest recap from P3's backend, following the P4 plan:

    GET {REWIND_API}/recap/latest -> {"status": "none" | "pending" | "ready", "drift_id": 3, "text": "You missed..."}

- ready:   return the text
- pending: the recap is still being written (3-5 s after a drift ends): poll every 0.5 s for up to 6 s;
           if still pending, say "Give me a second" and try once more
- none:    "Looks like you haven't missed anything."
"""
import os
import time

import requests

NOTHING = "Looks like you haven't missed anything."
WAIT = "Give me a second."
SORRY = "Sorry, I can't reach the recap right now."


def _get(api):
    r = requests.get(f"{api.rstrip('/')}/recap/latest", timeout=3)
    r.raise_for_status()
    return r.json()


def fetch_recap(api=None, poll=0.5, wait=6.0, say=None, log=print):
    """Returns the sentence to speak. `say(text)` is called for "Give me a second" while still pending."""
    api = api or os.environ.get("REWIND_API", "")
    if not api:
        return SORRY
    t0 = time.time()
    try:
        for attempt in range(2):
            deadline = time.time() + wait
            while True:
                d = _get(api)
                status = d.get("status")
                if status == "ready" and d.get("text"):
                    log(f"[recap] ready (drift {d.get('drift_id')}) after {time.time() - t0:.1f}s")
                    return d["text"]
                if status != "pending":
                    log(f"[recap] status={status}")
                    return NOTHING
                if time.time() >= deadline:
                    break
                time.sleep(poll)
            if attempt == 0:
                log("[recap] still pending after 6 s")
                if say:
                    say(WAIT)
        return NOTHING
    except Exception as e:
        log(f"[recap] error: {e.__class__.__name__}: {str(e)[:80]}")
        return SORRY


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    print(fetch_recap(say=lambda t: print("SAY:", t)))
