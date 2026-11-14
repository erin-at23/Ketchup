#!/usr/bin/env python3
"""Rewind P2: the lecture transcript.

Lecture audio -> Deepgram (live) -> one line per final result -> POST $REWIND_API/transcript

The band's mic is always on, but sound only leaves this laptop while the student is out of focus.
The last 60 s are held in memory here. When GET $REWIND_API/focus says {"focused": false}, that
minute goes to Deepgram first, then live sound, until P3 says {"focused": true} again.
With --nudge, after 60 s out of focus the band's LED blinks (the band has no vibration motor;
the mic pauses ~3 s for it, because the LED is locked while the mic is on).

    python3 audio/transcribe.py                      Vexo band over Bluetooth, focus-driven (the default)
    python3 audio/transcribe.py --nudge              ...and blink the band after 60 s out of focus
    python3 audio/transcribe.py --always             transcribe everything, ignore focus
    python3 audio/transcribe.py --usb                Vexo band on the USB cable
    python3 audio/transcribe.py --record lecture.ogg ...also save the band's sound (the fallback sample)
    python3 audio/transcribe.py --replay lecture.ogg pretend band plays a recording through the pipeline
    python3 audio/transcribe.py --sim                pretend band with the kit's 16 s sample voice
    python3 audio/transcribe.py --laptop-mic         backup: this computer's mic (pip install sounddevice)

Run it from my-project/. It reads DEEPGRAM_API_KEY and REWIND_API from the environment or from
my-project/.env. With no REWIND_API the lines only go to audio/transcript.jsonl (they always do).

Each line POSTed is {"start": epoch s, "end": epoch s, "text": "..."}. A 409 (no session yet)
keeps the line queued and retried, in order, until it goes through.

While this runs, everything the microphone hears goes to Deepgram (deepgram.com). The band's
light is solid blue while its microphone is on. Tell the people near the band.
"""
import argparse
import bisect
import collections
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent                      # my-project/
sys.path.insert(0, str(ROOT / "vexo-open-kit" / "examples" / "python"))
import speech                                                      # noqa: E402  kit's Deepgram engine; also finds vexo_open
from ogg_opus import OggOpusWriter                                 # noqa: E402
from vexo_open import audio as vexo_audio                          # noqa: E402
from vexo_open.sync import Band, VexoError                         # noqa: E402

BAND = 8                    # our band, Vexo-HACK-08. The only place this number is written.
ENDPOINTING_MS = 100        # the kit measured larger values delaying finals by seconds on the band mic
REWIND_SECONDS = 60         # how far back transcription starts when focus is lost
NUDGE_AFTER = 60            # seconds out of focus before the LED nudge (--nudge)
NUDGE_SECONDS = 3           # how long the LED blinks; the mic is off meanwhile
NUDGE_COLOUR = "#ff8800"
FOCUS_POLL = 0.5            # seconds between GET /focus
TRANSCRIPT_LOG = ROOT / "audio" / "transcript.jsonl"
SAMPLE_SPEECH = ROOT / "vexo-open-kit" / "dashboard" / "speech" / "sample-speech.opus"


def load_env():
    """Fill os.environ from .env in the current folder and in my-project/. The environment wins."""
    for folder in (Path.cwd(), ROOT):
        try:
            lines = (folder / ".env").read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            name, sep, value = line.partition("=")
            name, value = name.strip(), value.strip().strip("'\"")
            if sep and name and value and not name.startswith("#") and name not in os.environ:
                os.environ[name] = value


def clock(t):
    return time.strftime("%H:%M:%S", time.localtime(t)) + ".%d" % (int(t * 10) % 10)


# ---- P3's backend ---------------------------------------------------------------------------

class Poster:
    """Writes every line to transcript.jsonl and POSTs it to P3, in order, on its own thread.
    409 (no session yet), network errors and 5xx: keep the line and retry. Other 4xx: drop it."""

    MAX_QUEUE = 300         # about 15 minutes of lecture; past that the oldest line goes

    def __init__(self, api):
        self.url = api.rstrip("/") + "/transcript" if api else None
        TRANSCRIPT_LOG.parent.mkdir(parents=True, exist_ok=True)
        self.log = open(TRANSCRIPT_LOG, "a", encoding="utf-8")
        self.queue = collections.deque()
        self.cv = threading.Condition()
        self.closing = False
        if self.url:
            threading.Thread(target=self._run, daemon=True).start()

    def add(self, line):
        self.log.write(json.dumps(line) + "\n")
        self.log.flush()
        if not self.url:
            return
        with self.cv:
            if len(self.queue) >= self.MAX_QUEUE:
                self.queue.popleft()
                print("  ! backend queue full: dropped the oldest line (it is still in transcript.jsonl)")
            self.queue.append(line)
            self.cv.notify()

    def _run(self):
        problem = None                                      # say each kind of trouble once, not every second
        while True:
            with self.cv:
                while not self.queue and not self.closing:
                    self.cv.wait()
                if not self.queue:
                    return
                line = self.queue[0]
            status = self._post(line)
            if 200 <= status < 300 or (400 <= status < 500 and status != 409):
                with self.cv:
                    if self.queue and self.queue[0] is line:
                        self.queue.popleft()
                if status >= 300:
                    print("  ! backend refused a line (status %d), dropped it: %r" % (status, line["text"][:60]))
                elif problem:
                    print("  backend OK again (%d line(s) waiting)" % len(self.queue))
                problem = None
                continue
            kind = "409" if status == 409 else "down"
            if kind != problem:
                print("  ! %s: holding lines and retrying" % (
                    "409, no session started yet" if status == 409 else
                    "backend unreachable" if status == 0 else "backend error %d" % status))
                problem = kind
            time.sleep(1.0)

    def _post(self, line):
        request = urllib.request.Request(self.url, data=json.dumps(line).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=2) as answer:
                return answer.status
        except urllib.error.HTTPError as error:
            return error.code
        except (urllib.error.URLError, OSError, ValueError):
            return 0

    def close(self, wait=5.0):
        """Give queued lines up to `wait` seconds to go through."""
        end = time.time() + wait
        with self.cv:
            self.closing = True
            self.cv.notify()
        while self.url and self.queue and time.time() < end:
            time.sleep(0.1)
        if self.url and self.queue:
            print("%d line(s) never reached the backend; they are in %s" % (len(self.queue), TRANSCRIPT_LOG))
        self.log.close()


class FocusWatcher:
    """Polls GET $REWIND_API/focus on its own thread. Expects {"focused": true|false}.
    Until P3 answers, the student counts as focused (nothing is sent). If P3 stops answering,
    the last known state is kept."""

    def __init__(self, api, every=FOCUS_POLL):
        self.url = api.rstrip("/") + "/focus"
        self.every = every
        self.focused = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        problem = None
        while True:
            status, focused = self._get()
            if focused is not None:
                if problem:
                    print("  focus endpoint OK again")
                problem = None
                self.focused = focused
            else:
                kind = "404" if status == 404 else "bad" if status == 200 else "down"
                if kind != problem:
                    print("  ! %s: keeping last focus state (%s)" % (
                        "P3 has no GET /focus yet (404)" if status == 404 else
                        "GET /focus answer is not {\"focused\": true|false}" if status == 200 else
                        "focus endpoint unreachable" if status == 0 else "GET /focus error %d" % status,
                        "focused" if self.focused else "not focused"))
                    problem = kind
            time.sleep(self.every)

    def _get(self):
        try:
            with urllib.request.urlopen(self.url, timeout=2) as answer:
                data = json.loads(answer.read(4096))
                focused = data.get("focused") if isinstance(data, dict) else None
                return answer.status, focused if isinstance(focused, bool) else None
        except urllib.error.HTTPError as error:
            return error.code, None
        except (urllib.error.URLError, OSError, ValueError):
            return 0, None


# ---- Deepgram ---------------------------------------------------------------------------------

class LectureSpeech(speech.DeepgramSpeech):
    """The kit's Deepgram engine (key handling, error messages, KeepAlive every 4 s), but each
    is_final result with text is handed over with its word times instead of joined into sentences."""

    on_final = None

    def _message(self, message):
        kind = message.get("type")
        if kind == "Results":
            if not message.get("is_final"):
                return                                      # interim guess: skip
            best = (message.get("channel", {}).get("alternatives") or [{}])[0]
            text = str(best.get("transcript") or "").strip()
            words = best.get("words") or []
            if text and words and self.on_final:
                self.on_final(float(words[0]["start"]), float(words[-1]["end"]), text)
        elif kind == "Error":
            self.error = "it reported an error"

    @property
    def alive(self):
        return not self._done.is_set()


class Stream:
    """One Deepgram connection, plus a map from its audio clock to wall-clock time.

    Deepgram's word times count seconds of audio it has received on this connection. Each chunk
    we send is recorded as (audio offset, wall time it was captured), so a word time converts
    to epoch seconds exactly, even across lost band packets or a backlog sent late."""

    def __init__(self, send, endpointing, on_line):
        self.on_line = on_line
        self.offsets, self.walls, self.sent = [], [], 0.0
        self.ears = LectureSpeech(send=send, endpointing=endpointing)
        self.ears.on_final = self._final
        self.ears.start()                                   # raises SpeechError (no key, 401, offline...)

    @property
    def alive(self):
        return self.ears.alive

    def feed(self, chunk, wall, seconds):
        self.offsets.append(self.sent)
        self.walls.append(wall)
        self.sent += seconds
        if self.ears.send == "opus":
            self.ears.feed(chunk)
        else:
            self.ears.feed_pcm(chunk)

    def wall(self, offset):
        i = max(0, bisect.bisect_right(self.offsets, offset) - 1)
        return self.walls[i] + (offset - self.offsets[i])

    def _final(self, start, end, text):
        if self.offsets:
            self.on_line({"start": round(self.wall(start), 3), "end": round(self.wall(end), 3), "text": text})

    def stop(self):
        try:
            self.ears.stop()                                # asks for the last words, waits up to 5 s
        except (speech.SpeechError, OSError):
            pass


class Pipeline:
    """Sound in from any thread. While inactive, sound only goes into a rolling backlog of the last
    REWIND_SECONDS, kept in memory on this laptop. activate() opens Deepgram, sends that backlog
    first and then live sound, and reconnects if Deepgram drops. deactivate() closes it."""

    def __init__(self, send, chunk_seconds, endpointing, on_line, rewind=REWIND_SECONDS):
        self.send, self.endpointing, self.on_line = send, endpointing, on_line
        self.lock = threading.Lock()
        self.stream = None
        self.active = False
        self.backlog = collections.deque(maxlen=int(rewind / chunk_seconds))

    @property
    def healthy(self):
        return self.stream is not None and self.stream.alive

    def oldest(self):
        """Wall time of the oldest sound in the backlog, or None."""
        with self.lock:
            return self.backlog[0][1] if self.backlog else None

    def connect(self):
        stream = Stream(self.send, self.endpointing, self.on_line)
        with self.lock:
            while self.backlog:
                stream.feed(*self.backlog.popleft())
            self.stream = stream
            self.active = True

    def deactivate(self):
        """Stop sending. The last words still arrive (on a background thread); new sound goes
        back into the rolling backlog."""
        with self.lock:
            self.active = False
            stream, self.stream = self.stream, None
        if stream is not None:
            threading.Thread(target=stream.stop, daemon=True).start()

    def push(self, chunk, wall, seconds):
        with self.lock:
            stream = self.stream
            if self.active and stream is not None and stream.alive:
                try:
                    stream.feed(chunk, wall, seconds)
                    return
                except OSError:
                    stream.ears._ws.close()                 # broken socket: let the main loop reconnect
            self.backlog.append((chunk, wall, seconds))

    def keep_connected(self, first):
        """Called from the main loop. The first connect fails loudly; later ones retry quietly."""
        if self.healthy:
            return True
        if self.stream is not None:
            print("  ! Deepgram connection dropped: reconnecting (new stream clock)")
            self.stream = None
        try:
            self.connect()
            print("Deepgram: listening.")
            return True
        except speech.SpeechError as problem:
            if first:
                raise
            print("  ! Deepgram reconnect failed: %s" % problem)
            return False

    def stop(self):
        self.active = False
        if self.stream is not None:
            self.stream.stop()


# ---- sources --------------------------------------------------------------------------------

class BandSource:
    """The Vexo band's microphone: 50 Opus packets a second, sent to Deepgram as they are."""

    send, chunk_seconds = "opus", 0.02

    def __init__(self, transport, packets=None, record=None):
        self.transport, self.packets = transport, packets
        self.ogg = OggOpusWriter(record) if record else None
        self.band = None
        self.lost_link = False
        self.push = None

    def start(self, push):
        self.push = push
        self._connect()

    def _connect(self):
        options = {"audio_packets": self.packets} if self.packets else {}
        self.band = Band.connect(BAND, self.transport, auto_reconnect=True, **options)
        self.lost_link = False
        self.band.on("audio", self._audio)
        self.band.on("reconnecting", lambda e: print("  ! band link lost: reconnecting (up to %ss)" % e.seconds))
        self.band.on("reconnected", lambda e: print("  band link back after %.1fs" % e.seconds))
        self.band.on("disconnect", self._gone)
        self.band.configure(audio=True)                     # solid blue: the microphone is on
        print("Connected to %s. Microphone on (solid blue light)." % self.band.name)

    def _audio(self, a):
        if self.ogg:
            self.ogg.write(a.seq, a.opus)
        if self.push:
            self.push(bytes(a.opus), a.time - 0.02, 0.02)   # a.time ~ end of this 20 ms packet

    def _gone(self, e):
        if getattr(e, "reason", None) != "closed":
            print("  ! band disconnected (%s)" % getattr(e, "reason", "?"))
            self.lost_link = True

    def nudge(self, seconds=NUDGE_SECONDS):
        """Blink the band's LED. The band has no vibration motor, and it refuses LED commands
        while the mic is on, so the mic goes off for `seconds` and comes back afterwards."""
        if self.band is None or self.lost_link:
            return
        print("Nudge: blinking the band's LED for %g s (mic paused meanwhile)" % seconds)
        try:
            self.band.configure(audio=False)
            self.band.set_led(NUDGE_COLOUR, pattern="blink", period=250)
            self.band.wait(seconds)
            self.band.led_off()
        except VexoError as problem:
            print("  ! nudge failed: %s" % problem.message)
        finally:
            try:
                self.band.configure(audio=True)
            except VexoError as problem:
                print("  ! could not turn the mic back on: %s" % problem.message)

    def tick(self, seconds):
        if self.lost_link:
            print("  Trying to reach %s again. Double-tap the band (it blinks blue) if this keeps failing."
                  % ("Vexo-HACK-%02d" % BAND))
            try:
                self._connect()
            except VexoError as problem:
                print("  ! %s" % problem.message)
                time.sleep(3)
            return
        self.band.wait(seconds)

    def stop(self):
        if self.band is not None and not self.lost_link:
            try:
                self.band.configure(audio=False)
            except VexoError:
                pass
            try:
                self.band.disconnect()
            except VexoError:
                pass
        if self.ogg:
            self.ogg.close()
            print("Saved %.1f s of sound (%d packets lost)." % (self.ogg.seconds, self.ogg.lost))


class LaptopMicSource:
    """Backup: this computer's microphone, 16 kHz mono int16 in 100 ms chunks."""

    send, chunk_seconds = "pcm", 0.1

    def start(self, push):
        try:
            import sounddevice
        except ImportError:
            sys.exit("The laptop mic needs:  python3 -m pip install sounddevice")

        def callback(indata, frames, time_info, status):
            push(bytes(indata), time.time() - frames / 16000, frames / 16000)

        self.stream = sounddevice.InputStream(samplerate=16000, channels=1, dtype="int16",
                                              blocksize=1600, callback=callback)
        self.stream.start()
        print("Laptop microphone on.")

    def nudge(self, seconds=NUDGE_SECONDS):
        print("  (nudge skipped: the laptop mic has no LED)")

    def tick(self, seconds):
        time.sleep(seconds)

    def stop(self):
        self.stream.stop()
        self.stream.close()


# ---- main ----------------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Rewind P2: lecture audio to timestamped transcript lines.")
    p.add_argument("--usb", action="store_true", help="band on the USB cable instead of Bluetooth")
    p.add_argument("--sim", action="store_true", help="pretend band playing the kit's 16 s sample voice")
    p.add_argument("--replay", metavar="FILE", help="pretend band playing this .ogg/.opus once (stage fallback)")
    p.add_argument("--record", metavar="FILE", help="also save the band's sound to this .ogg file")
    p.add_argument("--no-transcribe", action="store_true", help="only record (with --record); nothing is sent")
    p.add_argument("--laptop-mic", action="store_true", help="backup plan: this computer's microphone")
    p.add_argument("--seconds", type=float, help="stop after this long (default: until Ctrl+C)")
    p.add_argument("--endpointing", type=int, default=ENDPOINTING_MS, help="Deepgram endpointing in ms")
    p.add_argument("--always", action="store_true", help="transcribe everything, ignore P3's focus state")
    p.add_argument("--rewind", type=float, default=REWIND_SECONDS, help="seconds of sound sent from before focus was lost")
    p.add_argument("--nudge", action="store_true", help="blink the band's LED after --nudge-after s out of focus")
    p.add_argument("--nudge-after", type=float, default=NUDGE_AFTER, help="seconds out of focus before the nudge")
    args = p.parse_args()

    load_env()
    api = os.environ.get("REWIND_API", "").strip()
    focus_mode = not args.always and not args.no_transcribe
    if focus_mode and not api:
        sys.exit("Focus mode polls $REWIND_API/focus, but REWIND_API is not set in .env. "
                 "Set it, or run with --always to transcribe everything.")

    if args.laptop_mic:
        source = LaptopMicSource()
    else:
        packets = None
        if args.replay or args.sim:
            packets = vexo_audio.ogg_packets(args.replay or SAMPLE_SPEECH)
            if args.seconds is None:
                args.seconds = len(packets) / 50 + 3        # play once, plus time for the last words
        transport = "sim" if packets else "serial" if args.usb else "ble"
        source = BandSource(transport, packets, args.record)

    poster = Poster(api)

    def on_line(line):
        poster.add(line)
        print("[%s-%s] %s" % (clock(line["start"]), clock(line["end"])[-4:], line["text"]))

    pipeline = focus = None
    if not args.no_transcribe:
        pipeline = Pipeline(source.send, source.chunk_seconds, args.endpointing, on_line, args.rewind)
        if focus_mode:
            speech.find_key()                               # no key -> stop here, before the mic goes on
            focus = FocusWatcher(api)
            print("Focus mode: holding the last %g s here; transcribing only while %s/focus says not focused."
                  % (args.rewind, api.rstrip("/")))
        else:
            pipeline.keep_connected(first=True)             # before the mic goes on: no key -> stop here
        print("Lines go to %s and %s" % (poster.url or "(no REWIND_API set: file only)", TRANSCRIPT_LOG))

    end = time.time() + args.seconds if args.seconds else None
    source.start(pipeline.push if pipeline else None)
    retry_at = 0.0
    unfocused_since, nudged = None, False
    try:
        while end is None or time.time() < end:
            source.tick(0.5)
            if not pipeline:
                continue
            want = focus is None or not focus.focused
            if focus is not None:
                if want and unfocused_since is None:
                    unfocused_since = time.time()
                    oldest = pipeline.oldest()
                    print("Student lost focus: transcribing from %s (%.0f s back)" % (
                        clock(oldest), time.time() - oldest) if oldest else "Student lost focus: transcribing")
                elif not want and unfocused_since is not None:
                    print("Student back in focus after %.0f s: transcription paused" % (time.time() - unfocused_since))
                    unfocused_since, nudged = None, False
                    pipeline.deactivate()
            if want and not pipeline.healthy and time.time() >= retry_at:
                if not pipeline.keep_connected(first=False):
                    retry_at = time.time() + 2
            if (args.nudge and unfocused_since is not None and not nudged
                    and time.time() - unfocused_since >= args.nudge_after):
                nudged = True                               # once per time out of focus
                source.nudge()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        source.stop()
        if pipeline:
            pipeline.stop()                                 # last words arrive here
        poster.close()


if __name__ == "__main__":
    main()
