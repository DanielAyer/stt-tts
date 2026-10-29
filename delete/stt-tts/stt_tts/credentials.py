"""Credential storage for outputs that need one (API keys, tokens).

Secrets never go in config.toml. Config only stores the secret's *name*.
Storage, in order of preference:
  1. The system keyring (optional `keyring` package: GNOME Keyring, KWallet, ...)
  2. secrets.json next to the config, created with owner-only permissions (0600)

On a headless server there is usually no keyring, so the file is used.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import config

SERVICE = "stt-tts"


def _keyring():
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring
    except ImportError:
        return None
    backend = keyring.get_keyring()
    return None if isinstance(backend, FailKeyring) else keyring


def _file() -> Path:
    return config.secrets_path()


def _read_file() -> dict[str, str]:
    path = _file()
    if not path.exists():
        return {}
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise PermissionError(f"{path} is readable by others (mode {oct(mode)}). Run: chmod 600 {path}")
    return json.loads(path.read_text())


def _write_file(data: dict[str, str]) -> None:
    path = _file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    os.chmod(path, 0o600)


def backend_name() -> str:
    return "system keyring" if _keyring() else f"owner-only file ({_file()})"


def set_secret(name: str, value: str) -> None:
    kr = _keyring()
    if kr:
        kr.set_password(SERVICE, name, value)
        return
    data = _read_file()
    data[name] = value
    _write_file(data)


def get_secret(name: str) -> str | None:
    kr = _keyring()
    if kr:
        return kr.get_password(SERVICE, name)
    return _read_file().get(name)


def delete_secret(name: str) -> None:
    kr = _keyring()
    if kr:
        try:
            kr.delete_password(SERVICE, name)
        except Exception:  # noqa: BLE001  (missing entry)
            pass
        return
    data = _read_file()
    if data.pop(name, None) is not None:
        _write_file(data)


def delete_all(names: list[str]) -> None:
    for name in names:
        delete_secret(name)
    path = _file()
    if path.exists() and not _read_file():
        path.unlink()


def mask(value: str | None) -> str:
    if not value:
        return "(none)"
    return value[:3] + "..." + value[-2:] if len(value) > 8 else "***"
