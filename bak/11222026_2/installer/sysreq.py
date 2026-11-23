"""Minimum system requirement checks.

The minimums come from the design doc (Target 1): 4 x86-64 cores with AVX2,
8 GB RAM. They are estimates derived from published model benchmarks, not
measurements of this exact stack, so a failed check is a warning the user can
override, never a hard stop.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

MIN_PYTHON = (3, 11)
MIN_CORES = 4
# An "8 GB" machine reports roughly 7.6-7.8 GiB to the OS.
MIN_RAM_GIB = 7.5
MIN_DISK_GIB = 2.0


@dataclass
class Check:
    name: str
    required: str
    found: str
    ok: bool
    blocking: bool = True  # False = informational only
    note: str = ""


def _cpu_flags() -> set[str]:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("flags"):
                return set(line.split(":", 1)[1].split())
    except OSError:
        pass
    return set()


def _ram_gib() -> float | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) / (1024 ** 2)
    except OSError:
        pass
    return None


def _nearest_existing(path: Path) -> Path:
    path = path.expanduser().absolute()
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def disk_free_gib(path: Path) -> float:
    return shutil.disk_usage(_nearest_existing(path)).free / (1024 ** 3)


def check_disk(path: Path) -> Check:
    free = disk_free_gib(path)
    return Check("Free disk at install location", f">= {MIN_DISK_GIB:.0f} GiB",
                 f"{free:.1f} GiB at {_nearest_existing(path)}", free >= MIN_DISK_GIB)


def run_checks(install_dir: Path) -> list[Check]:
    checks: list[Check] = []

    system = platform.system()
    checks.append(Check("Operating system", "Linux", system, system == "Linux"))

    machine = platform.machine()
    checks.append(Check("CPU architecture", "x86_64", machine, machine in ("x86_64", "AMD64"),
                        note="ARM boards (Raspberry Pi) are Target 2 and need different engines."))

    py = sys.version_info
    checks.append(Check("Python", ">= %d.%d" % MIN_PYTHON, f"{py.major}.{py.minor}.{py.micro}",
                        py >= MIN_PYTHON))

    cores = os.cpu_count() or 0
    try:
        physical = len({line for line in Path("/proc/cpuinfo").read_text().splitlines()
                        if line.startswith("core id")}) or cores
    except OSError:
        physical = cores
    checks.append(Check("CPU cores", f">= {MIN_CORES}", f"{physical} cores / {cores} threads",
                        physical >= MIN_CORES))

    flags = _cpu_flags()
    has_avx2 = "avx2" in flags
    checks.append(Check("CPU AVX2", "present", "present" if has_avx2 else "missing", has_avx2,
                        note="ONNX Runtime relies on AVX2 for fast CPU inference."))

    ram = _ram_gib()
    checks.append(Check("RAM", "8 GB", f"{ram:.1f} GiB" if ram else "unknown",
                        bool(ram and ram >= MIN_RAM_GIB)))

    checks.append(check_disk(install_dir))

    # Informational: these change features, not whether the module can run.
    venv_ok = _venv_available()
    checks.append(Check("python3-venv", "recommended", "present" if venv_ok else "missing",
                        venv_ok, blocking=False,
                        note="Without it the module runs on system Python. apt install python3-venv"))
    return checks


def _venv_available() -> bool:
    try:
        import ensurepip  # noqa: F401
        import venv  # noqa: F401
    except ImportError:
        return False
    return True


def failures(checks: list[Check]) -> list[Check]:
    return [c for c in checks if c.blocking and not c.ok]


def format_table(checks: list[Check]) -> str:
    rows = [("", "Check", "Required", "Found")]
    for c in checks:
        mark = "OK" if c.ok else ("FAIL" if c.blocking else "warn")
        rows.append((mark, c.name, c.required, c.found))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    lines = ["  ".join(cell.ljust(w) for cell, w in zip(row, widths)) for row in rows]
    lines.insert(1, "  ".join("-" * w for w in widths))
    notes = [f"  - {c.name}: {c.note}" for c in checks if not c.ok and c.note]
    return "\n".join(lines + (["", "Notes:"] + notes if notes else []))
