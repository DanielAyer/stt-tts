"""Command line: stt-tts <command>

  run                      start the module with the configured endpoints
  demo                     scripted walk through the state machine (stubs)
  transcribe FILE.wav      burst path: one file in, transcript out
  config show|get|set|path view or change settings after install
  channels                 list Zenoh channels a router knows about
  channels --assign        pick each zenoh.keys.* value from that list
  setup llm [--remove]     add, change or remove the LLM integration profile
  security status          show the security overlays and check the config
  security allow|deny ID   edit the input allow-list (cert:<name> or local:<adapter>)
  security acl             print a Zenoh router access-control block for the allow-list
  certs init               create a private certificate authority (CA)
  certs issue NAME         client cert for a device; --server --host H for a server
  certs list | trust llm   list issued certs; make the module trust the CA
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, config


def cmd_run(cfg, _args) -> int:
    from .pipeline import VoiceModule

    module = VoiceModule(cfg)
    print("stt-tts running. Engines and devices are stubs; see README.", flush=True)
    module.control.listen(module.press)  # blocks for keyboard/zenoh; returns for stub
    for source in module.sources:
        source.listen(module.speak)
    return 0


def cmd_demo(cfg, _args) -> int:
    from .pipeline import VoiceModule

    module = VoiceModule(cfg)
    module.on_event = lambda e: print(f"  {e['state']:<10} {e['event']:<16} "
                                      + " ".join(f"{k}={v!r}" for k, v in e.items()
                                                 if k not in ("event", "state", "t")))
    print("1. Press to listen, press to send:")
    module.press()
    module.press()
    print("2. LLM reply arrives and plays to the end:")
    module.speak("Here is the first sentence. Here is the second.")
    print("3. Press, press, reply arrives, press again to interrupt:")
    module.press()
    module.press()
    module.speak("This reply gets cut off. You never hear this part.", autoplay=False)
    module.play_next()
    module.press()
    return 0


def cmd_transcribe(cfg, args) -> int:
    from .adapters.stub import FileInput
    from .pipeline import VoiceModule

    module = VoiceModule(cfg)
    module.transcribe_frames(FileInput(cfg, args.file).frames())
    return 0


def cmd_config(cfg, args) -> int:
    path = config.config_path()
    if args.action == "path":
        print(path)
    elif args.action == "show":
        print(config.dumps(cfg))
    elif args.action == "get":
        print(json.dumps(config.get(cfg, args.key)))
    elif args.action == "set":
        from . import security
        value = config.set_value(cfg, args.key, args.value)
        security.enforce(cfg)  # refuse to save an insecure combination
        config.save(cfg, path)
        print(f"{args.key} = {json.dumps(value)}  (saved to {path})")
    return 0


def cmd_setup(cfg, args) -> int:
    from . import profile

    if args.remove:
        profile.remove(cfg)
        config.save(cfg)
        print("LLM integration removed and its API key deleted. Input security settings were kept.")
        return 0
    if profile.interactive(cfg):
        print("Saved to", config.save(cfg))
        return 0
    return 1


def cmd_certs(cfg, args) -> int:
    from . import pki, security

    try:
        if args.action == "init":
            ca = pki.init_ca(cfg, common_name=args.cn or "stt-tts CA",
                             days=args.days or pki.CA_DAYS, force=args.force)
            print(f"Created CA: {ca}")
            print(f"Its private key ({ca.parent / 'ca.key'}) can mint trusted certificates: keep it "
                  "owner-only and back it up. A replace (scorched earth) reinstall deletes it.")
            print("\nNext: stt-tts certs issue <name> [--server --host <host> ...]")
            return 0

        if args.action == "list":
            certs = pki.list_issued(cfg)
            if not certs:
                print("No certificates issued yet.")
            for c in certs:
                days = (c.expires - datetime.now(timezone.utc)).days
                where = f"  [{c.hosts}]" if c.hosts else ""
                print(f"{c.name:<20} {c.kind:<7} expires {c.expires:%Y-%m-%d} ({days} days){where}")
            return 0

        if args.action == "trust":
            changed = pki.trust(cfg, args.name)
            security.enforce(cfg)
            config.save(cfg)
            print("Set " + ", ".join(changed) + f" to {pki.pki_dir(cfg) / 'ca.crt'}")
            return 0

        # issue
        if not args.name:
            raise ValueError("certs issue needs a name, e.g. ravensnest or kitchen-mic")
        out = pki.issue(cfg, args.name, server=args.server, hosts=args.host,
                        days=args.days or pki.LEAF_DAYS, force=args.force)
        kind, hosts, expires = pki.describe(out / "cert.pem")
        print(f"Issued {kind} certificate '{args.name}', valid until {expires:%Y-%m-%d}")
        print(f"  {out}/key.pem   (private: copy only to that machine)")
        print(f"  {out}/cert.pem")
        print(f"  {out}/ca.crt")
        if args.server:
            print(f"  hosts: {hosts}")
            print("\nOn that machine, for llama.cpp:")
            print("  llama-server --ssl-key-file key.pem --ssl-cert-file cert.pem --host 0.0.0.0 ...")
            print("Then on this machine: stt-tts certs trust llm  (and use https://<host>:<port>/v1)")
        elif args.use_for:
            changed = pki.use_for(cfg, args.use_for, out)
            security.enforce(cfg)
            config.save(cfg)
            print("\nThe module now uses it: " + ", ".join(changed))
        else:
            print(f"\nAllow this device in: stt-tts security allow cert:{args.name}")
        return 0
    except pki.PKIError as exc:
        print(f"stt-tts certs: {exc}", file=sys.stderr)
        return 2


def cmd_security(cfg, args) -> int:
    from . import security

    sec = cfg["security"]["input"]
    if args.action == "status":
        creds = security.credentialed_outputs(cfg)
        print(f"Input allow-list:   {'on' if sec['enabled'] else 'off (any device may send)'}")
        print(f"Local devices:      {'trusted' if sec['local_trusted'] else 'must be allow-listed'}")
        print(f"Allowed identities: {', '.join(sec['allowed']) or '(none)'}")
        print(f"Zenoh mutual TLS:   {'on' if cfg['zenoh']['tls']['enabled'] else 'off'}")
        print(f"Credentialed outputs: {', '.join(creds) or '(none)'}"
              + ("  -> input security is required" if creds else ""))
        print(f"Output sealing:     {'on' if cfg['security']['output']['seal'] else 'off'}")
        try:
            security.enforce(cfg)
            print("Check: OK")
        except security.SecurityError as exc:
            print(f"Check: FAIL - {exc}")
            return 1
        return 0
    if args.action == "acl":
        print(json.dumps({"access_control": security.zenoh_acl(cfg)}, indent=2))
        return 0
    if not args.identity:
        raise ValueError("security allow/deny needs an identity, e.g. cert:kitchen-mic")
    ident = str(security.Identity.parse(args.identity))
    if args.action == "allow":
        if ident not in sec["allowed"]:
            sec["allowed"].append(ident)
        sec["enabled"] = True
    else:
        sec["allowed"] = [i for i in sec["allowed"] if i != ident]
    security.enforce(cfg)
    config.save(cfg)
    print(f"{args.action}: {ident}. Allowed now: {', '.join(sec['allowed']) or '(none)'}")
    if ident.startswith("cert:"):
        print("Regenerate the router's access control: stt-tts security acl")
    return 0


def cmd_channels(cfg, args) -> int:
    from . import discovery

    try:
        channels = discovery.discover(cfg, timeout_s=args.timeout)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2
    keys = discovery.unique_keys(channels)
    if not keys:
        print("No channels found. Is a zenohd router reachable at", cfg["zenoh"]["connect"], "?")
        return 1
    for i, key in enumerate(keys, 1):
        kinds = ",".join(sorted(c.kind for c in channels if c.key == key))
        print(f"{i:>3}. {key}  [{kinds}]")
    if args.assign:
        for name, current in cfg["zenoh"]["keys"].items():
            raw = input(f"zenoh.keys.{name} [{current}] (number, new key, or Enter to keep): ").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(keys):
                cfg["zenoh"]["keys"][name] = keys[int(raw) - 1]
            elif raw:
                cfg["zenoh"]["keys"][name] = raw
        print("Saved to", config.save(cfg))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stt-tts", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config", type=Path, help="config file (default: XDG config dir)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run").set_defaults(func=cmd_run)
    sub.add_parser("demo").set_defaults(func=cmd_demo)
    p = sub.add_parser("transcribe")
    p.add_argument("file")
    p.set_defaults(func=cmd_transcribe)
    p = sub.add_parser("config")
    p.add_argument("action", choices=["show", "get", "set", "path"])
    p.add_argument("key", nargs="?")
    p.add_argument("value", nargs="?")
    p.set_defaults(func=cmd_config)
    p = sub.add_parser("setup", help="optional profiles")
    p.add_argument("profile", choices=["llm"])
    p.add_argument("--remove", action="store_true")
    p.set_defaults(func=cmd_setup)
    p = sub.add_parser("certs", help="private certificate authority")
    p.add_argument("action", choices=["init", "issue", "list", "trust"])
    p.add_argument("name", nargs="?", help="issue: certificate name; trust: llm or zenoh")
    p.add_argument("--server", action="store_true", help="issue a server certificate")
    p.add_argument("--host", action="append", help="server hostname or IP (repeatable)")
    p.add_argument("--use-for", choices=["zenoh", "llm"],
                   help="make this the module's own client certificate for that link")
    p.add_argument("--days", type=int, help="validity in days")
    p.add_argument("--cn", help="init: CA common name")
    p.add_argument("--force", action="store_true", help="replace an existing CA or certificate")
    p.set_defaults(func=cmd_certs)
    p = sub.add_parser("security")
    p.add_argument("action", choices=["status", "allow", "deny", "acl"])
    p.add_argument("identity", nargs="?")
    p.set_defaults(func=cmd_security)
    p = sub.add_parser("channels")
    p.add_argument("--assign", action="store_true")
    p.add_argument("--timeout", type=float, default=2.0)
    p.set_defaults(func=cmd_channels)

    args = parser.parse_args(argv)
    if args.command == "config" and args.action in ("get", "set") and not args.key:
        parser.error("config get/set needs a key, e.g. zenoh.keys.out_text")
    if args.command == "config" and args.action == "set" and args.value is None:
        parser.error("config set needs a value")
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    if args.config:
        import os
        os.environ["STT_TTS_CONFIG"] = str(args.config)
    try:
        return args.func(config.load(), args)
    except KeyError as exc:
        print(f"stt-tts: unknown config key {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"stt-tts: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
