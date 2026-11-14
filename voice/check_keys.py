#!/usr/bin/env python3
"""Checks the voice keys in voice/.env without printing them, and lists the choices you need:
ElevenLabs voices (for ELEVENLABS_VOICE_ID) and Gemini models (for GEMINI_MODEL).

Run:  .venv/bin/python check_keys.py
"""
import os

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
env = lambda k: os.environ.get(k, "").strip()

print("== Deepgram ==")
if env("DEEPGRAM_API_KEY"):
    r = requests.get("https://api.deepgram.com/v1/projects", headers={"Authorization": f"Token {env('DEEPGRAM_API_KEY')}"}, timeout=8)
    print("key works" if r.ok else f"HTTP {r.status_code}: {r.text[:100]}")
else:
    print("DEEPGRAM_API_KEY is empty")

print("\n== ElevenLabs voices ==")
if env("ELEVENLABS_API_KEY"):
    r = requests.get("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": env("ELEVENLABS_API_KEY")}, timeout=8)
    if r.ok:
        for v in r.json().get("voices", []):
            labels = v.get("labels") or {}
            desc = ", ".join(str(labels[k]) for k in ("gender", "accent", "description", "use_case", "age") if labels.get(k))
            mark = "  <- current" if v["voice_id"] == env("ELEVENLABS_VOICE_ID") else ""
            print(f"{v['voice_id']}  {v['name']:<22} {desc}{mark}")
    else:
        print(f"HTTP {r.status_code}: {r.text[:100]}")
else:
    print("ELEVENLABS_API_KEY is empty")

print("\n== OpenAI ==")
if env("OPENAI_API_KEY"):
    r = requests.get(f"https://api.openai.com/v1/models/{env('OPENAI_MODEL') or 'gpt-4.1-mini'}",
                     headers={"Authorization": f"Bearer {env('OPENAI_API_KEY')}"}, timeout=8)
    print(f"key works, model {r.json().get('id')} available" if r.ok else f"HTTP {r.status_code}: {r.text[:100]}")
else:
    print("OPENAI_API_KEY is empty")

if env("LLM_PROVIDER") == "gemini":
    print("\n== Gemini models (support generateContent) ==")
if env("LLM_PROVIDER") == "gemini" and env("GOOGLE_API_KEY"):
    r = requests.get("https://generativelanguage.googleapis.com/v1beta/models", params={"pageSize": 200},
                     headers={"x-goog-api-key": env("GOOGLE_API_KEY")}, timeout=8)
    if r.ok:
        for m in r.json().get("models", []):
            if "generateContent" in m.get("supportedGenerationMethods", []):
                mark = "  <- current" if m["name"].split("/")[-1] == env("GEMINI_MODEL") else ""
                print(m["name"].split("/")[-1] + mark)
    else:
        print(f"HTTP {r.status_code}: {r.text[:100]}")
elif env("LLM_PROVIDER") == "gemini":
    print("GOOGLE_API_KEY is empty")
