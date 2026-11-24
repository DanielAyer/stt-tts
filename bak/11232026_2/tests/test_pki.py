"""Certificates from `stt-tts certs`, checked with real TLS handshakes.

Run with: python3 -m unittest discover -s tests
Skipped if the openssl command is not installed.
"""

import http.server
import json
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from stt_tts import config, pki  # noqa: E402
from stt_tts.adapters.llm_http import LLMBridge  # noqa: E402


def make_cfg(root: Path) -> dict:
    cfg = config.defaults()
    cfg["paths"]["pki_dir"] = str(root / "pki")
    return cfg


class FakeLLM(http.server.BaseHTTPRequestHandler):
    """Answers POST /v1/chat/completions like an OpenAI-compatible server."""

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        reply = {"choices": [{"message": {"role": "assistant",
                                          "content": f"heard: {body['messages'][-1]['content']}"}}],
                 "client_cert": bool(self.connection.getpeercert())}
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@unittest.skipUnless(shutil.which("openssl"), "openssl not installed")
class PKITest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.cfg = make_cfg(root)
        pki.init_ca(cls.cfg)
        cls.server_dir = pki.issue(cls.cfg, "ravensnest", server=True,
                                   hosts=["ravensnest", "127.0.0.1"])
        cls.client_dir = pki.issue(cls.cfg, "stt-tts")
        # A second, unrelated CA and a client cert from it
        cls.other = make_cfg(root / "other")
        pki.init_ca(cls.other, common_name="Someone else's CA")
        cls.rogue_dir = pki.issue(cls.other, "stt-tts")
        cls.ca = Path(cls.cfg["paths"]["pki_dir"]) / "ca.crt"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def start_server(self, require_client_cert: bool, cert_dir: Path | None = None) -> int:
        cert_dir = cert_dir or self.server_dir
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_dir / "cert.pem", cert_dir / "key.pem")
        if require_client_cert:
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(self.ca)
        server = http.server.HTTPServer(("127.0.0.1", 0), FakeLLM)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def bridge(self, port: int, host: str = "127.0.0.1", client=None, ca=True) -> LLMBridge:
        cfg = make_cfg(Path(self.tmp.name))
        cfg["llm"].update(enabled=True, base_url=f"https://{host}:{port}/v1",
                          ca_file=str(self.ca) if ca else "")
        if client:
            cfg["llm"].update(client_cert=str(client / "cert.pem"), client_key=str(client / "key.pem"))
        return LLMBridge(cfg)

    def call(self, bridge: LLMBridge, text: str = "hello") -> dict:
        url, headers, body = bridge.build_request(text)
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, context=bridge.tls_context(), timeout=5) as resp:
            return json.loads(resp.read())

    # --- server authentication (TLS) -----------------------------------------

    def test_module_trusts_server_from_our_ca(self):
        port = self.start_server(require_client_cert=False)
        reply = self.call(self.bridge(port))
        self.assertEqual(reply["choices"][0]["message"]["content"], "heard: hello")

    def test_wrong_hostname_is_refused(self):
        port = self.start_server(require_client_cert=False)
        bridge = self.bridge(port)
        ctx = bridge.tls_context()
        with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
            with self.assertRaises(ssl.SSLCertVerificationError):
                ctx.wrap_socket(raw, server_hostname="not-ravensnest")

    def test_without_our_ca_server_is_untrusted(self):
        port = self.start_server(require_client_cert=False)
        with self.assertRaises(Exception) as caught:
            self.call(self.bridge(port, ca=False))
        self.assertIn("CERTIFICATE_VERIFY_FAILED", str(caught.exception))

    def test_client_cert_cannot_pose_as_server(self):
        port = self.start_server(require_client_cert=False, cert_dir=self.client_dir)
        with self.assertRaises(Exception):
            self.call(self.bridge(port))

    # --- client authentication (mutual TLS) -------------------------------------

    def test_mutual_tls_with_module_cert(self):
        port = self.start_server(require_client_cert=True)
        reply = self.call(self.bridge(port, client=self.client_dir))
        self.assertTrue(reply["client_cert"])

    def test_mutual_tls_refuses_missing_client_cert(self):
        port = self.start_server(require_client_cert=True)
        with self.assertRaises(Exception):
            self.call(self.bridge(port))

    def test_mutual_tls_refuses_cert_from_another_ca(self):
        port = self.start_server(require_client_cert=True)
        with self.assertRaises(Exception):
            self.call(self.bridge(port, client=self.rogue_dir))

    # --- housekeeping -----------------------------------------------------------------

    def test_keys_are_owner_only(self):
        root = Path(self.cfg["paths"]["pki_dir"])
        for key in (root / "ca.key", self.server_dir / "key.pem", self.client_dir / "key.pem"):
            self.assertEqual(key.stat().st_mode & 0o077, 0, key)

    def test_list_and_kinds(self):
        kinds = {c.name: c.kind for c in pki.list_issued(self.cfg)}
        self.assertEqual(kinds, {"ravensnest": "server", "stt-tts": "client"})

    def test_reissue_needs_force(self):
        with self.assertRaises(pki.PKIError):
            pki.issue(self.cfg, "ravensnest", server=True)


if __name__ == "__main__":
    unittest.main()
