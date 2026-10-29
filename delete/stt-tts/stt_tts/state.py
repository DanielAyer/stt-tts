"""Single-control state machine.

One context-sensitive button drives every transition except "audio ready".

    Idle      --press-->                Listening
    Listening --press (send)-->         Thinking
    Thinking  --press (cancel reply)--> Listening
    Thinking  --audio ready-->          Speaking
    Speaking  --press (interrupt)-->    Listening
    Speaking  --playback done-->        Idle or Listening (config: control.after_reply)

The mic is open only in Listening (half-duplex).
"""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"


class Action(str, Enum):
    OPEN_MIC = "open_mic"
    CLOSE_MIC_AND_SEND = "close_mic_and_send"
    CANCEL_REPLY = "cancel_reply"
    START_PLAYBACK = "start_playback"
    INTERRUPT_PLAYBACK = "interrupt_playback"


class Controller:
    def __init__(self, after_reply: str = "idle") -> None:
        if after_reply not in ("idle", "listening"):
            raise ValueError("after_reply must be 'idle' or 'listening'")
        self.after_reply = after_reply
        self.state = State.IDLE

    @property
    def mic_open(self) -> bool:
        return self.state is State.LISTENING

    def press(self) -> list[Action]:
        if self.state is State.IDLE:
            self.state = State.LISTENING
            return [Action.OPEN_MIC]
        if self.state is State.LISTENING:
            self.state = State.THINKING
            return [Action.CLOSE_MIC_AND_SEND]
        if self.state is State.THINKING:
            self.state = State.LISTENING
            return [Action.CANCEL_REPLY, Action.OPEN_MIC]
        # SPEAKING
        self.state = State.LISTENING
        return [Action.INTERRUPT_PLAYBACK, Action.OPEN_MIC]

    def audio_ready(self) -> list[Action]:
        if self.state is not State.THINKING:
            return []  # reply arrived after a cancel: drop it
        self.state = State.SPEAKING
        return [Action.START_PLAYBACK]

    def playback_done(self) -> list[Action]:
        if self.state is not State.SPEAKING:
            return []
        if self.after_reply == "listening":
            self.state = State.LISTENING
            return [Action.OPEN_MIC]
        self.state = State.IDLE
        return []
