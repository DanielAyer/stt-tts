"""Command-line installer.

Takes every default (paths, endpoints, Zenoh keys) so it works over a plain
SSH session. The only question it asks is whether to continue when the
machine is below the minimum requirements. Everything else is changed after
install with `stt-tts config set ...` and `stt-tts channels --assign`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import core, sysreq
from stt_tts import config as vm_config


def ask_continue(fails: list[sysreq.Check], assume_yes: bool) -> bool:
    print("\nThis machine is below the minimum requirements:")
    for c in fails:
        print(f"  - {c.name}: need {c.required}, found {c.found}")
    print("The module may run slowly or not at all.")
    if assume_yes:
        print("--yes given: continuing.")
        return True
    if not sys.stdin.isatty():
        print("No terminal to ask; stopping. Re-run with --yes to continue anyway.")
        return False
    while True:
        answer = input("[c]ontinue anyway, or [s]top and clean up? ").strip().lower()
        if answer in ("c", "continue"):
            return True
        if answer in ("s", "stop", "q"):
            return False


def run(args) -> int:
    cfg = vm_config.defaults()
    if args.app_dir:
        cfg["paths"]["app_dir"] = str(Path(args.app_dir).expanduser())
    if args.data_dir:
        data = Path(args.data_dir).expanduser()
        cfg["paths"].update(data_dir=str(data), model_dir=str(data / "models"),
                            transcript_dir=str(data / "transcripts"), log_dir=str(data / "logs"))
    plan = core.Plan(cfg=cfg, install_zenoh=args.zenoh)
    if args.bin_dir:
        plan.bin_dir = Path(args.bin_dir).expanduser()
    if args.config_file:
        plan.config_file = Path(args.config_file).expanduser()
    manifest = core.Manifest()

    print("stt-tts installer (command line)\n")
    checks = sysreq.run_checks(plan.app_dir)
    print(sysreq.format_table(checks))
    fails = sysreq.failures(checks)
    if fails and not ask_continue(fails, args.yes):
        manifest.rollback()  # nothing written yet, but keep the contract
        print("Stopped. Nothing was installed.")
        return 1

    print("\nInstalling with defaults:")
    for key, value in cfg["paths"].items():
        print(f"  {key:<15} {value}")
    print(f"  {'config':<15} {plan.config_file}\n")

    try:
        core.install(plan, manifest, progress=lambda m: print("  " + m))
    except KeyboardInterrupt:
        print("\nStopped by user; cleaned up.")
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"\nInstall failed: {exc}")
        return 1

    advice = core.spool_advice(cfg["paths"]["spool_dir"])
    print("\nInstalled. Next steps:")
    print(f"  stt-tts demo                      # walk the state machine with stubs")
    print(f"  stt-tts config show               # see every setting")
    print(f"  stt-tts config set KEY VALUE      # change one, e.g. zenoh.keys.out_text")
    print(f"  stt-tts channels --assign         # pick Zenoh keys from a live router")
    if str(plan.bin_dir) not in _path_dirs():
        print(f"\n  Note: {plan.bin_dir} is not on your PATH.")
    if advice:
        print("\n" + advice)
    return 0


def _path_dirs() -> list[str]:
    import os
    return os.environ.get("PATH", "").split(os.pathsep)
