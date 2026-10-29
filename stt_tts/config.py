"""Configuration: defaults, load, save, and dotted-key get/set.

The config file is TOML. Every user-selectable endpoint (input sources,
output destinations, engines, Zenoh keys, paths) lives here, so a
command-line install can take all defaults and be changed later with:

    stt-tts config set zenoh.keys.out_text my/robot/transcript
"""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any

HOME = Path.home()


def _xdg(var: str, fallback: Path) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else fallback


def default_spool_dir() -> Path:
    """Prefer the dedicated tmpfs mount; else the per-user runtime dir (tmpfs on systemd)."""
    dedicated = Path("/mnt/voice-spool")
    if dedicated.is_dir() and os.access(dedicated, os.W_OK):
        return dedicated
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "stt-tts"
    return Path("/tmp") / f"stt-tts-{os.getuid()}"


def default_paths() -> dict[str, str]:
    data = _xdg("XDG_DATA_HOME", HOME / ".local" / "share") / "stt-tts"
    return {
        "app_dir": str(HOME / ".local" / "opt" / "stt-tts"),
        "data_dir": str(data),
        "model_dir": str(data / "models"),
        "transcript_dir": str(data / "transcripts"),
        "log_dir": str(data / "logs"),
        "spool_dir": str(default_spool_dir()),
    }


def config_path() -> Path:
    override = os.environ.get("STT_TTS_CONFIG")
    if override:
        return Path(override)
    return _xdg("XDG_CONFIG_HOME", HOME / ".config") / "stt-tts" / "config.toml"


DEFAULTS: dict[str, Any] = {
    "paths": {},  # filled from default_paths() at load time
    "control": {
        # Where button presses come from: "stub_button", "keyboard", "zenoh"
        "adapter": "stub_button",
        # After a reply finishes playing: "idle" or "listening"
        "after_reply": "idle",
    },
    "input": {
        # Audio sources, all normalized to 16 kHz mono PCM: "stub_mic", "file", "zenoh"
        "adapters": ["stub_mic"],
        "sample_rate": 16000,
    },
    "output": {
        # Audio destinations for TTS: "stub_speaker", "file", "zenoh"
        "adapters": ["stub_speaker"],
        # Small buffer keeps interrupts immediate
        "buffer_ms": 300,
    },
    "text": {
        # Where transcripts go: "stdout", "file", "zenoh"
        "sinks": ["stdout"],
        # Where text to speak comes from: "stdin", "zenoh"
        "sources": ["stdin"],
    },
    "stt": {
        "engine": "stub",  # planned: "moonshine"
        "model": "moonshine-small-streaming",
    },
    "tts": {
        "engine": "stub",  # planned: "kokoro"
        "model": "kokoro-82m-int8",
        "voice": "af_heart",
        "sample_rate": 24000,
    },
    "zenoh": {
        "enabled": False,
        "mode": "client",
        "connect": ["tcp/localhost:7447"],
        "keys": {
            "in_audio": "voice/in/audio",
            "in_control": "voice/in/control",
            "out_text": "voice/out/text",
            "tts_text": "voice/tts/text",
            "tts_audio": "voice/tts/audio",
            "events": "voice/events",
        },
    },
}


def defaults() -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULTS)
    cfg["paths"] = default_paths()
    return cfg


def _merge(base: dict, override: dict) -> dict:
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load(path: Path | None = None) -> dict[str, Any]:
    """Defaults overlaid with the user's file, so new keys always have a value."""
    path = path or config_path()
    cfg = defaults()
    if path.exists():
        with path.open("rb") as fh:
            _merge(cfg, tomllib.load(fh))
    return cfg


# --- Minimal TOML writer (stdlib has a reader only) -------------------------

def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"Unsupported config value: {value!r}")


def dumps(cfg: dict[str, Any]) -> str:
    lines: list[str] = ["# stt-tts configuration", ""]

    def section(name: str, table: dict[str, Any]) -> None:
        scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
        tables = {k: v for k, v in table.items() if isinstance(v, dict)}
        if scalars:
            lines.append(f"[{name}]")
            for key, value in scalars.items():
                lines.append(f"{key} = {_toml_value(value)}")
            lines.append("")
        for key, sub in tables.items():
            section(f"{name}.{key}", sub)

    for name, table in cfg.items():
        section(name, table)
    return "\n".join(lines)


def save(cfg: dict[str, Any], path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(cfg))
    return path


def get(cfg: dict[str, Any], dotted: str) -> Any:
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(dotted)
        node = node[part]
    return node


def set_value(cfg: dict[str, Any], dotted: str, raw: str) -> Any:
    """Set a dotted key, coercing the string to the existing value's type."""
    *parents, leaf = dotted.split(".")
    node = cfg
    for part in parents:
        node = node.setdefault(part, {})
    current = node.get(leaf)
    if isinstance(current, bool):
        value: Any = raw.strip().lower() in ("1", "true", "yes", "on")
    elif isinstance(current, int):
        value = int(raw)
    elif isinstance(current, float):
        value = float(raw)
    elif isinstance(current, list):
        value = [item.strip() for item in raw.split(",") if item.strip()]
    else:
        value = raw
    node[leaf] = value
    return value
