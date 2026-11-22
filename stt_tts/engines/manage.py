"""Installing engines: pip packages into the module's venv, then models.

Used by the installer (running on the system Python, installing into the
venv) and by `stt-tts setup engines` (running inside the venv). Both go
through subprocesses so they behave the same.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from . import EngineSpec, packages_for

Progress = Callable[[str], None]


class StepFailed(RuntimeError):
    def __init__(self, step: str, detail: str, shown: bool = False) -> None:
        super().__init__(f"{step} failed: {detail}")
        self.step = step
        self.detail = detail
        self.shown = shown  # already printed as it happened


def _tail(text: str, lines: int = 6) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def in_venv(python: Path) -> bool:
    """True if this interpreter belongs to a virtual environment (safe to pip into)."""
    result = subprocess.run([str(python), "-c", "import sys; print(sys.prefix != sys.base_prefix)"],
                            capture_output=True, text=True)
    return result.stdout.strip() == "True"


def install_packages(python: Path, specs: list[EngineSpec], progress: Progress) -> None:
    packages = packages_for(specs)
    if not packages:
        return
    if not in_venv(python):
        raise StepFailed("Package install", "no virtual environment for the module "
                         "(sudo apt install python3-venv, then rerun the installer)")
    progress("Installing engine packages: " + ", ".join(packages))
    result = subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check",
                             "-q", *packages], capture_output=True, text=True)
    if result.returncode != 0:
        raise StepFailed("Package install", _tail(result.stderr or result.stdout))


def fetch_models(python: Path, app_dir: Path, config_file: Path, kinds: list[str],
                 progress: Progress) -> None:
    """Run `stt-tts engines fetch` inside the venv so the engine packages are importable."""
    env = dict(os.environ, PYTHONPATH=str(app_dir), STT_TTS_CONFIG=str(config_file))
    for kind in kinds:
        proc = subprocess.Popen([str(python), "-m", "stt_tts", "engines", "fetch", "--kind", kind],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
                                cwd=str(app_dir))  # use the installed copy, not the caller's
        output = []
        for line in proc.stdout:
            line = line.rstrip()
            output.append(line)
            progress(line)
        if proc.wait() != 0:
            failed = [ln for ln in output if ln.startswith("Download failed:")]
            raise StepFailed(f"{kind.upper()} model download",
                             failed[-1] if failed else _tail("\n".join(output), 3), shown=True)


def current_python() -> Path:
    return Path(sys.executable)
