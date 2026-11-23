#!/usr/bin/env bash
# stt-tts installer launcher.
# Finds a Python >= 3.11 (stdlib only, nothing is installed system-wide)
# and hands off to the Python installer, which picks GUI or command line.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

pick_python() {
  for candidate in python3 python3.13 python3.12 python3.11; do
    if command -v "$candidate" >/dev/null 2>&1 &&
       "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      # Prefer one that has Tk when a display exists, so the GUI can open.
      if [[ -n "${DISPLAY:-}" ]] && ! "$candidate" -c 'import tkinter' 2>/dev/null; then
        FALLBACK="${FALLBACK:-$candidate}"
        continue
      fi
      echo "$candidate"
      return 0
    fi
  done
  if [[ -n "${FALLBACK:-}" ]]; then echo "$FALLBACK"; return 0; fi
  return 1
}

if ! PY="$(pick_python)"; then
  echo "stt-tts needs Python 3.11 or newer."
  echo "On Ubuntu: sudo apt install python3 python3-venv"
  exit 1
fi

cd "$HERE"
exec "$PY" -m installer "$@"
