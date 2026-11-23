"""The "none" engines: placeholders that need no packages or models."""

from __future__ import annotations

from .base import STTEngine, TTSEngine


class StubSTT(STTEngine):
    """Returns a placeholder transcript sized to the audio received."""

    def __init__(self, cfg: dict, opts: dict | None = None) -> None:
        super().__init__(cfg, opts)
        self._bytes = 0

    def feed(self, pcm: bytes) -> str | None:
        self._bytes += len(pcm)
        return None

    def finalize(self) -> str:
        seconds = self._bytes / (2 * self.cfg["input"]["sample_rate"])
        self._bytes = 0
        return f"(stub transcript of {seconds:.1f} s of audio)"


class StubTTS(TTSEngine):
    """Returns silence sized to the text."""

    WORDS_PER_SECOND = 2.5

    def synthesize(self, sentence: str) -> bytes:
        seconds = max(len(sentence.split()) / self.WORDS_PER_SECOND, 0.2)
        return b"\x00\x00" * int(seconds * self.sample_rate)
