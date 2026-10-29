"""Tk GUI installer: five pages, same install core as the command line.

1. Requirements  - results table; continue anyway or stop and clean up
2. Locations     - install dir, data store, spool, config file (Browse dialogs)
3. Endpoints     - audio inputs/outputs, text sinks/sources, control, after-reply
4. Zenoh         - router endpoint, live channel discovery, pick each key
5. Install       - summary, progress log
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import core, sysreq
from stt_tts import adapters, config as vm_config
from stt_tts.adapters.zenoh_adapter import zenoh_available

PAD = {"padx": 8, "pady": 4}


class Installer(tk.Tk):
    def __init__(self, args) -> None:
        super().__init__()
        self.title("stt-tts installer")
        self.geometry("760x540")
        self.minsize(680, 480)
        self.args = args
        self.cfg = vm_config.defaults()
        self.manifest = core.Manifest()
        self.result = 1
        self.protocol("WM_DELETE_WINDOW", self.stop_and_clean)

        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True, padx=12, pady=8)
        nav = ttk.Frame(self)
        nav.pack(fill="x", padx=12, pady=(0, 12))
        self.back_btn = ttk.Button(nav, text="< Back", command=self.back)
        self.back_btn.pack(side="left")
        self.cancel_btn = ttk.Button(nav, text="Stop and clean up", command=self.stop_and_clean)
        self.cancel_btn.pack(side="left", padx=8)
        self.next_btn = ttk.Button(nav, text="Next >", command=self.next)
        self.next_btn.pack(side="right")

        self.pages = [self.page_requirements, self.page_locations, self.page_endpoints,
                      self.page_zenoh, self.page_install]
        self.index = 0
        self.vars: dict[str, tk.Variable] = {}
        self.show()

    # --- navigation ----------------------------------------------------------

    def show(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        self.back_btn.state(["!disabled"] if self.index > 0 else ["disabled"])
        self.next_btn.configure(text="Next >", command=self.next)
        self.next_btn.state(["!disabled"])
        self.pages[self.index]()

    def back(self) -> None:
        self.collect()
        self.index -= 1
        self.show()

    def next(self) -> None:
        self.collect()
        if self.index == 1 and not self.confirm_disk():
            return
        self.index += 1
        self.show()

    def stop_and_clean(self) -> None:
        if messagebox.askyesno("Stop install", "Stop the installer and remove anything it created?"):
            self.manifest.rollback(lambda m: None)
            self.result = 1
            self.destroy()

    def heading(self, text: str, sub: str = "") -> None:
        ttk.Label(self.body, text=text, font=("TkDefaultFont", 13, "bold")).pack(anchor="w")
        if sub:
            ttk.Label(self.body, text=sub, wraplength=700, foreground="#555").pack(anchor="w", pady=(2, 8))

    # --- page 1: requirements --------------------------------------------------

    def page_requirements(self) -> None:
        self.heading("System requirements",
                     "Minimums for the mid-level target. They are estimates from published "
                     "benchmarks, so you can continue below them.")
        checks = sysreq.run_checks(Path(self.cfg["paths"]["app_dir"]))
        tree = ttk.Treeview(self.body, columns=("status", "required", "found"), height=9)
        tree.heading("#0", text="Check")
        tree.heading("status", text="")
        tree.heading("required", text="Required")
        tree.heading("found", text="Found")
        tree.column("#0", width=210)
        tree.column("status", width=50, anchor="center")
        tree.column("required", width=140)
        tree.column("found", width=280)
        for c in checks:
            status = "OK" if c.ok else ("FAIL" if c.blocking else "warn")
            tree.insert("", "end", text=c.name, values=(status, c.required, c.found))
        tree.pack(fill="x", pady=6)
        fails = sysreq.failures(checks)
        if fails:
            names = ", ".join(c.name for c in fails)
            ttk.Label(self.body, foreground="#a33", wraplength=700,
                      text=f"Below minimum: {names}. The module may run slowly or not at all. "
                           "Continue anyway, or stop and clean up.").pack(anchor="w", pady=6)
            self.next_btn.configure(text="Continue anyway >")
        for c in checks:
            if not c.ok and c.note:
                ttk.Label(self.body, text=f"{c.name}: {c.note}", wraplength=700,
                          foreground="#555").pack(anchor="w")

    # --- page 2: locations ----------------------------------------------------

    def page_locations(self) -> None:
        self.heading("Locations", "Where the module, its data and its audio spool live.")
        grid = ttk.Frame(self.body)
        grid.pack(fill="x")
        rows = [
            ("app_dir", "Install location", self.cfg["paths"]["app_dir"]),
            ("data_dir", "Data store (models, transcripts, logs)", self.cfg["paths"]["data_dir"]),
            ("spool_dir", "Audio spool (tmpfs recommended)", self.cfg["paths"]["spool_dir"]),
            ("config_file", "Config file", str(vm_config.config_path())),
        ]
        for r, (key, label, default) in enumerate(rows):
            var = self.vars.setdefault(key, tk.StringVar(value=default))
            ttk.Label(grid, text=label).grid(row=r * 2, column=0, columnspan=2, sticky="w", **PAD)
            ttk.Entry(grid, textvariable=var, width=70).grid(row=r * 2 + 1, column=0, sticky="we", padx=8)
            ttk.Button(grid, text="Browse...",
                       command=lambda v=var, f=(key == "config_file"): self.browse(v, f)
                       ).grid(row=r * 2 + 1, column=1, padx=4)
        grid.columnconfigure(0, weight=1)
        self.disk_label = ttk.Label(self.body, foreground="#555")
        self.disk_label.pack(anchor="w", pady=8)
        self.vars["app_dir"].trace_add("write", lambda *_: self.update_disk())
        self.update_disk()

    def browse(self, var: tk.StringVar, is_file: bool) -> None:
        start = Path(var.get()).expanduser()
        if is_file:
            path = filedialog.asksaveasfilename(initialdir=start.parent, initialfile=start.name)
        else:
            path = filedialog.askdirectory(initialdir=start if start.exists() else Path.home(),
                                           mustexist=False)
        if path:
            var.set(path)

    def update_disk(self) -> None:
        free = sysreq.disk_free_gib(Path(self.vars["app_dir"].get()))
        self.disk_label.configure(text=f"Free space at install location: {free:.1f} GiB "
                                       f"(minimum {sysreq.MIN_DISK_GIB:.0f} GiB)")

    def confirm_disk(self) -> bool:
        check = sysreq.check_disk(Path(self.cfg["paths"]["app_dir"]))
        if check.ok:
            return True
        return messagebox.askyesno("Low disk space",
                                   f"Only {check.found} free. Continue anyway?")

    # --- page 3: endpoints ----------------------------------------------------

    def page_endpoints(self) -> None:
        self.heading("Endpoints", "Audio in/out is the user side; transcripts and text to speak "
                                  "are the model side. Stub entries are placeholders until real "
                                  "devices are wired in.")
        frame = ttk.Frame(self.body)
        frame.pack(fill="both", expand=True)
        groups = [
            ("input.adapters", "Audio in", adapters.AUDIO_INPUTS),
            ("output.adapters", "Audio out", adapters.AUDIO_OUTPUTS),
            ("text.sinks", "Transcripts to", adapters.TEXT_SINKS),
            ("text.sources", "Speak text from", adapters.TEXT_SOURCES),
        ]
        for col, (key, label, registry) in enumerate(groups):
            box = ttk.LabelFrame(frame, text=label)
            box.grid(row=0, column=col, sticky="nsew", padx=4, pady=4)
            chosen = vm_config.get(self.cfg, key)
            for name in registry:
                var = self.vars.setdefault(f"{key}:{name}", tk.BooleanVar(value=name in chosen))
                ttk.Checkbutton(box, text=name, variable=var).pack(anchor="w", padx=6, pady=2)
            frame.columnconfigure(col, weight=1)

        ctl = ttk.Frame(self.body)
        ctl.pack(fill="x", pady=10)
        ttk.Label(ctl, text="Button (control) from:").grid(row=0, column=0, sticky="w", **PAD)
        var = self.vars.setdefault("control.adapter", tk.StringVar(value=self.cfg["control"]["adapter"]))
        ttk.Combobox(ctl, textvariable=var, values=list(adapters.CONTROLS), state="readonly",
                     width=16).grid(row=0, column=1, sticky="w")
        ttk.Label(ctl, text="After a reply finishes:").grid(row=1, column=0, sticky="w", **PAD)
        var = self.vars.setdefault("control.after_reply",
                                   tk.StringVar(value=self.cfg["control"]["after_reply"]))
        ttk.Radiobutton(ctl, text="go idle", value="idle", variable=var).grid(row=1, column=1, sticky="w")
        ttk.Radiobutton(ctl, text="reopen the mic", value="listening",
                        variable=var).grid(row=1, column=2, sticky="w")

    # --- page 4: zenoh ----------------------------------------------------------

    def page_zenoh(self) -> None:
        z = self.cfg["zenoh"]
        self.heading("Zenoh", "Optional. Publish transcripts and subscribe to audio, button "
                              "presses and text on Zenoh key expressions.")
        top = ttk.Frame(self.body)
        top.pack(fill="x")
        en = self.vars.setdefault("zenoh.enabled", tk.BooleanVar(value=z["enabled"]))
        ttk.Checkbutton(top, text="Enable Zenoh", variable=en).grid(row=0, column=0, sticky="w", **PAD)
        inst = self.vars.setdefault("install_zenoh", tk.BooleanVar(value=self.args.zenoh))
        ttk.Checkbutton(top, text="Install Zenoh support (needs network)",
                        variable=inst).grid(row=0, column=1, columnspan=2, sticky="w", **PAD)
        ttk.Label(top, text="Router").grid(row=1, column=0, sticky="w", **PAD)
        ep = self.vars.setdefault("zenoh.connect", tk.StringVar(value=", ".join(z["connect"])))
        ttk.Entry(top, textvariable=ep, width=36).grid(row=1, column=1, sticky="w")
        disc = ttk.Button(top, text="Discover channels", command=self.discover)
        disc.grid(row=1, column=2, padx=8)
        self.disc_status = ttk.Label(self.body, foreground="#555", wraplength=700)
        self.disc_status.pack(anchor="w", pady=4)
        if not zenoh_available():
            disc.state(["disabled"])
            self.disc_status.configure(text="Discovery needs the Zenoh Python package, which the "
                                            "installer doesn't have. Type keys now, or pick them "
                                            "after install with: stt-tts channels --assign")

        keys = ttk.LabelFrame(self.body, text="Key expressions (type a key or pick a discovered one)")
        keys.pack(fill="x", pady=6)
        self.key_boxes: list[ttk.Combobox] = []
        labels = {"in_audio": "Audio in (subscribe)", "in_control": "Button presses (subscribe)",
                  "out_text": "Transcripts (publish)", "tts_text": "Text to speak (subscribe)",
                  "tts_audio": "Spoken audio (publish)", "events": "Events (publish)"}
        for r, (name, current) in enumerate(z["keys"].items()):
            ttk.Label(keys, text=labels.get(name, name)).grid(row=r, column=0, sticky="w", **PAD)
            var = self.vars.setdefault(f"zenoh.keys.{name}", tk.StringVar(value=current))
            box = ttk.Combobox(keys, textvariable=var, width=42)
            box.grid(row=r, column=1, sticky="w", padx=8, pady=2)
            self.key_boxes.append(box)

    def discover(self) -> None:
        from stt_tts import discovery
        self.collect()
        self.disc_status.configure(text="Querying router admin space...")
        self.update_idletasks()
        try:
            found = discovery.unique_keys(discovery.discover(self.cfg))
        except Exception as exc:  # noqa: BLE001
            self.disc_status.configure(text=f"Discovery failed: {exc}")
            return
        for box in self.key_boxes:
            box.configure(values=found)
        self.disc_status.configure(
            text=f"Found {len(found)} channel(s). Pick from each list, or type a new key."
            if found else "Router answered but reported no channels. Type keys instead.")

    # --- page 5: install ------------------------------------------------------

    def page_install(self) -> None:
        self.heading("Install")
        summary = tk.Text(self.body, height=16, wrap="word")
        summary.pack(fill="both", expand=True)
        summary.insert("end", "Will install with:\n\n" + vm_config.dumps(self.cfg))
        summary.configure(state="disabled")
        self.log = summary
        self.next_btn.configure(text="Install", command=self.start_install)

    def start_install(self) -> None:
        self.next_btn.state(["disabled"])
        self.back_btn.state(["disabled"])
        self.cancel_btn.state(["disabled"])
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        msgs: queue.Queue = queue.Queue()
        plan = core.Plan(cfg=self.cfg, install_zenoh=bool(self.vars["install_zenoh"].get())
                         if "install_zenoh" in self.vars else False,
                         config_file=Path(self.vars["config_file"].get()).expanduser()
                         if "config_file" in self.vars else vm_config.config_path())

        def work():
            try:
                core.install(plan, self.manifest, progress=msgs.put)
                msgs.put(("done", core.spool_advice(self.cfg["paths"]["spool_dir"])))
            except Exception as exc:  # noqa: BLE001
                msgs.put(("failed", str(exc)))

        threading.Thread(target=work, daemon=True).start()
        self.after(100, self.pump, msgs)

    def pump(self, msgs: queue.Queue) -> None:
        while not msgs.empty():
            msg = msgs.get()
            if isinstance(msg, tuple):
                status, detail = msg
                if status == "done":
                    self.result = 0
                    self.log.insert("end", "\nInstalled. Try: stt-tts demo\n")
                    if detail:
                        self.log.insert("end", "\n" + detail + "\n")
                else:
                    self.log.insert("end", f"\nInstall failed and was rolled back: {detail}\n")
                self.next_btn.configure(text="Close", command=self.destroy)
                self.next_btn.state(["!disabled"])
                self.protocol("WM_DELETE_WINDOW", self.destroy)
                return
            self.log.insert("end", msg + "\n")
            self.log.see("end")
        self.after(100, self.pump, msgs)

    # --- collect widget values into cfg ----------------------------------------

    def collect(self) -> None:
        v = self.vars
        if "app_dir" in v:
            data = Path(v["data_dir"].get()).expanduser()
            self.cfg["paths"].update(
                app_dir=str(Path(v["app_dir"].get()).expanduser()),
                data_dir=str(data), model_dir=str(data / "models"),
                transcript_dir=str(data / "transcripts"), log_dir=str(data / "logs"),
                spool_dir=str(Path(v["spool_dir"].get()).expanduser()))
        for key, registry in (("input.adapters", adapters.AUDIO_INPUTS),
                              ("output.adapters", adapters.AUDIO_OUTPUTS),
                              ("text.sinks", adapters.TEXT_SINKS),
                              ("text.sources", adapters.TEXT_SOURCES)):
            if f"{key}:{next(iter(registry))}" in v:
                section, leaf = key.split(".")
                self.cfg[section][leaf] = [n for n in registry if v[f"{key}:{n}"].get()]
        for key in ("control.adapter", "control.after_reply"):
            if key in v:
                section, leaf = key.split(".")
                self.cfg[section][leaf] = v[key].get()
        if "zenoh.enabled" in v:
            self.cfg["zenoh"]["enabled"] = bool(v["zenoh.enabled"].get())
            self.cfg["zenoh"]["connect"] = [e.strip() for e in v["zenoh.connect"].get().split(",") if e.strip()]
            for name in self.cfg["zenoh"]["keys"]:
                self.cfg["zenoh"]["keys"][name] = v[f"zenoh.keys.{name}"].get().strip()


def run(args) -> int:
    app = Installer(args)
    app.mainloop()
    return app.result
