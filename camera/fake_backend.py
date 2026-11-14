"""A stand-in for P3's backend so you can test drift.py alone.
Run: python3 fake_backend.py   then set REWIND_API=http://localhost:8787
Returns 409 until you open http://localhost:8787/session/start (or pass --started)."""
import json, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
STATE = {"session": "--started" in sys.argv, "next": 1}
SAMPLE = [{"t": time.time(), "text": "So a vector in C++ grows automatically when you push_back past its capacity."}]
class H(BaseHTTPRequestHandler):
    def _send(self, code, obj):
        body = json.dumps(obj).encode(); self.send_response(code)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if self.path.startswith("/session/start"): STATE["session"] = True; print("session started"); return self._send(200, {"ok": True})
        if self.path.startswith("/transcript"): return self._send(200, {"transcript": SAMPLE})
        self._send(404, {"error": "not found"})
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); body = json.loads(self.rfile.read(n) or b"{}")
        if not STATE["session"]: print("409 (no session yet)", self.path); return self._send(409, {"error": "no session"})
        if self.path == "/drift/start":
            i = STATE["next"]; STATE["next"] += 1; print("START", i, body); return self._send(200, {"drift_id": i})
        if self.path == "/drift/end": print("END  ", body); return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})
    def log_message(self, *a): pass
print("fake backend on http://localhost:8787"); HTTPServer(("", 8787), H).serve_forever()
