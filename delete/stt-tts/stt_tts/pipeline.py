"""The main workflow: wires adapters, engines and the state machine together.

    STT path:  AudioInput -> STTEngine -> TextSink(s)
    TTS path:  TextSource -> TTSEngine -> AudioOutput(s)

Every endpoint is chosen by config, so swapping a device, engine or LLM never
touches this file.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from . import adapters, engines, security
from .state import Action, Controller, State

log = logging.getLogger("stt_tts")


class VoiceModule:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        chosen = (cfg["input"]["adapters"] + cfg["output"]["adapters"] + cfg["text"]["sinks"]
                  + cfg["text"]["sources"] + [cfg["control"]["adapter"]])
        if "zenoh" in chosen and not cfg["zenoh"]["enabled"]:
            raise ValueError("A zenoh adapter is selected but zenoh.enabled is false. "
                             "Run: stt-tts config set zenoh.enabled true")
        security.enforce(cfg)  # refuses a credentialed output behind open input
        self.gate = security.InputGate(cfg)
        self.controller = Controller(cfg["control"]["after_reply"])
        self.stt = engines.build_stt(cfg)
        self.tts = engines.build_tts(cfg)
        self.inputs = [adapters.build(adapters.AUDIO_INPUTS, n, cfg) for n in cfg["input"]["adapters"]]
        self.outputs = [adapters.build(adapters.AUDIO_OUTPUTS, n, cfg) for n in cfg["output"]["adapters"]]
        self.sinks = [adapters.build(adapters.TEXT_SINKS, n, cfg) for n in cfg["text"]["sinks"]]
        self.sources = [adapters.build(adapters.TEXT_SOURCES, n, cfg) for n in cfg["text"]["sources"]]
        self.control = adapters.build(adapters.CONTROLS, cfg["control"]["adapter"], cfg)
        self.llm = None
        if cfg["llm"]["enabled"]:
            self.llm = adapters.LLM_SINK(cfg)
            self.llm.on_reply = self.speak
            self.sinks.append(self.llm)
        self.events: list[dict] = []
        self.on_event: Callable[[dict], None] | None = None
        self._pending_reply: list[str] = []
        self._spoken: list[str] = []

    # --- events --------------------------------------------------------------

    def emit(self, event: str, **data) -> None:
        record = {"event": event, "state": self.controller.state.value, "t": time.time(), **data}
        self.events.append(record)
        log.info("%s %s", event, data)
        if self.on_event:
            self.on_event(record)

    def _run(self, actions: list[Action]) -> None:
        for action in actions:
            if action is Action.OPEN_MIC:
                for adapter in self.inputs:
                    adapter.open()
                self.emit("mic_open")
            elif action is Action.CLOSE_MIC_AND_SEND:
                self._capture_and_send()
            elif action is Action.CANCEL_REPLY:
                self._pending_reply.clear()
                self.emit("reply_cancelled")
            elif action is Action.START_PLAYBACK:
                self._play_reply()
            elif action is Action.INTERRUPT_PLAYBACK:
                for out in self.outputs:
                    out.stop()
                spoken = " ".join(self._spoken)
                self._pending_reply.clear()
                if self.llm:
                    self.llm.record_interrupt(spoken)
                self.emit("interrupted", spoken_text=spoken)

    # --- button --------------------------------------------------------------

    def press(self) -> None:
        self.emit("press")
        self._run(self.controller.press())

    # --- STT path ------------------------------------------------------------

    def _capture_and_send(self) -> None:
        """Stream path: frames that arrived while listening are finalized here.

        STUB: a real capture runs on its own thread while Listening and feeds
        the engine live; here we drain each input once at close.
        """
        tls = self.cfg["zenoh"]["tls"]["enabled"]
        for adapter in self.inputs:
            if not self.gate.admit_adapter(adapter.name, tls):
                self.emit("input_refused", adapter=adapter.name)
                continue
            for frame in adapter.frames():
                partial = self.stt.feed(frame)
                if partial:
                    self.emit("partial", text=partial)
            adapter.close()
        self.emit("mic_closed")
        self.deliver(self.stt.finalize(), mode="stream")

    def transcribe_frames(self, frames) -> str:
        """Burst path: a whole clip (e.g. a spooled file) in, text out."""
        text = self.stt.transcribe(frames)
        self.deliver(text, mode="burst")
        return text

    def deliver(self, text: str, mode: str) -> None:
        meta = {"mode": mode, "t": time.time()}
        self.emit("final", text=text, mode=mode)
        for sink in self.sinks:
            sink.send(text, meta)

    # --- TTS path ------------------------------------------------------------

    def speak(self, text: str, autoplay: bool = True) -> None:
        """Text arrives from a source (e.g. the LLM reply)."""
        self._pending_reply = self.tts.sentences(text)
        self._spoken = []
        self._autoplay = autoplay
        self.emit("reply_received", sentences=len(self._pending_reply))
        self._run(self.controller.audio_ready())

    def _play_reply(self) -> None:
        """STUB: synchronous. A real player runs on a thread so press() can interrupt mid-sentence."""
        if not getattr(self, "_autoplay", True):
            return
        while self.play_next():
            pass

    def play_next(self) -> bool:
        """Synthesize and play one sentence. Returns False when nothing is left."""
        if self.controller.state is not State.SPEAKING:
            return False
        if not self._pending_reply:
            self.emit("playback_done")
            self._run(self.controller.playback_done())
            return False
        sentence = self._pending_reply.pop(0)
        pcm = self.tts.synthesize(sentence)
        for out in self.outputs:
            out.play(pcm)
        self._spoken.append(sentence)
        self.emit("spoke", text=sentence)
        return True
