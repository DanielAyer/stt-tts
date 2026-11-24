"""The stt-tts Zenoh protocol: key layout and message formats.

stt-tts runs as a voice service on Zenoh. An app that wants a voice (an LLM,
a script) asks for a *doublet*: an audio side and a text side, bridged by
stt-tts. A terminal (phone, standalone device) lists the open doublets and
attaches its mic and speaker to the audio side of one.

Doublets are locked to publish and open to listen:
  - only the app that requested a doublet may publish its text/in
    (it proves this with the app_token it was given privately at registration)
  - only the one attached terminal may publish audio/in and control
    (it proves this with the terminal_token it was given privately at attach)
  - anyone may subscribe to anything

Tokens travel in each message's attachment. They are unguessable, but they are
not encrypted on the wire: use TLS on the Zenoh link to keep them private. The
planned crypto layer will replace them with signatures (fields reserved below).

Keys (prefix "stt-tts", configurable as service.prefix). <id> is the doublet id
the server assigns at registration; clients always use the key names it returns
and never build them, so the naming scheme can change on the server alone.

  service.naming = "readable" (default): <id> = <app>/<n>, e.g. "echo/1".
      <app> is the requested name cleaned into a key-safe slug; <n> counts up
      from 1 per app and is never reused while the server runs.
  service.naming = "opaque": <id> = a random UUID (hex), which reveals nothing.

  <p>/register                  queryable  app -> server   request a doublet
  <p>/doublets                  queryable  anyone          list open doublets
  <p>/live/<id>/app             liveliness app holds it; when it vanishes, the doublet closes
  <p>/live/<id>/term            liveliness attached terminal holds it; when it vanishes, the lock frees
  <p>/<id>/attach               queryable  terminal        take the publish lock
  <p>/<id>/detach               queryable  terminal        release it
  <p>/<id>/control              pub        terminal        button presses
  <p>/<id>/audio/in             pub        terminal        16 kHz mono 16-bit PCM frames
  <p>/<id>/audio/out            pub        server          TTS audio (rate in the encoding)
  <p>/<id>/text/out             pub        server          transcripts (JSON)
  <p>/<id>/text/in              pub        app             text to speak (plain text)
  <p>/<id>/events               pub        server          state changes (JSON)
"""

from __future__ import annotations

import json
import re
import secrets
import uuid
from dataclasses import dataclass

VERSION = 2
DEFAULT_PREFIX = "stt-tts"
AUDIO_IN_ENCODING = "audio/L16;rate=16000"
NAMING_MODES = ("readable", "opaque")
# First key segments the service uses itself: an app slug may not take them.
RESERVED = frozenset({"register", "doublets", "live"})
_SLUG_MAX = 32


def audio_out_encoding(rate: int) -> str:
    return f"audio/L16;rate={rate}"


def rate_from_encoding(encoding: str, default: int = 24000) -> int:
    for part in str(encoding).split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key == "rate" and value.isdigit():
            return int(value)
    return default


def new_token() -> str:
    return secrets.token_urlsafe(24)


def app_slug(name: str) -> str:
    """Clean an app name into one key-safe segment: 'My App!' -> 'my-app'.

    Keeps a-z, 0-9, '-' and '_'; anything else becomes '-'. Zenoh wildcards
    ('*', '$', '?', '#') and '/' can't survive, so a name can never widen a
    subscription or add key levels. Reserved segments get '-app' appended.
    """
    slug = re.sub(r"[^a-z0-9_-]+", "-", str(name).lower()).strip("-_")[:_SLUG_MAX].strip("-_")
    slug = slug or "app"
    return f"{slug}-app" if slug in RESERVED else slug


class Namer:
    """Assigns doublet ids. Not thread-safe: call it under the service's lock."""

    def __init__(self, mode: str = "readable") -> None:
        if mode not in NAMING_MODES:
            raise ValueError(f"service.naming must be one of {', '.join(NAMING_MODES)}; "
                             f"got {mode!r}")
        self.mode = mode
        self._last: dict[str, int] = {}

    def allocate(self, name: str) -> str:
        if self.mode == "opaque":
            return uuid.uuid4().hex
        slug = app_slug(name)
        n = self._last.get(slug, 0) + 1  # never reused: a stale terminal can't hit a new app
        self._last[slug] = n
        return f"{slug}/{n}"


@dataclass(frozen=True)
class Keys:
    prefix: str
    id: str  # "echo/1" (readable) or a UUID hex (opaque)

    @property
    def base(self) -> str:
        return f"{self.prefix}/{self.id}"

    def __getattr__(self, name: str) -> str:  # attach, detach, control, events
        if name in ("attach", "detach", "control", "events"):
            return f"{self.base}/{name}"
        raise AttributeError(name)

    @property
    def audio_in(self) -> str:
        return f"{self.base}/audio/in"

    @property
    def audio_out(self) -> str:
        return f"{self.base}/audio/out"

    @property
    def text_out(self) -> str:
        return f"{self.base}/text/out"

    @property
    def text_in(self) -> str:
        return f"{self.base}/text/in"

    @property
    def live_app(self) -> str:
        return f"{self.prefix}/live/{self.id}/app"

    @property
    def live_term(self) -> str:
        return f"{self.prefix}/live/{self.id}/term"

    def as_dict(self) -> dict:
        return {"attach": self.attach, "detach": self.detach, "control": self.control,
                "audio_in": self.audio_in, "audio_out": self.audio_out,
                "text_out": self.text_out, "text_in": self.text_in, "events": self.events,
                "live_app": self.live_app, "live_term": self.live_term}


def register_key(prefix: str) -> str:
    return f"{prefix}/register"


def list_key(prefix: str) -> str:
    return f"{prefix}/doublets"


def live_pattern(prefix: str) -> str:
    return f"{prefix}/live/**"


def parse_live_key(prefix: str, key: str) -> tuple[str, str] | None:
    """'stt-tts/live/echo/1/app' -> ('echo/1', 'app'). Ids may contain '/'."""
    head = f"{prefix}/live/"
    if not key.startswith(head):
        return None
    did, _, role = key[len(head):].rpartition("/")
    if not did or role not in ("app", "term"):
        return None
    return did, role


def loads(payload) -> dict:
    """Parse a JSON payload (ZBytes, bytes or str); {} if empty or invalid."""
    if payload is None:
        return {}
    try:
        text = payload.to_string() if hasattr(payload, "to_string") else (
            payload.decode() if isinstance(payload, bytes) else str(payload))
        value = json.loads(text) if text.strip() else {}
        return value if isinstance(value, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def dumps(message: dict) -> str:
    return json.dumps(message, separators=(",", ":"))


def attachment_text(sample) -> str:
    att = getattr(sample, "attachment", None)
    if att is None:
        return ""
    try:
        return att.to_string()
    except Exception:  # noqa: BLE001
        return ""
