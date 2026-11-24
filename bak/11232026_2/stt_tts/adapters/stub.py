"""Stub and simple adapters. Replace the STUB ones with real devices."""

from __future__ import annotations

import sys
import time
import wave
from pathlib import Path
from typing import Iterator

from .base import AudioInput, AudioOutput, ControlInput, TextSink, TextSource

FRAME_MS = 20


class StubMic(AudioInput):
    """STUB: emits silent 20 ms frames. Replace with a PipeWire/ALSA/Bluetooth capture."""

    name = "stub_mic"

    def __init__(self, cfg: dict, seconds: float = 1.0) -> None:
        super().__init__(cfg)
        self.seconds = seconds

    def frames(self) -> Iterator[bytes]:
        rate = self.cfg["input"]["sample_rate"]
        samples = rate * FRAME_MS // 1000
        for _ in range(int(self.seconds * 1000 / FRAME_MS)):
            yield b"\x00\x00" * samples


class FileInput(AudioInput):
    """Reads a 16-bit mono WAV in frames. Used for the burst (spooled file) path."""

    name = "file"

    def __init__(self, cfg: dict, path: str | Path | None = None) -> None:
        super().__init__(cfg)
        self.path = Path(path) if path else None

    def frames(self) -> Iterator[bytes]:
        if self.path is None:
            return
        with wave.open(str(self.path), "rb") as wav:
            samples = wav.getframerate() * FRAME_MS // 1000
            while chunk := wav.readframes(samples):
                yield chunk


class StubSpeaker(AudioOutput):
    """STUB: counts bytes instead of playing. Replace with a PipeWire/ALSA/Bluetooth sink."""

    name = "stub_speaker"

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.bytes_played = 0

    def play(self, pcm: bytes) -> None:
        self.bytes_played += len(pcm)

    def stop(self) -> None:
        pass


class FileOutput(AudioOutput):
    """Appends TTS audio to a WAV in the data directory."""

    name = "file"

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        out_dir = Path(cfg["paths"]["data_dir"]) / "tts_out"
        out_dir.mkdir(parents=True, exist_ok=True)
        self.path = out_dir / f"reply-{int(time.time())}.wav"
        self._wav: wave.Wave_write | None = None

    def play(self, pcm: bytes) -> None:
        if self._wav is None:
            self._wav = wave.open(str(self.path), "wb")
            self._wav.setnchannels(1)
            self._wav.setsampwidth(2)
            self._wav.setframerate(self.cfg["tts"]["sample_rate"])
        self._wav.writeframes(pcm)

    def stop(self) -> None:
        if self._wav is not None:
            self._wav.close()
            self._wav = None


class StdoutSink(TextSink):
    name = "stdout"

    def send(self, text: str, meta: dict) -> None:
        print(f"[transcript] {text}", flush=True)


class FileSink(TextSink):
    """Appends transcripts to a dated file in the transcript directory."""

    name = "file"

    def send(self, text: str, meta: dict) -> None:
        out_dir = Path(self.cfg["paths"]["transcript_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        day = time.strftime("%Y-%m-%d")
        with (out_dir / f"{day}.txt").open("a") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')}\t{text}\n")


class StdinSource(TextSource):
    """Each line on stdin is spoken."""

    name = "stdin"

    def listen(self, on_text) -> None:
        for line in sys.stdin:
            if line.strip():
                on_text(line.strip())


class StubButton(ControlInput):
    """STUB: no presses. Replace with a GPIO, serial, HID or Bluetooth button."""

    name = "stub_button"

    def listen(self, on_press) -> None:
        return


class KeyboardButton(ControlInput):
    """Enter key acts as the button. Handy over SSH while hardware is pending."""

    name = "keyboard"

    def listen(self, on_press) -> None:
        for _ in sys.stdin:
            on_press()
