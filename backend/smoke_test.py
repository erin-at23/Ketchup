"""End-to-end check of the backend. Run while the server is up:

    python smoke_test.py                       # uses REWIND_API from .env, or localhost:8000
    python smoke_test.py http://10.0.0.5:8000  # test another laptop's server

It starts a NEW session (wipes current data), posts a fake lecture,
simulates a drift, and waits for the recap.
"""
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()
API = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("REWIND_API", "http://localhost:8000")).rstrip("/")


def ok(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        sys.exit(1)


print(f"Testing {API}")
r = requests.post(f"{API}/session/start", timeout=5).json()
ok("start_time" in r, f"session started: {r}")

now = time.time()
lecture = [
    (now - 20, now - 15, "Today we start eigenvalues."),
    (now - 14, now - 9, "If A v equals lambda v, v is an eigenvector."),
    (now - 8, now - 3, "For the matrix two one one two, the eigenvalues are three and one."),
    (now - 2, now + 1, "Eigenvectors make repeated multiplication easy."),
]
for s, e, t in lecture:
    requests.post(f"{API}/transcript", json={"start": s, "end": e, "text": t}, timeout=5).raise_for_status()
ok(True, f"posted {len(lecture)} transcript lines")

d = requests.post(f"{API}/drift/start", json={"start": now - 12}, timeout=5).json()
drift_id = d["drift_id"]
ok(isinstance(drift_id, int), f"drift started: {d}")

st = requests.get(f"{API}/state", timeout=5).json()
ok(st["drifts"][-1]["status"] == "open", "drift shows as open in /state")

requests.post(f"{API}/drift/end", json={"drift_id": drift_id, "end": now - 4}, timeout=5).raise_for_status()
first = requests.get(f"{API}/recap/latest", timeout=5).json()
ok(first["status"] == "pending", f"recap pending right after drift end: {first['status']}")

t0 = time.time()
while time.time() - t0 < 20:
    rec = requests.get(f"{API}/recap/latest", timeout=5).json()
    if rec["status"] == "ready":
        break
    time.sleep(0.5)
ok(rec["status"] == "ready", f"recap ready after {time.time() - t0:.1f}s")
print("     recap:", rec["text"])

st = requests.get(f"{API}/state", timeout=5).json()
ok(st["drifts"][-1]["status"] == "ready" and len(st["recaps"]) == 1, "/state shows drift ready + 1 recap")
print("\nAll checks passed.")
