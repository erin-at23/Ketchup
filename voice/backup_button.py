#!/usr/bin/env python3
"""Rewind P4 backup: press SPACE (or click the button) -> fetch the recap -> ElevenLabs TTS -> speakers.

No Pipecat, Deepgram or Gemini involved, so the demo still has a voice if the agent gets stuck.

Run:  .venv/bin/python backup_button.py
"""
import os
import sys
import threading
import time
import tkinter as tk

import pyaudio
import requests
from dotenv import load_dotenv

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))
import recap

RATE = 24000


def speak(text, log=print):
    """Streams ElevenLabs audio (raw 24 kHz PCM) straight to the speakers as it arrives."""
    key, voice = os.environ.get("ELEVENLABS_API_KEY", ""), os.environ.get("ELEVENLABS_VOICE_ID", "")
    if not key or not voice:
        log("[tts] set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID in voice/.env")
        return
    t0 = time.time()
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream",
        params={"output_format": f"pcm_{RATE}"},
        headers={"xi-api-key": key},
        json={"text": text, "model_id": os.environ.get("ELEVENLABS_MODEL") or "eleven_flash_v2_5"},
        stream=True, timeout=10,
    )
    if not r.ok:
        log(f"[tts] HTTP {r.status_code}: {r.text[:120]}")
        return
    pa = pyaudio.PyAudio()
    out = pa.open(format=pyaudio.paInt16, channels=1, rate=RATE, output=True)
    first = True
    try:
        for chunk in r.iter_content(chunk_size=4096):
            if first:
                log(f"[tts] first audio after {time.time() - t0:.2f}s")
                first = False
            out.write(chunk)
    finally:
        out.stop_stream(); out.close(); pa.terminate()


def main():
    root = tk.Tk()
    root.title("Rewind voice (backup)")
    root.geometry("420x170")
    busy = {"on": False}
    status = tk.StringVar(value="ready")

    def run():
        t0 = time.time()
        text = recap.fetch_recap(say=lambda t: speak(t, log), log=log)
        log(f"[recap] {text}  ({time.time() - t0:.1f}s)")
        speak(text, log)
        busy["on"] = False
        status.set("ready")

    def log(msg):
        print(msg, flush=True)
        status.set(msg[:60])

    def go(_e=None):
        if busy["on"]:
            return
        busy["on"] = True
        status.set("fetching the recap…")
        threading.Thread(target=run, daemon=True).start()

    tk.Button(root, text="What did I miss?  (SPACE)", font=("Helvetica", 18), command=go, pady=12).pack(pady=18)
    tk.Label(root, textvariable=status, font=("Helvetica", 12), fg="#555").pack()
    root.bind("<KeyPress-space>", go)
    if len(sys.argv) > 1 and sys.argv[1] == "--once":   # for testing from the terminal
        root.after(200, go)
    root.mainloop()


if __name__ == "__main__":
    main()
