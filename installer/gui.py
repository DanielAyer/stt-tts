"""Tk GUI installer. Same install core as the command line.

Pages
  0. Existing install  - only if one is found: modify (rewalk, prefilled) or replace (scorched earth)
  1. Requirements      - results table; continue anyway or stop and clean up
  2. Locations         - install dir, data store, spool, config file (Browse dialogs)
  3. Endpoints         - audio inputs/outputs, text sinks/sources, control, after-reply
  4. Zenoh             - router, mutual TLS, live channel discovery, key names
  5. LLM integration   - optional profile: backend, URL, model, API key, TLS
  6. Security          - input allow-list (forced on with LLM), output sealing
  7. Install           - summary, progress log
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import core, sysreq
from stt_tts import adapters, config as vm_config, credentials, profile, security
from stt_tts.adapters.zenoh_adapter import zenoh_available

PAD = {"padx": 8, "pady": 3}
GREY = "#555"
RED = "#a33"


class Installer(tk.Tk):
    def __init__(self, args) -> None:
        super().__init__()
        self.title("stt-tts installer")
        self.geometry("800x600")
        self.minsize(720, 540)
        self.args = args
        self.manifest = core.Manifest()
        self.result = 1
        self.protocol("WM_DELETE_WINDOW", self.stop_and_clean)

        self.config_file = Path(args.config_file).expanduser() if getattr(args, "config_file", None) \
            else vm_config.config_path()
        self.existing = core.detect_existing(self.config_file)
        self.mode = "fresh"
        self.cfg = vm_config.defaults()
        self.vars: dict[str, tk.Variable] = {}

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

        self.pages = ([self.page_existing] if self.existing else []) + [
            self.page_requirements, self.page_locations, self.page_endpoints,
            self.page_zenoh, self.page_llm, self.page_security, self.page_install]
        self.index = 0
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
        page = self.pages[self.index]
        if page == self.page_existing and not self.leave_existing():
            return
        if page == self.page_locations and not self.confirm_disk():
            return
        if page == self.page_security and not self.validate_security():
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
            ttk.Label(self.body, text=sub, wraplength=740, foreground=GREY).pack(anchor="w", pady=(2, 8))

    def var(self, key: str, value, kind=tk.StringVar) -> tk.Variable:
        return self.vars.setdefault(key, kind(value=value))

    def path_row(self, parent, row: int, key: str, label: str, value: str, is_file: bool = True):
        v = self.var(key, value)
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", **PAD)
        ttk.Entry(parent, textvariable=v, width=48).grid(row=row, column=1, sticky="we", padx=4)
        ttk.Button(parent, text="Browse...", command=lambda: self.browse(v, is_file)
                   ).grid(row=row, column=2, padx=4)
        return v

    def browse(self, var: tk.StringVar, is_file: bool) -> None:
        start = Path(var.get() or Path.home()).expanduser()
        if is_file:
            path = filedialog.askopenfilename(initialdir=start.parent if start.parent.exists()
                                              else Path.home())
        else:
            path = filedialog.askdirectory(initialdir=start if start.exists() else Path.home(),
                                           mustexist=False)
        if path:
            var.set(path)

    # --- page 0: existing install ------------------------------------------------

    def page_existing(self) -> None:
        self.heading("stt-tts is already installed",
                     "Choose whether to change the current install or start over.")
        box = ttk.LabelFrame(self.body, text="Current install")
        box.pack(fill="x", pady=4)
        for line in self.existing.summary():
            ttk.Label(box, text=line, font=("TkFixedFont", 10)).pack(anchor="w", padx=8)
        choice = self.var("existing.mode", "modify")
        for value, title, text in (
                ("modify", "Modify",
                 "Walk through the options again, starting from your current settings. "
                 "Data, transcripts and stored keys are kept."),
                ("replace", "Replace",
                 "Delete everything (program, data store, transcripts, config, stored keys, "
                 "launcher) and install fresh.")):
            ttk.Radiobutton(self.body, text=title, value=value, variable=choice
                            ).pack(anchor="w", pady=(12, 0))
            ttk.Label(self.body, text=text, wraplength=700, foreground=GREY
                      ).pack(anchor="w", padx=24)
        ttk.Label(self.body, foreground=RED, wraplength=740,
                  text="Replace cannot be undone. The deletion happens when you press Install "
                       "on the last page, so you can still back out until then.").pack(anchor="w", pady=8)

    def leave_existing(self) -> bool:
        choice = self.vars["existing.mode"].get()
        if choice == "replace" and not messagebox.askyesno(
                "Replace install", "Delete the existing install, including all data and stored "
                "keys, when you press Install?\n\n" + "\n".join(self.existing.summary())):
            return False
        if choice != self.mode:  # start the walk from the right settings
            keep = {"existing.mode": self.vars["existing.mode"]}
            self.vars = keep
        self.mode = choice
        self.cfg = self.existing.cfg if choice == "modify" else vm_config.defaults()
        return True

    # --- page 1: requirements --------------------------------------------------

    def page_requirements(self) -> None:
        self.heading("System requirements",
                     "Minimums for the mid-level target. They are estimates from published "
                     "benchmarks, so you can continue below them.")
        checks = sysreq.run_checks(Path(self.cfg["paths"]["app_dir"]))
        tree = ttk.Treeview(self.body, columns=("status", "required", "found"), height=9)
        for col, text, width in (("#0", "Check", 210), ("status", "", 50),
                                 ("required", "Required", 140), ("found", "Found", 300)):
            tree.heading(col, text=text)
            tree.column(col, width=width, anchor="center" if col == "status" else "w")
        for c in checks:
            status = "OK" if c.ok else ("FAIL" if c.blocking else "warn")
            tree.insert("", "end", text=c.name, values=(status, c.required, c.found))
        tree.pack(fill="x", pady=6)
        fails = sysreq.failures(checks)
        if fails:
            names = ", ".join(c.name for c in fails)
            ttk.Label(self.body, foreground=RED, wraplength=740,
                      text=f"Below minimum: {names}. The module may run slowly or not at all. "
                           "Continue anyway, or stop and clean up.").pack(anchor="w", pady=6)
            self.next_btn.configure(text="Continue anyway >")
        for c in checks:
            if not c.ok and c.note:
                ttk.Label(self.body, text=f"{c.name}: {c.note}", wraplength=740,
                          foreground=GREY).pack(anchor="w")

    # --- page 2: locations ----------------------------------------------------

    def page_locations(self) -> None:
        self.heading("Locations", "Where the program, its data and its audio spool live.")
        grid = ttk.Frame(self.body)
        grid.pack(fill="x")
        p = self.cfg["paths"]
        self.path_row(grid, 0, "app_dir", "Install location", p["app_dir"], is_file=False)
        self.path_row(grid, 1, "data_dir", "Data store", p["data_dir"], is_file=False)
        self.path_row(grid, 2, "spool_dir", "Audio spool (tmpfs)", p["spool_dir"], is_file=False)
        self.path_row(grid, 3, "config_file", "Config file", str(self.config_file))
        grid.columnconfigure(1, weight=1)
        ttk.Label(self.body, foreground=GREY, wraplength=740,
                  text="The data store holds models, transcripts and logs.").pack(anchor="w", pady=4)
        self.disk_label = ttk.Label(self.body, foreground=GREY)
        self.disk_label.pack(anchor="w", pady=4)
        self.vars["app_dir"].trace_add("write", lambda *_: self.update_disk())
        self.update_disk()

    def update_disk(self) -> None:
        free = sysreq.disk_free_gib(Path(self.vars["app_dir"].get()))
        self.disk_label.configure(text=f"Free space at install location: {free:.1f} GiB "
                                       f"(minimum {sysreq.MIN_DISK_GIB:.0f} GiB)")

    def confirm_disk(self) -> bool:
        check = sysreq.check_disk(Path(self.cfg["paths"]["app_dir"]))
        return check.ok or messagebox.askyesno("Low disk space",
                                               f"Only {check.found} free. Continue anyway?")

    # --- page 3: endpoints ----------------------------------------------------

    def page_endpoints(self) -> None:
        self.heading("Endpoints", "Audio in/out is the user side; transcripts and text to speak "
                                  "are the model side. Stub entries are placeholders until real "
                                  "devices are wired in. The LLM has its own page.")
        frame = ttk.Frame(self.body)
        frame.pack(fill="x")
        groups = [("input.adapters", "Audio in", adapters.AUDIO_INPUTS),
                  ("output.adapters", "Audio out", adapters.AUDIO_OUTPUTS),
                  ("text.sinks", "Transcripts to", adapters.TEXT_SINKS),
                  ("text.sources", "Speak text from", adapters.TEXT_SOURCES)]
        for col, (key, label, registry) in enumerate(groups):
            box = ttk.LabelFrame(frame, text=label)
            box.grid(row=0, column=col, sticky="nsew", padx=4, pady=4)
            chosen = vm_config.get(self.cfg, key)
            for name in registry:
                v = self.var(f"{key}:{name}", name in chosen, tk.BooleanVar)
                ttk.Checkbutton(box, text=name, variable=v).pack(anchor="w", padx=6, pady=2)
            frame.columnconfigure(col, weight=1)
        ctl = ttk.Frame(self.body)
        ctl.pack(fill="x", pady=10)
        ttk.Label(ctl, text="Button (control) from:").grid(row=0, column=0, sticky="w", **PAD)
        ttk.Combobox(ctl, textvariable=self.var("control.adapter", self.cfg["control"]["adapter"]),
                     values=list(adapters.CONTROLS), state="readonly", width=16
                     ).grid(row=0, column=1, sticky="w")
        ttk.Label(ctl, text="After a reply finishes:").grid(row=1, column=0, sticky="w", **PAD)
        v = self.var("control.after_reply", self.cfg["control"]["after_reply"])
        ttk.Radiobutton(ctl, text="go idle", value="idle", variable=v).grid(row=1, column=1, sticky="w")
        ttk.Radiobutton(ctl, text="reopen the mic", value="listening", variable=v
                        ).grid(row=1, column=2, sticky="w")

    # --- page 4: zenoh ----------------------------------------------------------

    def page_zenoh(self) -> None:
        z = self.cfg["zenoh"]
        self.heading("Zenoh", "Optional. Publish transcripts and subscribe to audio, button "
                              "presses and text on Zenoh key expressions.")
        top = ttk.Frame(self.body)
        top.pack(fill="x")
        ttk.Checkbutton(top, text="Enable Zenoh", variable=self.var("zenoh.enabled", z["enabled"],
                        tk.BooleanVar)).grid(row=0, column=0, sticky="w", **PAD)
        ttk.Checkbutton(top, text="Install Zenoh support (needs network)",
                        variable=self.var("install_zenoh", self.args.zenoh, tk.BooleanVar)
                        ).grid(row=0, column=1, columnspan=2, sticky="w", **PAD)
        ttk.Label(top, text="Router").grid(row=1, column=0, sticky="w", **PAD)
        ttk.Entry(top, textvariable=self.var("zenoh.connect", ", ".join(z["connect"])), width=36
                  ).grid(row=1, column=1, sticky="w")
        disc = ttk.Button(top, text="Discover channels", command=self.discover)
        disc.grid(row=1, column=2, padx=8)

        tls = ttk.LabelFrame(self.body, text="Mutual TLS (needed to allow-list Zenoh devices)")
        tls.pack(fill="x", pady=4)
        ttk.Checkbutton(tls, text="Use mutual TLS", variable=self.var(
            "zenoh.tls.enabled", z["tls"]["enabled"], tk.BooleanVar)).grid(row=0, column=0, sticky="w", **PAD)
        self.path_row(tls, 1, "zenoh.tls.root_ca", "CA certificate", z["tls"]["root_ca"])
        self.path_row(tls, 2, "zenoh.tls.cert", "This module's cert", z["tls"]["cert"])
        self.path_row(tls, 3, "zenoh.tls.key", "This module's key", z["tls"]["key"])
        tls.columnconfigure(1, weight=1)

        self.disc_status = ttk.Label(self.body, foreground=GREY, wraplength=740)
        self.disc_status.pack(anchor="w", pady=2)
        if not zenoh_available():
            disc.state(["disabled"])
            self.disc_status.configure(text="Discovery needs the Zenoh Python package, which the "
                                            "installer doesn't have. Type keys now, or pick them "
                                            "after install with: stt-tts channels --assign")
        keys = ttk.LabelFrame(self.body, text="Key expressions (type a key or pick a discovered one)")
        keys.pack(fill="x", pady=4)
        self.key_boxes: list[ttk.Combobox] = []
        labels = {"in_audio": "Audio in (subscribe)", "in_control": "Button presses (subscribe)",
                  "out_text": "Transcripts (publish)", "tts_text": "Text to speak (subscribe)",
                  "tts_audio": "Spoken audio (publish)", "events": "Events (publish)"}
        for r, (name, current) in enumerate(z["keys"].items()):
            ttk.Label(keys, text=labels.get(name, name)).grid(row=r // 2, column=(r % 2) * 2,
                                                             sticky="w", **PAD)
            box = ttk.Combobox(keys, textvariable=self.var(f"zenoh.keys.{name}", current), width=22)
            box.grid(row=r // 2, column=(r % 2) * 2 + 1, sticky="w", padx=4, pady=2)
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

    # --- page 5: LLM integration -------------------------------------------------

    def page_llm(self) -> None:
        llm = self.cfg["llm"]
        self.heading("LLM integration (optional)",
                     "Send transcripts to an OpenAI-compatible endpoint and speak its replies. "
                     "Turning this on also requires input security (next page), because the "
                     "module will hold a credential.")
        on = self.var("llm.enabled", llm["enabled"], tk.BooleanVar)
        ttk.Checkbutton(self.body, text="Enable LLM integration", variable=on).pack(anchor="w", pady=4)
        form = ttk.Frame(self.body)
        form.pack(fill="x")
        backend = self.var("llm.backend", llm["backend"])
        url = self.var("llm.base_url", llm["base_url"])
        ttk.Label(form, text="Backend").grid(row=0, column=0, sticky="w", **PAD)
        combo = ttk.Combobox(form, textvariable=backend, values=list(profile.BACKENDS),
                             state="readonly", width=28)
        combo.grid(row=0, column=1, sticky="w", padx=4)
        combo.bind("<<ComboboxSelected>>", lambda _e: url.set(profile.BACKENDS[backend.get()]))
        ttk.Label(form, text="Base URL").grid(row=1, column=0, sticky="w", **PAD)
        ttk.Entry(form, textvariable=url, width=48).grid(row=1, column=1, sticky="we", padx=4)
        ttk.Label(form, text="Model (blank = default)").grid(row=2, column=0, sticky="w", **PAD)
        ttk.Entry(form, textvariable=self.var("llm.model", llm["model"]), width=30
                  ).grid(row=2, column=1, sticky="w", padx=4)

        stored = bool(llm["api_key_secret"])
        ttk.Label(form, text="API key").grid(row=3, column=0, sticky="w", **PAD)
        ttk.Entry(form, textvariable=self.var("llm.api_key", ""), show="*", width=30
                  ).grid(row=3, column=1, sticky="w", padx=4)
        hint = ("A key is stored. Leave blank to keep it." if stored
                else "Leave blank if your server needs no key.")
        ttk.Label(form, text=hint, foreground=GREY).grid(row=4, column=1, sticky="w", padx=4)
        if stored:
            ttk.Checkbutton(form, text="Remove the stored key",
                            variable=self.var("llm.remove_key", False, tk.BooleanVar)
                            ).grid(row=5, column=1, sticky="w", padx=4)
        form.columnconfigure(1, weight=1)

        tls = ttk.LabelFrame(self.body, text="HTTPS (required for any host other than this machine)")
        tls.pack(fill="x", pady=8)
        ttk.Checkbutton(tls, text="Verify the server's certificate", variable=self.var(
            "llm.verify_tls", llm["verify_tls"], tk.BooleanVar)).grid(row=0, column=0, columnspan=2,
                                                                      sticky="w", **PAD)
        self.path_row(tls, 1, "llm.ca_file", "Custom CA (own server)", llm["ca_file"])
        self.path_row(tls, 2, "llm.client_cert", "Client cert (mutual TLS)", llm["client_cert"])
        self.path_row(tls, 3, "llm.client_key", "Client key", llm["client_key"])
        tls.columnconfigure(1, weight=1)
        ttk.Label(self.body, foreground=GREY, wraplength=740,
                  text=f"Keys are stored in the {credentials.backend_name()}, never in the config "
                       "file. Change this later with: stt-tts setup llm").pack(anchor="w")

    # --- page 6: security -----------------------------------------------------------

    def page_security(self) -> None:
        sec = self.cfg["security"]
        llm_on = bool(self.vars.get("llm.enabled") and self.vars["llm.enabled"].get())
        self.heading("Security", "Open by default: any device may send audio, and outputs are "
                                 "sent as-is. Turn on the overlays you need.")
        inp = ttk.LabelFrame(self.body, text="Input allow-list")
        inp.pack(fill="x", pady=4)
        enabled = self.var("sec.in.enabled", sec["input"]["enabled"], tk.BooleanVar)
        cb = ttk.Checkbutton(inp, text="Only accept allow-listed devices", variable=enabled)
        cb.grid(row=0, column=0, columnspan=2, sticky="w", **PAD)
        if llm_on:
            enabled.set(True)
            cb.state(["disabled"])
            ttk.Label(inp, foreground=RED, text="Required: LLM integration stores a credential."
                      ).grid(row=1, column=0, columnspan=2, sticky="w", padx=8)
        ttk.Checkbutton(inp, text="Trust mics and buttons attached to this machine",
                        variable=self.var("sec.in.local", sec["input"]["local_trusted"], tk.BooleanVar)
                        ).grid(row=2, column=0, columnspan=2, sticky="w", **PAD)
        ttk.Label(inp, text="Allowed devices, one per line\n(cert:<name> or local:<adapter>)"
                  ).grid(row=3, column=0, sticky="nw", **PAD)
        self.allowed_text = tk.Text(inp, height=4, width=40)
        self.allowed_text.grid(row=3, column=1, sticky="we", padx=4, pady=4)
        current = self.vars.get("sec.in.allowed")
        self.allowed_text.insert("1.0", current.get() if current else "\n".join(sec["input"]["allowed"]))
        self.var("sec.in.allowed", "\n".join(sec["input"]["allowed"]))
        ttk.Label(inp, foreground=GREY, wraplength=700,
                  text="Device names, MAC addresses and IDs are not accepted: they're easy to fake. "
                       "Zenoh devices are identified by certificate, which needs mutual TLS "
                       "(Zenoh page).").grid(row=4, column=0, columnspan=2, sticky="w", padx=8)
        inp.columnconfigure(1, weight=1)

        out = ttk.LabelFrame(self.body, text="Output sealing (sign and encrypt)")
        out.pack(fill="x", pady=8)
        ttk.Checkbutton(out, text="Sign and encrypt Zenoh and file outputs", variable=self.var(
            "sec.out.seal", sec["output"]["seal"], tk.BooleanVar)).grid(row=0, column=0, columnspan=2,
                                                                        sticky="w", **PAD)
        ttk.Label(out, text="Recipient public keys (files, comma-separated)").grid(row=1, column=0,
                                                                                   sticky="w", **PAD)
        ttk.Entry(out, textvariable=self.var("sec.out.recipients",
                                             ", ".join(sec["output"]["recipients"])), width=40
                  ).grid(row=1, column=1, sticky="we", padx=4)
        ttk.Label(out, foreground=GREY, text="Stub: settings are saved, but sealing isn't implemented "
                                             "yet.").grid(row=2, column=0, columnspan=2, sticky="w", padx=8)
        out.columnconfigure(1, weight=1)

    def validate_security(self) -> bool:
        problems = []
        for ident in self.cfg["security"]["input"]["allowed"]:
            try:
                security.Identity.parse(ident)
            except security.SecurityError as exc:
                problems.append(str(exc))
        settings = self.llm_settings()
        if settings:
            problems += profile.problems(self.cfg, settings)
        if problems:
            messagebox.showerror("Security settings", "\n\n".join(dict.fromkeys(problems)))
            return False
        return True

    def llm_settings(self) -> profile.LLMSettings | None:
        v = self.vars
        if not (v.get("llm.enabled") and v["llm.enabled"].get()):
            return None
        key = v["llm.api_key"].get().strip()
        remove = bool(v.get("llm.remove_key") and v["llm.remove_key"].get())
        sec = self.cfg["security"]["input"]
        return profile.LLMSettings(
            backend=v["llm.backend"].get(), base_url=v["llm.base_url"].get().strip(),
            model=v["llm.model"].get().strip(), api_key="" if remove else (key or None),
            verify_tls=bool(v["llm.verify_tls"].get()), ca_file=v["llm.ca_file"].get().strip(),
            client_cert=v["llm.client_cert"].get().strip(),
            client_key=v["llm.client_key"].get().strip(),
            allowed=list(sec["allowed"]), local_trusted=sec["local_trusted"])

    # --- page 7: install ------------------------------------------------------

    def page_install(self) -> None:
        verb = {"fresh": "Install", "modify": "Update the existing install",
                "replace": "Delete the existing install, then install fresh"}[self.mode]
        self.heading(verb)
        summary = tk.Text(self.body, height=20, wrap="word")
        summary.pack(fill="both", expand=True)
        settings = self.llm_settings()
        llm_line = "off"
        if settings:
            key = ("new key" if settings.api_key else "remove key" if settings.api_key == ""
                   else "keep stored key" if self.cfg["llm"]["api_key_secret"] else "no key")
            llm_line = f"{settings.backend} at {settings.base_url} ({key})"
        summary.insert("end", f"Mode: {self.mode}\nLLM integration: {llm_line}\n\n"
                              + vm_config.dumps(self.cfg))
        summary.configure(state="disabled")
        self.log = summary
        self.next_btn.configure(text="Install", command=self.start_install)

    def start_install(self) -> None:
        for btn in (self.next_btn, self.back_btn, self.cancel_btn):
            btn.state(["disabled"])
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        msgs: queue.Queue = queue.Queue()
        settings = self.llm_settings()
        was_on = bool(self.existing and self.mode == "modify" and self.existing.cfg["llm"]["enabled"])
        plan = core.Plan(cfg=self.cfg, mode=self.mode, existing=self.existing,
                         install_zenoh=bool(self.vars["install_zenoh"].get())
                         if "install_zenoh" in self.vars else False,
                         llm=settings, remove_llm=was_on and settings is None,
                         config_file=Path(self.vars["config_file"].get()).expanduser()
                         if "config_file" in self.vars else self.config_file)

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
                    self.log.insert("end", "\nFinished. Try: stt-tts demo\n"
                                           "Run ./install.sh again any time to modify or replace.\n")
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
        z = self.cfg["zenoh"]
        if "zenoh.enabled" in v:
            z["enabled"] = bool(v["zenoh.enabled"].get())
            z["connect"] = [e.strip() for e in v["zenoh.connect"].get().split(",") if e.strip()]
            for name in z["keys"]:
                z["keys"][name] = v[f"zenoh.keys.{name}"].get().strip()
            z["tls"].update(enabled=bool(v["zenoh.tls.enabled"].get()),
                            root_ca=v["zenoh.tls.root_ca"].get().strip(),
                            cert=v["zenoh.tls.cert"].get().strip(),
                            key=v["zenoh.tls.key"].get().strip())
        sec = self.cfg["security"]
        if "sec.in.enabled" in v:
            if hasattr(self, "allowed_text") and self.allowed_text.winfo_exists():
                v["sec.in.allowed"].set(self.allowed_text.get("1.0", "end"))
            sec["input"].update(
                enabled=bool(v["sec.in.enabled"].get()),
                local_trusted=bool(v["sec.in.local"].get()),
                allowed=[line.strip() for line in v["sec.in.allowed"].get().splitlines() if line.strip()])
            sec["output"].update(
                seal=bool(v["sec.out.seal"].get()),
                recipients=[r.strip() for r in v["sec.out.recipients"].get().split(",") if r.strip()])
        if "config_file" in v:
            self.config_file = Path(v["config_file"].get()).expanduser()


def run(args) -> int:
    app = Installer(args)
    app.mainloop()
    return app.result
