#!/usr/bin/env python3
"""
Rewind P1: drift detection from a head-mounted, outward-facing camera.

Step 1 (local, every frame): is the wearer looking at the slides or speaker?
        ORB feature matching against reference frames you set with the `c` key.
Step 2 (only when off target): what are they looking at?
        2a. MediaPipe object detection finds phones, laptops and notes (local).
        2b. If a screen is in view, Gemini judges whether its contents relate
            to the lecture (one frame, cached, on a background thread).

Drift events go to the backend:
    POST {REWIND_API}/drift/start  {"start": epoch, "cause": ..., "detail": ...}
    POST {REWIND_API}/drift/end    {"drift_id": id, "end": epoch}

Keys (in the video window):
    c  add a target reference (look at the slides or speaker first)
    x  clear all references
    d  manual drift on/off (demo backup)
    m  mark the start of a staged drift (for measure.py; local log only)
    [  ]  lower / raise the match threshold
    r  reload config.json
    q  quit (an open drift is ended cleanly)
"""

import argparse
import base64
import json
import os
import queue
import sys
import threading
import time

import cv2
import numpy as np
import requests

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    "inlier_threshold": 25,        # RANSAC inliers needed to count as "on target"
    "away_seconds": 5.0,           # off target, no screen -> drift after this long
    "screen_drift_seconds": 2.0,   # unrelated screen -> drift after this long (from when it appeared)
    "phone_only_seconds": 3.0,     # fallback when Gemini is unavailable: phone in view this long -> drift
    "return_seconds": 2.0,         # focused this long -> drift ends
    "merge_gap_seconds": 3.0,      # drifts closer than this are merged
    "unrelated_confidence": 0.7,   # Gemini confidence needed to call a screen a distraction
    "verdict_ttl_seconds": 6.0,    # reuse a Gemini verdict this long
    "histogram_change": 0.7,       # below this correlation the screen "changed" (e.g. a tab switch)
    "detector_score": 0.35,        # MediaPipe detection confidence
    "laptop_min_area": 0.04,       # a laptop must fill this fraction of the frame to count
    "ref_refresh_seconds": 10.0,   # steady strong match this long -> refresh the automatic reference
    "gemini_timeout": 4.0,         # timeout for the AI check (Gemini or OpenAI)
    "transcript_seconds": 60,
    "openai_image_detail": "low",  # "low" is fastest; "high" reads small text better
    "openai_extra": {},            # extra request fields, e.g. {"temperature": 0}
    "object_hold_seconds": 1.5,    # keep a detected screen/page "in view" this long (detections flicker)
    "max_targets": 6,              # targets you can add with c (e.g. several slides); the oldest is dropped
    "away_backdate_max_seconds": 3.0,  # a look-away may be backdated this far into a preceding "checking" period
}

MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/object_detector/"
             "efficientdet_lite0/int8/1/efficientdet_lite0.tflite")


# ---------------------------------------------------------------- config

def load_env(path):
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_config():
    cfg = dict(DEFAULTS)
    path = os.path.join(HERE, "config.json")
    if os.path.exists(path):
        try:
            cfg.update(json.load(open(path, encoding="utf-8")))
        except Exception as e:
            print(f"[config] could not read config.json: {e}")
    return cfg


class Clock:
    """Wall clock for a live camera; simulated clock when replaying a video file."""

    def __init__(self, fps=None):
        self.fps = fps
        self.t0 = time.time()
        self.frame = 0

    def now(self):
        return self.t0 + self.frame / self.fps if self.fps else time.time()


# ---------------------------------------------------------------- backend

class Backend(threading.Thread):
    """Sends drift events from a background thread so the video never freezes."""

    def __init__(self, api, dry):
        super().__init__(daemon=True)
        self.api = api.rstrip("/") if api else None
        self.dry = dry or not self.api
        self.q = queue.Queue()
        self.status = "printing events (no REWIND_API)" if self.dry else "ready"
        self.shutting_down = False
        # REWIND_LOG lets test runs write elsewhere, so they don't mix with real measurements
        self.log = open(os.environ.get("REWIND_LOG") or os.path.join(HERE, "drift_log.jsonl"), "a", encoding="utf-8")
        # drift ids restart per backend session, so mark each run for measure.py
        self._record({"event": "run", "t": round(time.time(), 3), "dry": self.dry})

    def send_start(self, drift):
        self.q.put(("start", drift))

    def send_end(self, drift):
        self.q.put(("end", drift))

    def send_mark(self, t):
        """Staged-drift marker for measure.py (local log only, never sent to the backend)."""
        self.q.put(("mark", t))

    def close(self, timeout=6):
        self.shutting_down = True
        self.q.put(("stop", None))
        self.join(timeout)
        self.log.close()

    def run(self):
        while True:
            kind, drift = self.q.get()
            if kind == "stop":
                return
            try:
                if kind == "mark":
                    self._record({"event": "mark", "t": round(drift, 3)})
                elif kind == "start":
                    self._start(drift)
                else:
                    self._end(drift)
            except Exception as e:
                self.status = f"error: {e}"

    def _record(self, event):
        self.log.write(json.dumps(event) + "\n")
        self.log.flush()

    def _start(self, drift):
        body = {"start": round(drift["start"], 3), "cause": drift["cause"]}
        if drift.get("detail"):
            body["detail"] = drift["detail"]
        if self.dry:
            drift["id"] = drift["local_id"]
            print(json.dumps({"POST": "/drift/start", **body}), flush=True)
            self._record({"event": "start", "drift_id": drift["id"], **body, "detected_at": drift["opened_at"]})
            return
        while True:
            try:
                r = requests.post(f"{self.api}/drift/start", json=body, timeout=2)
                if r.status_code == 409:
                    self.status = "no session yet (409), retrying"
                else:
                    r.raise_for_status()
                    drift["id"] = r.json()["drift_id"]
                    self.status = f"drift {drift['id']} started"
                    self._record({"event": "start", "drift_id": drift["id"], **body, "detected_at": drift["opened_at"]})
                    return
            except Exception as e:
                self.status = f"start failed ({e.__class__.__name__}), retrying"
            if self.shutting_down or drift.get("end") is not None and time.time() - drift["end"] > 30:
                self.status = "gave up on a drift start"
                return
            time.sleep(2)

    def _end(self, drift):
        if drift.get("id") is None:
            return  # the start never got through, so there is nothing to end
        body = {"drift_id": drift["id"], "end": round(drift["end"], 3)}
        if self.dry:
            print(json.dumps({"POST": "/drift/end", **body}), flush=True)
            self._record({"event": "end", **body})
            return
        for _ in range(3):
            try:
                r = requests.post(f"{self.api}/drift/end", json=body, timeout=2)
                r.raise_for_status()
                self.status = f"drift {drift['id']} ended"
                self._record({"event": "end", **body})
                return
            except Exception as e:
                self.status = f"end failed ({e.__class__.__name__}), retrying"
                time.sleep(1)


# ---------------------------------------------------------------- step 1: on target?

class Heartbeat(threading.Thread):
    """Tells the backend every 2 s that the camera is running and what it sees, so the
    Ketchup page can show a live camera status. Never blocks the video loop."""

    def __init__(self, api):
        super().__init__(daemon=True)
        self.url = api.rstrip("/") + "/camera/ping" if api else None
        self.info = {}

    def update(self, **info):
        self.info = info

    def run(self):
        while self.url:
            try:
                requests.post(self.url, json=self.info, timeout=1.5)
            except Exception:
                pass
            time.sleep(2)


class TargetMatcher:
    def __init__(self, max_targets=6):
        self.orb = cv2.ORB_create(nfeatures=1000)
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING)
        self.refs = []  # {"kp", "des", "auto", "thumb"}
        self.max_targets = max_targets
        self.last_best = None   # the reference that matched best last time is tried first

    def add(self, gray, auto=False):
        kp, des = self.orb.detectAndCompute(gray, None)
        if des is None or len(kp) < 50:
            return False
        if auto:
            self.refs = [r for r in self.refs if not r["auto"]]
        self.refs.append({"kp": kp, "des": des, "auto": auto, "thumb": cv2.resize(gray, (128, 72))})
        manual = [r for r in self.refs if not r["auto"]]
        if len(manual) > self.max_targets:
            self.refs.remove(manual[0])
        return True

    def clear(self):
        self.refs = []
        self.last_best = None

    def score(self, gray, stop_at=None):
        """Best RANSAC inlier count against any reference, or None if not calibrated.
        Stops early once a reference reaches stop_at, so many targets stay cheap."""
        if not self.refs:
            return None
        kp, des = self.orb.detectAndCompute(gray, None)
        if des is None or len(kp) < 10:
            return 0
        best = 0
        order = sorted(self.refs, key=lambda r: r is not self.last_best)
        for r in order:
            pairs = self.bf.knnMatch(r["des"], des, k=2)
            good = [m for m, n in (p for p in pairs if len(p) == 2) if m.distance < 0.75 * n.distance]
            if len(good) < 8:
                continue
            src = np.float32([r["kp"][m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
            dst = np.float32([kp[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
            _, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
            if mask is not None and int(mask.sum()) > best:
                best = int(mask.sum())
                self.last_best = r
                if stop_at is not None and best >= stop_at:
                    break
        return best


# ---------------------------------------------------------------- step 2a: screens and notes

class ScreenDetector:
    def __init__(self, score):
        self.det = None
        self.error = None
        try:
            import mediapipe as mp
            from mediapipe.tasks import python as mpt
            from mediapipe.tasks.python import vision
            path = os.path.join(HERE, "efficientdet_lite0.tflite")
            if not os.path.exists(path):
                print("[detector] downloading the MediaPipe object detection model…")
                tmp = path + ".part"
                # requests ships its own CA bundle (certifi); urllib fails on python.org's macOS Python
                r = requests.get(MODEL_URL, timeout=30)
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    f.write(r.content)
                os.replace(tmp, path)
            opts = vision.ObjectDetectorOptions(
                base_options=mpt.BaseOptions(model_asset_path=path),
                max_results=6,
                score_threshold=score,
                category_allowlist=["cell phone", "laptop", "keyboard", "book", "tv"],
            )
            self.det = vision.ObjectDetector.create_from_options(opts)
            self.mp = mp
        except Exception as e:
            self.error = str(e).splitlines()[0][:120]
            print(f"[detector] disabled: {self.error}")

    def detect(self, bgr):
        if not self.det:
            return []
        h, w = bgr.shape[:2]
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        res = self.det.detect(self.mp.Image(image_format=self.mp.ImageFormat.SRGB, data=rgb))
        out = []
        for d in res.detections:
            c, b = d.categories[0], d.bounding_box
            out.append({
                "label": c.category_name, "score": c.score,
                "box": (b.origin_x, b.origin_y, b.width, b.height),
                "area": b.width * b.height / (w * h),
                "cy": (b.origin_y + b.height / 2) / h,
            })
        return out


def read_objects(objs, cfg):
    """-> (screen_in_view, phone_in_view, notes_in_view). A 'tv' is ignored: it is usually the projector."""
    phone = any(o["label"] == "cell phone" for o in objs)
    laptop = any(o["label"] in ("laptop", "keyboard") and (o["area"] >= cfg["laptop_min_area"] or o["cy"] > 0.5)
                 for o in objs)
    notes = any(o["label"] == "book" for o in objs)
    return phone or laptop, phone, notes


def color_hist(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    h = cv2.calcHist([hsv], [0, 1], None, [24, 16], [0, 180, 0, 256])
    return cv2.normalize(h, h).flatten()


# ---------------------------------------------------------------- step 2b: is it related?

class Judge:
    """Asks Gemini whether the screen in view relates to the lecture. One request at a time."""

    PROMPT = (
        "This frame is from a camera on a student's glasses during a lecture. "
        "Session topic: \"{topic}\". The speaker just said: \"{recent}\". "
        "Describe what is on the screen or paper in view in a few words, then judge whether it relates "
        "to this session. Reply with JSON only: "
        "{{\"content\": \"...\", \"relation\": \"related\" | \"unrelated\" | \"unclear\" | \"none\", \"confidence\": 0-1}}. "
        "Code, notes, slides or documents on the subject count as related. Shopping, social media, "
        "messaging, games and unrelated videos count as unrelated. If you cannot read the screen, say unclear. "
        "If no screen, paper or book is actually in view (just a room, wall, window or people), say none."
    )

    def __init__(self, cfg, api):
        self.provider = os.environ.get("JUDGE_PROVIDER", "gemini").strip().lower()
        if self.provider == "openai":
            self.key = os.environ.get("OPENAI_API_KEY", "")
            self.model = os.environ.get("OPENAI_MODEL", "")
            need = "set OPENAI_API_KEY and OPENAI_MODEL"
        else:
            self.provider = "gemini"
            self.key = os.environ.get("GEMINI_API_KEY", "")
            self.model = os.environ.get("GEMINI_MODEL", "")
            need = "set GEMINI_API_KEY and GEMINI_MODEL"
        self.topic = os.environ.get("REWIND_TOPIC", "the lecture")
        self.api = api.rstrip("/") if api else None
        self.transcript_path = os.environ.get("REWIND_TRANSCRIPT_PATH", "/transcript")
        self.cfg = cfg
        self.available = bool(self.key and self.model)
        self.inflight = False
        self.req_id = 0
        self.sent_at = 0.0
        self.verbose = False
        self.verdict = None  # {"relation", "confidence", "content", "t", "episode"}
        self.last_error = "" if self.available else need
        self.need = need

    def request(self, bgr, now, episode):
        """Starts a check in the background. Returns True if one was sent. A request that has run past
        the timeout (requests' timeout is per read, so a slow stream can hang far longer) is abandoned."""
        if not self.available:
            return False
        if self.inflight and time.time() - self.sent_at < self.cfg["gemini_timeout"] + 1.0:
            return False
        self.req_id += 1
        self.sent_at = time.time()
        self.inflight = True
        threading.Thread(target=self._run, args=(bgr.copy(), now, episode, self.req_id), daemon=True).start()
        return True

    def _recent_transcript(self, now):
        if not self.api:
            return ""
        try:
            since = now - self.cfg["transcript_seconds"]
            r = requests.get(f"{self.api}{self.transcript_path}", params={"since": since}, timeout=1)
            data = r.json()
            lines = data.get("transcript", data) if isinstance(data, dict) else data
            # the backend may ignore ?since and return everything, so filter here too ("t" or "end"/"start")
            recent = [x for x in lines if isinstance(x, dict)
                      and (x.get("t") or x.get("end") or x.get("start") or since) >= since]
            return " ".join(x.get("text", "") for x in recent)[-1200:]
        except Exception:
            return ""

    def _run(self, bgr, now, episode, rid):
        t0 = time.time()
        err = ""
        try:
            h, w = bgr.shape[:2]
            if w > 768:
                bgr = cv2.resize(bgr, (768, int(768 * h / w)))
            ok, jpg = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 80])
            prompt = self.PROMPT.format(topic=self.topic, recent=self._recent_transcript(now) or "(not available)")
            b64 = base64.b64encode(jpg.tobytes()).decode()
            text = self._ask_openai(prompt, b64) if self.provider == "openai" else self._ask_gemini(prompt, b64)
            v = json.loads(text[text.find("{"): text.rfind("}") + 1])
            verdict = {
                "relation": str(v.get("relation", "unclear")).lower(),
                "confidence": float(v.get("confidence", 0)),
                "content": str(v.get("content", ""))[:60],
                "t": now, "episode": episode,
            }
        except Exception as e:
            verdict = {"relation": "unclear", "confidence": 0, "content": "", "t": now, "episode": episode}
            err = f"{e.__class__.__name__}: {str(e)[:60]}"
        late = rid != self.req_id   # a newer request replaced this one: drop the answer
        if self.verbose:
            print(f"[judge] {'DROPPED late ' if late else ''}verdict in {time.time() - t0:.1f}s: "
                  f"{verdict['relation']} {verdict['confidence']:.2f} {verdict['content']} {err}".rstrip(), flush=True)
        if late:
            return
        self.verdict, self.last_error = verdict, err
        self.inflight = False

    @staticmethod
    def _check(r):
        if not r.ok:  # surface the API's own message (e.g. a wrong model ID) without the request
            try:
                msg = r.json().get("error", {}).get("message", "")
            except Exception:
                msg = ""
            raise RuntimeError(f"HTTP {r.status_code} {msg[:80]}")

    def _ask_gemini(self, prompt, b64):
        body = {
            "contents": [{"parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
            ]}],
            "generationConfig": {"responseMimeType": "application/json"},  # Gemini 4 rejects temperature
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        r = requests.post(url, headers={"x-goog-api-key": self.key}, json=body, timeout=self.cfg["gemini_timeout"])
        self._check(r)
        parts = r.json()["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)

    def _ask_openai(self, prompt, b64):
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}",
                                                    "detail": self.cfg["openai_image_detail"]}},
            ]}],
            "response_format": {"type": "json_object"},
            **self.cfg["openai_extra"],
        }
        r = requests.post("https://api.openai.com/v1/chat/completions", json=body,
                          headers={"Authorization": f"Bearer {self.key}"}, timeout=self.cfg["gemini_timeout"])
        self._check(r)
        return r.json()["choices"][0]["message"]["content"] or ""


# ---------------------------------------------------------------- the drift state machine

class DriftMachine:
    def __init__(self, backend, cfg):
        self.backend = backend
        self.cfg = cfg
        self.drift = None          # the open automatic drift
        self.manual = None         # the open manual drift
        self.on_since = None
        self.next_local_id = 1

    def _open(self, start, cause, detail, now):
        d = {"local_id": self.next_local_id, "id": None, "start": start, "end": None, "opened_at": round(now, 3),
             "cause": cause, "detail": detail, "pending_end": None}
        self.next_local_id += 1
        self.backend.send_start(d)
        print(f"[drift] START {cause} {detail or ''}".rstrip(), flush=True)
        return d

    def _close(self, d, end):
        d["end"] = end
        self.backend.send_end(d)
        print(f"[drift] END after {end - d['start']:.1f}s", flush=True)

    def toggle_manual(self, now):
        if self.manual is None:
            if self.drift:
                self._close(self.drift, self.drift["pending_end"] or now)
                self.drift = None
            self.manual = self._open(now, "manual", "manual key", now)
        else:
            self._close(self.manual, now)
            self.manual = None

    def close_all(self, now):
        for d in (self.drift, self.manual):
            if d is not None and d["end"] is None:
                self._close(d, d.get("pending_end") or now)
        self.drift = self.manual = None

    def update(self, now, status, cand):
        """status: 'focused' | 'off' | 'hold'. cand: {"start", "need", "cause", "detail"} when off."""
        if self.manual is not None:
            return
        cfg = self.cfg
        qualifies = status == "off" and cand is not None and now - cand["start"] >= cand["need"]
        d = self.drift
        if d is None:
            if qualifies:
                self.drift = self._open(cand["start"], cand["cause"], cand["detail"], now)
                self.on_since = None
            return
        if d["pending_end"] is None:
            if status == "focused":
                self.on_since = self.on_since or now
                if now - self.on_since >= cfg["return_seconds"]:
                    d["pending_end"] = self.on_since   # wait out the merge window before ending
            else:
                self.on_since = None
            return
        # inside the merge window
        gap_start = d["pending_end"]
        recent_cand = status == "off" and cand is not None and cand["start"] - gap_start < cfg["merge_gap_seconds"]
        if qualifies and recent_cand:
            d["pending_end"] = None          # merge into the same drift
            self.on_since = None
        elif now - gap_start >= cfg["merge_gap_seconds"] and not recent_cand:
            self._close(d, gap_start)
            self.drift = None
            if qualifies:
                self.drift = self._open(cand["start"], cand["cause"], cand["detail"], now)

    @property
    def state(self):
        if self.manual:
            return "DRIFTING (manual)"
        if self.drift and self.drift["pending_end"] is None:
            return "DRIFTING"
        if self.drift:
            return "RETURNING"
        return "FOCUSED"


# ---------------------------------------------------------------- overlay

def draw(frame, info, objs, matcher, scale):
    h, w = frame.shape[:2]
    state = info["state"]
    color = (40, 40, 220) if state.startswith("DRIFT") else (60, 160, 60) if state == "FOCUSED" else (40, 170, 230)
    cv2.rectangle(frame, (0, 0), (w, 34), color, -1)
    cv2.putText(frame, f"{state}   {info['why']}", (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)
    for o in objs:
        x, y, bw, bh = [int(v * scale) for v in o["box"]]
        cv2.rectangle(frame, (x, y), (x + bw, y + bh), (0, 200, 255), 2)
        cv2.putText(frame, f"{o['label']} {o['score']:.2f}", (x, max(y - 6, 46)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 255), 1)
    lines = [
        f"match: {info['inliers'] if info['inliers'] is not None else 'no target (press c)'} / {info['thr']}",
        f"verdict: {info['verdict']}",
        f"backend: {info['backend']}",
        "keys: c target  x clear  d manual  m mark staged  [ ] threshold  r reload  q quit",
    ]
    for i, t in enumerate(lines):
        y = h - 12 - (len(lines) - 1 - i) * 22
        cv2.putText(frame, t, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3)
        cv2.putText(frame, t, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    for i, r in enumerate(matcher.refs):
        th = cv2.cvtColor(r["thumb"], cv2.COLOR_GRAY2BGR)
        x0, y0 = w - (i + 1) * 136, 42
        if x0 > 0:
            frame[y0:y0 + 72, x0:x0 + 128] = th
            cv2.putText(frame, "auto" if r["auto"] else "target", (x0 + 4, y0 + 66), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 255), 1)


# ---------------------------------------------------------------- main

def list_cameras():
    for i in range(6):
        cap = cv2.VideoCapture(i)
        ok, f = False, None
        if cap.isOpened():
            # Continuity Camera and some USB cams return no frame on the first reads
            t_end = time.time() + 2.0
            while not ok and time.time() < t_end:
                ok, f = cap.read()
        print(f"camera {i}: " + (f"{f.shape[1]}x{f.shape[0]}" if ok else "not available"))
        cap.release()


def main():
    ap = argparse.ArgumentParser(description="Rewind drift detector (outward camera)")
    ap.add_argument("--camera", type=int, default=None, help="camera index (default: CAMERA_INDEX or 0)")
    ap.add_argument("--video", help="replay a recorded video instead of the live camera")
    ap.add_argument("--dry", action="store_true", help="print events instead of sending them")
    ap.add_argument("--headless", action="store_true", help="no window (for testing)")
    ap.add_argument("--calibrate-at", type=float, default=None, help="video mode: auto-press c at this second")
    ap.add_argument("--list-cameras", action="store_true")
    ap.add_argument("--fast", action="store_true",
                    help="video mode: replay as fast as possible (default: real speed when the AI check is on or a window is shown)")
    ap.add_argument("--verbose", action="store_true", help="print match/status once per second (numbers only, no frames)")
    args = ap.parse_args()

    load_env(os.path.join(HERE, ".env"))
    load_env(os.path.join(os.path.dirname(HERE), ".env"))
    if args.list_cameras:
        return list_cameras()

    cfg = load_config()
    api = os.environ.get("REWIND_API", "")
    source = args.video if args.video else (args.camera if args.camera is not None else int(os.environ.get("CAMERA_INDEX", "0")))
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        sys.exit(f"Could not open {source}. Try --list-cameras.")
    clock = Clock(cap.get(cv2.CAP_PROP_FPS) or 30 if args.video else None)

    backend = Backend(api, args.dry)
    backend.start()
    beat = Heartbeat(None if args.dry else api)
    beat.start()
    matcher = TargetMatcher(cfg["max_targets"])
    detector = ScreenDetector(cfg["detector_score"])
    judge = Judge(cfg, api)
    judge.verbose = args.verbose
    machine = DriftMachine(backend, cfg)
    if not judge.available:
        print(f"[judge] AI check off ({judge.need}). Fallback: a phone in view means drift.")
    else:
        print(f"[judge] AI check: {judge.provider} {judge.model}")

    inliers, objs = None, []
    last_detect = -1e9
    strong_since = away_since = screen_since = unsettled_since = None
    episode, hist_ref = 0, None
    last_obj_t = None
    phone_votes = other_votes = 0
    unrelated_since = None
    marks = 0
    frame_i = 0
    t_start = last_print = clock.now()
    # A replay must run at real speed when AI verdicts (real seconds) race the video clock
    pace = bool(args.video) and not args.fast and (judge.available or not args.headless)
    wall0 = time.time()

    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                if args.video:
                    break
                continue
            frame_i += 1
            clock.frame = frame_i
            now = clock.now()
            if pace:
                ahead = wall0 + frame_i / clock.fps - time.time()
                if ahead > 0:
                    time.sleep(ahead)

            h, w = frame.shape[:2]
            scale = w / 640
            small = cv2.resize(frame, (640, int(640 * h / w)))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

            if args.video and args.calibrate_at is not None and not matcher.refs and frame_i / clock.fps >= args.calibrate_at:
                matcher.add(gray)
                print("[target] reference set (auto)", flush=True)

            # step 1: on target?
            if frame_i % 2 == 0 or inliers is None:
                inliers = matcher.score(gray, stop_at=2 * cfg["inlier_threshold"])
            thr = cfg["inlier_threshold"]
            on_target = inliers is not None and inliers >= thr
            if on_target and inliers >= 1.5 * thr:
                strong_since = strong_since or now
                if now - strong_since >= cfg["ref_refresh_seconds"]:
                    matcher.add(gray, auto=True)
                    strong_since = now
            else:
                strong_since = None

            # step 2a: screens and notes, a few times a second
            fresh_detect = now - last_detect >= 0.25
            if fresh_detect:
                objs = detector.detect(small)
                last_detect = now
            screen, phone, notes = read_objects(objs, cfg)
            # Up close, a laptop screen is often detected as a "book", so with the AI check on,
            # paper/book goes to the AI too. Detections flicker, so hold them briefly.
            # A "tv" is usually the projector: without the AI it is ignored, with the AI it is judged
            # (after a slide change step 1 can lose the target, and the AI sees a related slide).
            tv = any(o["label"] == "tv" for o in objs)
            looked_at = screen or ((notes or tv) and judge.available)
            if looked_at:
                last_obj_t = now
            hold = cfg["object_hold_seconds"]
            in_view = last_obj_t is not None and now - last_obj_t < hold

            # step 2b: ask the AI about a screen or page that is in view while off target
            if in_view and not on_target:
                if screen_since is None:
                    screen_since, episode, hist_ref = now, episode + 1, None
                    phone_votes = other_votes = 0
                    unrelated_since = None
                if fresh_detect and looked_at:   # count once per detector run: phone vs laptop/page
                    phone_votes += phone
                    other_votes += not phone
                hist = color_hist(small)
                changed = hist_ref is not None and cv2.compareHist(hist_ref, hist, cv2.HISTCMP_CORREL) < cfg["histogram_change"]
                v = judge.verdict
                stale = v is None or v["episode"] != episode or now - v["t"] > cfg["verdict_ttl_seconds"] or changed
                if judge.available and stale and judge.request(small, now, episode):
                    hist_ref = hist
            elif not in_view:
                # The episode (and its verdict) ends only when the screen leaves view. A match that
                # flickers around the threshold must not restart it, or verdicts are thrown away.
                screen_since = None

            # decide this frame's status
            cand, why = None, ""
            v = judge.verdict if judge.verdict and judge.verdict["episode"] == episode else None
            # the AI saw no screen or paper: the detection was a false alarm, so this is looking away
            ai_none = in_view and judge.available and v is not None and v["relation"] == "none"
            if not matcher.refs:
                status, why = "hold", "press c while looking at the slides"
            elif on_target:
                status, why = "focused", "on target"
            elif in_view and judge.available and not ai_none:
                if v and v["relation"] == "related":
                    status, why = "focused", f"related: {v['content']}"
                    unrelated_since = None
                elif v and v["relation"] == "unrelated" and v["confidence"] >= cfg["unrelated_confidence"]:
                    status, why = "off", f"unrelated: {v['content']}"
                    # Backdate to the first frame rated unrelated, not to when the screen appeared:
                    # the same laptop may have shown related code before the tab switch.
                    if unrelated_since is None:
                        unrelated_since = max(v["t"], screen_since)
                    cand = {"start": unrelated_since, "need": cfg["screen_drift_seconds"],
                            "cause": "phone" if phone_votes > other_votes else "unrelated_screen", "detail": v["content"]}
                else:
                    status, why = "hold", "checking the screen…" if judge.inflight or not v else "screen unclear"
            elif notes and not ai_none:
                status, why = "focused", "taking notes"
            elif screen and not ai_none:
                if phone:
                    status, why = "off", "phone in view"
                    cand = {"start": screen_since, "need": cfg["phone_only_seconds"], "cause": "phone", "detail": "phone in view"}
                else:
                    status, why = "focused", "laptop (no AI check)"
            else:
                status, why = "off", "looking away" + (" (AI: no screen)" if ai_none else "")
                # The look-away began when the wearer stopped being focused (e.g. while the AI was still
                # checking a screen they turned past), capped so a long wait can't make a drift instant.
                away_since = away_since or max(unsettled_since or now, now - cfg["away_backdate_max_seconds"])
                cand = {"start": away_since, "need": cfg["away_seconds"], "cause": "away", "detail": ""}
            if not (status == "off" and cand and cand["cause"] == "away"):
                away_since = None
            unsettled_since = None if status == "focused" else (unsettled_since or now)

            machine.update(now, status, cand)
            # without a target nothing is judged; don't show a misleading FOCUSED
            shown_state = "NO TARGET (press c)" if not matcher.refs and machine.state == "FOCUSED" else machine.state
            beat.update(state=shown_state, why=why, match=inliers, thr=thr, targets=len(matcher.refs))

            if args.verbose and now - last_print >= 1.0:
                last_print = now
                seen = ",".join(f"{o['label']}:{o['score']:.2f}" for o in objs) or "-"
                print(f"[{now - t_start:6.1f}s] match={inliers}/{thr} refs={len(matcher.refs)} "
                      f"{status:7} {shown_state:19} {why} | objs={seen}", flush=True)

            if not args.headless:
                vtxt = "off" if not judge.available else (
                    "checking…" if judge.inflight else
                    (f"{v['relation']} {v['confidence']:.2f} {v['content']}" if v else (judge.last_error or "-")))
                info = {"state": shown_state, "why": why, "inliers": inliers, "thr": thr,
                        "verdict": vtxt, "backend": backend.status}
                draw(frame, info, objs, matcher, scale)
                cv2.imshow("Rewind drift", frame)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                elif key == ord("c"):
                    print("[target] reference added" if matcher.add(gray) else "[target] not enough detail in view", flush=True)
                elif key == ord("x"):
                    matcher.clear()
                    print("[target] references cleared", flush=True)
                elif key == ord("m"):
                    marks += 1
                    backend.send_mark(clock.now())
                    print(f"[mark] staged drift #{marks}", flush=True)
                elif key == ord("d"):
                    machine.toggle_manual(clock.now())
                elif key == ord("["):
                    cfg["inlier_threshold"] = max(5, cfg["inlier_threshold"] - 2)
                elif key == ord("]"):
                    cfg["inlier_threshold"] += 2
                elif key == ord("r"):
                    cfg.update(load_config())
                    print("[config] reloaded", flush=True)
    finally:
        machine.close_all(clock.now())
        backend.close()
        cap.release()
        if not args.headless:
            cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
