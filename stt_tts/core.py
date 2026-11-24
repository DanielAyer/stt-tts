"""The conversion core: audio -> text and text -> audio, nothing else.

No Zenoh, no sessions, no state machine, no routing. Anything that needs those
(for example switchboard-at-home) calls this module and keeps its own state.

    from stt_tts.core import Core

    core = Core()                       # loads config and engines once
    stream = core.open_stream()         # one per utterance
    stream.feed(pcm16k)                 # 16 kHz mono 16-bit LE PCM, any chunk size
    stream.partial()                    # in-progress text ("" if the engine has none)
    text = stream.finalize()            # final text; the stream can be reused
    stream.close()

    for pcm in core.synthesize_stream("Hello there. How are you?"):
        ...                             # PCM at core.output_rate, one chunk per sentence
    core.close()

Models load once per Core. Speech-to-text streams share the loaded model, so
opening a stream is cheap. Text-to-speech is not thread-safe: callers that
synthesize from several threads must take turns (a job queue in front of the
core does this).
"""

from __future__ import annotations

from typing import Iterator

from . import config as _config
from . import engines

INPUT_RATE = 16000


class Stream:
    """One speech-to-text stream. Not thread-safe; use one per utterance in flight."""

    def __init__(self, engine) -> None:
        self._engine = engine

    def feed(self, pcm: bytes) -> None:
        if pcm:
            self._engine.feed(pcm)

    def partial(self) -> str:
        update = getattr(self._engine, "update", None)
        return update() if update else ""

    def finalize(self) -> str:
        return self._engine.finalize()

    def close(self) -> None:
        self._engine.close()


class Core:
    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg if cfg is not None else _config.load()
        self.input_rate = INPUT_RATE
        self._tts = engines.build_tts(self.cfg)
        self.voice = engines.options(self.cfg, engines.spec("tts", self.cfg["tts"]["engine"])).get("voice")

    @property
    def output_rate(self) -> int:
        return self._tts.sample_rate

    # --- speech to text ---------------------------------------------------------------

    def open_stream(self) -> Stream:
        return Stream(engines.build_stt(self.cfg))

    def transcribe(self, pcm: bytes) -> str:
        """A whole clip at once (16 kHz mono 16-bit LE PCM)."""
        stream = self.open_stream()
        try:
            stream.feed(pcm)
            return stream.finalize()
        finally:
            stream.close()

    # --- text to speech ---------------------------------------------------------------

    def _check_voice(self, voice: str | None) -> None:
        if voice is not None and voice != self.voice:
            raise ValueError(f"voice '{voice}' isn't loaded; this core speaks '{self.voice}' "
                             "(per-call voices are not supported yet)")

    def sentences(self, text: str) -> list[str]:
        return self._tts.sentences(text)

    def synthesize_stream(self, text: str, voice: str | None = None) -> Iterator[bytes]:
        """PCM at output_rate, one chunk per sentence, so playback can start early."""
        self._check_voice(voice)
        for sentence in self._tts.sentences(text):
            yield self._tts.synthesize(sentence)

    def synthesize(self, text: str, voice: str | None = None) -> bytes:
        return b"".join(self.synthesize_stream(text, voice))

    def voices(self) -> list[str]:
        return [self.voice] if self.voice else []

    def close(self) -> None:
        self._tts.close()
