"""Zenoh adapters: subscribe to and publish on configurable key expressions.

Requires the optional `eclipse-zenoh` package. Key names come from
config `zenoh.keys.*`; defaults are voice/in/audio, voice/in/control,
voice/out/text, voice/tts/text, voice/tts/audio and voice/events.

STUB: the session handling below is written against the zenoh 1.x Python API
but has not been run against a live router yet.
"""

from __future__ import annotations

import json
import queue
from typing import Iterator

from .base import AudioInput, AudioOutput, ControlInput, TextSink, TextSource


def zenoh_available() -> bool:
    try:
        import zenoh  # noqa: F401
    except ImportError:
        return False
    return True


def open_session(cfg: dict):
    """Open a Zenoh session from config. Raises RuntimeError if zenoh is missing."""
    try:
        import zenoh
    except ImportError as exc:
        raise RuntimeError(
            "Zenoh support needs the optional package: pip install eclipse-zenoh"
        ) from exc
    zcfg = zenoh.Config()
    zcfg.insert_json5("mode", json.dumps(cfg["zenoh"]["mode"]))
    zcfg.insert_json5("connect/endpoints", json.dumps(cfg["zenoh"]["connect"]))
    return zenoh.open(zcfg)


class _ZenohBase:
    _session = None

    def session(self):
        if _ZenohBase._session is None:
            _ZenohBase._session = open_session(self.cfg)
        return _ZenohBase._session

    def key(self, name: str) -> str:
        return self.cfg["zenoh"]["keys"][name]


class ZenohAudioInput(_ZenohBase, AudioInput):
    """Subscribes to zenoh.keys.in_audio for PCM frames."""

    name = "zenoh"

    def __init__(self, cfg: dict) -> None:
        AudioInput.__init__(self, cfg)
        self._frames: queue.Queue[bytes | None] = queue.Queue()
        self._sub = None

    def open(self) -> None:
        self._sub = self.session().declare_subscriber(
            self.key("in_audio"),
            lambda sample: self._frames.put(bytes(sample.payload.to_bytes())),
        )

    def close(self) -> None:
        self._frames.put(None)
        if self._sub is not None:
            self._sub.undeclare()
            self._sub = None

    def frames(self) -> Iterator[bytes]:
        while (frame := self._frames.get()) is not None:
            yield frame


class ZenohAudioOutput(_ZenohBase, AudioOutput):
    """Publishes TTS PCM on zenoh.keys.tts_audio; an empty payload means stop."""

    name = "zenoh"

    def play(self, pcm: bytes) -> None:
        self.session().put(self.key("tts_audio"), pcm)

    def stop(self) -> None:
        self.session().put(self.key("events"), json.dumps({"event": "interrupted"}))


class ZenohTextSink(_ZenohBase, TextSink):
    """Publishes transcripts on zenoh.keys.out_text as JSON."""

    name = "zenoh"

    def send(self, text: str, meta: dict) -> None:
        self.session().put(self.key("out_text"), json.dumps({"text": text, **meta}))


class ZenohTextSource(_ZenohBase, TextSource):
    """Subscribes to zenoh.keys.tts_text; each message is spoken."""

    name = "zenoh"

    def listen(self, on_text) -> None:
        self.session().declare_subscriber(
            self.key("tts_text"),
            lambda sample: on_text(sample.payload.to_string()),
        )


class ZenohButton(_ZenohBase, ControlInput):
    """Any message on zenoh.keys.in_control counts as one press."""

    name = "zenoh"

    def listen(self, on_press) -> None:
        self.session().declare_subscriber(self.key("in_control"), lambda _s: on_press())
