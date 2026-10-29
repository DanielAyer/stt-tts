"""Command-line installer.

Takes every default (paths, endpoints, Zenoh keys) so it works over a plain
SSH session. It asks only:
  - what to do if an install already exists (modify, replace, or cancel)
  - whether to continue if the machine is below the minimum requirements
  - the LLM questions, and only if --llm is given
Everything else is changed after install with `stt-tts config set`,
`stt-tts setup llm`, `stt-tts security` and `stt-tts channels --assign`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import core, sysreq
from stt_tts import config as vm_config
from stt_tts import profile


def _interactive() -> bool:
    return sys.stdin.isatty()


def ask_existing(existing: core.Existing, args) -> str | None:
    """Return 'modify', 'replace', or None to cancel."""
    print("An existing stt-tts install was found:")
    for line in existing.summary():
        print("  " + line)
    if args.modify:
        return "modify"
    if args.replace:
        return "replace"
    if not _interactive():
        print("No terminal to ask. Re-run with --modify or --replace.")
        return None
    print("\n  [m] Modify:  keep your data, keys and settings; refresh the program files")
    print("  [r] Replace: delete everything (program, data, transcripts, config, stored keys)")
    print("               and install fresh. This cannot be undone.")
    print("  [c] Cancel")
    while True:
        answer = input("Choose m, r or c: ").strip().lower()
        if answer in ("m", "modify"):
            return "modify"
        if answer in ("c", "cancel", "q"):
            return None
        if answer in ("r", "replace"):
            confirm = input("Type 'replace' to delete the existing install: ").strip().lower()
            if confirm == "replace":
                return "replace"
            print("Not confirmed.")


def ask_continue(fails: list[sysreq.Check], assume_yes: bool) -> bool:
    print("\nThis machine is below the minimum requirements:")
    for c in fails:
        print(f"  - {c.name}: need {c.required}, found {c.found}")
    print("The module may run slowly or not at all.")
    if assume_yes:
        print("--yes given: continuing.")
        return True
    if not _interactive():
        print("No terminal to ask; stopping. Re-run with --yes to continue anyway.")
        return False
    while True:
        answer = input("[c]ontinue anyway, or [s]top and clean up? ").strip().lower()
        if answer in ("c", "continue"):
            return True
        if answer in ("s", "stop", "q"):
            return False


def run(args) -> int:
    config_file = Path(args.config_file).expanduser() if args.config_file else vm_config.config_path()
    os.environ["STT_TTS_CONFIG"] = str(config_file)
    print("stt-tts installer (command line)\n")

    mode, existing = "fresh", core.detect_existing(
        config_file, Path(args.app_dir).expanduser() if args.app_dir else None)
    if existing:
        choice = ask_existing(existing, args)
        if choice is None:
            print("Cancelled. Nothing was changed.")
            return 1
        mode = choice
        print()

    cfg = existing.cfg if (existing and mode == "modify") else vm_config.defaults()
    if args.app_dir:
        cfg["paths"]["app_dir"] = str(Path(args.app_dir).expanduser())
    if args.data_dir:
        data = Path(args.data_dir).expanduser()
        cfg["paths"].update(data_dir=str(data), model_dir=str(data / "models"),
                            transcript_dir=str(data / "transcripts"), log_dir=str(data / "logs"),
                            pki_dir=str(data / "pki"))
    plan = core.Plan(cfg=cfg, mode=mode, existing=existing, install_zenoh=args.zenoh,
                     config_file=config_file)
    if args.bin_dir:
        plan.bin_dir = Path(args.bin_dir).expanduser()
    manifest = core.Manifest()

    checks = sysreq.run_checks(plan.app_dir)
    print(sysreq.format_table(checks))
    fails = sysreq.failures(checks)
    if fails and not ask_continue(fails, args.yes):
        manifest.rollback()  # nothing written yet, but keep the contract
        print("Stopped. Nothing was installed or changed.")
        return 1

    if args.llm:
        if not _interactive():
            print("--llm needs a terminal for its questions; run `stt-tts setup llm` later instead.")
            return 1
        print()
        plan.llm = profile.ask(cfg)
        problems = profile.problems(cfg, plan.llm)
        if problems:
            print("\nLLM settings not accepted:")
            for p in problems:
                print("  - " + p)
            return 1

    verb = {"fresh": "Installing", "modify": "Updating", "replace": "Replacing with a fresh install"}[mode]
    print(f"\n{verb}:")
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
    print("\nDone. Next steps:")
    print("  stt-tts demo                      # walk the state machine with stubs")
    print("  stt-tts config show               # see every setting")
    print("  stt-tts setup llm                 # add LLM integration (sets up input security too)")
    print("  stt-tts security status           # check the security overlays")
    print("  stt-tts channels --assign         # pick Zenoh keys from a live router")
    print("  ./install.sh                      # run again to modify or replace this install")
    if str(plan.bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"\n  Note: {plan.bin_dir} is not on your PATH.")
    if advice:
        print("\n" + advice)
    return 0
