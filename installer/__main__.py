"""stt-tts installer entry point.

    ./install.sh              GUI if a window can open, else command line
    ./install.sh --cli        force the command-line installer
    ./install.sh --gui        require the GUI (fails if no display)
    ./install.sh --yes        continue past failed requirement checks
    ./install.sh --uninstall  remove what the installer created
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from installer import core, display  # noqa: E402
from stt_tts import config as vm_config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="install.sh", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--cli", action="store_true", help="force the command-line installer")
    mode.add_argument("--gui", action="store_true", help="require the GUI installer")
    mode.add_argument("--uninstall", action="store_true", help="remove an install")
    parser.add_argument("--yes", action="store_true", help="continue if requirements fail")
    parser.add_argument("--zenoh", action="store_true", help="also pip install eclipse-zenoh")
    parser.add_argument("--app-dir", help="install location (CLI)")
    parser.add_argument("--data-dir", help="data store: models, transcripts, logs (CLI)")
    parser.add_argument("--bin-dir", help="where the launcher goes (default ~/.local/bin)")
    parser.add_argument("--config-file", help="config file location")
    args = parser.parse_args(argv)

    if args.uninstall:
        app = Path(args.app_dir).expanduser() if args.app_dir else \
            Path(vm_config.default_paths()["app_dir"])
        return 0 if core.uninstall(app) else 1

    if args.cli:
        use_gui, reason = False, "--cli given"
    else:
        use_gui, reason = display.gui_available()
        if args.gui and not use_gui:
            print(f"GUI requested but unavailable: {reason}")
            return 2

    if use_gui:
        from installer import gui
        return gui.run(args)

    print(f"Using the command-line installer ({reason}).")
    from installer import cli
    return cli.run(args)


if __name__ == "__main__":
    sys.exit(main())
