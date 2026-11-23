"""The doublet service: stt-tts as a voice service on Zenoh.

    stt-tts serve

See protocol.py for the key layout. Each doublet runs its own button state
machine and speech-to-text stream on its own worker thread, so one slow
doublet never stalls another. The TTS engine is shared: it speaks one
sentence at a time, and doublets take turns.

Audio out is paced at real time with a small lead (output.buffer_ms), so the
terminal never holds more than a fraction of a second: an interrupt stops the
server at once, and the terminal flushes the little it has when it sees the
"interrupted" event.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field

from . import engines, protocol
from .adapters.zenoh_adapter import open_session
from .state import Action, Controller, State

log = logging.getLogger("stt_tts.service")
CHUNK_MS = 100
PARTIAL_EVERY_S = 0.5


@dataclass
class Doublet:
    id: str
    name: str
    app_token: str
    keys: protocol.Keys
    created: float = field(default_factory=time.time)
    terminal: str | None = None
    terminal_token: str | None = None
    app_seen: bool = False
    closed: bool = False


class VoiceService:
    def __init__(self, cfg: dict) -> None:
        import zenoh

        self.zenoh = zenoh
        self.cfg = cfg
        self.prefix = cfg["service"]["prefix"]
        self.grace_s = float(cfg["service"]["app_grace_s"])
        self.namer = protocol.Namer(cfg["service"].get("naming", "readable"))
        self.lead_s = cfg["output"]["buffer_ms"] / 1000
        self.session = open_session(cfg)
        self.tts = engines.build_tts(cfg)
        self.tts_lock = threading.Lock()
        self.doublets: dict[str, DoubletRunner] = {}
        self.lock = threading.Lock()
        self._decls = [
            self.session.declare_queryable(protocol.register_key(self.prefix), self._on_register),
            self.session.declare_queryable(protocol.list_key(self.prefix), self._on_list),
            self.session.liveliness().declare_subscriber(protocol.live_pattern(self.prefix),
                                                         self._on_liveliness, history=True),
        ]
        self._stop = threading.Event()
        self._janitor = threading.Thread(target=self._sweep, daemon=True)
        self._janitor.start()

    # --- registry -----------------------------------------------------------------

    def _on_register(self, query) -> None:
        request = protocol.loads(query.payload)
        name = str(request.get("name") or "app")[:64]
        with self.lock:
            did = self.namer.allocate(name)
        d = Doublet(did, name, protocol.new_token(), protocol.Keys(self.prefix, did))
        runner = DoubletRunner(self, d)
        with self.lock:
            self.doublets[d.id] = runner
        reply = {
            "version": protocol.VERSION, "id": d.id, "name": name, "app_token": d.app_token,
            "keys": d.keys.as_dict(),
            "formats": {"audio_in": protocol.AUDIO_IN_ENCODING,
                        "audio_out": protocol.audio_out_encoding(self.tts.sample_rate),
                        "text_out": "application/json", "text_in": "text/plain"},
            "liveliness_grace_s": self.grace_s,
            "server_public_key": None,  # reserved for the future crypto layer
        }
        query.reply(query.key_expr, protocol.dumps(reply),
                    encoding=self.zenoh.Encoding.APPLICATION_JSON)
        log.info("registered doublet %s (%s)", d.id, name)

    def _on_list(self, query) -> None:
        with self.lock:
            listing = [{"id": r.d.id, "name": r.d.name, "terminal": r.d.terminal,
                        "state": r.controller.state.value, "created": r.d.created,
                        "keys": r.d.keys.as_dict()} for r in self.doublets.values()]
        query.reply(query.key_expr, protocol.dumps({"doublets": listing}),
                    encoding=self.zenoh.Encoding.APPLICATION_JSON)

    def _on_liveliness(self, sample) -> None:
        parsed = protocol.parse_live_key(self.prefix, str(sample.key_expr))
        if not parsed:
            return
        did, role = parsed
        with self.lock:
            runner = self.doublets.get(did)
        if runner is None:
            return
        alive = sample.kind == self.zenoh.SampleKind.PUT
        runner.post(("live", role, alive))

    def _sweep(self) -> None:
        """Close doublets whose app never declared its liveliness token."""
        while not self._stop.wait(1.0):
            now = time.time()
            with self.lock:
                stale = [r for r in self.doublets.values()
                         if not r.d.app_seen and now - r.d.created > self.grace_s]
            for runner in stale:
                runner.post(("close", "the app never declared its liveliness token"))

    def forget(self, did: str) -> None:
        with self.lock:
            self.doublets.pop(did, None)

    def put(self, key: str, payload, encoding: str | None = None) -> None:
        enc = self.zenoh.Encoding(encoding) if encoding else None
        self.session.put(key, payload, encoding=enc)

    def close(self) -> None:
        self._stop.set()
        with self.lock:
            runners = list(self.doublets.values())
        for r in runners:
            r.post(("close", "server shutting down"))
            r.join(2)
        for decl in self._decls:
            decl.undeclare()
        self.tts.close()
        self.session.close()


class DoubletRunner:
    """One doublet: its own state machine, STT stream and worker thread."""

    def __init__(self, service: VoiceService, d: Doublet) -> None:
        self.s = service
        self.d = d
        self.controller = Controller(service.cfg["control"]["after_reply"])
        self.stt = engines.build_stt(service.cfg)
        self.inbox: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.player: threading.Thread | None = None
        self.spoken: list[str] = []
        self._pending = ""
        self.last_partial = 0.0
        self.dropped_frames = 0
        k, z = d.keys, service.session
        self._decls = [
            z.declare_queryable(k.attach, lambda q: self.post(("attach", q))),
            z.declare_queryable(k.detach, lambda q: self.post(("detach", q))),
            z.declare_subscriber(k.control, lambda s: self.post(("control", s))),
            z.declare_subscriber(k.audio_in, lambda s: self.post(("audio", s))),
            z.declare_subscriber(k.text_in, lambda s: self.post(("text", s))),
        ]
        self.worker = threading.Thread(target=self._run, name=f"doublet-{d.id}", daemon=True)
        self.worker.start()

    def post(self, item) -> None:
        self.inbox.put(item)

    def join(self, timeout: float) -> None:
        self.worker.join(timeout)

    # --- events -----------------------------------------------------------------

    def event(self, name: str, **data) -> None:
        msg = {"event": name, "doublet": self.d.id, "state": self.controller.state.value,
               "t": time.time(), **data}
        self.s.put(self.d.keys.events, protocol.dumps(msg), "application/json")

    # --- worker: everything for this doublet happens here, in order ---------------

    def _run(self) -> None:
        handlers = {"attach": self._attach, "detach": self._detach, "control": self._control,
                    "audio": self._audio, "text": self._text, "live": self._live,
                    "played": self._played, "close": self._close}
        while not self.d.closed:
            kind, *args = self.inbox.get()
            try:
                handlers[kind](*args)
            except Exception:  # noqa: BLE001
                log.exception("doublet %s: error handling %s", self.d.id, kind)

    def _holder(self, token: str) -> bool:
        return bool(self.d.terminal_token) and token == self.d.terminal_token

    def _attach(self, query) -> None:
        req = protocol.loads(query.payload)
        terminal = str(req.get("terminal") or "terminal")[:64]
        if self.d.terminal_token:
            reply = {"ok": False, "reason": "locked", "held_by": self.d.terminal}
        else:
            self.d.terminal, self.d.terminal_token = terminal, protocol.new_token()
            reply = {"ok": True, "terminal_token": self.d.terminal_token,
                     "audio_out": protocol.audio_out_encoding(self.s.tts.sample_rate)}
            self.event("attached", terminal=terminal)
        query.reply(query.key_expr, protocol.dumps(reply),
                    encoding=self.s.zenoh.Encoding.APPLICATION_JSON)

    def _detach(self, query) -> None:
        ok = self._holder(protocol.loads(query.payload).get("terminal_token", ""))
        if ok:
            self._release("detached")
        query.reply(query.key_expr, protocol.dumps({"ok": ok}),
                    encoding=self.s.zenoh.Encoding.APPLICATION_JSON)

    def _release(self, why: str) -> None:
        terminal = self.d.terminal
        self._stop_playback()
        self.stt.finalize()  # discard any half-heard utterance
        self.controller.state = State.IDLE
        self.d.terminal = self.d.terminal_token = None
        self.event("released", terminal=terminal, reason=why)

    def _live(self, role: str, alive: bool) -> None:
        if role == "app":
            if alive:
                self.d.app_seen = True
            elif self.d.app_seen:
                self._close("the app went away")
        elif role == "term" and not alive and self.d.terminal_token:
            self._release("terminal went away")

    def _control(self, sample) -> None:
        if not self._holder(protocol.attachment_text(sample)):
            return  # locked to publish: only the attached terminal
        msg = protocol.loads(sample.payload)
        if msg.get("op", "press") == "press":
            self.event("press")
            self._apply(self.controller.press())

    def _audio(self, sample) -> None:
        if not self._holder(protocol.attachment_text(sample)):
            self.dropped_frames += 1
            return
        if self.controller.state is not State.LISTENING:
            return  # half-duplex: the mic only counts while listening
        self.stt.feed(sample.payload.to_bytes())
        now = time.monotonic()
        if hasattr(self.stt, "update") and now - self.last_partial >= PARTIAL_EVERY_S:
            self.last_partial = now
            partial = self.stt.update()
            if partial:
                self.event("partial", text=partial)

    def _text(self, sample) -> None:
        if protocol.attachment_text(sample) != self.d.app_token:
            return  # locked to publish: only the app that owns this doublet
        text = sample.payload.to_string().strip()
        if not text:
            return
        actions = self.controller.audio_ready()
        if not actions:
            self.event("reply_dropped", reason=f"not expecting a reply in state "
                                               f"{self.controller.state.value}")
            return
        self.event("reply_received")
        self._pending = text
        self._apply(actions)

    def _apply(self, actions: list[Action]) -> None:
        for action in actions:
            if action is Action.OPEN_MIC:
                self.last_partial = 0.0
                self.event("listening")
            elif action is Action.CLOSE_MIC_AND_SEND:
                text = self.stt.finalize()
                self.event("thinking")
                msg = {"text": text, "doublet": self.d.id, "terminal": self.d.terminal,
                       "t": time.time()}
                self.s.put(self.d.keys.text_out, protocol.dumps(msg), "application/json")
            elif action is Action.CANCEL_REPLY:
                self.event("reply_cancelled")
            elif action is Action.START_PLAYBACK:
                self._start_playback(self._pending)
            elif action is Action.INTERRUPT_PLAYBACK:
                spoken = " ".join(self.spoken)
                self._stop_playback()
                self.event("interrupted", spoken_text=spoken)

    # --- playback ---------------------------------------------------------------------

    def _start_playback(self, text: str) -> None:
        self.cancel.clear()
        self.spoken = []
        self.event("speaking")
        self.player = threading.Thread(target=self._play, args=(text,), daemon=True)
        self.player.start()

    def _stop_playback(self) -> None:
        self.cancel.set()
        if self.player and self.player.is_alive() and threading.current_thread() is not self.player:
            self.player.join(5)

    def _play(self, text: str) -> None:
        """Synthesize sentence by sentence; send audio paced to real time plus a small lead."""
        tts, lead = self.s.tts, self.s.lead_s
        deadline = time.monotonic()  # when the terminal will have played everything sent so far
        for sentence in tts.sentences(text):
            if self.cancel.is_set():
                return
            with self.s.tts_lock:
                pcm = tts.synthesize(sentence)
            rate = tts.sample_rate
            chunk = rate * 2 * CHUNK_MS // 1000
            enc = protocol.audio_out_encoding(rate)
            for i in range(0, len(pcm), chunk):
                wait = deadline - lead - time.monotonic()  # keep at most `lead` buffered there
                if wait > 0 and self.cancel.wait(wait):
                    return
                if self.cancel.is_set():
                    return
                piece = pcm[i:i + chunk]
                self.s.put(self.d.keys.audio_out, piece, enc)
                deadline = max(deadline, time.monotonic()) + len(piece) / 2 / rate
            self.spoken.append(sentence)
        remaining = deadline - time.monotonic()
        if remaining > 0 and self.cancel.wait(remaining):
            return
        self.post(("played",))

    def _played(self) -> None:
        if self.controller.state is not State.SPEAKING or self.cancel.is_set():
            return
        self.event("playback_done")
        self._apply(self.controller.playback_done())
        if self.controller.state is State.IDLE:
            self.event("idle")

    # --- shutdown -------------------------------------------------------------------------

    def _close(self, reason: str) -> None:
        if self.d.closed:
            return
        self._stop_playback()
        self.event("closed", reason=reason)
        for decl in self._decls:
            decl.undeclare()
        self.stt.close()
        self.d.closed = True
        self.s.forget(self.d.id)
        log.info("closed doublet %s: %s", self.d.id, reason)
