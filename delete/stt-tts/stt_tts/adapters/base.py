"""Adapter interfaces: the user-side and model-side endpoints.

Audio in the module is always 16-bit little-endian mono PCM bytes.
Input adapters deliver 16 kHz; output adapters accept the TTS rate.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Iterator

FrameCallback = Callable[[bytes], None]
TextCallback = Callable[[str], None]
PressCallback = Callable[[], None]


class AudioInput(ABC):
    """A user-side audio source (mic, Bluetooth, network stream, file)."""

    name = "base"

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg

    @abstractmethod
    def frames(self) -> Iterator[bytes]:
        """Yield PCM frames while the mic is open; stop when closed."""

    def open(self) -> None:  # optional hook
        pass

    def close(self) -> None:  # optional hook
        pass


class AudioOutput(ABC):
    """A user-side audio destination (speaker, Bluetooth, network stream, file)."""

    name = "base"

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg

    @abstractmethod
    def play(self, pcm: bytes) -> None:
        """Queue PCM for playback."""

    @abstractmethod
    def stop(self) -> None:
        """Drop all queued audio immediately (interrupt)."""


class TextSink(ABC):
    """A model-side destination for transcripts (LLM adapter, stdout, Zenoh)."""

    name = "base"

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg

    @abstractmethod
    def send(self, text: str, meta: dict) -> None:
        """Deliver a final transcript."""


class TextSource(ABC):
    """A model-side source of text to speak (LLM reply, stdin, Zenoh)."""

    name = "base"

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg

    @abstractmethod
    def listen(self, on_text: TextCallback) -> None:
        """Call on_text for each piece of text to speak. May block."""


class ControlInput(ABC):
    """The physical (or virtual) single button."""

    name = "base"

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg

    @abstractmethod
    def listen(self, on_press: PressCallback) -> None:
        """Call on_press for each button press. May block."""
