"""LLM integration profile: an OpenAI-compatible HTTP endpoint.

One adapter covers llama.cpp's llama-server, Ollama, vLLM and cloud APIs,
because they all accept POST {base_url}/chat/completions.

It is both ends of the model side: it receives transcripts (TextSink) and
hands the reply to TTS. History is kept so an interrupt can record only the
part of the reply that was actually spoken.

Transport rules (configurable, not assumed by the core):
  - https:// is required for any host that isn't this machine
  - certificates are verified (custom CA via llm.ca_file)
  - optional client certificate for mutual TLS (llm.client_cert / llm.client_key)
  - the API key comes from the credential store, never from config.toml

STUB: request building and validation are real; the network call is not.
"""

from __future__ import annotations

import ipaddress
import json
import ssl
from typing import Callable
from urllib.parse import urlparse

from .. import credentials
from .base import TextSink

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def is_local(host: str | None) -> bool:
    if not host:
        return False
    if host in LOCAL_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_url(base_url: str) -> str:
    """Return a problem description, or '' if the URL is acceptable."""
    url = urlparse(base_url)
    if url.scheme not in ("http", "https"):
        return f"LLM URL must start with http:// or https:// (got '{base_url}')"
    if url.scheme == "http" and not is_local(url.hostname):
        return (f"Refusing plain http to {url.hostname}: the API key and your speech would cross "
                "the network unencrypted. Use https://, or run the LLM on this machine.")
    return ""


class LLMBridge(TextSink):
    name = "llm"

    def __init__(self, cfg: dict) -> None:
        super().__init__(cfg)
        self.llm = cfg["llm"]
        problem = check_url(self.llm["base_url"])
        if problem:
            raise ValueError(problem)
        self.history: list[dict] = []
        if self.llm["system_prompt"]:
            self.history.append({"role": "system", "content": self.llm["system_prompt"]})
        self.on_reply: Callable[[str], None] | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.llm["api_key_secret"]:
            key = credentials.get_secret(self.llm["api_key_secret"])
            if not key:
                raise ValueError(f"No stored secret named '{self.llm['api_key_secret']}'. "
                                 "Run: stt-tts setup llm")
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def tls_context(self) -> ssl.SSLContext | None:
        if urlparse(self.llm["base_url"]).scheme != "https":
            return None
        ctx = ssl.create_default_context(cafile=self.llm["ca_file"] or None)
        if not self.llm["verify_tls"]:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        if self.llm["client_cert"]:
            ctx.load_cert_chain(self.llm["client_cert"], self.llm["client_key"] or None)
        return ctx

    def build_request(self, text: str) -> tuple[str, dict[str, str], bytes]:
        self.history.append({"role": "user", "content": text})
        body = {"messages": self.history, "stream": self.llm["stream"]}
        if self.llm["model"]:
            body["model"] = self.llm["model"]
        url = self.llm["base_url"].rstrip("/") + "/chat/completions"
        return url, self._headers(), json.dumps(body).encode()

    def send(self, text: str, meta: dict) -> None:
        url, headers, body = self.build_request(text)
        # STUB: replace with a streaming POST (urllib/httpx) that yields sentences
        # to on_reply as they complete, using self.tls_context().
        reply = f"(stub reply from {self.llm['backend']} to: {text})"
        self.history.append({"role": "assistant", "content": reply})
        if self.on_reply:
            self.on_reply(reply)

    def record_interrupt(self, spoken_text: str) -> None:
        """Trim the last reply in history to what the user actually heard."""
        if self.history and self.history[-1]["role"] == "assistant":
            self.history[-1]["content"] = spoken_text + " [interrupted]"
