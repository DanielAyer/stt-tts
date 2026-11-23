"""Reference clients for the doublet service, and the test tools built on them.

    stt-tts test-app                 an app that answers "You said: ..." (proves the text side)
    stt-tts test-terminal clip.wav   a terminal that speaks a WAV file and saves the reply

They double as reference code for the phone app: every step a terminal or app
takes is here, in order.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import wave
from pathlib import Path

from . import protocol
from .adapters.zenoh_adapter import open_session


def _get_json(session, key: str, payload: dict | None = None, timeout: float = 5.0) -> dict:
    for reply in session.get(key, payload=json.dumps(payload or {}), timeout=timeout):
        if reply.ok is not None:
            return protocol.loads(reply.ok.payload)
    raise RuntimeError(f"No answer on {key}. Is `stt-tts serve` running and reachable?")


def list_doublets(session, prefix: str) -> list[dict]:
    return _get_json(session, protocol.list_key(prefix)).get("doublets", [])


class App:
    """The text side: requests a doublet, reads transcripts, publishes replies."""

    def __init__(self, session, prefix: str, name: str) -> None:
        self.session = session
        self.info = _get_json(session, protocol.register_key(prefix), {"name": name})
        self.keys = self.info["keys"]
        self.token = session.liveliness().declare_token(self.keys["live_app"])

    def on_transcript(self, callback) -> None:
        self._sub = self.session.declare_subscriber(
            self.keys["text_out"], lambda s: callback(protocol.loads(s.payload)))

    def say(self, text: str) -> None:
        self.session.put(self.keys["text_in"], text, attachment=self.info["app_token"])

    def close(self) -> None:
        self.token.undeclare()


class Terminal:
    """The audio side: attaches to a doublet, sends mic audio and presses, receives speech."""

    def __init__(self, session, doublet: dict, name: str) -> None:
        self.session = session
        self.keys = doublet["keys"]
        self.events: queue.Queue = queue.Queue()
        self.transcripts: list[str] = []
        self.audio: list[bytes] = []
        self.audio_rate = 24000
        self._subs = [
            session.declare_subscriber(self.keys["events"],
                                       lambda s: self.events.put(protocol.loads(s.payload))),
            session.declare_subscriber(self.keys["audio_out"], self._on_audio),
            # open to listen: a terminal can read its own transcript too
            session.declare_subscriber(self.keys["text_out"], lambda s: self.transcripts.append(
                protocol.loads(s.payload).get("text", ""))),
        ]
        reply = _get_json(session, self.keys["attach"], {"terminal": name})
        if not reply.get("ok"):
            raise RuntimeError(f"Doublet is locked by {reply.get('held_by')}")
        self.lock_token = reply["terminal_token"]
        self.token = session.liveliness().declare_token(self.keys["live_term"])

    def _on_audio(self, sample) -> None:
        self.audio_rate = protocol.rate_from_encoding(str(sample.encoding))
        self.audio.append(sample.payload.to_bytes())

    def press(self) -> None:
        self.session.put(self.keys["control"], json.dumps({"op": "press"}),
                         attachment=self.lock_token)

    def send_audio(self, pcm: bytes) -> None:
        import zenoh
        self.session.put(self.keys["audio_in"], pcm, attachment=self.lock_token,
                         encoding=zenoh.Encoding(protocol.AUDIO_IN_ENCODING))

    def wait_for(self, *names: str, timeout: float = 60.0) -> dict:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                ev = self.events.get(timeout=max(0.05, end - time.monotonic()))
            except queue.Empty:
                break
            if ev.get("event") in names:
                return ev
        raise TimeoutError(f"no {'/'.join(names)} event within {timeout:.0f} s")

    def detach(self) -> None:
        _get_json(self.session, self.keys["detach"], {"terminal_token": self.lock_token})
        self.token.undeclare()


# --- test tools ------------------------------------------------------------------

def run_test_app(cfg: dict, name: str = "echo") -> int:
    session = open_session(cfg)
    app = App(session, cfg["service"]["prefix"], name)
    print(f"Registered doublet {app.info['id']} ('{name}'). Waiting for speech; Ctrl-C to stop.")

    def answer(msg: dict) -> None:
        text = msg.get("text", "")
        print(f"heard: {text}")
        app.say(f"You said: {text}" if text else "I didn't catch that.")

    app.on_transcript(answer)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    app.close()
    session.close()
    return 0


def _read_wav_16k(path: str) -> bytes:
    with wave.open(path, "rb") as wav:
        channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
        raw = wav.readframes(wav.getnframes())
    if width != 2:
        raise ValueError(f"{path}: need 16-bit PCM")
    if (channels, rate) != (1, 16000):
        import numpy as np
        audio = np.frombuffer(raw, dtype="<i2").reshape(-1, channels).mean(axis=1)
        audio = np.interp(np.arange(0, len(audio), rate / 16000), np.arange(len(audio)), audio)
        raw = audio.astype("<i2").tobytes()
    return raw


def run_test_terminal(cfg: dict, wav_path: str, name: str = "test-terminal",
                      doublet_name: str | None = None, out_path: str = "reply.wav") -> int:
    session = open_session(cfg)
    prefix = cfg["service"]["prefix"]
    doublets = list_doublets(session, prefix)
    if doublet_name:
        doublets = [d for d in doublets if d["name"] == doublet_name or d["id"] == doublet_name]
    free = [d for d in doublets if not d["terminal"]]
    if not free:
        print("No free doublet found. Start an app first, e.g.: stt-tts test-app")
        session.close()
        return 1
    d = free[0]
    print(f"Attaching to doublet {d['id']} ('{d['name']}')")
    term = Terminal(session, d, name)
    pcm = _read_wav_16k(wav_path)

    term.press()                                  # Idle -> Listening
    term.wait_for("listening", timeout=10)
    frame = 640                                   # 20 ms at 16 kHz, 16-bit
    for i in range(0, len(pcm), frame):           # stream in real time, like a mic
        term.send_audio(pcm[i:i + frame])
        time.sleep(0.02)
    sent = time.monotonic()
    term.press()                                  # Listening -> send
    term.wait_for("thinking", timeout=30)
    term.wait_for("speaking", timeout=60)
    first_audio = None
    while first_audio is None and time.monotonic() - sent < 60:
        if term.audio:
            first_audio = time.monotonic()
        time.sleep(0.01)
    term.wait_for("playback_done", "interrupted", timeout=120)

    audio = b"".join(term.audio)
    with wave.open(out_path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(term.audio_rate)
        wav.writeframes(audio)
    print(f"Sent {len(pcm) / 32000:.1f} s of audio; reply: {len(audio) / 2 / term.audio_rate:.1f} s "
          f"saved to {out_path}")
    if term.transcripts:
        print(f"Transcript: {term.transcripts[-1]}")
    if first_audio:
        print(f"Time from releasing the button to the first reply audio: {first_audio - sent:.2f} s")
    term.detach()
    session.close()
    return 0
