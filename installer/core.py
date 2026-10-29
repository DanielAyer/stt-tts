"""The install itself, shared by the GUI and command-line front ends.

Modes
  fresh    nothing installed yet
  modify   an install exists: keep data, keys and settings as the starting
           point, refresh the program files, save the changed options
  replace  "scorched earth": delete the existing program, data store, config,
           stored keys and launcher, then install fresh

Every file, directory and stored key the installer creates is recorded in a
Manifest. If the install is stopped or fails, rollback() undoes exactly those
and restores anything it replaced. A replace's deletions happen before the
new install starts and cannot be rolled back; both front ends say so first.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from stt_tts import config as vm_config  # noqa: E402
from stt_tts import credentials, profile  # noqa: E402

Progress = Callable[[str], None]
MANIFEST_NAME = "install-manifest.json"


# --- existing installs ---------------------------------------------------------

@dataclass
class Existing:
    config_file: Path
    cfg: dict
    has_config: bool
    manifest: Path | None

    @property
    def app_dir(self) -> Path:
        return Path(self.cfg["paths"]["app_dir"]).expanduser()

    def summary(self) -> list[str]:
        p = self.cfg["paths"]
        lines = [f"Program:    {p['app_dir']}", f"Data store: {p['data_dir']}",
                 f"Config:     {self.config_file}" + ("" if self.has_config else " (missing)")]
        if self.cfg["llm"]["enabled"]:
            lines.append(f"LLM:        {self.cfg['llm']['backend']} at {self.cfg['llm']['base_url']}")
        return lines


def detect_existing(config_file: Path | None = None, app_dir: Path | None = None) -> Existing | None:
    config_file = config_file or vm_config.config_path()
    cfg = vm_config.load(config_file)
    app = Path(app_dir) if app_dir else Path(cfg["paths"]["app_dir"]).expanduser()
    manifest = app / MANIFEST_NAME
    if not config_file.exists() and not manifest.exists():
        return None
    if app_dir:
        cfg["paths"]["app_dir"] = str(app)
    return Existing(config_file, cfg, config_file.exists(), manifest if manifest.exists() else None)


def _safe_to_delete(path: Path) -> bool:
    """Guard for scorched earth: only paths that are clearly ours."""
    path = path.expanduser().absolute()
    protected = {Path("/"), Path.home(), Path.home() / ".local", Path.home() / ".config",
                 Path.home() / ".local" / "share", Path.home() / ".local" / "opt"}
    return path not in protected and len(path.parts) >= 3 and "stt-tts" in path.as_posix()


def scorch(existing: Existing, bin_dir: Path, progress: Progress = print) -> None:
    """Delete an existing install completely: program, data, config, keys, launcher."""
    targets: list[Path] = []
    if existing.manifest:
        targets += [Path(p) for p in json.loads(existing.manifest.read_text())["created"]]
    p = existing.cfg["paths"]
    targets += [Path(p[k]) for k in ("app_dir", "data_dir", "model_dir", "transcript_dir",
                                       "log_dir", "spool_dir")]
    targets += [bin_dir / "stt-tts", existing.config_file]

    progress("Deleting stored keys")
    credentials.delete_all([profile.SECRET_NAME])
    secrets_file = vm_config.secrets_path()
    if secrets_file.exists():
        secrets_file.unlink()

    resolved = [t.expanduser().absolute() for t in targets]
    seen = set()
    for target in resolved:
        if target in seen or not (target.exists() or target.is_symlink()):
            continue
        seen.add(target)
        if not _safe_to_delete(target):
            # Generic parents like ~/.local hold our folders; leave them quietly.
            if not any(other != target and target in other.parents for other in resolved):
                progress(f"  kept {target} (not clearly an stt-tts path)")
            continue
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
        progress(f"  deleted {target}")
    config_dir = existing.config_file.parent
    if config_dir.exists() and not any(config_dir.iterdir()) and _safe_to_delete(config_dir):
        config_dir.rmdir()
        progress(f"  deleted {config_dir}")


# --- plan and manifest ---------------------------------------------------------

@dataclass
class Plan:
    cfg: dict
    mode: str = "fresh"  # fresh | modify | replace
    existing: Existing | None = None
    install_zenoh: bool = False
    llm: "profile.LLMSettings | None" = None  # None = leave the LLM profile as it is
    remove_llm: bool = False
    bin_dir: Path = field(default_factory=lambda: Path.home() / ".local" / "bin")
    config_file: Path = field(default_factory=vm_config.config_path)

    @property
    def app_dir(self) -> Path:
        return Path(self.cfg["paths"]["app_dir"]).expanduser()


class Manifest:
    def __init__(self) -> None:
        self.created: list[Path] = []
        self.backups: list[tuple[Path, Path]] = []  # (original, backup)
        self.secrets: list[tuple[str, str | None]] = []  # (name, previous value)

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

    def remember_secret(self, name: str) -> None:
        try:
            previous = credentials.get_secret(name)
        except PermissionError:
            previous = None
        self.secrets.append((name, previous))

    def rollback(self, progress: Progress = print) -> None:
        for name, previous in reversed(self.secrets):
            if previous is None:
                credentials.delete_secret(name)
            else:
                credentials.set_secret(name, previous)
            progress(f"restored stored key '{name}'")
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
        self.secrets.clear()

    def to_json(self, previous: list[str] | None = None) -> str:
        created = list(dict.fromkeys((previous or []) + [str(p) for p in self.created
                                                         if not str(p).endswith(".bak")]))
        return json.dumps({"created": created}, indent=2)


LAUNCHER = """#!/bin/sh
# stt-tts launcher (written by the installer)
export PYTHONPATH="{app}${{PYTHONPATH:+:$PYTHONPATH}}"
export STT_TTS_CONFIG="{config}"
exec "{python}" -m stt_tts "$@"
"""


# --- install ---------------------------------------------------------------------

def install(plan: Plan, manifest: Manifest, progress: Progress = print) -> None:
    """Run every step; on any error or Ctrl-C, roll back and re-raise."""
    os.environ["STT_TTS_CONFIG"] = str(plan.config_file)  # keys live next to this config
    if plan.mode == "replace" and plan.existing:
        progress("Replacing: deleting the existing install (this part cannot be undone)")
        scorch(plan.existing, plan.bin_dir, progress)
    try:
        _install(plan, manifest, progress)
    except BaseException:
        progress("Install failed or was stopped; cleaning up.")
        manifest.rollback(progress)
        raise


def _install(plan: Plan, manifest: Manifest, progress: Progress) -> None:
    paths = plan.cfg["paths"]
    app = plan.app_dir
    previous = []
    old_record = app / MANIFEST_NAME
    if plan.mode == "modify" and old_record.exists():
        previous = json.loads(old_record.read_text())["created"]

    progress("Creating directories")
    for key in ("app_dir", "data_dir", "model_dir", "transcript_dir", "log_dir", "spool_dir"):
        manifest.mkdir(Path(paths[key]).expanduser())

    progress(f"Copying program files to {app}")
    target = app / "stt_tts"
    existed = target.exists()
    if existed:
        shutil.rmtree(target)
    shutil.copytree(REPO_ROOT / "stt_tts", target, ignore=shutil.ignore_patterns("__pycache__"))
    if not existed:
        manifest.file(target)
    for doc in ("README.md", "LICENSE"):
        if (REPO_ROOT / doc).exists():
            new = not (app / doc).exists()
            shutil.copy2(REPO_ROOT / doc, app / doc)
            if new:
                manifest.file(app / doc)

    python = _make_venv(app, manifest, progress)

    if plan.install_zenoh:
        progress("Installing optional Zenoh support (pip install eclipse-zenoh)")
        result = subprocess.run([str(python), "-m", "pip", "install", "eclipse-zenoh"],
                                capture_output=True, text=True)
        if result.returncode != 0:
            progress("  Zenoh install failed (network or pip missing); continuing without it.")

    # STUB: model downloads (Moonshine, Kokoro) go here once engines are implemented.
    progress("Models: skipped (engines are stubs)")

    if plan.remove_llm:
        progress("Removing the LLM integration profile")
        manifest.remember_secret(profile.SECRET_NAME)
        profile.remove(plan.cfg)
    elif plan.llm is not None:
        progress(f"Configuring LLM integration ({plan.llm.backend})")
        manifest.remember_secret(profile.SECRET_NAME)
        problems = profile.apply(plan.cfg, plan.llm)
        if problems:
            raise ValueError("LLM settings rejected: " + "; ".join(problems))
        progress(f"  key stored in {credentials.backend_name()}" if plan.cfg["llm"]["api_key_secret"]
                 else "  no API key")

    from stt_tts import security
    security.enforce(plan.cfg)  # never save an insecure combination

    progress(f"Writing launcher to {plan.bin_dir / 'stt-tts'}")
    manifest.mkdir(plan.bin_dir)
    launcher = plan.bin_dir / "stt-tts"
    manifest.backup(launcher)
    launcher.write_text(LAUNCHER.format(app=app, python=python, config=plan.config_file))
    launcher.chmod(0o755)
    if not any(o == launcher for o, _ in manifest.backups):
        manifest.file(launcher)

    progress(f"Writing config to {plan.config_file}")
    manifest.mkdir(plan.config_file.parent)
    manifest.backup(plan.config_file)
    vm_config.save(plan.cfg, plan.config_file)
    if not any(o == plan.config_file for o, _ in manifest.backups):
        manifest.file(plan.config_file)
    secrets_file = vm_config.secrets_path()
    if secrets_file.exists() and str(secrets_file) not in previous:
        previous.append(str(secrets_file))

    (app / MANIFEST_NAME).write_text(manifest.to_json(previous))
    for _, bak in manifest.backups:  # backups were only for rollback
        bak.unlink(missing_ok=True)
    progress("Done.")


def _make_venv(app: Path, manifest: Manifest, progress: Progress) -> Path:
    venv_dir = app / "venv"
    python = venv_dir / "bin" / "python"
    if python.exists():
        progress("Virtual environment: keeping existing")
        return python
    progress("Creating virtual environment")
    result = subprocess.run([sys.executable, "-m", "venv", str(venv_dir)],
                            capture_output=True, text=True)
    if result.returncode == 0:
        manifest.file(venv_dir)
        return python
    if venv_dir.exists():
        shutil.rmtree(venv_dir)
    progress("  venv unavailable (apt install python3-venv); using system Python")
    return Path(sys.executable)


def uninstall(app_dir: Path, progress: Progress = print) -> bool:
    record = app_dir / MANIFEST_NAME
    if not record.exists():
        progress(f"No install manifest at {record}")
        return False
    credentials.delete_secret(profile.SECRET_NAME)
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
