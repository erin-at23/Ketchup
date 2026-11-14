#!/usr/bin/env python3
"""Rewind P4: "What did I miss?" voice agent (Pipecat).

    local mic (push-to-talk) -> Deepgram STT -> LLM (OpenAI, or Gemini) + get_missed_recap tool -> ElevenLabs TTS -> speakers

Push-to-talk: hold SPACE while the small "Rewind voice" window is in front. Mic audio only reaches
Deepgram while SPACE is held, so the lecturer and P2's lecture mic can never trigger the agent.

The tool calls GET $REWIND_API/recap/latest (see recap.py for the none / pending / ready handling)
and speaks the recap text directly, which saves a second LLM round trip (about 1 s).

Run:  .venv/bin/python agent.py          (keys in voice/.env, see .env.example)
"""
import os

# python.org's macOS Python has no root certificates, so websocket TLS (Deepgram, ElevenLabs) fails with
# CERTIFICATE_VERIFY_FAILED. Point OpenSSL at certifi's bundle before any connection is made.
import certifi
os.environ.setdefault("SSL_CERT_FILE", certifi.where())

import asyncio
import sys
import threading
import time
import tkinter as tk

from dotenv import load_dotenv
from loguru import logger

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    Frame,
    FunctionCallResultProperties,
    InputAudioRawFrame,
    ProposedUserStartedSpeakingFrame,
    ProposedUserStoppedSpeakingFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams
from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies
from pipecat.workers.runner import WorkerRunner

import recap

SYSTEM_PROMPT = (
    "You help a student catch up on a lecture. When they ask what they missed, call get_missed_recap "
    "and say its text naturally. Keep it under 30 seconds. Don't add facts. "
    "Questions like 'What did I miss?', 'Catch me up', 'What happened?' or 'What did they say?' all mean: "
    "call get_missed_recap."
)

STATUS = {"text": "starting…", "held": False}


def need(name):
    v = os.environ.get(name, "").strip()
    if not v:
        sys.exit(f"Set {name} in voice/.env (see .env.example).")
    return v


# ---------------------------------------------------------------- push-to-talk

class PushToTalkGate(FrameProcessor):
    """Passes mic audio only while SPACE is held, and turns press/release into a user turn."""

    def __init__(self):
        super().__init__()
        self.held = False
        self.released_at = None

    async def set_held(self, held):
        if held == self.held:
            return
        self.held = held
        if held:
            STATUS["text"] = "listening…"
            await self.push_frame(VADUserStartedSpeakingFrame())
            await self.push_frame(ProposedUserStartedSpeakingFrame())
        else:
            self.released_at = time.time()
            STATUS["text"] = "thinking…"
            await self.push_frame(VADUserStoppedSpeakingFrame())      # Deepgram finalizes on this
            await self.push_frame(ProposedUserStoppedSpeakingFrame())  # ends the user turn

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and not self.held:
            return  # the mic is "off" unless SPACE is held
        await self.push_frame(frame, direction)


class Probe(FrameProcessor):
    """Logs what the student said and how long the first audio took after SPACE was released."""

    def __init__(self, gate, label):
        super().__init__()
        self.gate, self.label = gate, label

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            logger.info(f"[heard] {frame.text}")
            STATUS["text"] = f"heard: {frame.text}"
        elif isinstance(frame, BotStartedSpeakingFrame) and self.gate.released_at:
            dt = time.time() - self.gate.released_at
            logger.info(f"[latency] first audio {dt:.2f}s after SPACE was released")
            STATUS["text"] = f"speaking ({dt:.1f}s)"
            self.gate.released_at = None
        await self.push_frame(frame, direction)


# ---------------------------------------------------------------- the tool

def make_tool(llm):
    async def get_missed_recap(params):
        loop = asyncio.get_running_loop()

        def say(text):  # "Give me a second" while the recap is still being written
            asyncio.run_coroutine_threadsafe(llm.push_frame(TTSSpeakFrame(text)), loop)

        text = await asyncio.to_thread(recap.fetch_recap, None, 0.5, 6.0, say, logger.info)
        logger.info(f"[recap] {text}")
        await llm.push_frame(TTSSpeakFrame(text))
        # Speak the recap as written and skip the second LLM call: the text is already natural prose.
        await params.result_callback({"recap": text}, properties=FunctionCallResultProperties(run_llm=False))

    return get_missed_recap


# ---------------------------------------------------------------- pipeline

async def run_agent(gate_holder, loop_holder):
    loop_holder["loop"] = asyncio.get_running_loop()
    transport = LocalAudioTransport(LocalAudioTransportParams(audio_in_enabled=True, audio_out_enabled=True))
    stt = DeepgramSTTService(api_key=need("DEEPGRAM_API_KEY"))
    provider = (os.environ.get("LLM_PROVIDER") or "openai").strip().lower()
    if provider == "gemini":
        from pipecat.services.google.llm import GoogleLLMService
        llm = GoogleLLMService(
            api_key=need("GOOGLE_API_KEY"),
            settings=GoogleLLMService.Settings(model=need("GEMINI_MODEL"), system_instruction=SYSTEM_PROMPT),
        )
    else:
        from pipecat.services.openai.llm import OpenAILLMService
        llm = OpenAILLMService(
            api_key=need("OPENAI_API_KEY"),
            settings=OpenAILLMService.Settings(model=os.environ.get("OPENAI_MODEL") or "gpt-4.1-mini",
                                               system_instruction=SYSTEM_PROMPT),
        )
    logger.info(f"[agent] LLM: {provider}")
    tts = ElevenLabsTTSService(
        api_key=need("ELEVENLABS_API_KEY"),
        settings=ElevenLabsTTSService.Settings(
            voice=need("ELEVENLABS_VOICE_ID"), model=os.environ.get("ELEVENLABS_MODEL") or "eleven_flash_v2_5"),
    )
    llm.register_function("get_missed_recap", make_tool(llm))
    tools = ToolsSchema(standard_tools=[FunctionSchema(
        name="get_missed_recap",
        description="Get the recap of what the student missed while their attention drifted.",
        properties={}, required=[])])
    context = LLMContext(messages=[], tools=tools)
    aggregators = LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(user_turn_strategies=ExternalUserTurnStrategies()))

    gate = PushToTalkGate()
    gate_holder["gate"] = gate
    pipeline = Pipeline([
        transport.input(), gate, stt, Probe(gate, "stt"), aggregators.user(), llm, tts,
        transport.output(), Probe(gate, "out"), aggregators.assistant(),
    ])
    worker = PipelineWorker(pipeline, params=PipelineParams(), idle_timeout_secs=None,
                            cancel_on_idle_timeout=False)
    gate_holder["worker"] = worker
    STATUS["text"] = "ready: hold SPACE and ask"
    logger.info("[agent] ready: hold SPACE in the Rewind voice window and ask")
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()


def main():
    holder, loop_holder = {}, {}
    t = threading.Thread(target=lambda: asyncio.run(run_agent(holder, loop_holder)), daemon=True)
    t.start()

    root = tk.Tk()
    root.title("Rewind voice")
    root.geometry("420x170")
    big = tk.Label(root, text="Hold SPACE and ask\n\"What did I miss?\"", font=("Helvetica", 20), pady=18)
    big.pack(fill="x")
    small = tk.Label(root, text="", font=("Helvetica", 12), fg="#555")
    small.pack()
    pending = {"release": None}

    def send(held):
        gate, loop = holder.get("gate"), loop_holder.get("loop")
        if gate and loop:
            asyncio.run_coroutine_threadsafe(gate.set_held(held), loop)

    def press(_e):
        if pending["release"]:  # key auto-repeat: a release followed by a press is still "held"
            root.after_cancel(pending["release"])
            pending["release"] = None
        if not STATUS["held"]:
            STATUS["held"] = True
            big.config(text="Listening…", bg="#c8f7c5")
            send(True)

    def release(_e):
        def really():
            pending["release"] = None
            STATUS["held"] = False
            big.config(text="Hold SPACE and ask\n\"What did I miss?\"", bg=root.cget("bg"))
            send(False)
        pending["release"] = root.after(120, really)

    def tick():
        small.config(text=STATUS["text"])
        if not t.is_alive():
            small.config(text="agent stopped: see the terminal")
        root.after(200, tick)

    def close():
        w, loop = holder.get("worker"), loop_holder.get("loop")
        if w and loop:
            asyncio.run_coroutine_threadsafe(w.cancel(), loop)
        root.after(300, root.destroy)

    root.bind("<KeyPress-space>", press)
    root.bind("<KeyRelease-space>", release)
    root.protocol("WM_DELETE_WINDOW", close)
    tick()
    root.mainloop()


if __name__ == "__main__":
    main()
