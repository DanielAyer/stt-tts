"""Speech engines as plugins: STT (audio -> text) and TTS (text -> audio).

Each engine is described by an EngineSpec: its pip packages, how to fetch its
models, and the adapter class. The installer and `stt-tts setup engines`
install only the packages of the engines selected, so a machine never carries
an engine it doesn't use. "none" keeps the module agnostic: a stub that
needs nothing, for bringing your own engine.

Engine adapters import their packages lazily, so the core runs without them.

To add an engine: write an adapter (subclass STTEngine or TTSEngine), describe
it with an EngineSpec, and add it to SPECS below. It then appears in the
installer, in `stt-tts setup engines` and in config.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .base import STTEngine, TTSEngine  # noqa: F401  (re-exported)

Progress = Callable[[str], None]


class EngineUnavailable(RuntimeError):
    """The selected engine's packages or models are not installed."""


@dataclass(frozen=True)
class EngineSpec:
    name: str              # config value, e.g. "moonshine"
    kind: str              # "stt" or "tts"
    label: str             # shown in the installer
    summary: str           # one line on why you'd pick it
    adapter: str           # "module:Class" under stt_tts.engines
    packages: tuple[str, ...] = ()
    defaults: dict = field(default_factory=dict)  # per-engine options in config
    fetch: str = ""        # "module:function(cfg, progress)" that downloads models


SPECS: dict[tuple[str, str], EngineSpec] = {}


def _add(spec: EngineSpec) -> None:
    SPECS[(spec.kind, spec.name)] = spec


_add(EngineSpec(
    "moonshine", "stt", "Moonshine (streaming, CPU)",
    "Low-latency streaming English transcription on the CPU.",
    "moonshine:MoonshineSTT", ("moonshine-voice>=0.1.6",),
    {"model": "small-streaming", "language": "en"}, "moonshine:fetch_stt"))
_add(EngineSpec(
    "none", "stt", "None (bring your own)",
    "No engine installed; a stub that returns placeholder text.",
    "stub:StubSTT"))

_add(EngineSpec(
    "moonshine", "tts", "Moonshine TTS",
    "Same package as Moonshine STT; Kokoro, Piper and ZipVoice voices, voice cloning.",
    "moonshine:MoonshineTTS", ("moonshine-voice>=0.1.6",),
    {"voice": "kokoro_af_heart", "language": "en-us", "speed": 1.0}, "moonshine:fetch_tts"))
_add(EngineSpec(
    "kokoro", "tts", "Kokoro (kokoro-onnx)",
    "Kokoro-82M on ONNX Runtime; supports blended voices.",
    "kokoro:KokoroTTS", ("kokoro-onnx>=0.4",),
    {"voice": "af_heart", "speed": 1.0}, "kokoro:fetch"))
_add(EngineSpec(
    "none", "tts", "None (bring your own)",
    "No engine installed; a stub that returns silence.",
    "stub:StubTTS"))

ALIASES = {"stub": "none"}
DEFAULT = {"stt": "moonshine", "tts": "moonshine"}


def names(kind: str) -> list[str]:
    return [n for (k, n) in SPECS if k == kind]


def spec(kind: str, name: str) -> EngineSpec:
    name = ALIASES.get(name, name)
    try:
        return SPECS[(kind, name)]
    except KeyError:
        raise ValueError(f"Unknown {kind.upper()} engine '{name}'. "
                         f"Available: {', '.join(names(kind))}") from None


def selected(cfg: dict) -> dict[str, EngineSpec]:
    return {"stt": spec("stt", cfg["stt"]["engine"]), "tts": spec("tts", cfg["tts"]["engine"])}


def packages_for(specs) -> list[str]:
    """pip requirements for the given specs, without duplicates, in order."""
    return list(dict.fromkeys(p for s in specs for p in s.packages))


def options(cfg: dict, s: EngineSpec) -> dict:
    """This engine's options: its defaults overlaid with config [stt.<name>] / [tts.<name>]."""
    return {**s.defaults, **cfg[s.kind].get(s.name, {})}


def ensure_options(cfg: dict) -> None:
    """Write default option tables for the selected engines into cfg, so they're editable."""
    for s in selected(cfg).values():
        if s.defaults:
            cfg[s.kind][s.name] = options(cfg, s)


def _load(ref: str):
    module, _, attr = ref.partition(":")
    return getattr(importlib.import_module(f"{__name__}.{module}"), attr)


def model_dir(cfg: dict, s: EngineSpec) -> Path:
    return Path(cfg["paths"]["model_dir"]).expanduser() / s.name


def fetch(cfg: dict, s: EngineSpec, progress: Progress = print) -> None:
    """Download this engine's models into the data store. Needs its packages installed."""
    if not s.fetch:
        return
    try:
        _load(s.fetch)(cfg, progress)
    except ImportError as exc:
        raise EngineUnavailable(f"{s.label}: packages not installed ({exc.name}). "
                                "Run: stt-tts setup engines") from exc


def _build(cfg: dict, kind: str):
    s = spec(kind, cfg[kind]["engine"])
    try:
        cls = _load(s.adapter)
        return cls(cfg, options(cfg, s))
    except ImportError as exc:
        raise EngineUnavailable(f"{kind.upper()} engine '{s.name}' is selected but its packages "
                                f"aren't installed ({exc.name}). Run: stt-tts setup engines") from exc


def build_stt(cfg: dict) -> STTEngine:
    return _build(cfg, "stt")


def build_tts(cfg: dict) -> TTSEngine:
    return _build(cfg, "tts")
