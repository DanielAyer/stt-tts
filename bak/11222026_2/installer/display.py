"""Decide between the GUI and the command-line installer.

Over SSH a window can only appear if:
  1. the session forwards X11 (ssh -X or ssh -Y), which sets DISPLAY,
  2. Tk is installed on the server (apt install python3-tk), and
  3. a test window actually opens (forwarding can be set but broken).
If any step fails, the installer falls back to the command line.
"""

from __future__ import annotations

import os


def gui_available() -> tuple[bool, str]:
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        hint = " (connect with ssh -X to forward X11)" if os.environ.get("SSH_CONNECTION") else ""
        return False, "no display found" + hint
    try:
        import tkinter
    except ImportError:
        return False, "Tk is not installed (apt install python3-tk)"
    try:
        root = tkinter.Tk()
        root.withdraw()
        root.update_idletasks()
        root.destroy()
    except tkinter.TclError as exc:
        return False, f"could not open a window: {exc}"
    return True, "display available"
