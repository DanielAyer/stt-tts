"""The install itself, shared by the GUI and command-line front ends.

Every file and directory the installer creates is recorded in a Manifest.
If the user stops the install, or anything fails, rollback() removes exactly
those paths (and restores a config it replaced), leaving pre-existing
directories alone.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from stt_tts import config as vm_config  # noqa: E402

Progress = Callable[[str], None]


@dataclass
class Plan:
    cfg: dict
    install_zenoh: bool = False
    bin_dir: Path = field(default_factory=lambda: Path.home() / ".local" / "bin")
    config_file: Path = field(default_factory=vm_config.config_path)

    @property
    def app_dir(self) -> Path:
        return Path(self.cfg["paths"]["app_dir"]).expanduser()


class Manifest:
    def __init__(self) -> None:
        self.created: list[Path] = []
        self.backups: list[tuple[Path, Path]] = []  # (original, backup)

    def mkdir(self, path: Path) -> None:
        """Create path and any missing parents, remembering only what was new."""
        missing = []
        p = path.expanduser().absolute()
        while not p.exists():
            missing.append(p)
            p = p.parent
        for d in reversed(missing):
            d.mkdir()
            self.created.append(d)

    def file(self, path: Path) -> None:
        self.created.append(path)

    def backup(self, path: Path) -> None:
        if path.exists():
            bak = path.with_suffix(path.suffix + ".bak")
            shutil.copy2(path, bak)
            self.backups.append((path, bak))
            self.created.append(bak)

    def rollback(self, progress: Progress = print) -> None:
        for original, bak in self.backups:
            if bak.exists():
                shutil.copy2(bak, original)
                progress(f"restored {original}")
        for path in reversed(self.created):
            try:
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.is_dir():
                    shutil.rmtree(path)
                else:
                    continue
                progress(f"removed {path}")
            except OSError as exc:
                progress(f"could not remove {path}: {exc}")
        self.created.clear()
        self.backups.clear()

    def to_json(self) -> str:
        return json.dumps({"created": [str(p) for p in self.created]}, indent=2)


LAUNCHER = """#!/bin/sh
# stt-tts launcher (written by the installer)
export PYTHONPATH="{app}${{PYTHONPATH:+:$PYTHONPATH}}"
exec "{python}" -m stt_tts "$@"
"""


def install(plan: Plan, manifest: Manifest, progress: Progress = print) -> None:
    """Run every step; on any error or Ctrl-C, roll back and re-raise."""
    try:
        _install(plan, manifest, progress)
    except BaseException:
        progress("Install failed or was stopped; cleaning up.")
        manifest.rollback(progress)
        raise


def _install(plan: Plan, manifest: Manifest, progress: Progress) -> None:
    paths = plan.cfg["paths"]
    app = plan.app_dir

    progress("Creating directories")
    for key in ("app_dir", "data_dir", "model_dir", "transcript_dir", "log_dir", "spool_dir"):
        manifest.mkdir(Path(paths[key]).expanduser())

    progress(f"Copying module to {app}")
    target = app / "stt_tts"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(REPO_ROOT / "stt_tts", target,
                    ignore=shutil.ignore_patterns("__pycache__"))
    manifest.file(target)
    for doc in ("README.md", "LICENSE"):
        if (REPO_ROOT / doc).exists():
            shutil.copy2(REPO_ROOT / doc, app / doc)
            manifest.file(app / doc)

    python = _make_venv(app, manifest, progress)

    if plan.install_zenoh:
        progress("Installing optional Zenoh support (pip install eclipse-zenoh)")
        result = subprocess.run([str(python), "-m", "pip", "install", "eclipse-zenoh"],
                                capture_output=True, text=True)
        if result.returncode != 0:
            progress("  Zenoh install failed (network or pip missing); continuing without it.")
            plan.cfg["zenoh"]["enabled"] = False

    # STUB: model downloads (Moonshine, Kokoro) go here once engines are implemented.
    progress("Models: skipped (engines are stubs)")

    progress(f"Writing launcher to {plan.bin_dir / 'stt-tts'}")
    manifest.mkdir(plan.bin_dir)
    launcher = plan.bin_dir / "stt-tts"
    manifest.backup(launcher)
    launcher.write_text(LAUNCHER.format(app=app, python=python))
    launcher.chmod(0o755)
    if not any(o == launcher for o, _ in manifest.backups):
        manifest.file(launcher)

    progress(f"Writing config to {plan.config_file}")
    manifest.mkdir(plan.config_file.parent)
    manifest.backup(plan.config_file)
    vm_config.save(plan.cfg, plan.config_file)
    if not any(o == plan.config_file for o, _ in manifest.backups):
        manifest.file(plan.config_file)

    (app / "install-manifest.json").write_text(manifest.to_json())
    progress("Done.")


def _make_venv(app: Path, manifest: Manifest, progress: Progress) -> Path:
    venv_dir = app / "venv"
    progress("Creating virtual environment")
    result = subprocess.run([sys.executable, "-m", "venv", str(venv_dir)],
                            capture_output=True, text=True)
    if result.returncode == 0:
        manifest.file(venv_dir)
        return venv_dir / "bin" / "python"
    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    progress("  venv unavailable (apt install python3-venv); using system Python")
    return Path(sys.executable)


def uninstall(app_dir: Path, progress: Progress = print) -> bool:
    record = app_dir / "install-manifest.json"
    if not record.exists():
        progress(f"No install manifest at {record}")
        return False
    manifest = Manifest()
    manifest.created = [Path(p) for p in json.loads(record.read_text())["created"]]
    manifest.created.append(record)
    manifest.rollback(progress)
    return True


def spool_advice(spool_dir: str) -> str | None:
    """Explain how to get a dedicated tmpfs spool if the chosen one isn't tmpfs."""
    try:
        mounts = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return None
    spool = Path(spool_dir).expanduser().absolute()
    best = ("", "")
    for line in mounts:
        parts = line.split()
        if len(parts) >= 3 and str(spool).startswith(parts[1]) and len(parts[1]) > len(best[0]):
            best = (parts[1], parts[2])
    if best[1] == "tmpfs":
        return None
    return ("Spool is not on tmpfs, so clips will touch the disk. For a RAM spool run:\n"
            "  sudo mkdir -p /mnt/voice-spool\n"
            "  echo 'tmpfs /mnt/voice-spool tmpfs size=256M,mode=1777,noatime,nodev,nosuid,noexec 0 0'"
            " | sudo tee -a /etc/fstab\n"
            "  sudo mount /mnt/voice-spool\n"
            "  stt-tts config set paths.spool_dir /mnt/voice-spool")
