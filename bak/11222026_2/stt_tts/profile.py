"""LLM integration profile: shared by `stt-tts setup llm` and the installer.

Enabling the profile always configures input security too, because the
profile can carry a credential (see security.enforce).
"""

from __future__ import annotations

import getpass
from dataclasses import dataclass, field

from . import credentials, security
from .adapters.llm_http import check_url

SECRET_NAME = "llm-api-key"

BACKENDS = {
    "llama.cpp": "http://localhost:8080/v1",
    "Ollama": "http://localhost:11434/v1",
    "vLLM": "http://localhost:8000/v1",
    "Cloud (OpenAI-compatible)": "https://api.example.com/v1",
}


@dataclass
class LLMSettings:
    backend: str = "llama.cpp"
    base_url: str = BACKENDS["llama.cpp"]
    model: str = ""
    api_key: str | None = None  # None = leave the stored key as it is; "" = no key
    verify_tls: bool = True
    ca_file: str = ""
    client_cert: str = ""
    client_key: str = ""
    allowed: list[str] = field(default_factory=list)
    local_trusted: bool = True


def current(cfg: dict) -> LLMSettings:
    llm, sec = cfg["llm"], cfg["security"]["input"]
    return LLMSettings(backend=llm["backend"], base_url=llm["base_url"], model=llm["model"],
                       verify_tls=llm["verify_tls"], ca_file=llm["ca_file"],
                       client_cert=llm["client_cert"], client_key=llm["client_key"],
                       allowed=list(sec["allowed"]), local_trusted=sec["local_trusted"])


def problems(cfg: dict, s: LLMSettings) -> list[str]:
    """Validate without saving anything."""
    found = []
    if bad := check_url(s.base_url):
        found.append(bad)
    for ident in s.allowed:
        try:
            security.Identity.parse(ident)
        except security.SecurityError as exc:
            found.append(str(exc))
    if not s.allowed and not s.local_trusted:
        found.append("Allow at least one input: trust local devices, or add a cert:<name> identity.")
    trial = _apply_to_cfg(_copy(cfg), s, has_key=bool(s.api_key) or
                          (s.api_key is None and bool(cfg["llm"]["api_key_secret"])))
    try:
        security.enforce(trial)
    except security.SecurityError as exc:
        found.append(str(exc))
    return found


def apply(cfg: dict, s: LLMSettings) -> list[str]:
    """Validate, then store the key and update cfg in place. Returns problems (empty = saved)."""
    found = problems(cfg, s)
    if found:
        return found
    if s.api_key:
        credentials.set_secret(SECRET_NAME, s.api_key)
    elif s.api_key == "":
        credentials.delete_secret(SECRET_NAME)
    has_key = bool(s.api_key) or (s.api_key is None and bool(cfg["llm"]["api_key_secret"]))
    _apply_to_cfg(cfg, s, has_key)
    return []


def remove(cfg: dict) -> None:
    """Disable the profile and delete its stored key. Input security settings are kept."""
    credentials.delete_secret(SECRET_NAME)
    cfg["llm"]["enabled"] = False
    cfg["llm"]["api_key_secret"] = ""


def _apply_to_cfg(cfg: dict, s: LLMSettings, has_key: bool) -> dict:
    cfg["llm"].update(enabled=True, backend=s.backend, base_url=s.base_url, model=s.model,
                      api_key_secret=SECRET_NAME if has_key else "", verify_tls=s.verify_tls,
                      ca_file=s.ca_file, client_cert=s.client_cert, client_key=s.client_key)
    cfg["security"]["input"].update(enabled=True, allowed=list(s.allowed),
                                    local_trusted=s.local_trusted)
    return cfg


def _copy(cfg: dict) -> dict:
    import copy
    return copy.deepcopy(cfg)


# --- terminal walkthrough ------------------------------------------------------

def _ask(prompt: str, default: str = "") -> str:
    raw = input(f"{prompt} [{default}]: " if default else f"{prompt}: ").strip()
    return raw or default


def _yes(prompt: str, default: bool) -> bool:
    raw = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not raw else raw.startswith("y")


def interactive(cfg: dict) -> bool:
    """Walk the LLM profile in the terminal and save it. Returns True if saved."""
    s = ask(cfg)
    found = apply(cfg, s)
    if found:
        print("\nNot saved:")
        for p in found:
            print("  - " + p)
        return False
    return True


def ask(cfg: dict) -> LLMSettings:
    """Ask the LLM profile questions in the terminal. Saves nothing."""
    s = current(cfg)
    names = list(BACKENDS)
    print("LLM integration. Enabling it also turns on input security (required when a")
    print("credential is stored), so only devices you allow can reach the LLM.\n")
    for i, name in enumerate(names, 1):
        print(f"  {i}. {name}  ({BACKENDS[name]})")
    pick = _ask("Backend number", str(names.index(s.backend) + 1 if s.backend in names else 1))
    s.backend = names[int(pick) - 1] if pick.isdigit() and 1 <= int(pick) <= len(names) else s.backend
    default_url = s.base_url if cfg["llm"]["enabled"] else BACKENDS[s.backend]
    s.base_url = _ask("Base URL", default_url)
    s.model = _ask("Model name (blank = server default)", s.model)

    stored = credentials.get_secret(SECRET_NAME) if cfg["llm"]["api_key_secret"] else None
    print(f"API key: currently {credentials.mask(stored)}; stored in {credentials.backend_name()}")
    key = getpass.getpass("New API key (Enter = keep, '-' = remove): ").strip()
    s.api_key = None if key == "" else ("" if key == "-" else key)

    if s.base_url.startswith("https://"):
        s.verify_tls = _yes("Verify the server's certificate", s.verify_tls)
        s.ca_file = _ask("Custom CA file for your own server (blank = system CAs)", s.ca_file)
        if _yes("Use a client certificate (mutual TLS)", bool(s.client_cert)):
            s.client_cert = _ask("Client certificate file", s.client_cert)
            s.client_key = _ask("Client key file", s.client_key)

    print("\nInput security: who may send audio and button presses.")
    s.local_trusted = _yes("Trust mics and buttons attached to this machine", s.local_trusted)
    raw = _ask("Allowed device certificates, comma-separated cert:<name> (blank = none)",
               ", ".join(s.allowed))
    s.allowed = [i.strip() for i in raw.split(",") if i.strip()]
    return s
