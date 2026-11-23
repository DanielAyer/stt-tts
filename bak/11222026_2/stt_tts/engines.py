"""Speech engines: STT (audio -> text) and TTS (text -> audio).

Both are STUBs. The planned engines are Moonshine (STT) and Kokoro (TTS);
see the README section "What our rig uses".
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Iterator


class STTEngine(ABC):
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg

    @abstractmethod
    def feed(self, pcm: bytes) -> str | None:
        """Stream path: add a frame; may return a partial transcript."""

    @abstractmethod
    def finalize(self) -> str:
        """End of utterance: return the final transcript and reset."""

    def transcribe(self, frames: Iterator[bytes]) -> str:
        """Burst path: a whole clip at once."""
        for frame in frames:
            self.feed(frame)
        return self.finalize()


class TTSEngine(ABC):
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg

    @abstractmethod
    def synthesize(self, sentence: str) -> bytes:
        """Return PCM for one sentence at tts.sample_rate."""

    def sentences(self, text: str) -> list[str]:
        """Split so playback starts after the first sentence and interrupts drop the rest."""
        parts = re.split(r"(?<=[.!?])\s+", text.strip())
        return [p for p in parts if p]


class StubSTT(STTEngine):
    """STUB: replace with Moonshine Small Streaming (moonshine-voice, ONNX)."""

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self._bytes = 0

    def feed(self, pcm: bytes) -> str | None:
        self._bytes += len(pcm)
        return None

    def finalize(self) -> str:
        seconds = self._bytes / (2 * self.cfg["input"]["sample_rate"])
        self._bytes = 0
        return f"(stub transcript of {seconds:.1f} s of audio)"


class StubTTS(TTSEngine):
    """STUB: returns silence sized to the text. Replace with Kokoro-82M (ONNX int8)."""

    WORDS_PER_SECOND = 2.5

    def synthesize(self, sentence: str) -> bytes:
        seconds = max(len(sentence.split()) / self.WORDS_PER_SECOND, 0.2)
        return b"\x00\x00" * int(seconds * self.cfg["tts"]["sample_rate"])


STT_ENGINES = {"stub": StubSTT}
TTS_ENGINES = {"stub": StubTTS}


def build_stt(cfg: dict) -> STTEngine:
    name = cfg["stt"]["engine"]
    if name not in STT_ENGINES:
        raise ValueError(f"STT engine '{name}' is not implemented yet. Available: {list(STT_ENGINES)}")
    return STT_ENGINES[name](cfg)


def build_tts(cfg: dict) -> TTSEngine:
    name = cfg["tts"]["engine"]
    if name not in TTS_ENGINES:
        raise ValueError(f"TTS engine '{name}' is not implemented yet. Available: {list(TTS_ENGINES)}")
    return TTS_ENGINES[name](cfg)
