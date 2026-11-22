"""Engine interfaces and PCM helpers.

Inside the module, audio is 16-bit little-endian mono PCM bytes: 16 kHz into
STT, the engine's own rate out of TTS (engine.sample_rate).
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from array import array
from typing import Iterator, Sequence


try:  # every real engine brings numpy; the core works without it
    import numpy as _np
except ImportError:  # pragma: no cover
    _np = None


def pcm16_to_floats(pcm: bytes) -> list[float]:
    pcm = pcm[: len(pcm) - len(pcm) % 2]
    if _np is not None:
        return (_np.frombuffer(pcm, dtype="<i2").astype(_np.float32) / 32768.0).tolist()
    samples = array("h")
    samples.frombytes(pcm)
    return [s / 32768.0 for s in samples]


def floats_to_pcm16(samples: Sequence[float]) -> bytes:
    if _np is not None:
        arr = _np.clip(_np.asarray(samples, dtype=_np.float32), -1.0, 1.0)
        return (arr * 32767).astype("<i2").tobytes()
    out = array("h", (int(max(-1.0, min(1.0, float(s))) * 32767) for s in samples))
    return out.tobytes()


class STTEngine(ABC):
    def __init__(self, cfg: dict, opts: dict | None = None) -> None:
        self.cfg = cfg
        self.opts = opts or {}

    @abstractmethod
    def feed(self, pcm: bytes) -> str | None:
        """Stream path: add a frame; may return an updated partial transcript."""

    @abstractmethod
    def finalize(self) -> str:
        """End of utterance: return the final transcript and reset."""

    def transcribe(self, frames: Iterator[bytes]) -> str:
        """Burst path: a whole clip at once."""
        for frame in frames:
            self.feed(frame)
        return self.finalize()

    def close(self) -> None:
        pass


class TTSEngine(ABC):
    sample_rate = 24000

    def __init__(self, cfg: dict, opts: dict | None = None) -> None:
        self.cfg = cfg
        self.opts = opts or {}

    @abstractmethod
    def synthesize(self, sentence: str) -> bytes:
        """Return PCM for one sentence at self.sample_rate."""

    def sentences(self, text: str) -> list[str]:
        """Split so playback starts after the first sentence and interrupts drop the rest."""
        parts = re.split(r"(?<=[.!?])\s+", text.strip())
        return [p for p in parts if p]

    def close(self) -> None:
        pass
